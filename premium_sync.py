"""
premium_sync.py — Shared Premium + Download Database (Cross-Bot Sync)
----------------------------------------------------------------------
All bot instances on the same server read/write from ONE central SQLite file.

What is synced:
  1. PREMIUM status  — unlocked on any bot, visible on all bots  (≤60 s)
  2. DOWNLOAD count  — downloaded on any bot, counted on all bots (immediate)
     Daily download limits reset at midnight IST across all bots together.

Architecture:
  • Central DB   : /home/ubuntu/premium_shared.db  (same absolute path for every bot)
  • Premium reads : in-memory cache (TTL=60 s) — max 1 DB read per user per minute
  • Download reads: always fresh from DB (single fast SELECT, no cache needed)
  • Download writes: atomic increment via SQL (safe under concurrent bots)
  • Thread-safe    : asyncio.Lock for write operations

Usage in video69_bot.py:
    from premium_sync import (
        init_premium_sync,
        is_premium_synced, grant_premium_synced, get_premium_until_synced,
        get_synced_download_count, increment_synced_download,
        reset_synced_downloads_if_new_day,
        invalidate_cache,
    )

    await init_premium_sync()                          # call once in post_init

    # Premium
    if await is_premium_synced(user_id): ...
    await grant_premium_synced(user_id, hours=12)

    # Downloads
    count = await get_synced_download_count(user_id)   # how many downloaded today
    await increment_synced_download(user_id)            # record a new download
    await reset_synced_downloads_if_new_day(user_id)   # auto-reset at midnight IST
"""

import os
import asyncio
import aiosqlite
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Tuple

# ── Config ────────────────────────────────────────────────────────────────────
# ALL bots on the same server must use this exact path.
SHARED_DB_PATH = os.getenv("SHARED_PREMIUM_DB", "/home/ubuntu/premium_shared.db")

# IST offset (UTC+5:30) — used for midnight reset of download counts
_IST = timedelta(hours=5, minutes=30)

# Premium cache TTL: how long (seconds) to trust a cached result before re-querying DB.
# 60 s = fresh enough that a cross-bot premium grant propagates quickly.
PREMIUM_CACHE_TTL = 60

# ── In-memory caches ──────────────────────────────────────────────────────────
# Premium: { user_id: (premium_until, cached_at_monotonic) }
_prem_cache: Dict[int, Tuple[Optional[datetime], float]] = {}

_write_lock = asyncio.Lock()
_db_ready = False


# ── SQL schemas ───────────────────────────────────────────────────────────────
_DDL = """
-- ── Shared premium table ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS shared_premium (
    user_id       INTEGER PRIMARY KEY,
    premium_until TEXT,               -- ISO-8601 UTC, NULL = no premium
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sp_premium
    ON shared_premium(premium_until)
    WHERE premium_until IS NOT NULL;

-- ── Shared download counter table ────────────────────────────────────────
-- reset_date is stored as YYYY-MM-DD in IST so each bot knows when to reset
CREATE TABLE IF NOT EXISTS shared_downloads (
    user_id          INTEGER PRIMARY KEY,
    downloaded_count INTEGER NOT NULL DEFAULT 0,
    reset_date       TEXT    NOT NULL DEFAULT ''   -- IST date 'YYYY-MM-DD'
);
"""


