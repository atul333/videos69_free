"""
premium_sync.py — Shared Premium Database (Cross-Bot Sync)
-----------------------------------------------------------
All bot instances on the same server read/write from ONE central SQLite file.
When any bot grants premium to a user, every other bot immediately sees it.

Architecture:
  • Central DB: /home/ubuntu/premium_shared.db  (absolute path, same for all bots)
  • All reads: in-memory cache (fast, zero I/O on hot path)
  • Cache TTL: 60 seconds per user — auto-refreshed from DB on expiry
  • All writes: instant to SQLite, cache updated immediately
  • Thread-safe: uses asyncio.Lock for concurrent write protection

Usage in video69_bot.py:
    from premium_sync import is_premium_synced, grant_premium_synced, init_premium_sync

    # On startup (inside post_init):
    await init_premium_sync()

    # Check premium (replaces local is_premium_user):
    if await is_premium_synced(user_id):
        ...

    # Grant premium (replaces local grant_premium_access):
    await grant_premium_synced(user_id, hours=12)
"""

import os
import asyncio
import aiosqlite
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Tuple

# ── Config ────────────────────────────────────────────────────────────────────
# ALL bots on the same server must use this exact path.
# Change only if your bots run on different servers (then use a shared NFS mount).
SHARED_DB_PATH = os.getenv("SHARED_PREMIUM_DB", "/home/ubuntu/premium_shared.db")

# How long (seconds) to trust a cached premium result before re-checking DB.
# 60 s = good balance: fresh enough that a cross-bot grant propagates quickly.
CACHE_TTL_SECONDS = 60

# ── In-memory cache ───────────────────────────────────────────────────────────
# { user_id: (premium_until: Optional[datetime], cached_at: float) }
_cache: Dict[int, Tuple[Optional[datetime], float]] = {}
_write_lock = asyncio.Lock()
_db_ready = False


# ── SQL ───────────────────────────────────────────────────────────────────────
_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS shared_premium (
    user_id       INTEGER PRIMARY KEY,
    premium_until TEXT,               -- ISO-8601 UTC, NULL = no premium
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
)
"""
_CREATE_IDX = """
CREATE INDEX IF NOT EXISTS idx_sp_premium
    ON shared_premium(premium_until)
    WHERE premium_until IS NOT NULL
"""


# ── Helpers ───────────────────────────────────────────────────────────────────
def _ser(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.isoformat()


def _des(s) -> Optional[datetime]:
    if s is None or not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Init ──────────────────────────────────────────────────────────────────────
async def init_premium_sync():
    """Create the shared DB table if it doesn't exist. Call once at startup."""
    global _db_ready
    try:
        async with aiosqlite.connect(SHARED_DB_PATH) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=NORMAL")
            await db.execute(_CREATE_TABLE)
            await db.execute(_CREATE_IDX)
            await db.commit()
        _db_ready = True
        print(f"✅ [PremiumSync] Shared DB ready: {SHARED_DB_PATH}")
    except Exception as e:
        print(f"⚠️ [PremiumSync] Could not init shared DB: {e}")
        print(f"⚠️ [PremiumSync] Premium sync disabled — bots will work independently.")
        _db_ready = False


# ── Core read ─────────────────────────────────────────────────────────────────
async def _fetch_premium_until(user_id: int) -> Optional[datetime]:
    """Read premium_until from the shared DB. Returns None on error."""
    try:
        async with aiosqlite.connect(SHARED_DB_PATH) as db:
            async with db.execute(
                "SELECT premium_until FROM shared_premium WHERE user_id = ?",
                (user_id,)
            ) as cur:
                row = await cur.fetchone()
                if row:
                    return _des(row[0])
    except Exception as e:
        print(f"⚠️ [PremiumSync] Read error for user {user_id}: {e}")
    return None


# ── Public API ────────────────────────────────────────────────────────────────
async def is_premium_synced(user_id: int) -> bool:
    """
    Return True if user has active premium in the SHARED database.
    Uses in-memory cache (TTL=60s) — max 1 DB read per user per minute.
    Falls back to False on DB error (fail-safe).
    """
    if not _db_ready:
        return False

    import time as _time_mod
    now_mono = _time_mod.monotonic()
    now_utc  = _now()

    cached = _cache.get(user_id)
    if cached is not None:
        premium_until, cached_at = cached
        if now_mono - cached_at < CACHE_TTL_SECONDS:
            # Cache hit — return without DB access
            return bool(premium_until) and now_utc < premium_until

    # Cache miss or expired — re-read from shared DB
    premium_until = await _fetch_premium_until(user_id)
    _cache[user_id] = (premium_until, now_mono)
    return bool(premium_until) and now_utc < premium_until


