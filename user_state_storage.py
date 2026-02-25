"""
User State Storage Module — SQLite + In-Memory Write-Behind Cache
-----------------------------------------------------------------
Architecture:
  • All READS  → instant from in-memory dict (zero I/O)
  • All WRITES → update in-memory dict + add user_id to _dirty_set
  • Background task flushes dirty users to SQLite every 10 seconds
  • On shutdown, does a final forced flush

This gives the bot zero-lag writes on the hot path while keeping
data safely persisted to a proper indexed database.
"""

import json
import os
import asyncio
import aiosqlite
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, Set

# ── Config ────────────────────────────────────────────────────────────────────
DB_FILE         = "user_states.db"
OLD_JSON_FILE   = "user_states.json"        # migrated automatically if present
FLUSH_INTERVAL  = 10                         # seconds between background flushes
MAX_SEEN_VIDEOS = 5000                       # cap per user to keep DB lean

# ── In-memory state ───────────────────────────────────────────────────────────
_cache: Dict[int, Dict[str, Any]] = {}       # THE single source of truth for reads
_dirty: Set[int] = set()                     # user_ids that need writing to DB
_flush_task: Optional[asyncio.Task] = None
_db_initialized: bool = False


# ── Helpers ───────────────────────────────────────────────────────────────────
def _ser_dt(dt) -> Optional[str]:
    """Serialize datetime → ISO string."""
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    return dt

def _des_dt(s) -> Optional[datetime]:
    """Deserialize ISO string → datetime."""
    if s is None or not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


# ── Database schema ───────────────────────────────────────────────────────────
_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS users (
    user_id            INTEGER PRIMARY KEY,
    seen_videos        TEXT    NOT NULL DEFAULT '[]',
    hourly_count       INTEGER NOT NULL DEFAULT 0,
    last_reset         TEXT,
    premium_until      TEXT,
    ad_link            TEXT,
    ad_token           TEXT,
    downloaded_count   INTEGER NOT NULL DEFAULT 0,
    failed_message_ids TEXT    NOT NULL DEFAULT '[]',
    updated_at         TEXT    NOT NULL DEFAULT (datetime('now'))
)
"""
_CREATE_IDX_PREMIUM = """
CREATE INDEX IF NOT EXISTS idx_premium
    ON users(premium_until)
    WHERE premium_until IS NOT NULL