# ── Internal helpers ──────────────────────────────────────────────────────────
def _ser(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _des(s) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _today_ist() -> str:
    """Return today's date in IST as 'YYYY-MM-DD' string."""
    return (_now_utc() + _IST).strftime("%Y-%m-%d")


async def _open_db() -> aiosqlite.Connection:
    """Open the shared DB with WAL mode enabled."""
    db = await aiosqlite.connect(SHARED_DB_PATH)
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA synchronous=NORMAL")
    return db


# ── Init ──────────────────────────────────────────────────────────────────────
async def init_premium_sync():
    """
    Create all shared tables if they don't exist. Call ONCE in post_init.
    Sets _db_ready=True on success; on failure bots degrade gracefully to
    independent operation (no crash, just no cross-bot sync).
    """
    global _db_ready
    try:
        db = await _open_db()
        # Execute each statement individually (aiosqlite doesn't support executescript)
        for stmt in _DDL.split(";"):
            stmt = stmt.strip()
            if stmt:
                await db.execute(stmt)
        await db.commit()
        await db.close()
        _db_ready = True
        print(f"✅ [SharedSync] Shared DB ready: {SHARED_DB_PATH}")
    except Exception as e:
        print(f"⚠️ [SharedSync] Could not init shared DB: {e}")
        print(f"⚠️ [SharedSync] Running without cross-bot sync.")
        _db_ready = False


# ══════════════════════════════════════════════════════════════════════════════
#  PREMIUM SYNC
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_premium_until(user_id: int) -> Optional[datetime]:
    """Read premium_until from shared DB (single SELECT, no cache)."""
    try:
        db = await _open_db()
        async with db.execute(
            "SELECT premium_until FROM shared_premium WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        await db.close()
        return _des(row[0]) if row else None
    except Exception as e:
        print(f"⚠️ [SharedSync] premium read error uid={user_id}: {e}")
        return None


async def is_premium_synced(user_id: int) -> bool:
    """
    True if user has active premium in the shared DB.
    Uses in-memory cache (TTL=60 s) — at most 1 DB read per user per minute.
    Returns False on DB error (fail-safe).
    """
    if not _db_ready:
        return False

    import time as _t
    now_mono = _t.monotonic()

    cached = _prem_cache.get(user_id)
    if cached:
        puntil, at = cached
        if now_mono - at < PREMIUM_CACHE_TTL:
            return bool(puntil) and _now_utc() < puntil

    puntil = await _fetch_premium_until(user_id)
    _prem_cache[user_id] = (puntil, now_mono)
    return bool(puntil) and _now_utc() < puntil


async def grant_premium_synced(user_id: int, hours: int = 12) -> datetime:
    """
    Write premium_until to the shared DB and update local cache immediately.
    All other bots will see this within PREMIUM_CACHE_TTL seconds (≤60 s).
    Returns the new premium_until (UTC).
    """
    puntil = _now_utc() + timedelta(hours=hours)

    if not _db_ready:
        import time as _t
        _prem_cache[user_id] = (puntil, _t.monotonic())
        return puntil

    async with _write_lock:
        try:
            db = await _open_db()
            await db.execute("""
                INSERT INTO shared_premium (user_id, premium_until, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(user_id) DO UPDATE SET
                    premium_until = excluded.premium_until,
                    updated_at    = excluded.updated_at
            """, (user_id, _ser(puntil)))
            await db.commit()
            await db.close()

            import time as _t
            _prem_cache[user_id] = (puntil, _t.monotonic())
            print(f"✅ [SharedSync] Premium granted uid={user_id} until {puntil.isoformat()}")
        except Exception as e:
            print(f"⚠️ [SharedSync] premium write error uid={user_id}: {e}")

    return puntil


async def get_premium_until_synced(user_id: int) -> Optional[datetime]:
    """
    Return active premium_until datetime from shared DB (cached).
    Returns None if no premium or it has expired.
    """
    if not _db_ready:
        return None

    import time as _t
    now_mono = _t.monotonic()

    cached = _prem_cache.get(user_id)
    if cached:
        puntil, at = cached
        if now_mono - at < PREMIUM_CACHE_TTL:
            return puntil if (puntil and _now_utc() < puntil) else None

    puntil = await _fetch_premium_until(user_id)
    _prem_cache[user_id] = (puntil, now_mono)
    return puntil if (puntil and _now_utc() < puntil) else None


async def revoke_premium_synced(user_id: int):
    """Set premium_until = NULL in the shared DB (admin tool)."""
    if not _db_ready:
        return
    async with _write_lock:
        try:
            db = await _open_db()
            await db.execute(
                "UPDATE shared_premium SET premium_until=NULL, updated_at=datetime('now') "
                "WHERE user_id=?", (user_id,)
            )
            await db.commit()
            await db.close()
            import time as _t
            _prem_cache[user_id] = (None, _t.monotonic())
            print(f"🗑️ [SharedSync] Premium revoked uid={user_id}")
        except Exception as e:
            print(f"⚠️ [SharedSync] revoke error uid={user_id}: {e}")


async def get_all_premium_users_synced() -> Dict[int, datetime]:
    """Return {user_id: premium_until} for all currently active premium users."""
    result = {}
    if not _db_ready:
        return result
    try:
        db = await _open_db()
        now_iso = _ser(_now_utc())
        async with db.execute(
            "SELECT user_id, premium_until FROM shared_premium "
            "WHERE premium_until IS NOT NULL AND premium_until > ?", (now_iso,)
        ) as cur:
            async for row in cur:
                puntil = _des(row[1])
                if puntil:
                    result[row[0]] = puntil
        await db.close()
    except Exception as e:
        print(f"⚠️ [SharedSync] get_all_premium_users error: {e}")
    return result


# ══════════════════════════════════════════════════════════════════════════════
#  DOWNLOAD SYNC
# ══════════════════════════════════════════════════════════════════════════════

async def get_synced_download_count(user_id: int) -> int:
    """
    Return how many downloads this user has made TODAY (IST) across ALL bots.
    If the stored reset_date is older than today, returns 0 (treats as reset).
    Returns 0 on DB error (fail-open — don't block downloads on DB failure).
    """
    if not _db_ready:
        return 0
    today = _today_ist()
    try:
        db = await _open_db()
        async with db.execute(
            "SELECT downloaded_count, reset_date FROM shared_downloads WHERE user_id=?",
            (user_id,)
        ) as cur:
            row = await cur.fetchone()
        await db.close()

        if row is None:
            return 0
        count, reset_date = row[0], row[1]
        # If the stored date is from a previous day, treat as 0 (new day)
        return count if reset_date == today else 0
    except Exception as e:
        print(f"⚠️ [SharedSync] download read error uid={user_id}: {e}")
        return 0


async def increment_synced_download(user_id: int) -> int:
    """
    Atomically increment the shared download counter for user_id (today IST).
    If the stored date is a previous day, resets the counter to 1 first.
    Returns the NEW total download count after increment.
    """
    if not _db_ready:
        return 0

    today = _today_ist()
    async with _write_lock:
        try:
            db = await _open_db()

            async with db.execute(
                "SELECT downloaded_count, reset_date FROM shared_downloads WHERE user_id=?",
                (user_id,)
            ) as cur:
                row = await cur.fetchone()

            if row is None:
                # First ever download for this user
                new_count = 1
                await db.execute(
                    "INSERT INTO shared_downloads (user_id, downloaded_count, reset_date) "
                    "VALUES (?, 1, ?)",
                    (user_id, today)
                )
            elif row[1] != today:
                # New day — reset counter to 1
                new_count = 1
                await db.execute(
                    "UPDATE shared_downloads SET downloaded_count=1, reset_date=? WHERE user_id=?",
                    (today, user_id)
                )
            else:
                # Same day — increment
                new_count = row[0] + 1
                await db.execute(
                    "UPDATE shared_downloads SET downloaded_count=downloaded_count+1 WHERE user_id=?",
                    (user_id,)
                )

            await db.commit()
            await db.close()
            print(f"📥 [SharedSync] Download #{new_count} recorded for uid={user_id} (date={today})")
            return new_count
        except Exception as e:
            print(f"⚠️ [SharedSync] download write error uid={user_id}: {e}")
            return 0


async def reset_synced_downloads(user_id: int):
    """
    Manually reset a user's shared download counter to 0.
    Useful for admin commands or midnight cron cleanup.
    """
    if not _db_ready:
        return
    async with _write_lock:
        try:
            db = await _open_db()
            await db.execute(
                "UPDATE shared_downloads SET downloaded_count=0, reset_date=? WHERE user_id=?",
                (_today_ist(), user_id)
            )
            await db.commit()
            await db.close()
            print(f"🔄 [SharedSync] Downloads reset for uid={user_id}")
        except Exception as e:
            print(f"⚠️ [SharedSync] download reset error uid={user_id}: {e}")


async def get_total_synced_downloads_today() -> int:
    """Return total downloads across ALL users today (for admin stats)."""
    if not _db_ready:
        return 0
    today = _today_ist()
    try:
        db = await _open_db()
        async with db.execute(
            "SELECT SUM(downloaded_count) FROM shared_downloads WHERE reset_date=?", (today,)
        ) as cur:
            row = await cur.fetchone()
        await db.close()
        return row[0] or 0 if row else 0
    except Exception as e:
        print(f"⚠️ [SharedSync] total downloads error: {e}")
        return 0


# ══════════════════════════════════════════════════════════════════════════════
#  CACHE UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def invalidate_cache(user_id: int):
    """Force premium cache eviction for a user (no download cache to clear)."""
    _prem_cache.pop(user_id, None)