async def grant_premium_synced(user_id: int, hours: int = 12) -> datetime:
    """
    Grant premium to user_id for `hours` hours in the SHARED database.
    Also updates the in-memory cache immediately.
    Returns the new premium_until datetime (UTC).

    All other bots will see this within CACHE_TTL_SECONDS (≤60 s).
    """
    premium_until = _now() + timedelta(hours=hours)

    if not _db_ready:
        # Graceful degradation: just update cache, no DB write
        import time as _time_mod
        _cache[user_id] = (premium_until, _time_mod.monotonic())
        return premium_until

    async with _write_lock:
        try:
            async with aiosqlite.connect(SHARED_DB_PATH) as db:
                await db.execute("PRAGMA journal_mode=WAL")
                await db.execute("""
                    INSERT INTO shared_premium (user_id, premium_until, updated_at)
                    VALUES (?, ?, datetime('now'))
                    ON CONFLICT(user_id) DO UPDATE SET
                        premium_until = excluded.premium_until,
                        updated_at    = excluded.updated_at
                """, (user_id, _ser(premium_until)))
                await db.commit()

            # Immediately update local cache so the granting bot doesn't re-read DB
            import time as _time_mod
            _cache[user_id] = (premium_until, _time_mod.monotonic())
            print(f"✅ [PremiumSync] Premium granted for user {user_id} until {premium_until.isoformat()}")
        except Exception as e:
            print(f"⚠️ [PremiumSync] Write error for user {user_id}: {e}")

    return premium_until


async def get_premium_until_synced(user_id: int) -> Optional[datetime]:
    """
    Return the premium_until datetime for the user from the shared DB.
    Returns None if user has no premium or premium has expired.
    Uses the same cache as is_premium_synced().
    """
    if not _db_ready:
        return None

    import time as _time_mod
    now_mono = _time_mod.monotonic()

    cached = _cache.get(user_id)
    if cached is not None:
        premium_until, cached_at = cached
        if now_mono - cached_at < CACHE_TTL_SECONDS:
            now_utc = _now()
            return premium_until if (premium_until and now_utc < premium_until) else None

    premium_until = await _fetch_premium_until(user_id)
    _cache[user_id] = (premium_until, now_mono)
    now_utc = _now()
    return premium_until if (premium_until and now_utc < premium_until) else None


async def revoke_premium_synced(user_id: int):
    """
    Manually revoke premium (set premium_until = NULL) in the shared DB.
    Useful for admin commands.
    """
    if not _db_ready:
        return

    async with _write_lock:
        try:
            async with aiosqlite.connect(SHARED_DB_PATH) as db:
                await db.execute("""
                    UPDATE shared_premium
                    SET premium_until = NULL, updated_at = datetime('now')
                    WHERE user_id = ?
                """, (user_id,))
                await db.commit()
            import time as _time_mod
            _cache[user_id] = (None, _time_mod.monotonic())
            print(f"🗑️ [PremiumSync] Premium revoked for user {user_id}")
        except Exception as e:
            print(f"⚠️ [PremiumSync] Revoke error for user {user_id}: {e}")


async def get_all_premium_users() -> Dict[int, datetime]:
    """
    Return {user_id: premium_until} for all users with active premium.
    Used for admin stats.
    """
    result = {}
    if not _db_ready:
        return result
    try:
        now_iso = _ser(_now())
        async with aiosqlite.connect(SHARED_DB_PATH) as db:
            async with db.execute(
                "SELECT user_id, premium_until FROM shared_premium "
                "WHERE premium_until IS NOT NULL AND premium_until > ?",
                (now_iso,)
            ) as cur:
                async for row in cur:
                    uid = row[0]
                    puntil = _des(row[1])
                    if puntil:
                        result[uid] = puntil
    except Exception as e:
        print(f"⚠️ [PremiumSync] get_all_premium_users error: {e}")
    return result


def invalidate_cache(user_id: int):
    """Force a cache eviction for a specific user (call after local state changes)."""
    _cache.pop(user_id, None)