"""
_UPSERT = """
INSERT INTO users
    (user_id, seen_videos, hourly_count, last_reset, premium_until,
     ad_link, ad_token, downloaded_count, failed_message_ids, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
ON CONFLICT(user_id) DO UPDATE SET
    seen_videos        = excluded.seen_videos,
    hourly_count       = excluded.hourly_count,
    last_reset         = excluded.last_reset,
    premium_until      = excluded.premium_until,
    ad_link            = excluded.ad_link,
    ad_token           = excluded.ad_token,
    downloaded_count   = excluded.downloaded_count,
    failed_message_ids = excluded.failed_message_ids,
    updated_at         = excluded.updated_at
"""


# ── Init ──────────────────────────────────────────────────────────────────────
async def init_db():
    """
    Create the SQLite database, load all users into the cache,
    and migrate data from old JSON file if it exists.
    Called once from post_init (async context).
    """
    global _db_initialized

    async with aiosqlite.connect(DB_FILE) as db:
        # Enable WAL mode — much faster for concurrent reads + writes
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")   # safe + fast
        await db.execute("PRAGMA cache_size=-32000")    # 32 MB page cache
        await db.execute(_CREATE_TABLE)
        await db.execute(_CREATE_IDX_PREMIUM)
        await db.commit()

    # Load all existing rows into cache
    await _load_all_into_cache()

    # Migrate from old JSON file if SQLite is empty and JSON exists
    if not _cache and os.path.exists(OLD_JSON_FILE):
        print(f"📦 Migrating data from {OLD_JSON_FILE} → {DB_FILE} ...")
        await _migrate_from_json()

    _db_initialized = True
    premium_count = sum(
        1 for s in _cache.values()
        if s.get('premium_until') and _des_dt(s['premium_until']) and
           _des_dt(s['premium_until']) > datetime.now(timezone.utc)
    )
    print(f"🗄️  SQLite DB ready: {DB_FILE}")
    print(f"   👥 {len(_cache)} users loaded into memory cache")
    if premium_count:
        print(f"   💎 {premium_count} active premium users")


async def _load_all_into_cache():
    """Read every row from users table into _cache."""
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users") as cur:
            async for row in cur:
                uid = row["user_id"]
                _cache[uid] = {
                    "seen_videos":        json.loads(row["seen_videos"] or "[]"),
                    "hourly_count":       row["hourly_count"],
                    "last_reset":         _des_dt(row["last_reset"]),
                    "premium_until":      _des_dt(row["premium_until"]),
                    "ad_link":            row["ad_link"],
                    "ad_token":           row["ad_token"],
                    "downloaded_count":   row["downloaded_count"],
                    "failed_message_ids": json.loads(row["failed_message_ids"] or "[]"),
                }


async def _migrate_from_json():
    """One-time migration: read old JSON → write to SQLite → rename JSON as backup."""
    try:
        with open(OLD_JSON_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        async with aiosqlite.connect(DB_FILE) as db:
            for uid_str, state in data.items():
                uid = int(uid_str)
                sv = state.get("seen_videos", [])
                if len(sv) > MAX_SEEN_VIDEOS:
                    sv = sv[-MAX_SEEN_VIDEOS:]
                _cache[uid] = {
                    "seen_videos":        sv,
                    "hourly_count":       state.get("hourly_count", 0),
                    "last_reset":         _des_dt(state.get("last_reset")),
                    "premium_until":      _des_dt(state.get("premium_until")),
                    "ad_link":            state.get("ad_link"),
                    "ad_token":           state.get("ad_token"),
                    "downloaded_count":   state.get("downloaded_count", 0),
                    "failed_message_ids": state.get("failed_message_ids", []),
                }
                await db.execute(_UPSERT, _row_params(uid, _cache[uid]))
            await db.commit()

        # Rename old file so we never migrate twice
        os.rename(OLD_JSON_FILE, OLD_JSON_FILE + ".migrated")
        print(f"✅ Migrated {len(_cache)} users. Old file → {OLD_JSON_FILE}.migrated")
    except Exception as e:
        print(f"⚠️ Migration error: {e}")


# ── Background flush loop ─────────────────────────────────────────────────────
def start_flush_loop() -> asyncio.Task:
    """Start the background task that flushes dirty users every FLUSH_INTERVAL seconds."""
    global _flush_task
    _flush_task = asyncio.create_task(_flush_loop(), name="db_flush_loop")
    return _flush_task


async def _flush_loop():
    """Background coroutine — flushes dirty users to SQLite every 10 seconds."""
    while True:
        try:
            await asyncio.sleep(FLUSH_INTERVAL)
            await _flush_dirty()
        except asyncio.CancelledError:
            await _flush_dirty(force=True)   # Final save on shutdown
            break
        except Exception as e:
            print(f"⚠️ DB flush loop error: {e}")


async def _flush_dirty(force: bool = False):
    """Write all dirty users to SQLite in a single transaction."""
    if not _dirty:
        return

    to_save = list(_dirty)
    _dirty.clear()                     # Clear before await so new dirties aren't lost

    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            for uid in to_save:
                if uid in _cache:
                    await db.execute(_UPSERT, _row_params(uid, _cache[uid]))
            await db.commit()
        if force:
            print(f"💾 Final flush: saved {len(to_save)} user(s) to SQLite")
    except Exception as e:
        _dirty.update(to_save)         # Re-queue on failure so data isn't lost
        print(f"⚠️ DB write error: {e}")


def stop_flush_loop():
    """Cancel the background flush task (call on shutdown)."""
    if _flush_task and not _flush_task.done():
        _flush_task.cancel()


def _row_params(uid: int, state: Dict[str, Any]) -> tuple:
    """Build the parameter tuple for the UPSERT statement."""
    sv = state.get("seen_videos", [])
    if len(sv) > MAX_SEEN_VIDEOS:
        sv = sv[-MAX_SEEN_VIDEOS:]
    return (
        uid,
        json.dumps(sv),
        state.get("hourly_count", 0),
        _ser_dt(state.get("last_reset")),
        _ser_dt(state.get("premium_until")),
        state.get("ad_link"),
        state.get("ad_token"),
        state.get("downloaded_count", 0),
        json.dumps(state.get("failed_message_ids", [])),
    )


# ── Public API  (called by video69_bot.py — keeps same interface) ─────────────
def load_user_states() -> Dict[int, Dict[str, Any]]:
    """Return the in-memory cache (populated by init_db at startup)."""
    return _cache


def save_user_states(states: Dict[int, Dict[str, Any]], _hint_uid: int = None):
    """
    Mark user(s) as dirty for background flush (returns instantly, no I/O).
    Pass _hint_uid to mark only one user; otherwise all users in `states` are marked.
    """
    if _hint_uid is not None:
        _dirty.add(_hint_uid)
    else:
        _dirty.update(states.keys())


def save_user_state(user_id: int):
    """Mark a single user as dirty — cheapest possible way to schedule a save."""
    _dirty.add(user_id)


def init_user_state_data(user_id: int, states: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
    """Initialize user state with defaults if not present."""
    if user_id in states:
        return states[user_id]

    now = datetime.now(timezone.utc)
    ist_offset = timedelta(hours=5, minutes=30)
    now_ist = now + ist_offset
    period_start_ist = now_ist.replace(
        hour=(now_ist.hour // 12) * 12, minute=0, second=0, microsecond=0
    )
    last_reset = period_start_ist - ist_offset

    new_state = {
        "seen_videos":        [],
        "hourly_count":       0,
        "last_reset":         last_reset,
        "premium_until":      None,
        "ad_link":            None,
        "ad_token":           None,
        "downloaded_count":   0,
        "failed_message_ids": [],
    }
    states[user_id] = new_state
    save_user_state(user_id)           # Queue for DB write
    return new_state


def cleanup_expired_data(states: Dict[int, Dict[str, Any]]) -> int:
    """Clear expired premium tokens. Returns number of users cleaned."""
    now = datetime.now(timezone.utc)
    cleaned = 0
    for uid, state in states.items():
        puntil = _des_dt(state.get("premium_until"))
        if puntil and now > puntil and state.get("ad_token"):
            state["ad_token"] = None
            state["ad_link"]  = None
            save_user_state(uid)
            cleaned += 1
    return cleaned


def get_premium_users_count(states: Dict[int, Dict[str, Any]]) -> int:
    now = datetime.now(timezone.utc)
    return sum(
        1 for s in states.values()
        if _des_dt(s.get("premium_until")) and _des_dt(s["premium_until"]) > now
    )


def get_stats(states: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    total  = len(states)
    prem   = 0
    videos = 0
    for s in states.values():
        puntil = _des_dt(s.get("premium_until"))
        if puntil and puntil > now:
            prem += 1
        videos += len(s.get("seen_videos", []))
    return {
        "total_users":        total,
        "premium_users":      prem,
        "free_users":         total - prem,
        "total_videos_watched": videos,
        "avg_videos_per_user":  videos / total if total else 0,
    }
