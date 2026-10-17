"""
free_users_db.py — Shared Free-User Database (All Free Bots)
=============================================================
ONE shared SQLite database for ALL 9 free bots running in the same process.
Completely separate from user_states.db and premium_shared.db.

Architecture:
  • All READS  → instant from in-memory dict (zero I/O on hot path)
  • All WRITES → update in-memory dict + add user_id to _dirty set
  • Background task flushes dirty users to SQLite every FLUSH_INTERVAL seconds
  • On shutdown: CancelledError triggers a final forced flush

Tracked per user (cross-bot, shared across all 9 bots):
  • user_id          — Telegram user ID (cross-bot identity, primary key)
  • first_name       — display name
  • username         — @handle (nullable)
  • first_seen       — first interaction timestamp (any bot)
  • last_active      — last activity timestamp (any bot)
  • total_delivered  — lifetime videos delivered across ALL bots
  • daily_count      — today's watch count (resets midnight IST)
  • daily_date       — IST date of current daily_count (YYYY-MM-DD)
  • milestone_10/20/30 — promotion flags, each fires exactly once
  • seen_videos      — set of seen video IDs (capped at MAX_SEEN, cross-bot)

Per-bot registration (for knowing which users belong to which bot):
  • bot_registrations table — (user_id, bot_name) pairs
"""

import json
import os
import asyncio
import aiosqlite
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, Set, List

# ── Config ────────────────────────────────────────────────────────────────────
DB_FILE        = os.getenv("FREE_USERS_DB", "free_users.db")
FLUSH_INTERVAL = 30       # seconds between background flushes
MAX_SEEN       = 5000     # cap per-user seen_videos to keep DB lean
_IST           = timedelta(hours=5, minutes=30)

# ── In-memory state (module-level, shared by ALL bot instances in the process) ─
_cache: Dict[int, Dict[str, Any]] = {}  # single source of truth for reads
_dirty: Set[int] = set()                # user_ids needing a DB write
_flush_task: Optional[asyncio.Task] = None
_persistent_db = None                  # reused connection — avoids open/close every flush
_initialized: bool = False

# Per-bot user lists (loaded from DB at startup)
_bot_reg: Dict[str, Set[int]] = {}     # {bot_name: {user_ids}}
_reg_dirty: Set[tuple] = set()         # {(user_id, bot_name)} pairs to persist

# Video metadata & reaction cache (shared across all bots)
_video_meta_cache: Dict[tuple, Dict[str, Any]] = {}       # (channel_id, message_id) -> {"dur": int, "sz": int, "key": str}
_video_reactions_cache: Dict[str, Dict[str, Any]] = {}    # video_key -> {"likes": int, "dislikes": int, "dur": int, "sz": int}
_user_reactions_cache: Dict[tuple, str] = {}              # (user_id, video_key) -> "like" | "dislike"

# ── Datetime helpers ──────────────────────────────────────────────────────────

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _today_ist() -> str:
    """Today's date in IST as 'YYYY-MM-DD'."""
    return (_now_utc() + _IST).strftime("%Y-%m-%d")


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _from_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ── Schema ────────────────────────────────────────────────────────────────────

_DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id         INTEGER PRIMARY KEY,
        first_name      TEXT    NOT NULL DEFAULT '',
        username        TEXT,
        first_seen      TEXT    NOT NULL,
        last_active     TEXT    NOT NULL,
        total_delivered INTEGER NOT NULL DEFAULT 0,
        daily_count     INTEGER NOT NULL DEFAULT 0,
        daily_date      TEXT    NOT NULL DEFAULT '',
        milestone_10    INTEGER NOT NULL DEFAULT 0,
        milestone_20    INTEGER NOT NULL DEFAULT 0,
        milestone_30    INTEGER NOT NULL DEFAULT 0,
        last_milestone  INTEGER NOT NULL DEFAULT 0,
        seen_videos     TEXT    NOT NULL DEFAULT '[]',
        updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_users_last_active ON users(last_active)",
    "CREATE INDEX IF NOT EXISTS idx_users_total       ON users(total_delivered)",
    """
    CREATE TABLE IF NOT EXISTS bot_registrations (
        user_id       INTEGER NOT NULL,
        bot_name      TEXT    NOT NULL,
        registered_at TEXT    NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (user_id, bot_name)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_bot_reg ON bot_registrations(bot_name)",
    """
    CREATE TABLE IF NOT EXISTS video_metadata (
        video_channel_id INTEGER NOT NULL,
        message_id       INTEGER NOT NULL,
        duration         INTEGER NOT NULL DEFAULT 0,
        file_size        INTEGER NOT NULL DEFAULT 0,
        video_key        TEXT    NOT NULL,
        PRIMARY KEY (video_channel_id, message_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_video_meta_key ON video_metadata(video_key)",
    """
    CREATE TABLE IF NOT EXISTS video_reactions (
        video_key        TEXT PRIMARY KEY,
        duration         INTEGER NOT NULL DEFAULT 0,
        file_size        INTEGER NOT NULL DEFAULT 0,
        likes_count      INTEGER NOT NULL DEFAULT 0,
        dislikes_count   INTEGER NOT NULL DEFAULT 0,
        last_updated     TEXT    NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_video_reactions (
        user_id          INTEGER NOT NULL,
        video_key        TEXT NOT NULL,
        reaction         TEXT NOT NULL,
        updated_at       TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (user_id, video_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_user_reactions_uid ON user_video_reactions(user_id)",
]

_UPSERT = """
INSERT INTO users
    (user_id, first_name, username, first_seen, last_active,
     total_delivered, daily_count, daily_date,
     milestone_10, milestone_20, milestone_30, last_milestone, seen_videos, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
ON CONFLICT(user_id) DO UPDATE SET
    first_name      = excluded.first_name,
    username        = excluded.username,
    last_active     = excluded.last_active,
    total_delivered = excluded.total_delivered,
    daily_count     = excluded.daily_count,
    daily_date      = excluded.daily_date,
    milestone_10    = excluded.milestone_10,
    milestone_20    = excluded.milestone_20,
    milestone_30    = excluded.milestone_30,
    last_milestone  = excluded.last_milestone,
    seen_videos     = excluded.seen_videos,
    updated_at      = excluded.updated_at
"""


def _row_params(uid: int, u: Dict[str, Any]) -> tuple:
    """Build the parameter tuple for the UPSERT statement."""
    sv = u.get("seen_videos", set())
    sv_list = list(sv) if isinstance(sv, set) else list(sv)
    if len(sv_list) > MAX_SEEN:
        sv_list = sv_list[-MAX_SEEN:]
    return (
        uid,
        u.get("first_name", ""),
        u.get("username"),
        _iso(u.get("first_seen")) or _iso(_now_utc()),
        _iso(u.get("last_active")) or _iso(_now_utc()),
        u.get("total_delivered", 0),
        u.get("daily_count", 0),
        u.get("daily_date", ""),
        1 if u.get("milestone_10") else 0,
        1 if u.get("milestone_20") else 0,
        1 if u.get("milestone_30") else 0,
        int(u.get("last_milestone", 0)),
        json.dumps(sv_list),
    )


# ── Initialisation ────────────────────────────────────────────────────────────

async def init_db(db_path: Optional[str] = None):
    """
    Create schema, load all users into cache, load bot registrations.
    Call ONCE from free_bot_runner.py before starting any bot.
    Never call from individual bot post_init hooks.
    """
    global _initialized, DB_FILE
    if db_path:
        DB_FILE = db_path

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA cache_size=-16000")  # 16 MB page cache
        for stmt in _DDL_STATEMENTS:
            await db.execute(stmt)
        # Migrate existing users table if last_milestone column does not exist yet
        try:
            await db.execute("ALTER TABLE users ADD COLUMN last_milestone INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        await db.commit()

    await _load_all_into_cache()
    await _load_bot_registrations()
    await _load_video_data_into_cache()
    _initialized = True
    print(f"[FreeDB] Ready: {DB_FILE} -- {len(_cache)} users, {len(_video_reactions_cache)} videos loaded")


async def _load_all_into_cache():
    """Read every row from the users table into _cache."""
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users") as cur:
            async for row in cur:
                uid = row["user_id"]
                row_keys = row.keys()
                last_m = 0
                if "last_milestone" in row_keys and row["last_milestone"]:
                    last_m = int(row["last_milestone"])
                elif row["milestone_30"]:
                    last_m = 30
                elif row["milestone_20"]:
                    last_m = 20
                elif row["milestone_10"]:
                    last_m = 10

                _cache[uid] = {
                    "first_name":      row["first_name"],
                    "username":        row["username"],
                    "first_seen":      _from_iso(row["first_seen"]),
                    "last_active":     _from_iso(row["last_active"]),
                    "total_delivered": row["total_delivered"],
                    "daily_count":     row["daily_count"],
                    "daily_date":      row["daily_date"],
                    "milestone_10":    bool(row["milestone_10"]),
                    "milestone_20":    bool(row["milestone_20"]),
                    "milestone_30":    bool(row["milestone_30"]),
                    "last_milestone":  last_m,
                    "seen_videos":     set(json.loads(row["seen_videos"] or "[]")),
                }


async def _load_bot_registrations():
    """Load bot_registrations table into _bot_reg in-memory dict."""
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT user_id, bot_name FROM bot_registrations") as cur:
                async for row in cur:
                    bot_name = row[1]
                    if bot_name not in _bot_reg:
                        _bot_reg[bot_name] = set()
                    _bot_reg[bot_name].add(row[0])
    except Exception as e:
        print(f"[FreeDB] load_bot_registrations error: {e}")


async def _load_video_data_into_cache():
    """Load video metadata, reactions, and user reactions into in-memory caches."""
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM video_metadata") as cur:
                async for row in cur:
                    k = (row["video_channel_id"], row["message_id"])
                    _video_meta_cache[k] = {
                        "dur": row["duration"],
                        "sz":  row["file_size"],
                        "key": row["video_key"],
                    }
            async with db.execute("SELECT * FROM video_reactions") as cur:
                async for row in cur:
                    vk = row["video_key"]
                    _video_reactions_cache[vk] = {
                        "likes":    row["likes_count"],
                        "dislikes": row["dislikes_count"],
                        "dur":      row["duration"],
                        "sz":       row["file_size"],
                    }
            async with db.execute("SELECT user_id, video_key, reaction FROM user_video_reactions") as cur:
                async for row in cur:
                    _user_reactions_cache[(row["user_id"], row["video_key"])] = row["reaction"]
    except Exception as e:
        print(f"[FreeDB] load_video_data error: {e}")


# ── Background flush loop ─────────────────────────────────────────────────────

def start_flush_loop() -> asyncio.Task:
    """Start the background task that flushes dirty users every FLUSH_INTERVAL seconds."""
    global _flush_task
    _flush_task = asyncio.create_task(_flush_loop(), name="free_db_flush")
    return _flush_task


async def _flush_loop():
    """Background coroutine — flushes dirty users to SQLite every FLUSH_INTERVAL seconds."""
    while True:
        try:
            await asyncio.sleep(FLUSH_INTERVAL)
            await _flush_dirty()
            await _flush_registrations()
        except asyncio.CancelledError:
            await _flush_dirty(force=True)   # Final save on shutdown
            await _flush_registrations()
            break
        except Exception as e:
            print(f"⚠️ [FreeDB] Flush loop error: {e}")


async def _flush_dirty(force: bool = False):
    """Write all dirty users to SQLite in one transaction. Reuses persistent connection."""
    global _persistent_db
    if not _dirty:
        return

    to_save = list(_dirty)
    _dirty.clear()   # Clear before await so new dirty entries aren't lost

    try:
        if _persistent_db is None:
            _persistent_db = await aiosqlite.connect(DB_FILE)
            await _persistent_db.execute("PRAGMA journal_mode=WAL")
            await _persistent_db.execute("PRAGMA synchronous=NORMAL")
        for uid in to_save:
            if uid in _cache:
                await _persistent_db.execute(_UPSERT, _row_params(uid, _cache[uid]))
        await _persistent_db.commit()
    except Exception as e:
        _dirty.update(to_save)  # Re-queue on failure so data isn't lost
        try:
            if _persistent_db:
                await _persistent_db.close()
        except Exception:
            pass
        _persistent_db = None
        print(f"[FreeDB] Flush error (will retry): {e}")


async def _flush_registrations():
    """Persist new bot_registration pairs to DB."""
    if not _reg_dirty:
        return
    to_save = list(_reg_dirty)
    _reg_dirty.clear()
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.executemany(
                "INSERT OR IGNORE INTO bot_registrations (user_id, bot_name) VALUES (?, ?)",
                to_save,
            )
            await db.commit()
    except Exception as e:
        _reg_dirty.update(to_save)
        print(f"[FreeDB] Registration flush error: {e}")


def stop_flush_loop():
    """Cancel the background flush task (triggers final forced flush via CancelledError)."""
    if _flush_task and not _flush_task.done():
        _flush_task.cancel()


async def close_db():
    """Close the persistent DB connection. Call from runner after stop_flush_loop."""
    global _persistent_db
    if _persistent_db:
        try:
            await _persistent_db.close()
        except Exception:
            pass
        _persistent_db = None


# ── Public API ────────────────────────────────────────────────────────────────

def _make_new_user(user_id: int, first_name: str, username: Optional[str]) -> Dict[str, Any]:
    now = _now_utc()
    return {
        "first_name":      first_name,
        "username":        username,
        "first_seen":      now,
        "last_active":     now,
        "total_delivered": 0,
        "daily_count":     0,
        "daily_date":      "",
        "milestone_10":    False,
        "milestone_20":    False,
        "milestone_30":    False,
        "last_milestone":  0,
        "seen_videos":     set(),
    }


def get_or_create_user(user_id: int, first_name: str = "",
                       username: Optional[str] = None) -> Dict[str, Any]:
    """
    Return user dict from cache (creating a new record if this user is unknown).
    Updates first_name/username if they changed.
    Always updates last_active.
    Returns instantly — zero I/O.
    """
    if user_id not in _cache:
        _cache[user_id] = _make_new_user(user_id, first_name, username)
        _dirty.add(user_id)
        print(f"[FreeDB] New user: {first_name} (ID: {user_id})")
    else:
        u = _cache[user_id]
        if u.get("first_name") != first_name or u.get("username") != username:
            u["first_name"] = first_name
            u["username"] = username
        u["last_active"] = _now_utc()
        _dirty.add(user_id)
    return _cache[user_id]


def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    """Return user dict or None if unknown. Zero I/O."""
    return _cache.get(user_id)


def get_all_user_ids() -> List[int]:
    """Return all registered user IDs. Zero I/O."""
    return list(_cache.keys())


def get_user_count() -> int:
    """Return total registered users. Zero I/O."""
    return len(_cache)


def mark_user_dirty(user_id: int):
    """Schedule a user record for the next DB flush."""
    if user_id in _cache:
        _dirty.add(user_id)


# ── Daily limit helpers ───────────────────────────────────────────────────────

def check_and_reset_daily(user_id: int) -> bool:
    """
    Reset daily_count to 0 if today's IST date differs from the stored daily_date.
    Returns True if a reset occurred.
    """
    u = _cache.get(user_id)
    if not u:
        return False
    today = _today_ist()
    if u["daily_date"] != today:
        u["daily_count"] = 0
        u["daily_date"] = today
        _dirty.add(user_id)
        return True
    return False


def get_daily_count(user_id: int) -> int:
    """Return today's watch count, auto-resetting if it's a new IST day."""
    check_and_reset_daily(user_id)
    u = _cache.get(user_id)
    return u["daily_count"] if u else 0


# ── Video delivery ────────────────────────────────────────────────────────────

def get_random_unseen_video(user_id: int, min_id: int, max_id: int) -> int:
    """
    Return a random video message ID not yet seen by this user (cross-bot).

    FAST PATH (O(1) average): probabilistic sampling — avoids full set difference
    for most users (seen < 80% of catalog).
    SLOW PATH: exact set difference when near end of catalog.
    Reset: when all videos have been seen, clears seen_videos and starts over.
    """
    import random
    u = _cache.get(user_id)
    seen = u["seen_videos"] if u else set()
    all_ids = frozenset(range(min_id, max_id + 1))
    fast_threshold = int(len(all_ids) * 0.8)

    # FAST PATH — probabilistic sampling
    if len(seen) < fast_threshold:
        for _ in range(50):
            vid = random.randint(min_id, max_id)
            if vid not in seen:
                return vid
        # Fell through 50 tries — fall to slow path

    # SLOW PATH — exact set difference
    unseen = all_ids - seen
    if not unseen:
        # All videos seen — reset and restart from full catalog
        if u:
            u["seen_videos"] = set()
            _dirty.add(user_id)
        unseen = all_ids

    return random.choice(list(unseen))


def record_video_delivered(user_id: int, video_id: int) -> tuple:
    """
    Record a successfully delivered video.
    MUST be called only AFTER copy_message succeeds — prevents duplicate counts.

    Increments: daily_count, total_delivered.
    Adds video_id to seen_videos.
    Returns: (new_daily_count, new_total_delivered)
    """
    check_and_reset_daily(user_id)
    u = _cache[user_id]
    u["daily_count"]     += 1
    u["total_delivered"] += 1
    u["seen_videos"].add(video_id)
    u["last_active"] = _now_utc()
    _dirty.add(user_id)
    return u["daily_count"], u["total_delivered"]


# ── Promotion milestones ──────────────────────────────────────────────────────

def check_and_claim_milestones(user_id: int) -> List[int]:
    """
    Check if the user crossed a multiple of 10 videos (10, 20, 30, 40, 50, ...).
    Each multiple of 10 fires exactly ONCE.
    Cross-bot: milestone is cross-bot because total_delivered is cross-bot.

    Returns list of newly triggered milestone numbers, e.g. [10], [20], [30], [40], etc.
    Returns [] if no new milestone was crossed.
    """
    u = _cache.get(user_id)
    if not u:
        return []

    total = u.get("total_delivered", 0)
    last = u.get("last_milestone", 0)

    # Fallback to legacy milestone flags if last_milestone was not initialized
    if last == 0:
        if u.get("milestone_30"):
            last = 30
        elif u.get("milestone_20"):
            last = 20
        elif u.get("milestone_10"):
            last = 10

    target = (total // 10) * 10

    if target >= 10 and target > last:
        u["last_milestone"] = target
        if target >= 10:
            u["milestone_10"] = True
        if target >= 20:
            u["milestone_20"] = True
        if target >= 30:
            u["milestone_30"] = True
        _dirty.add(user_id)
        return [target]

    return []


# ── Per-bot user registration ─────────────────────────────────────────────────

def register_user_with_bot(user_id: int, bot_name: str):
    """
    Record that this user has interacted with this specific bot. Idempotent.
    Zero I/O on the hot path — new pairs flushed in the background.
    """
    if bot_name not in _bot_reg:
        _bot_reg[bot_name] = set()
    if user_id not in _bot_reg[bot_name]:
        _bot_reg[bot_name].add(user_id)
        _reg_dirty.add((user_id, bot_name))


def get_bot_user_ids(bot_name: str) -> List[int]:
    """Return all user IDs that have interacted with this bot. Zero I/O."""
    return list(_bot_reg.get(bot_name, set()))


def get_all_user_ids() -> List[int]:
    """Return all user IDs across all bots from shared cache. Zero I/O."""
    return list(_cache.keys())


# ── Statistics ────────────────────────────────────────────────────────────────

def get_stats() -> Dict[str, Any]:
    """Return aggregate stats across all bots. Zero I/O — reads entirely from cache."""
    today = _today_ist()
    total          = len(_cache)
    active_today   = sum(
        1 for u in _cache.values()
        if u.get("daily_date") == today and u.get("daily_count", 0) > 0
    )
    videos_today   = sum(
        u.get("daily_count", 0) for u in _cache.values()
        if u.get("daily_date") == today
    )
    total_lifetime = sum(u.get("total_delivered", 0) for u in _cache.values())
    total_likes    = sum(v.get("likes", 0) for v in _video_reactions_cache.values())
    total_dislikes = sum(v.get("dislikes", 0) for v in _video_reactions_cache.values())
    return {
        "total_users":     total,
        "active_today":    active_today,
        "videos_today":    videos_today,
        "total_delivered": total_lifetime,
        "total_likes":     total_likes,
        "total_dislikes":  total_dislikes,
        "tracked_videos":  len(_video_reactions_cache),
    }


def get_behaviour_analytics() -> Dict[str, Any]:
    """
    Compute customer behaviour analytics, retention cohorts,
    and conversion funnel across all bots. Zero I/O.
    """
    now = _now_utc()
    total_users = len(_cache)
    if total_users == 0:
        return {
            "total_users": 0,
            "new_24h": 0,
            "active_24h": 0,
            "active_7d": 0,
            "inactive_7d": 0,
            "total_watched": 0,
            "avg_delivered": 0.0,
            "cohorts": {"bouncers": 0, "casuals": 0, "engaged": 0, "loyal": 0, "superfans": 0},
            "milestones": {"m10": 0, "m20": 0, "m30": 0, "m50": 0},
        }

    active_24h = 0
    active_7d = 0
    inactive_7d = 0
    new_24h = 0

    bouncers = 0    # 1-2 vids
    casuals = 0     # 3-9 vids
    engaged = 0     # 10-29 vids
    loyal = 0       # 30-49 vids
    superfans = 0   # 50+ vids

    m10 = 0
    m20 = 0
    m30 = 0
    m50 = 0
    total_watched = 0

    for u in _cache.values():
        td = u.get("total_delivered", 0)
        total_watched += td

        # Recency
        la = u.get("last_active")
        if la:
            age_sec = (now - la).total_seconds()
            if age_sec <= 86400:
                active_24h += 1
            if age_sec <= 7 * 86400:
                active_7d += 1
            else:
                inactive_7d += 1

        # First seen (new)
        fs = u.get("first_seen")
        if fs and (now - fs).total_seconds() <= 86400:
            new_24h += 1

        # Cohort distribution
        if td <= 2:
            bouncers += 1
        elif td <= 9:
            casuals += 1
        elif td <= 29:
            engaged += 1
        elif td <= 49:
            loyal += 1
        else:
            superfans += 1

        # Milestones
        lm = u.get("last_milestone", 0)
        if lm >= 10 or td >= 10:
            m10 += 1
        if lm >= 20 or td >= 20:
            m20 += 1
        if lm >= 30 or td >= 30:
            m30 += 1
        if lm >= 50 or td >= 50:
            m50 += 1

    avg_del = round(total_watched / total_users, 1)

    return {
        "total_users": total_users,
        "new_24h": new_24h,
        "active_24h": active_24h,
        "active_7d": active_7d,
        "inactive_7d": inactive_7d,
        "total_watched": total_watched,
        "avg_delivered": avg_del,
        "cohorts": {
            "bouncers": bouncers,
            "casuals": casuals,
            "engaged": engaged,
            "loyal": loyal,
            "superfans": superfans,
        },
        "milestones": {
            "m10": m10,
            "m20": m20,
            "m30": m30,
            "m50": m50,
        },
    }


def get_content_analytics() -> Dict[str, Any]:
    """
    Compute content performance, top liked/disliked videos,
    and user video duration preference. Zero I/O.
    """
    total_likes = sum(v.get("likes", 0) for v in _video_reactions_cache.values())
    total_dislikes = sum(v.get("dislikes", 0) for v in _video_reactions_cache.values())

    dur_short = 0    # < 3 min
    dur_medium = 0   # 3 - 7 min
    dur_long = 0     # > 7 min

    vids = []
    for k, v in _video_reactions_cache.items():
        l = v.get("likes", 0)
        d = v.get("dislikes", 0)
        dur = v.get("dur", 0)
        sz = v.get("sz", 0)

        if l > 0 or d > 0:
            vids.append({"key": k, "likes": l, "dislikes": d, "dur": dur, "sz": sz})

        if dur > 0:
            if dur < 180:
                dur_short += l
            elif dur < 420:
                dur_medium += l
            else:
                dur_long += l

    top_liked = sorted(vids, key=lambda x: x["likes"], reverse=True)[:5]
    top_disliked = sorted(vids, key=lambda x: x["dislikes"], reverse=True)[:5]

    total_reactions = total_likes + total_dislikes
    sentiment_pct = round((total_likes / total_reactions) * 100, 1) if total_reactions > 0 else 0.0

    return {
        "total_tracked": len(_video_reactions_cache),
        "total_likes": total_likes,
        "total_dislikes": total_dislikes,
        "sentiment_pct": sentiment_pct,
        "top_liked": top_liked,
        "top_disliked": top_disliked,
        "duration_prefs": {
            "short": dur_short,
            "medium": dur_medium,
            "long": dur_long,
        },
    }


def get_user_full_profile(user_id: int) -> Optional[Dict[str, Any]]:
    """Return comprehensive profile and behaviour for a specific user. Zero I/O."""
    u = _cache.get(user_id)
    if not u:
        return None

    user_bots = [bname for bname, uids in _bot_reg.items() if user_id in uids]
    likes_given = sum(1 for (uid, _), r in _user_reactions_cache.items() if uid == user_id and r == "like")
    dislikes_given = sum(1 for (uid, _), r in _user_reactions_cache.items() if uid == user_id and r == "dislike")

    return {
        "user_id": user_id,
        "first_name": u.get("first_name", ""),
        "username": u.get("username"),
        "first_seen": u.get("first_seen"),
        "last_active": u.get("last_active"),
        "total_delivered": u.get("total_delivered", 0),
        "daily_count": u.get("daily_count", 0),
        "daily_date": u.get("daily_date", ""),
        "last_milestone": u.get("last_milestone", 0),
        "seen_count": len(u.get("seen_videos", set())),
        "bots": user_bots,
        "likes_given": likes_given,
        "dislikes_given": dislikes_given,
    }


# ── Video Metadata & Reactions ────────────────────────────────────────────────

async def get_or_fetch_video_meta(
    channel_id: int, message_id: int, bot=None
) -> tuple[int, int, str]:
    """
    Return (duration_seconds, file_size_bytes, video_key).
    Checks cache -> checks DB -> fetches from Telegram channel via bot if needed.
    video_key is f"{duration}_{file_size}" so identical length/size videos share
    the exact same likes/dislikes across all bots.
    """
    cache_key = (channel_id, message_id)
    if cache_key in _video_meta_cache:
        m = _video_meta_cache[cache_key]
        return m["dur"], m["sz"], m["key"]

    dur, sz = 0, 0
    if bot:
        try:
            fwd = await bot.forward_message(
                chat_id=channel_id,
                from_chat_id=channel_id,
                message_id=message_id,
                disable_notification=True,
            )
            vid = fwd.video or fwd.document
            dur = getattr(vid, "duration", 0) or 0
            sz  = getattr(vid, "file_size", 0) or 0
            if not isinstance(dur, (int, float)):
                dur = 0
            else:
                dur = int(dur)
            if not isinstance(sz, (int, float)):
                sz = 0
            else:
                sz = int(sz)
            await bot.delete_message(chat_id=channel_id, message_id=fwd.message_id)
        except Exception:
            dur, sz = 0, 0

    # Create cross-bot key based on video length and size
    if dur > 0 and sz > 0:
        video_key = f"{dur}_{sz}"
    else:
        video_key = f"vid_{message_id}"

    _video_meta_cache[cache_key] = {"dur": dur, "sz": sz, "key": video_key}

    # Ensure reaction entry exists in cache
    if video_key not in _video_reactions_cache:
        _video_reactions_cache[video_key] = {
            "likes":    0,
            "dislikes": 0,
            "dur":      dur,
            "sz":       sz,
        }

    # Persist metadata to DB
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                """
                INSERT INTO video_metadata (video_channel_id, message_id, duration, file_size, video_key)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(video_channel_id, message_id) DO UPDATE SET
                    duration = excluded.duration,
                    file_size = excluded.file_size,
                    video_key = excluded.video_key
                """,
                (channel_id, message_id, dur, sz, video_key),
            )
            await db.commit()
    except Exception as e:
        print(f"[FreeDB] save video_metadata error: {e}")

    return dur, sz, video_key


def get_video_reactions(video_key: str) -> tuple[int, int]:
    """Return (likes_count, dislikes_count) for video_key. Zero I/O."""
    stats = _video_reactions_cache.get(video_key)
    if not stats:
        return 0, 0
    return stats.get("likes", 0), stats.get("dislikes", 0)


def get_user_reaction(user_id: int, video_key: str) -> Optional[str]:
    """Return user's reaction ('like' or 'dislike') or None. Zero I/O."""
    return _user_reactions_cache.get((user_id, video_key))


async def toggle_reaction(
    user_id: int,
    video_key: str,
    reaction: str,  # 'like' or 'dislike'
    duration: int = 0,
    file_size: int = 0,
) -> tuple[int, int, str]:
    """
    Toggle or switch reaction for user_id on video_key.
    Returns (likes_count, dislikes_count, status_action)
    status_action is: 'liked', 'disliked', or 'removed'.
    """
    curr = _user_reactions_cache.get((user_id, video_key))
    stats = _video_reactions_cache.setdefault(
        video_key,
        {"likes": 0, "dislikes": 0, "dur": duration, "sz": file_size},
    )
    if duration > 0:
        stats["dur"] = duration
    if file_size > 0:
        stats["sz"] = file_size

    if curr == reaction:
        # Same reaction clicked again -> toggle off
        if reaction == "like":
            stats["likes"] = max(0, stats["likes"] - 1)
        else:
            stats["dislikes"] = max(0, stats["dislikes"] - 1)
        _user_reactions_cache.pop((user_id, video_key), None)
        action = "removed"
        db_reaction = None
    elif curr is not None:
        # Switching from like to dislike or vice versa
        if reaction == "like":
            stats["likes"] += 1
            stats["dislikes"] = max(0, stats["dislikes"] - 1)
        else:
            stats["dislikes"] += 1
            stats["likes"] = max(0, stats["likes"] - 1)
        _user_reactions_cache[(user_id, video_key)] = reaction
        action = "liked" if reaction == "like" else "disliked"
        db_reaction = reaction
    else:
        # First-time reaction
        if reaction == "like":
            stats["likes"] += 1
        else:
            stats["dislikes"] += 1
        _user_reactions_cache[(user_id, video_key)] = reaction
        action = "liked" if reaction == "like" else "disliked"
        db_reaction = reaction

    # Persist immediately to shared DB
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                """
                INSERT INTO video_reactions (video_key, duration, file_size, likes_count, dislikes_count, last_updated)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(video_key) DO UPDATE SET
                    likes_count = excluded.likes_count,
                    dislikes_count = excluded.dislikes_count,
                    last_updated = excluded.last_updated
                """,
                (video_key, stats["dur"], stats["sz"], stats["likes"], stats["dislikes"]),
            )
            if db_reaction is None:
                await db.execute(
                    "DELETE FROM user_video_reactions WHERE user_id = ? AND video_key = ?",
                    (user_id, video_key),
                )
            else:
                await db.execute(
                    """
                    INSERT INTO user_video_reactions (user_id, video_key, reaction, updated_at)
                    VALUES (?, ?, ?, datetime('now'))
                    ON CONFLICT(user_id, video_key) DO UPDATE SET
                        reaction = excluded.reaction,
                        updated_at = excluded.updated_at
                    """,
                    (user_id, video_key, db_reaction),
                )
            await db.commit()
    except Exception as e:
        print(f"[FreeDB] toggle_reaction persist error: {e}")

    return stats["likes"], stats["dislikes"], action

