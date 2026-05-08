import os
import random
import json
import subprocess
import sys
import logging
import time as _time
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, ChatMemberHandler, filters,
    ConversationHandler
)
from telegram.request import HTTPXRequest
import aiohttp
import hashlib
import asyncio
import aiosqlite

# Import persistent user state storage (SQLite-backed)
from user_state_storage import (
    load_user_states,
    save_user_states,
    init_db,
    start_flush_loop,
    stop_flush_loop,
    close_persistent_db,
    init_user_state_data,
    cleanup_expired_data,
    get_stats
)

# Import shared sync (cross-bot premium + download sharing)
from premium_sync import (
    init_premium_sync,
    is_premium_synced,
    grant_premium_synced,
    grant_premium_until_synced,
    get_premium_until_synced,
    revoke_premium_synced,
    invalidate_cache as _invalidate_premium_cache,
    # Download sync
    get_synced_download_count,
    increment_synced_download,
    reset_synced_downloads,
    get_total_synced_downloads_today,
    # Bot tokens sync
    register_bot_token,
)

# Import Razorpay payment integration
from razorpay_payment import (
    create_payment_link,
    create_qr_code_for_plan,
    store_qr_message_id,
    store_limit_message_id,
    start_webhook_server,
    stop_webhook_server,
    get_recent_payments,
    PLANS as RZP_PLANS,
    RZP_HOURS,
    RZP_AMOUNT,
)

# Load environment variables
load_dotenv()

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,          # DEBUG captures INFO + WARNING + ERROR + CRITICAL
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger("video69_bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("aiosqlite").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

BOT_TOKEN = os.getenv('BOT_TOKEN')
CHANNEL_USERNAME = os.getenv('CHANNEL_USERNAME')
CHANNEL_ID = int(os.getenv('CHANNEL_ID', '-1003067861005'))  # Private channel ID from .env

# Use channel ID for private channels, username for public channels
CHANNEL_IDENTIFIER = CHANNEL_ID if CHANNEL_ID else f"@{CHANNEL_USERNAME}"

# Admin configuration for support system
ADMIN_USER_ID = 7708376300  # Admin's Telegram user ID (https://t.me/Deep12048)

# Configure request — pool_size matches MAX_CONCURRENT_REQUESTS (16)
# so no coroutine ever blocks waiting for a free connection.
request = HTTPXRequest(
    connection_pool_size=16,  # must be >= MAX_CONCURRENT_REQUESTS to avoid pool starvation
    connect_timeout=15.0,
    read_timeout=15.0,
    write_timeout=15.0,
    pool_timeout=5.0
)

# 16 concurrent handlers — safe for t4g.medium (2 ARM vCPU, 4 GB RAM);
# async coroutines are lightweight (~50 KB each), so 16 adds ~800 KB overhead max.
# Telegram still bottlenecks at ~30 msg/sec per token; 16 slots ensures
# no handler waits in the semaphore queue during burst traffic.
MAX_CONCURRENT_REQUESTS = 16
request_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

# ── Admin broadcast state ─────────────────────────────────────────────────────
# Stores per-broadcast message IDs so EACH broadcast can be deleted independently.
# Format: {broadcast_id: {'ids': {user_id: message_id}, 'label': str, 'sent': int}}
# Capped at _MAX_BROADCAST_HISTORY entries; oldest removed when cap is hit.
_broadcast_history: dict[str, dict] = {}
_MAX_BROADCAST_HISTORY = 20          # keep last 20 broadcasts in memory
# ConversationHandler state for /broadcast flow
_BROADCAST_WAITING = 1

# ── /stats cooldown ──
# Prevents the bot from sending duplicate stats messages when /stats is
# pressed many times in rapid succession. First call goes through; any
# subsequent call within _STATS_COOLDOWN seconds is silently dropped.
_stats_lock    = asyncio.Lock()
_stats_last_ts: float = 0.0
_STATS_COOLDOWN = 5.0  # seconds


# Load user states from persistent storage (replaces in-memory dictionary)
# Structure: {user_id: {'seen_videos': [], 'hourly_count': 0, 'last_reset': datetime, 'premium_until': datetime}}
user_states = load_user_states()

# Track used tokens to prevent link reuse (bounded to prevent unbounded memory growth)
used_tokens: set = set()
_MAX_USED_TOKENS = 10000  # Evict oldest when this limit is reached

# Per-user cooldown: user_id → timestamp of last video served (1 video per 2s)
import time as _time
_next_video_cooldown: dict[int, float] = {}
_NEXT_VIDEO_COOLDOWN_SECONDS = 2

# Video limit for free users (resets every 24 hours)
DAILY_VIDEO_LIMIT = 1  # Free users can watch 1 video per day
# Premium access duration after watching ad (12 hours)
PREMIUM_DURATION_HOURS = 12
# Number of videos that can be downloaded/saved by normal users (every 24 hours)
DOWNLOADABLE_VIDEOS_LIMIT = 1
# Number of videos that can be downloaded by premium users (every 24 hours)
PREMIUM_DOWNLOADABLE_VIDEOS_LIMIT = 20

# AroLinks URL Shortener API Configuration
AROLINK_API_TOKEN = "cf0c76a4739d65e08d5990b0db1865b680f1de4a"
AROLINK_API_URL = "https://arolinks.com/api"
# VPLink URL Shortener API Configuration
VPLINK_API_TOKEN = "686ddc23e543449e56029d3de0a59e19677617de"
VPLINK_API_URL = "https://vplink.in/api"
# Bot username for deep links (from .env)
BOT_USERNAME = os.getenv('BOT_USERNAME', 'Test_videos69_bot')
# Secret key for encrypting verification tokens — load from .env
SECRET_KEY = os.getenv('SECRET_KEY', 'change-this-in-your-dotenv-file')

# Message ID range configuration (from .env)
MIN_MESSAGE_ID = 1
MAX_MESSAGE_ID = int(os.getenv('MAX_MESSAGE_ID', '10'))  # Maximum message ID in your channel

# Pre-built frozenset of all video IDs — computed ONCE at startup.
_ALL_VIDEO_IDS: frozenset = frozenset(range(MIN_MESSAGE_ID, MAX_MESSAGE_ID + 1))
_TOTAL_VIDEOS: int = len(_ALL_VIDEO_IDS)          # cached length, avoids len() call per request
_FAST_PATH_THRESHOLD: int = int(_TOTAL_VIDEOS * 0.8)  # integer, avoids float multiply per request

# Module-level IST offset — avoids re-creating timedelta on EVERY request
_IST_OFFSET = timedelta(hours=5, minutes=30)

# One-second resolution datetime.now() cache — avoids repeated syscalls per request
_now_cache: list = [None, 0.0]  # [cached_dt, cached_at_monotonic]

def _now_utc() -> datetime:
    """Return datetime.now(timezone.utc) cached at 1-second resolution.
    Eliminates 3-5 syscalls per request replaced by 1 per second globally.
    """
    mono = _time.monotonic()
    if mono - _now_cache[1] >= 1.0:
        _now_cache[0] = datetime.now(timezone.utc)
        _now_cache[1] = mono
    return _now_cache[0]

# Mandatory channels — loaded from .env (change without touching code)
MANDATORY_CHANNEL        = int(os.getenv('MANDATORY_CHANNEL_1_ID',   '-1003827947228'))
MANDATORY_CHANNEL_LINK   = os.getenv('MANDATORY_CHANNEL_1_LINK', 'https://t.me/+8MA7MsUSTD0zZDMx')
MANDATORY_CHANNEL_2      = int(os.getenv('MANDATORY_CHANNEL_2_ID',   '-1003747307292'))
MANDATORY_CHANNEL_2_LINK = os.getenv('MANDATORY_CHANNEL_2_LINK', 'https://t.me/+twTah7XKdRUxNGVl')

# ── Bot users registry (persisted in user_states.db, 'bot_users' table) ──────
# All reads come from _users_cache (instant, no I/O).
# Writes mark the user dirty; the shared flush loop in user_state_storage.py
# Writes mark the user dirty; _bot_users_flush_loop writes to SQLite every 30 seconds.
OLD_USERS_JSON = "users.json"      # migrated automatically if still present

_users_cache: dict[int, dict] = {}  # {user_id: {first_name, username, ...}}
_users_dirty: set[int] = set()      # user_ids needing a DB write


# ── Persistent pending-deletion registry ─────────────────────────────────────
# Stores video messages that must be deleted after 20 min.
# Survives bot restarts: on startup, overdue entries are deleted immediately
# and still-fresh entries are rescheduled for their remaining window.
# Table lives in user_states.db for simplicity (single DB open connection).

async def _init_pending_deletions_table(db):
    """Create pending_deletions table if it doesn't exist."""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS pending_deletions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id     INTEGER NOT NULL,
            message_id  INTEGER NOT NULL,
            delete_at   REAL    NOT NULL   -- Unix timestamp (UTC)
        )
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_pending_deletions_chat
            ON pending_deletions(chat_id)
    """)
    await db.commit()


async def _insert_pending_deletion(chat_id: int, message_id: int, delete_at: float):
    """Persist a scheduled deletion to the DB."""
    try:
        async with aiosqlite.connect("user_states.db") as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute(
                "INSERT INTO pending_deletions (chat_id, message_id, delete_at) VALUES (?, ?, ?)",
                (chat_id, message_id, delete_at)
            )
            await db.commit()
    except Exception as e:
        print(f"⚠️ [pending_deletions] insert error: {e}")


async def _remove_pending_deletion(chat_id: int, message_id: int):
    """Remove a deletion record once the message has been deleted."""
    try:
        async with aiosqlite.connect("user_states.db") as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute(
                "DELETE FROM pending_deletions WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id)
            )
            await db.commit()
    except Exception as e:
        print(f"⚠️ [pending_deletions] remove error: {e}")


async def _get_all_pending_deletions() -> list[tuple]:
    """Return all (id, chat_id, message_id, delete_at) rows."""
    rows = []
    try:
        async with aiosqlite.connect("user_states.db") as db:
            async with db.execute(
                "SELECT id, chat_id, message_id, delete_at FROM pending_deletions"
            ) as cur:
                rows = await cur.fetchall()
    except Exception as e:
        print(f"⚠️ [pending_deletions] fetch error: {e}")
    return rows


async def _delete_all_pending_deletions_db():
    """Remove ALL rows from pending_deletions (used after bulk cleanup)."""
    try:
        async with aiosqlite.connect("user_states.db") as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("DELETE FROM pending_deletions")
            await db.commit()
    except Exception as e:
        print(f"⚠️ [pending_deletions] delete-all error: {e}")

# ── Persistent Video Ratings ─────────────────────────────────────────────────
async def _init_ratings_table(db):
    """Create video_ratings and user_votes tables."""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS video_ratings (
            message_id INTEGER PRIMARY KEY,
            likes INTEGER NOT NULL DEFAULT 0,
            dislikes INTEGER NOT NULL DEFAULT 0
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS user_votes (
            user_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            vote_type TEXT NOT NULL,
            PRIMARY KEY (user_id, message_id)
        )
    """)
    await db.commit()

async def _get_video_rating_buttons(message_id: int) -> list[InlineKeyboardButton]:
    """Returns a list with Like, Percentage, and Dislike buttons."""
    likes = 0
    dislikes = 0
    try:
        async with aiosqlite.connect("user_states.db") as db:
            async with db.execute("SELECT likes, dislikes FROM video_ratings WHERE message_id = ?", (message_id,)) as cur:
                row = await cur.fetchone()
                if row:
                    likes, dislikes = row[0], row[1]
    except Exception as e:
        print(f"⚠️ [ratings] fetch error: {e}")

    total = likes + dislikes
    if total > 0:
        pct = int((likes / total) * 100)
        pct_text = f"{pct}%"
    else:
        pct_text = "0%"

    return [
        InlineKeyboardButton("👍 Like", callback_data=f"vote_like_{message_id}"),
        InlineKeyboardButton(pct_text, callback_data=f"vote_none_{message_id}"),
        InlineKeyboardButton("👎 Dislike", callback_data=f"vote_dislike_{message_id}")
    ]

async def _record_video_vote(user_id: int, message_id: int, vote_type: str) -> tuple[bool, str]:
    """Records a vote (like or dislike) and returns (changed, message)."""
    try:
        async with aiosqlite.connect("user_states.db") as db:
            await db.execute("PRAGMA journal_mode=WAL")
            
            # Check existing vote
            async with db.execute("SELECT vote_type FROM user_votes WHERE user_id = ? AND message_id = ?", (user_id, message_id)) as cur:
                existing = await cur.fetchone()
                
            if existing:
                old_vote = existing[0]
                if old_vote == vote_type:
                    return False, "You already voted this!"
                
                # Switch vote
                await db.execute("UPDATE user_votes SET vote_type = ? WHERE user_id = ? AND message_id = ?", (vote_type, user_id, message_id))
                
                if vote_type == 'like':
                    await db.execute("UPDATE video_ratings SET likes = likes + 1, dislikes = MAX(0, dislikes - 1) WHERE message_id = ?", (message_id,))
                else:
                    await db.execute("UPDATE video_ratings SET dislikes = dislikes + 1, likes = MAX(0, likes - 1) WHERE message_id = ?", (message_id,))
                
                await db.commit()
                return True, "Vote changed!"
            else:
                # New vote
                await db.execute("INSERT INTO user_votes (user_id, message_id, vote_type) VALUES (?, ?, ?)", (user_id, message_id, vote_type))
                await db.execute("INSERT OR IGNORE INTO video_ratings (message_id, likes, dislikes) VALUES (?, 0, 0)", (message_id,))
                
                if vote_type == 'like':
                    await db.execute("UPDATE video_ratings SET likes = likes + 1 WHERE message_id = ?", (message_id,))
                else:
                    await db.execute("UPDATE video_ratings SET dislikes = dislikes + 1 WHERE message_id = ?", (message_id,))
            
                await db.commit()
                return True, "Vote registered!"
    except Exception as e:
        print(f"⚠️ [ratings] Record error: {e}")
        return False, "Database error."


# ── Called once from init_db() via post_init ────────────────────────
async def _init_users_table(db):
    """Create bot_users table and indexes (called inside init_db)."""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS bot_users (
            user_id     INTEGER PRIMARY KEY,
            first_name  TEXT    NOT NULL DEFAULT '',
            username    TEXT,
            joined_date TEXT    NOT NULL DEFAULT (datetime('now')),
            last_seen   TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)
    # Index to speed up "get all user_ids" for broadcasts
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_bot_users_joined
            ON bot_users(joined_date)
    """)
    await db.commit()


async def _load_users_from_db(db):
    """Load all rows from bot_users into _users_cache."""
    db.row_factory = aiosqlite.Row
    async with db.execute("SELECT * FROM bot_users") as cur:
        async for row in cur:
            uid = row["user_id"]
            _users_cache[uid] = {
                "user_id":     uid,
                "first_name":  row["first_name"],
                "username":    row["username"],
                "joined_date": row["joined_date"],
                "last_seen":   row["last_seen"],
            }


async def _migrate_users_json(db):
    """One-time migration from users.json → bot_users table."""
    try:
        with open(OLD_USERS_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        for u in data:
            uid = u.get("user_id")
            if not uid:
                continue
            _users_cache[uid] = {
                "user_id":     uid,
                "first_name":  u.get("first_name", ""),
                "username":    u.get("username"),
                "joined_date": u.get("joined_date", _now_utc().isoformat()),
                "last_seen":   u.get("last_seen",   _now_utc().isoformat()),
            }
            await db.execute("""
                INSERT OR IGNORE INTO bot_users
                    (user_id, first_name, username, joined_date, last_seen)
                VALUES (?, ?, ?, ?, ?)
            """, (uid,
                  _users_cache[uid]["first_name"],
                  _users_cache[uid]["username"],
                  _users_cache[uid]["joined_date"],
                  _users_cache[uid]["last_seen"]))
        await db.commit()
        os.rename(OLD_USERS_JSON, OLD_USERS_JSON + ".migrated")
        print(f"✅ Migrated {len(data)} users from users.json → SQLite")
    except Exception as e:
        print(f"⚠️ users.json migration error: {e}")


_users_persistent_db = None   # reused connection for bot_users flushes

async def _flush_users_to_db():
    """Write dirty users to bot_users table — reuses a persistent connection."""
    global _users_persistent_db
    if not _users_dirty:
        return
    to_save = list(_users_dirty)
    _users_dirty.clear()
    try:
        if _users_persistent_db is None:
            _users_persistent_db = await aiosqlite.connect("user_states.db")
            await _users_persistent_db.execute("PRAGMA journal_mode=WAL")
            await _users_persistent_db.execute("PRAGMA synchronous=NORMAL")
        for uid in to_save:
            if uid not in _users_cache:
                continue
            u = _users_cache[uid]
            await _users_persistent_db.execute("""
                INSERT INTO bot_users
                    (user_id, first_name, username, joined_date, last_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    first_name  = excluded.first_name,
                    username    = excluded.username,
                    last_seen   = excluded.last_seen
            """, (uid, u["first_name"], u["username"],
                  u["joined_date"], u["last_seen"]))
        await _users_persistent_db.commit()
    except Exception as e:
        _users_dirty.update(to_save)
        try:
            if _users_persistent_db:
                await _users_persistent_db.close()
        except Exception:
            pass
        _users_persistent_db = None

# ── Bot-users background flush loop (mirrors user_state_storage flush loop) ──────
_bot_users_flush_task = None

async def _bot_users_flush_loop():
    """Flush dirty bot_users to SQLite every 30 seconds."""
    while True:
        try:
            await asyncio.sleep(30)
            await _flush_users_to_db()
        except asyncio.CancelledError:
            await _flush_users_to_db()   # final forced flush on shutdown
            break
        except Exception as e:
            print(f"⚠️ bot_users flush error: {e}")


# ── Public functions (same signatures as before) ─────────────────────────────


def add_user_to_file(user_id: int, first_name: str, username=None) -> bool:
    """Add or update a user in cache + mark dirty for the flush loop.
    Returns instantly — no I/O, no task creation.
    """
    if user_id in _users_cache:
        u = _users_cache[user_id]
        # Only dirty-mark when something actually changed (avoids pointless writes)
        if u["first_name"] != first_name or u.get("username") != username:
            u["first_name"] = first_name
            u["username"]   = username
            u["last_seen"]  = _now_utc().isoformat()  # reuse cached now
            _users_dirty.add(user_id)
        return False  # Existing user — no task needed

    # New user
    now_iso = _now_utc().isoformat()  # reuse cached now — no extra syscall
    _users_cache[user_id] = {
        "user_id":     user_id,
        "first_name":  first_name,
        "username":    username,
        "joined_date": now_iso,
        "last_seen":   now_iso,
    }
    _users_dirty.add(user_id)
    # Only print for genuinely new users (not on every returning user)
    print(f"✅ New user registered: {first_name} (ID: {user_id})")
    return True


def get_all_user_ids() -> list[int]:
    """Return all user IDs instantly from cache (O(1))."""
    return list(_users_cache.keys())


def get_user_count() -> int:
    """Return total user count instantly from cache."""
    return len(_users_cache)


def batch_remove_users(user_ids_to_remove):
    """Remove users from cache and schedule DB delete."""
    if not user_ids_to_remove:
        return
    remove_set = set(user_ids_to_remove)
    for uid in remove_set:
        _users_cache.pop(uid, None)
        _users_dirty.discard(uid)

    async def _delete_from_db():
        _opened_own = False
        db = _users_persistent_db
        try:
            if db is None:
                db = await aiosqlite.connect("user_states.db")
                await db.execute("PRAGMA journal_mode=WAL")
                _opened_own = True
            await db.executemany(
                "DELETE FROM bot_users WHERE user_id = ?",
                [(uid,) for uid in remove_set]
            )
            await db.commit()
            print(f"🗑️ Removed {len(remove_set)} blocked users from DB")
        except Exception as e:
            print(f"⚠️ Error removing users from DB: {e}")
        finally:
            if _opened_own and db:
                await db.close()

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_delete_from_db())
    except RuntimeError:
        pass


# ── Membership cache: stores per-channel status ───────────────────────────────
# Format: {user_id: (r1: bool, r2: bool, expires_at: float)}
_membership_cache: dict[int, tuple[bool, bool, float]] = {}
MEMBERSHIP_CACHE_TTL = 300  # 5 minutes — proactively invalidated by ChatMemberHandler on leave


def _build_join_keyboard(r1: bool, r2: bool) -> InlineKeyboardMarkup:
    """Build a join keyboard showing ONLY the channels the user hasn't joined yet."""
    buttons = []
    if not r1:
        buttons.append([InlineKeyboardButton("📢 Join Channel 1", url=MANDATORY_CHANNEL_LINK)])
    if not r2:
        buttons.append([InlineKeyboardButton("📢 Join Channel 2", url=MANDATORY_CHANNEL_2_LINK)])
    buttons.append([InlineKeyboardButton("🔄 I've Joined — Verify", callback_data='check_membership')])
    return InlineKeyboardMarkup(buttons)


async def check_channel_membership(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> tuple[bool, bool, bool]:
    """Check if user is a member of BOTH mandatory channels.
    Returns (all_joined, joined_ch1, joined_ch2).
    Both checks run in parallel. Result cached for 30 seconds.

    IMPORTANT: The bot MUST be an admin in both mandatory channels.
    Without admin rights, get_chat_member will fail and return False (fail-closed).
    """
    now = _time.monotonic()

    # Return cached result if still fresh
    cached = _membership_cache.get(user_id)
    if cached is not None:
        r1, r2, expires_at = cached
        if now < expires_at:
            return r1 and r2, r1, r2

    # Evict stale entries if cache grows too large
    if len(_membership_cache) > 50000:
        cutoff = now - MEMBERSHIP_CACHE_TTL
        stale = [k for k, (_, __, exp) in _membership_cache.items() if exp < cutoff]
        for k in stale:
            del _membership_cache[k]

    async def _check_one(channel_id: int) -> bool:
        """Fail-CLOSED: returns False on any error.
        Bot MUST be admin in the channel for this to work.
        """
        try:
            member = await context.bot.get_chat_member(chat_id=channel_id, user_id=user_id)
            result = member.status not in ('left', 'kicked', 'banned')
            print(f"🔍 Membership check: user={user_id} channel={channel_id} status='{member.status}' → {'✅ member' if result else '❌ not member'}")
            return result
        except Exception as e:
            err = str(e).lower()
            # Log a clear warning if the bot is not admin (most common misconfiguration)
            if any(kw in err for kw in ('forbidden', 'not enough rights', 'not a member',
                                        'have no rights', 'bot was kicked', 'chat not found')):
                print(f"⚠️  ADMIN REQUIRED: Bot is not admin in channel {channel_id}. "
                      f"Add the bot as admin to enforce membership! Error: {e}")
            else:
                print(f"⚠️  Membership check error for channel {channel_id}: {e}")
            return False   # FAIL CLOSED — block user when check fails

    r1, r2 = await asyncio.gather(
        _check_one(MANDATORY_CHANNEL),
        _check_one(MANDATORY_CHANNEL_2),
    )
    _membership_cache[user_id] = (r1, r2, now + MEMBERSHIP_CACHE_TTL)
    return r1 and r2, r1, r2


# Use ReplyKeyboardRemove to clear the old cached keyboards
_REMOVE_KEYBOARD = ReplyKeyboardRemove()


async def load_videos_from_channel(application: Application):
    """Initialize bot - no video loading needed!"""
    
    try:
        print(f"📡 Connecting to channel...")
        
        # Get channel chat
        chat = await application.bot.get_chat(CHANNEL_IDENTIFIER)
        print(f"✅ Connected to channel: {chat.title}")
        print(f"📋 Channel ID: {chat.id}")
        
        print("\n✅ Bot ready! Videos will be sent randomly from the channel.")
        print("💡 No pre-loading needed - instant startup!\n")
        
        # We don't load videos anymore - we'll generate random message IDs
        # and try to send them. If they don't exist or aren't videos, we'll try another.
        
    except Exception as e:
        print(f"❌ Error connecting to channel: {e}")
        print("⚠️ Make sure the bot is an admin in the channel.")



# Channel post handler removed - bot only uses predefined videos


def get_random_video(user_id: int) -> int:
    """Return a random unseen video message ID for the user.

    Strategy:
    - FAST PATH (O(1) average): if user has seen <80% of videos, just pick
      random IDs from the pre-built list and check against in-memory sets.
      Statistically finds an unseen video in 1-3 tries for most users.
    - SLOW PATH (O(n) fallback): when user is near the end of the catalog,
      compute the exact set difference and pick from remaining videos.
    """
    if user_id not in user_states:
        init_user_state(user_id)

    user_state  = user_states[user_id]
    seen        = user_state.get('seen_videos', set())        # already a set
    failed      = user_state.get('failed_message_ids', set()) # already a set
    seen_count  = len(seen)

    # ── FAST PATH: probabilistic sampling ──────────────────────────────────
    if seen_count < _FAST_PATH_THRESHOLD:  # integer compare, no float multiply
        for _ in range(50):
            vid = random.randrange(MIN_MESSAGE_ID, MAX_MESSAGE_ID + 1)  # O(1)
            if vid not in seen and vid not in failed:  # O(1) set lookup
                return vid
        # Fell through 50 tries (very unlikely) — fall to slow path

    # ── SLOW PATH: exact set difference (only when near end of catalog) ───
    unseen = _ALL_VIDEO_IDS - seen - failed
    if not unseen:
        seen.clear()                     # reset without reallocating the set
        unseen = _ALL_VIDEO_IDS - failed
        if not unseen:
            failed.clear()
            unseen = _ALL_VIDEO_IDS

    return random.choice(list(unseen))




def mark_video_as_seen(user_id: int, video_id: int):
    """Mark a video as seen. seen_videos is a set — .add() is O(1)."""
    if user_id not in user_states:
        init_user_state(user_id)
    user_states[user_id]['seen_videos'].add(video_id)   # set.add: O(1), no dup check
    save_user_states(user_states, _hint_uid=user_id)


def init_user_state(user_id):
    """Initialize user state with default values if not present."""
    if user_id not in user_states:
        init_user_state_data(user_id, user_states)


def is_premium_user(user_id: int) -> bool:
    """Return True if user has active premium (local state only — fast sync path).
    Also returns True on the May 1 2026 free-premium day.
    The shared DB check is done asynchronously via is_premium_user_async().
    This sync version is kept for backward compatibility with non-async callers.
    """
    # May 1, 2026 = free premium day for everyone (IST)
    if _is_may1_free_day():
        return True
    if user_id not in user_states:
        init_user_state(user_id)
    premium_until = user_states[user_id].get('premium_until')
    return bool(premium_until) and _now_utc() < premium_until


def _is_may1_free_day() -> bool:
    """Return True if today (IST) is May 1, 2026 — the free-premium promotion day."""
    now_ist = _now_utc() + _IST_OFFSET
    return now_ist.year == 2026 and now_ist.month == 5 and now_ist.day == 1


async def is_premium_user_async(user_id: int) -> bool:
    """Check premium against BOTH local state AND the shared cross-bot DB.
    If either says the user is premium, they are treated as premium.
    Also returns True on the May 1 2026 free-premium promotion day.
    Shared DB check uses a 60-second cache — no DB hit on every request.
    """
    # May 1 free-premium day — everyone is premium, no DB needed
    if _is_may1_free_day():
        return True
        
    # Cross-bot shared check (cached, ≤1 DB read per user per 60 s)
    shared = await is_premium_synced(user_id)
    if shared:
        # Sync the shared premium status into local state so this bot
        # stays consistent without hitting the shared DB on every call.
        from premium_sync import get_premium_until_synced as _gpu
        puntil = await _gpu(user_id)
        if puntil:
            if user_id not in user_states:
                init_user_state(user_id)
            user_states[user_id]['premium_until'] = puntil
            save_user_states(user_states, _hint_uid=user_id)
        return True

    # If shared is False, it could mean premium expired, was revoked, or DB is offline.
    from premium_sync import _db_ready
    if _db_ready:
        # DB is online but user is not premium. Clear local state if it exists.
        if user_id in user_states and user_states[user_id].get('premium_until'):
            user_states[user_id]['premium_until'] = None
            save_user_states(user_states, _hint_uid=user_id)
        return False
    else:
        # DB is offline, fallback to fast local check
        return is_premium_user(user_id)


def check_and_reset_daily_limit(user_id: int) -> bool:
    """Check if daily limit needs reset (at midnight IST). Uses _IST_OFFSET constant."""
    if user_id not in user_states:
        init_user_state(user_id)
    user_state = user_states[user_id]
    now = _now_utc()
    now_ist = now + _IST_OFFSET
    current_day_start_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    current_day_start = current_day_start_ist - _IST_OFFSET
    if current_day_start > user_state['last_reset']:
        user_state['hourly_count'] = 0
        user_state['downloaded_count'] = 0
        user_state['last_reset'] = current_day_start
        save_user_states(user_states, _hint_uid=user_id)
        return True
    return False



def increment_video_count(user_id: int):
    """Increment user's daily video count."""
    if user_id not in user_states:
        init_user_state(user_id)
    user_states[user_id]['hourly_count'] += 1
    save_user_states(user_states, _hint_uid=user_id)


def utc_to_ist(utc_time: datetime) -> datetime:
    """Convert UTC datetime to IST. Uses module-level _IST_OFFSET constant."""
    return utc_time + _IST_OFFSET


def grant_premium_access(user_id: int):
    """Grant premium access for 12 hours after watching ad (local state).
    Use grant_premium_access_async() to also sync to the shared cross-bot DB.
    """
    if user_id not in user_states:
        init_user_state(user_id)
    user_states[user_id]['premium_until'] = (
        _now_utc() + timedelta(hours=PREMIUM_DURATION_HOURS)
    )
    # Invalidate membership cache so next check re-evaluates premium state
    _membership_cache.pop(user_id, None)
    save_user_states(user_states, _hint_uid=user_id)


async def grant_premium_access_async(user_id: int):
    """Grant premium access for 12 hours in BOTH local state AND the shared
    cross-bot DB.  Call this instead of grant_premium_access() so that ALL
    bots on the same server see the user as premium immediately.
    """
    # 1. Write to local state (instant)
    grant_premium_access(user_id)
    # 2. Write to shared DB (propagates to all other bots within cache TTL)
    await grant_premium_synced(user_id, hours=PREMIUM_DURATION_HOURS)
    print(f"🔗 [PremiumSync] Premium synced across all bots for user {user_id}")


# ── Persistent aiohttp session (created once, reused for all HTTP requests) ──────
# Creating a new ClientSession per-request is very expensive (TCP handshake each time)
_http_session: aiohttp.ClientSession | None = None

async def _get_http_session() -> aiohttp.ClientSession:
    """Return the shared aiohttp session, creating it if needed."""
    global _http_session
    if _http_session is None or _http_session.closed:
        connector = aiohttp.TCPConnector(limit=4, ttl_dns_cache=300)  # 4 = enough for 1 service
        _http_session = aiohttp.ClientSession(connector=connector)
    return _http_session


async def _shorten_via_arolinks(bot_deep_link: str, alias: str, user_id: int) -> str | None:
    """Try to shorten a link via AroLinks. Returns shortened URL or None."""
    api_url = f"{AROLINK_API_URL}?api={AROLINK_API_TOKEN}&url={bot_deep_link}&alias={alias}"
    try:
        session = await _get_http_session()
        async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=10)) as response:
            if response.status == 200:
                result = await response.json(content_type=None)
                if result.get("status") == "success":
                    shortened_url = result.get("shortenedUrl", "").strip()
                    print(f"✅ [AroLinks] Shortened link for user {user_id}: {shortened_url}")
                    return shortened_url
                else:
                    print(f"❌ [AroLinks] API error: {result.get('message', 'Unknown error')}")
            else:
                print(f"❌ [AroLinks] HTTP {response.status}")
    except Exception as e:
        print(f"❌ [AroLinks] Exception: {e}")
    return None


async def _shorten_via_vplink(bot_deep_link: str, alias: str, user_id: int) -> str | None:
    """Try to shorten a link via VPLink. Returns shortened URL or None."""
    api_url = f"{VPLINK_API_URL}?api={VPLINK_API_TOKEN}&url={bot_deep_link}&alias={alias}_vp"
    try:
        session = await _get_http_session()
        async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=10)) as response:
            if response.status == 200:
                result = await response.json(content_type=None)
                if result.get("status") == "success":
                    shortened_url = result.get("shortenedUrl", "").strip()
                    print(f"✅ [VPLink] Shortened link for user {user_id}: {shortened_url}")
                    return shortened_url
                else:
                    print(f"❌ [VPLink] API error: {result.get('message', 'Unknown error')}")
            else:
                print(f"❌ [VPLink] HTTP {response.status}")
    except Exception as e:
        print(f"❌ [VPLink] Exception: {e}")
    return None


async def create_shortened_links(user_id) -> tuple[str | None, str | None]:
    """Create unique shortened links via BOTH AroLinks and VPLink concurrently.
    Returns (arolinks_url, vplink_url) — either may be None on failure."""
    if user_id not in user_states:
        init_user_state(user_id)

    # Generate a unique verification token
    timestamp = int(_now_utc().timestamp())
    random_part = random.randint(10000, 99999)
    token_data = f"{user_id}_{timestamp}_{random_part}"
    encrypted_token = hashlib.sha256(f"{token_data}_{SECRET_KEY}".encode()).hexdigest()[:16]
    full_token = f"{user_id}_{timestamp}_{encrypted_token}"

    # Store the token in user state for verification
    user_states[user_id]['ad_token'] = full_token
    save_user_states(user_states, _hint_uid=user_id)

    # Create Telegram bot deep link
    bot_deep_link = f"https://t.me/{BOT_USERNAME}?start={full_token}"
    alias = f"v_{user_id}_{timestamp}"

    # Call BOTH shorteners concurrently
    aro_url, vp_url = await asyncio.gather(
        _shorten_via_arolinks(bot_deep_link, alias, user_id),
        _shorten_via_vplink(bot_deep_link, alias, user_id),
    )

    if aro_url:
        user_states[user_id]['ad_link'] = aro_url
        save_user_states(user_states, _hint_uid=user_id)

    if not aro_url and not vp_url:
        print(f"❌ Both shorteners failed for user {user_id}")

    return aro_url, vp_url


# Legacy alias kept for any remaining call sites
async def create_shortened_link(user_id):
    aro_url, _ = await create_shortened_links(user_id)
    return aro_url



async def delete_messages(context: ContextTypes.DEFAULT_TYPE):
    """Delete messages after 20 minutes and remove from persistent registry."""
    job_data = context.job.data
    chat_id = job_data['chat_id']
    message_ids = job_data['message_ids']
    
    # Delete the video and warning messages
    for message_id in message_ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            pass  # Silently ignore — message may already be deleted
        # Remove from the persistent pending-deletions registry
        await _remove_pending_deletion(chat_id, message_id)
    
    # No notification message - keep the chat clean!


async def broadcast_daily_reset(context: ContextTypes.DEFAULT_TYPE):
    """Broadcast message to all users about limit reset - sent every 24 hours at midnight IST.
    On May 1, 2026, sends a special FREE PREMIUM DAY message and grants premium
    to every registered user via the shared DB (so all bots honour it).
    """
    print("\n" + "=" * 60)
    print("🔔 DAILY RESET BROADCAST TRIGGERED!")
    print("=" * 60)

    # Get current time in IST
    current_time_ist = _now_utc() + _IST_OFFSET
    time_str = current_time_ist.strftime('%I:00 %p')   # e.g. "12:00 AM"
    date_str = current_time_ist.strftime('%B %d, %Y')  # e.g. "May 01, 2026"

    print(f"⏰ Broadcasting at: {time_str} IST")
    print(f"📅 Date: {date_str}")

    # ── May 1 free-premium-day branch ────────────────────────────────────────
    is_may1 = (
        current_time_ist.year == 2026 and
        current_time_ist.month == 5 and
        current_time_ist.day == 1
    )

    if is_may1:
        print("🎉 MAY 1 FREE PREMIUM DAY BROADCAST!")

        keyboard = [
            [InlineKeyboardButton("🎬 Watch Unlimited Now!", callback_data='next_video')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        message_text = (
            f"🎉 **Happy May Day! Free Premium for Everyone!** 🎉\n\n"
            f"📅 Date: **{date_str}**\n\n"
            f"👋 Hello! As a special gift for **May Day**, we are giving "
            f"**FREE Premium Access** to ALL users for the entire day!\n\n"
            f"⭐ **What you get TODAY — absolutely FREE:**\n"
            f"• 🔓 **Unlimited videos** all day long\n"
            f"• 📥 **20 downloads** today\n"
            f"• ⏰ Valid till **11:59 PM IST tonight**\n\n"
            f"⚠️ **From May 2 onwards**, regular limits resume.\n"
            f"Watch an ad anytime to unlock premium again!\n\n"
            f"🍿 Enjoy your free premium day! Click below to start watching!"
        )

        # Grant premium valid until 11:59:59 PM IST on May 1 to every registered user.
        # Uses the shared DB so ALL bots on the server honour this grant automatically.
        may1_end_utc = current_time_ist.replace(
            hour=23, minute=59, second=59, microsecond=0
        ) - _IST_OFFSET   # IST 23:59:59 → UTC (18:29:59 UTC)

        user_ids = get_all_user_ids()
        grant_ok = 0
        grant_errors = 0
        for uid in user_ids:
            try:
                await grant_premium_until_synced(uid, may1_end_utc)
                grant_ok += 1
            except Exception as grant_err:
                grant_errors += 1
                if grant_errors <= 3:   # log first 3 errors only
                    print(f"⚠️ May1 grant error uid={uid}: {grant_err}")

        print(f"🎉 May 1 free premium granted to {grant_ok}/{len(user_ids)} users (errors: {grant_errors})")


    else:
        # ── Normal daily-reset message ────────────────────────────────────────
        keyboard = [[InlineKeyboardButton("🎬 Watch Now", callback_data='next_video')]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        message_text = (
            f"🎬 **Daily Limit Reset!** 🎬\n\n"
            f"⏰ Time: **{time_str} IST**\n"
            f"📅 Date: **{date_str}**\n\n"
            f"✨ Your limits have been renewed!\n"
            f"🎥 You can now watch **3 free videos**\n"
            f"📥 You can download **2 videos**\n"
            f"💎 Premium users get **20 downloads**!\n\n"
            f"⏱️ Limits reset every **24 hours at midnight**\n"
            f"💎 Want unlimited videos + 20 downloads? Watch an ad!\n\n"
            f"Click below to start watching!\n\n"
            f"Enjoy! 🍿"
        )

        user_ids = get_all_user_ids()

    # ── Send broadcast to all users ───────────────────────────────────────────
    successful = 0
    failed = 0
    blocked_users = []

    for user_id in user_ids:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=message_text,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
            successful += 1
            await asyncio.sleep(0.05)  # 50 ms — Telegram allows ~30 msg/sec per token
        except Exception as e:
            failed += 1
            if "Forbidden" in str(e) or "blocked" in str(e).lower():
                blocked_users.append(user_id)

    # Remove users who blocked the bot
    if blocked_users:
        batch_remove_users(blocked_users)

    print(f"📢 Broadcast complete! Sent to {successful} users, {failed} failed")
    print(f"⏰ Broadcast time: {time_str} IST")
    print(f"📅 Broadcast date: {date_str}")
    print(f"👥 Total users in database: {get_user_count()}")




async def rate_limited_send_video(update: Update, context: ContextTypes.DEFAULT_TYPE, 
                                   chat_id=None, user_id=None):
    """Rate-limited wrapper for send_random_video to prevent overload"""
    async with request_semaphore:
        try:
            await send_random_video(update, context, chat_id, user_id)
        except Exception:
            # Send user-friendly error message
            try:
                await context.bot.send_message(
                    chat_id=chat_id or update.effective_chat.id,
                    text="⚠️ **Server is busy**\n\n"
                         "Please try again in a moment. We're experiencing high traffic."
                )
            except:
                pass  # Silently fail if we can't even send error message


async def send_random_video(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id=None, user_id=None):
    """Send a random video to the user"""
    if chat_id is None:
        chat_id = update.effective_chat.id
    if user_id is None:
        user_id = update.effective_user.id
    
    try:
        # FIRST: Check if user is still a member of the mandatory channels
        all_joined, r1, r2 = await check_channel_membership(context, user_id)

        if not all_joined:
            missing = ([] if r1 else ['Channel 1']) + ([] if r2 else ['Channel 2'])
            missing_str = ' and '.join(missing)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🔒 **You need to join {missing_str}!**\n\n"
                     f"Please join the required channel(s) below, then click Verify.\n\n"
                     f"After joining, you can watch videos! 🎬",
                parse_mode='Markdown',
                reply_markup=_build_join_keyboard(r1, r2)
            )
            return
        
        # Compute is_premium ONCE — checks local state + shared cross-bot DB
        is_premium = await is_premium_user_async(user_id)
        if not is_premium:
            check_and_reset_daily_limit(user_id)  # reset counter if new day
        can_watch = is_premium or user_states[user_id]['hourly_count'] < DAILY_VIDEO_LIMIT

        if not can_watch:
            # Always show Image 2 (Full plans + Ads) whenever limit is reached
            aro_url, vp_url = await create_shortened_links(user_id)
            keyboard = [[InlineKeyboardButton(lbl, callback_data=f'rzp_plan_{k}')]
                        for k, (lbl, *_) in RZP_PLANS.items()]
            if aro_url:
                keyboard.append([InlineKeyboardButton("🔥 AroLinks — Watch Ad & Get Premium", url=aro_url)])
            if vp_url:
                keyboard.append([InlineKeyboardButton("⚡ VPLink — Watch Ad & Get Premium", url=vp_url)])
            if not aro_url and not vp_url:
                keyboard.append([InlineKeyboardButton("🔥▶️ Watch Ad → Get FREE Premium! 🎁", callback_data='watch_ad')])
            
            reply_markup = InlineKeyboardMarkup(keyboard)

            # Calculate time until next free video reset
            now = _now_utc()
            now_ist = now + _IST_OFFSET
            next_reset_ist = (now_ist + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            next_reset = next_reset_ist - _IST_OFFSET
            time_until_reset = next_reset - now
            minutes_until_reset = int(time_until_reset.total_seconds() / 60)
            hours_until_reset = int(time_until_reset.total_seconds() / 3600)
            
            if hours_until_reset > 0:
                time_msg = f"{hours_until_reset} hour(s) and {minutes_until_reset % 60} minute(s)"
            else:
                time_msg = f"{minutes_until_reset} minute(s)"

            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🚫 **Video Limit Reached!**\n\n"
                     f"You've used your free video. Buy a plan or watch an ad to continue watching ⬇️\n\n"
                     f"⏳ Next free video in: **{time_msg}**",
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
            return
        
        # Try to find a valid video (try up to 10 random message IDs)
        max_attempts = 10
        video_sent = False
        message_id = None  # guard: prevents NameError if get_random_video() raises on attempt 0
        
        for attempt in range(max_attempts):
            try:
                message_id = get_random_video(user_id)

                # is_premium already computed above — reuse it, no extra syscall
                # Read download count from shared DB so all bots see the same number
                downloaded_count = await get_synced_download_count(user_id)
                download_limit = PREMIUM_DOWNLOADABLE_VIDEOS_LIMIT if is_premium else DOWNLOADABLE_VIDEOS_LIMIT
                can_show_download_button = downloaded_count < download_limit
                
                # Build caption text based on download availability and premium status
                if can_show_download_button:
                    remaining = download_limit - downloaded_count
                    if is_premium:
                        caption_text = (
                            f"👆 Enjoy the video!\n\n"
                            f"💎 Premium User Benefits:\n"
                            f"📥 Click 'Download' button to get a downloadable copy\n"
                            f"📥 Downloads remaining: {remaining}/{download_limit}\n\n"
                            f"⚠️ This video will be deleted after 20 minutes."
                        )
                    else:
                        caption_text = (
                            f"👆 Enjoy the video!\n\n"
                            f"💾 Click 'Download' button to get a downloadable copy\n"
                            f"📥 Downloads remaining: {remaining}\n\n"
                            f"⚠️ This video will be deleted after 20 minutes."
                        )
                else:
                    # Different messages for premium vs free users when limit reached
                    if is_premium:
                        caption_text = (
                            f"👆 Enjoy the video!\n\n"
                            f"🔒 Download limit reached ({download_limit} videos).\n"
                            f"⚠️ This video will be deleted after 20 minutes."
                        )
                    else:
                        caption_text = (
                            f"👆 Enjoy the video!\n\n"
                            f"🔒 Download limit reached ({download_limit} video).\n\n"
                            f"💎 Want 20 downloads per day?\n"
                            f"Watch an ad for Premium Access:\n"
                            f"✅ Unlimited videos + 20 downloads!\n\n"
                            f"⚠️ This video will be deleted after 20 minutes."
                        )

                # Build inline keyboard
                keyboard = []
                
                # Add Like/Dislike buttons as first row
                like_dislike_row = await _get_video_rating_buttons(message_id)
                keyboard.append(like_dislike_row)
                
                if can_show_download_button:
                    remaining = download_limit - downloaded_count
                    download_callback = f"download_{message_id}"
                    keyboard.append([InlineKeyboardButton(f"📥 Download ({remaining} left)", callback_data=download_callback)])
                else:
                    if not is_premium:
                        keyboard.append([InlineKeyboardButton("🔥▶️ Watch Ad → Get FREE Premium! 🎁", callback_data='watch_ad')])
                keyboard.append([InlineKeyboardButton("▶️ Next", callback_data='next_video')])
                if not is_premium:
                    keyboard.append([InlineKeyboardButton("🔓 Unlock Premium", callback_data='unlock_premium')])
                reply_markup = InlineKeyboardMarkup(keyboard)

                # ── SINGLE API CALL: video + caption + buttons in one request ──
                # This halves Telegram API usage vs the old send_video + send_message pattern.
                video_msg = await context.bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=CHANNEL_IDENTIFIER,
                    message_id=message_id,
                    caption=caption_text,
                    reply_markup=reply_markup,
                    protect_content=True,
                )

                # Batch both mutations into one save_user_states call (single SQLite flush).
                user_states[user_id]['seen_videos'].add(message_id)
                # Always increment hourly_count for ALL users (premium + free).
                # The daily-limit check is: is_premium OR count < LIMIT
                # Since is_premium short-circuits first, premium users are never blocked
                # by this counter — but it lets /stats correctly count their videos.
                user_states[user_id]['hourly_count'] += 1
                save_user_states(user_states, _hint_uid=user_id)  # single flush

                # Schedule deletion — only one message now (video+caption are the same msg)
                _delete_at = _now_utc().timestamp() + 1200  # 20 min from now (Unix UTC)
                context.job_queue.run_once(
                    delete_messages,
                    when=1200,  # 20 minutes
                    data={'chat_id': chat_id, 'message_ids': [video_msg.message_id]},
                    name=f"delete_{chat_id}_{video_msg.message_id}"
                )
                # Persist so the deletion survives a bot restart
                asyncio.create_task(
                    _insert_pending_deletion(chat_id, video_msg.message_id, _delete_at)
                )
                
                video_sent = True
                break  # Success
                
            except Exception:
                # Message ID doesn't exist or isn't a video — mark failed and retry.
                # Guard: message_id may be None if get_random_video() itself raised.
                if message_id is not None:
                    failed_ids = user_states[user_id].setdefault('failed_message_ids', set())
                    if message_id not in failed_ids:
                        failed_ids.add(message_id)
                        save_user_states(user_states, _hint_uid=user_id)
                continue
        
        # If we couldn't find any valid video after all attempts
        if not video_sent:
            keyboard = [[InlineKeyboardButton("🔄 Try Again", callback_data='next_video')]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ No videos available at the moment.\n\nPlease try again!",
                reply_markup=reply_markup
            )
        
    except Exception as e:
        print(f"❌ Error sending video: {e}")
        keyboard = [[InlineKeyboardButton("🔄 Try Again", callback_data='next_video')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await context.bot.send_message(
            chat_id=chat_id,
            text="❌ Sorry, there was an error sending the video. Please try again.",
            reply_markup=reply_markup
        )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    user = update.effective_user
    user_id = user.id
    first_name = user.first_name or "there"
    username = user.username
    
    # Save user to file (persistent storage)
    add_user_to_file(user_id, first_name, username)
    
    # Check if user is a member of the mandatory channels
    all_joined, r1, r2 = await check_channel_membership(context, user_id)

    if not all_joined:
        missing = ([] if r1 else ['Channel 1']) + ([] if r2 else ['Channel 2'])
        missing_str = ' and '.join(missing)
        await update.message.reply_text(
            f"👋 Welcome {first_name}!\n\n"
            f"🔒 To use this bot, please join **{missing_str}** first.\n\n"
            f"Click the button(s) below, then tap Verify. 🎬",
            parse_mode='Markdown',
            reply_markup=_build_join_keyboard(r1, r2)
        )
        return
    
    # Check if user came from ad link (deep link with token)
    if context.args and len(context.args) > 0:
        token = context.args[0]
        
        # Verify the token belongs to this user and hasn't been used
        if token in used_tokens:
            # Token already used
            await update.message.reply_text(
                "❌ This link has already been used!\n\n"
                "Each ad link can only be used once. Please watch a new ad to get premium access.",
            )
            print(f"⚠️ User {user_id} tried to reuse token: {token}")
            return
        
        # Validate token format: user_id_timestamp_hash
        try:
            token_parts = token.split('_')
            if len(token_parts) >= 3:
                token_user_id = int(token_parts[0])
                
                # Check if token belongs to this user
                if token_user_id == user_id:
                    # Check if this is the user's assigned token
                    if user_id in user_states and user_states[user_id].get('ad_token') == token:
                        # Valid token! Grant premium access (local + shared cross-bot DB)
                        await grant_premium_access_async(user_id)
                        used_tokens.add(token)  # Mark token as used
                        # Evict old tokens if set is too large
                        if len(used_tokens) > _MAX_USED_TOKENS:
                            used_tokens.clear()  # wholesale clear — tokens are single-use anyway
                        
                        # Clear the stored token
                        user_states[user_id]['ad_token'] = None
                        user_states[user_id]['ad_link'] = None
                        # Save to persistent storage
                        save_user_states(user_states, _hint_uid=user_id)
                        
                        # Calculate expiry time in IST
                        expiry_time_utc = user_states[user_id]['premium_until']
                        expiry_time_local = utc_to_ist(expiry_time_utc)
                        
                        # Send premium activation message
                        keyboard = [[InlineKeyboardButton("🎬 Get Video", callback_data='next_video')]]
                        reply_markup = InlineKeyboardMarkup(keyboard)
                        
                        await update.message.reply_text(
                            f"✅ **Premium Access Activated!**\n\n"
                            f"Hello {first_name}! 👋\n\n"
                            f"🎉 You now have **unlimited access** for the next **12 hours**!\n\n"
                            f"⏰ Your premium access will expire at: {expiry_time_local.strftime('%I:%M %p')} IST\n\n"
                            f"🎥 Click below to start watching unlimited videos!\n\n"
                            f"Enjoy! 🍿",
                            parse_mode='Markdown',
                            reply_markup=reply_markup
                        )
                        
                        print(f"✅ Premium activated for user: {first_name} (ID: {user_id})")
                        return
                    else:
                        await update.message.reply_text(
                            "❌ Invalid or expired ad link!\n\n"
                            "Please watch a new ad to get premium access."
                        )
                        print(f"⚠️ Invalid token for user {user_id}: {token}")
                        return
                else:
                    await update.message.reply_text(
                        "❌ This link was created for a different user!\n\n"
                        "Please watch the ad yourself to get premium access."
                    )
                    print(f"⚠️ User {user_id} tried to use token for user {token_user_id}")
                    return
        except Exception as e:
            print(f"⚠️ Error validating token: {e}")
            # Continue to show welcome message if token is invalid
    
    # Check if user already has premium (checks both local state + shared cross-bot DB)
    if await is_premium_user_async(user_id):
        # Prefer premium_until from shared DB for correct cross-bot expiry display
        premium_until = await get_premium_until_synced(user_id) or user_states.get(user_id, {}).get('premium_until')
        if premium_until is None:
            premium_until = _now_utc() + timedelta(hours=1)  # fallback
        time_remaining = premium_until - _now_utc()
        hours_remaining = int(time_remaining.total_seconds() / 3600)
        minutes_remaining = int((time_remaining.total_seconds() % 3600) / 60)
        
        # Convert expiry time to IST
        expiry_time_local = utc_to_ist(premium_until)
        
        keyboard = [[InlineKeyboardButton("🎬 Get Video", callback_data='next_video')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"🎬 **Welcome Back, {first_name}!** 🎬\n\n"
            f"✅ You have **Premium Access**!\n\n"
            f"⏰ Time remaining: **{hours_remaining}h {minutes_remaining}m**\n"
            f"⏰ Expires at: **{expiry_time_local.strftime('%I:%M %p')} IST**\n\n"
            f"🎥 Click below to watch unlimited videos!\n\n"
            f"Enjoy! 🍿",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
        return
    
    # Regular welcome message for new/non-premium users
    welcome_message = f"""
🎬 **Welcome to Videos Bot!** 🎬

Hello {first_name}! 👋

I'm here to share amazing videos with you!

🎥 You can watch **1 free video per day**!
⏰ Your limit resets every **24 hours at midnight IST**

💾 You can **download 1 video per day**!

💎 Want unlimited access? Watch an ad to get **12 hours of premium**!

Enjoy! 🍿
    """
    
    # ── Single API call: welcome text + inline button in one message ──
    # Previously this was 2 separate reply_text calls (one to clear the keyboard
    # via ReplyKeyboardRemove, one to send the inline button). Merged into one.
    # ReplyKeyboardRemove is still sent as reply_markup so old menus are cleared.
    # The inline button is embedded directly in the same message.
    inline_keyboard = [
        [InlineKeyboardButton("🎬 Get First Video", callback_data='next_video')]
    ]
    await update.message.reply_text(
        welcome_message,
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(inline_keyboard)
    )



async def buy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /buy — create a Razorpay payment link + QR code for premium subscription."""
    user = update.effective_user
    user_id = user.id
    first_name = user.first_name or "there"

    # Check if already premium
    if await is_premium_user_async(user_id):
        premium_until = await get_premium_until_synced(user_id) or user_states.get(user_id, {}).get('premium_until')
        if premium_until:
            expiry_ist = utc_to_ist(premium_until)
            time_remaining = premium_until - _now_utc()
            days_rem = int(time_remaining.total_seconds() / 86400)
            hrs_rem  = int((time_remaining.total_seconds() % 86400) / 3600)
            await update.message.reply_text(
                f"✅ *You already have Premium Access!*\n\n"
                f"⏰ Expires: *{expiry_ist.strftime('%d %b %Y, %I:%M %p')} IST*\n"
                f"⏳ Remaining: *{days_rem}d {hrs_rem}h*\n\n"
                f"No need to pay again — enjoy unlimited videos! 🍿",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🎬 Watch Now", callback_data='next_video')]]
                )
            )
            return

    days = RZP_HOURS // 24
    amount_inr = RZP_AMOUNT / 100

    # Send "creating link…" placeholder so user knows something is happening
    wait_msg = await update.message.reply_text(
        f"⏳ Creating your payment link...\n"
        f"Please wait a moment!"
    )

    session = await _get_http_session()
    payment_url, qr_url = await create_payment_link(user_id, first_name, session)

    # Delete the placeholder
    try:
        await context.bot.delete_message(
            chat_id=update.effective_chat.id,
            message_id=wait_msg.message_id
        )
    except Exception:
        pass

    if not payment_url:
        await update.message.reply_text(
            "❌ Could not create payment link right now.\n"
            "Please try again in a minute or contact admin."
        )
        return

    keyboard = [
        [InlineKeyboardButton(f"💳 Pay ₹{amount_inr:.0f} — Get {days}-Day Premium", url=payment_url)],
        [InlineKeyboardButton("💬 Need Help?", url="https://t.me/videos69Admin_Bot")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Send QR code image first
    try:
        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=qr_url,
            caption=(
                f"📱 *Scan QR or tap the button below to pay*\n\n"
                f"💎 *{days}-Day Premium Plan — ₹{amount_inr:.0f}*\n\n"
                f"✅ Unlimited videos for *{days} days*\n"
                f"✅ *20 downloads* per day\n"
                f"✅ Works across *all our bots*\n\n"
                f"🔒 *Safe & secure* — powered by Razorpay\n\n"
                f"⚡ Premium activates *automatically* within seconds of payment!"
            ),
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
    except Exception as e:
        print(f"⚠️ [BUY] Could not send QR photo: {e} — sending text fallback")
        await update.message.reply_text(
            f"💎 *{days}-Day Premium — ₹{amount_inr:.0f}*\n\n"
            f"✅ Unlimited videos for *{days} days*\n"
            f"✅ *20 downloads* per day\n"
            f"✅ Works across *all our bots*\n\n"
            f"⚡ Premium activates *automatically* after payment!",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )


async def admin_payments_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin /payments — show recent Razorpay payment records."""
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ You are not authorized to use this command.")
        return

    payments = await get_recent_payments(limit=10)
    if not payments:
        await update.message.reply_text("ℹ️ No payment records found yet.")
        return

    lines = ["💳 *Recent Razorpay Payments (last 10)*\n"]
    for p in payments:
        status_icon = "✅" if p['status'] == 'paid' else "⏳"
        amount_inr = p['amount_paise'] / 100
        paid_at = p.get('paid_at') or '—'
        lines.append(
            f"{status_icon} uid=`{p['user_id']}` | "
            f"₹{amount_inr:.0f} | {p['premium_hours']//24}d | "
            f"{p['status']} | {paid_at[:16]}"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode='Markdown'
    )


async def admin_revoke_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin /revoke <user_id> — revoke a user's premium across ALL bots."""
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ You are not authorized to use this command.")
        return

    if not context.args or len(context.args) < 1:
        await update.message.reply_text(
            "⚠️ *Usage:* `/revoke <user_id>`\n\n"
            "*Example:* `/revoke 7708376300`\n\n"
            "This will immediately remove their premium access from ALL bots.",
            parse_mode='Markdown'
        )
        return

    try:
        target_uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. Must be a number.")
        return

    # Revoke from shared DB (all bots see this within cache TTL ≤60s)
    await revoke_premium_synced(target_uid)

    # Also clear from local user_states if this bot has them
    if target_uid in user_states:
        user_states[target_uid]['premium_until'] = None
        save_user_states(user_states, _hint_uid=target_uid)

    # Invalidate premium cache so next check fetches fresh from DB
    _invalidate_premium_cache(target_uid)

    await update.message.reply_text(
        f"✅ *Premium Revoked!*\n\n"
        f"👤 User ID: `{target_uid}`\n"
        f"🗑️ Premium access removed from *all bots* immediately.\n\n"
        f"User will see free-tier limits on their next request.",
        parse_mode='Markdown'
    )

    # Broadcast notification to the user across all registered bots
    from premium_sync import get_all_bot_tokens
    all_tokens = await get_all_bot_tokens()
    
    if not all_tokens and BOT_TOKEN:
        all_tokens = [BOT_TOKEN]

    user_text = (
        "❌ *Your premium access has been revoked.*\n\n"
        "If you believe this is a mistake, please contact the admin.\n"
        "You can still use the bot with the free daily limit."
    )

    async with aiohttp.ClientSession() as session:
        for tk in all_tokens:
            try:
                await session.post(
                    f"https://api.telegram.org/bot{tk}/sendMessage",
                    json={
                        "chat_id": target_uid,
                        "text": user_text,
                        "parse_mode": "Markdown"
                    }
                )
            except Exception as e:
                logger.error(f"Failed to send revoke notification to {target_uid} via token {tk[:10]}...: {e}")

    print(f"🗑️ [REVOKE] Admin revoked premium for uid={target_uid}")


async def admin_checkpremium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin /checkpremium <user_id> — check if a user has active premium."""
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ You are not authorized to use this command.")
        return

    if not context.args or len(context.args) < 1:
        await update.message.reply_text(
            "⚠️ *Usage:* `/checkpremium <user_id>`\n\n"
            "*Example:* `/checkpremium 7708376300`\n\n"
            "This will show if the user has active premium and when it expires.",
            parse_mode='Markdown'
        )
        return

    try:
        target_uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. Must be a number.")
        return

    # Check premium status
    from premium_sync import get_premium_until_synced, is_premium_synced
    is_active = await is_premium_synced(target_uid)
    premium_until = await get_premium_until_synced(target_uid)

    if is_active and premium_until:
        expiry_local = utc_to_ist(premium_until)
        await update.message.reply_text(
            f"✅ *User Has Active Premium!*\n\n"
            f"👤 User ID: `{target_uid}`\n"
            f"⏰ Expires: *{expiry_local.strftime('%d %b %Y, %I:%M %p')} IST*\n\n"
            f"Premium is active across *all bots*.",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(
            f"❌ *No Active Premium*\n\n"
            f"👤 User ID: `{target_uid}`\n\n"
            f"This user does NOT have active premium access.",
            parse_mode='Markdown'
        )

    print(f"🔍 [CHECKPREMIUM] Admin checked premium for uid={target_uid} (active={is_active})")


async def admin_approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin /approve <user_id> <days> — grant a user premium across ALL bots for specific days."""
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ You are not authorized to use this command.")
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "⚠️ *Usage:* `/approve <user_id> <number_of_days>`\n\n"
            "*Example:* `/approve 7708376300 30`\n\n"
            "This will immediately grant premium access to the user across ALL bots.",
            parse_mode='Markdown'
        )
        return

    try:
        target_uid = int(context.args[0])
        days = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Invalid arguments. Both user ID and days must be numbers.")
        return

    if days <= 0:
        await update.message.reply_text("❌ Days must be greater than 0.")
        return

    # Grant premium in shared DB
    hours = days * 24
    from premium_sync import grant_premium_synced
    premium_until = await grant_premium_synced(target_uid, hours=hours)

    # Update local state if the user exists here
    if target_uid in user_states:
        user_states[target_uid]['premium_until'] = premium_until
        save_user_states(user_states, _hint_uid=target_uid)

    # Invalidate membership cache so next check re-evaluates premium state
    _membership_cache.pop(target_uid, None)

    expiry_local = utc_to_ist(premium_until)

    await update.message.reply_text(
        f"✅ *Premium Approved!*\n\n"
        f"👤 User ID: `{target_uid}`\n"
        f"⏳ Duration: *{days} Days*\n"
        f"⏰ Expires: *{expiry_local.strftime('%d %b %Y, %I:%M %p')} IST*\n\n"
        f"Premium access granted across *all bots* immediately.",
        parse_mode='Markdown'
    )

    # Broadcast notification to the user across all registered bots
    from premium_sync import get_all_bot_tokens
    all_tokens = await get_all_bot_tokens()
    
    if not all_tokens and BOT_TOKEN:
        all_tokens = [BOT_TOKEN]

    user_text = (
        f"🎉 **Premium Access Granted by Admin!**\n\n"
        f"You have been granted **{days} days** of unlimited premium access!\n\n"
        f"⏰ Expires: *{expiry_local.strftime('%d %b %Y, %I:%M %p')} IST*\n\n"
        f"Enjoy unlimited videos and downloads! 🍿"
    )
    
    # Create inline keyboard with Get Video button
    keyboard = [[InlineKeyboardButton("🎬 Get Video", callback_data='next_video')]]
    reply_markup_dict = {
        "inline_keyboard": [[{"text": btn.text, "callback_data": btn.callback_data} for btn in row] for row in keyboard]
    }

    async with aiohttp.ClientSession() as session:
        for tk in all_tokens:
            try:
                await session.post(
                    f"https://api.telegram.org/bot{tk}/sendMessage",
                    json={
                        "chat_id": target_uid,
                        "text": user_text,
                        "parse_mode": "Markdown",
                        "reply_markup": reply_markup_dict
                    }
                )
            except Exception as e:
                logger.error(f"Failed to send approve notification to {target_uid} via token {tk[:10]}...: {e}")

    print(f"✅ [APPROVE] Admin granted premium for uid={target_uid} for {days} days")


async def admin_approve_sec_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin /approvesec <user_id> <seconds> — grant a user premium across ALL bots for specific seconds (testing)."""
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ You are not authorized to use this command.")
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "⚠️ *Usage:* `/approvesec <user_id> <seconds>`\n\n"
            "*Example:* `/approvesec 7708376300 5`\n\n"
            "This will grant premium access to the user across ALL bots for a few seconds.",
            parse_mode='Markdown'
        )
        return

    try:
        target_uid = int(context.args[0])
        seconds = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Invalid arguments. Both user ID and seconds must be numbers.")
        return

    if seconds <= 0:
        await update.message.reply_text("❌ Seconds must be greater than 0.")
        return

    # Grant premium in shared DB
    from premium_sync import grant_premium_until_synced
    premium_until = _now_utc() + timedelta(seconds=seconds)
    await grant_premium_until_synced(target_uid, premium_until)

    # Update local state if the user exists here
    if target_uid in user_states:
        user_states[target_uid]['premium_until'] = premium_until
        save_user_states(user_states, _hint_uid=target_uid)

    # Invalidate membership cache
    _membership_cache.pop(target_uid, None)

    expiry_local = utc_to_ist(premium_until)

    await update.message.reply_text(
        f"✅ *Premium Approved (Short)!*\n\n"
        f"👤 User ID: `{target_uid}`\n"
        f"⏳ Duration: *{seconds} Seconds*\n"
        f"⏰ Expires: *{expiry_local.strftime('%I:%M:%S %p IST')}*\n\n"
        f"Premium access granted across *all bots* temporarily.",
        parse_mode='Markdown'
    )

    # Notify user
    from premium_sync import get_all_bot_tokens
    all_tokens = await get_all_bot_tokens()
    if not all_tokens and BOT_TOKEN:
        all_tokens = [BOT_TOKEN]

    user_text = (
        f"🎉 **Temporary Premium Access Granted!**\n\n"
        f"You have been granted **{seconds} seconds** of premium access!\n"
        f"Enjoy! 🍿"
    )
    
    keyboard = [[InlineKeyboardButton("🎬 Get Video", callback_data='next_video')]]
    reply_markup_dict = {
        "inline_keyboard": [[{"text": btn.text, "callback_data": btn.callback_data} for btn in row] for row in keyboard]
    }

    async with aiohttp.ClientSession() as session:
        for tk in all_tokens:
            try:
                await session.post(
                    f"https://api.telegram.org/bot{tk}/sendMessage",
                    json={
                        "chat_id": target_uid,
                        "text": user_text,
                        "parse_mode": "Markdown",
                        "reply_markup": reply_markup_dict
                    }
                )
            except Exception:
                pass

    print(f"✅ [APPROVESEC] Admin granted {seconds}s premium to uid={target_uid}")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command - Direct users to admin bot"""
    user = update.effective_user
    first_name = user.first_name or "there"
    
    help_message = f"""
❓ **Need Help?** ❓

Hello {first_name}! 👋

If you're facing any issues or have questions, you can contact our admin!

📞 **Contact Admin Bot:**
Click the button below to message our admin support bot.

👨‍💼 **Admin Support Available**

Your message will be forwarded to the admin, and they will reply to you directly!
    """
    
    # Create button to admin bot
    keyboard = [[InlineKeyboardButton("💬 Contact Admin", url="https://t.me/videos69Admin_Bot")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        help_message,
        parse_mode='Markdown',
        reply_markup=reply_markup
    )



async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle regular messages - Forward to/from admin OR handle menu buttons"""
    # Ignore messages without a user (e.g., channel posts)
    if not update.effective_user:
        return
    
    user_id = update.effective_user.id
    message = update.message
    if message.text == "\U0001f4ac Contact Us":
            # User clicked "Contact Us" button - show admin bot link
            first_name = update.effective_user.first_name or "there"

            help_message = f"""
\u2753 **Need Help?** \u2753

Hello {first_name}! \U0001f44b

If you're facing any issues or have questions, you can contact our admin!

\U0001f4de **Contact Admin Bot:**
Click the button below to message our admin support bot.

\U0001f468\u200d\U0001f4bc **Admin Support Available**

Your message will be forwarded to the admin, and they will reply to you directly!
            """

            keyboard = [[InlineKeyboardButton("\U0001f4ac Contact Admin", url="https://t.me/videos69Admin_Bot")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await message.reply_text(
                help_message,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button callbacks"""
    query = update.callback_query
    user_id = query.from_user.id

    # Answer the query immediately (Telegram requires this within 30s)
    # Skip for next_video — it handles its own answer (cooldown alert)
    if query.data != 'next_video':
        try:
            await query.answer()
        except Exception:
            pass
    
    if query.data == 'check_membership':
        user_id = query.from_user.id
        first_name = query.from_user.first_name or "there"

        # Invalidate cache so this verify always does a fresh API check
        _membership_cache.pop(user_id, None)
        all_joined, r1, r2 = await check_channel_membership(context, user_id)

        if all_joined:
            # User has joined both — show welcome
            keyboard = [[InlineKeyboardButton("🎬 Get First Video", callback_data='next_video')]]
            try:
                await query.message.edit_text(
                    f"✅ **Verified!**\n\n"
                    f"Welcome {first_name}! 👋\n\n"
                    f"You've successfully joined both channels!\n\n"
                    f"🎥 You can watch **1 free video** per day!\n"
                    f"📥 You can download **1 video** per day!\n"
                    f"⏰ Limits reset every **24 hours at midnight IST**!\n\n"
                    f"💎 Want unlimited access? Watch an ad for **12 hours of premium**!\n\n"
                    f"Click below to start watching! 🍿",
                    parse_mode='Markdown',
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            except Exception as e:
                if "Message is not modified" not in str(e):
                    print(f"⚠️ Error editing message: {e}")
        else:
            # Show only the channel(s) the user hasn't joined yet
            missing = ([] if r1 else ['Channel 1']) + ([] if r2 else ['Channel 2'])
            missing_str = ' and '.join(missing)
            try:
                await query.message.edit_text(
                    f"❌ **Still need to join {missing_str}**\n\n"
                    f"Please join the channel(s) shown below, then tap Verify again. 🔒",
                    parse_mode='Markdown',
                    reply_markup=_build_join_keyboard(r1, r2)
                )
            except Exception as e:
                if "Message is not modified" not in str(e):
                    print(f"⚠️ Error editing message: {e}")
    
    elif query.data == 'ignore':
        # Ignore this callback (used for "Processing..." buttons)
        return
        
    elif query.data == 'next_video':
        user_id = query.from_user.id
        
        # Check if the clicked button is specifically "▶️ Next"
        is_actual_next_button = False
        if query.message and query.message.reply_markup and query.message.reply_markup.inline_keyboard:
            for row in query.message.reply_markup.inline_keyboard:
                for btn in row:
                    if btn.callback_data == 'next_video' and btn.text == "▶️ Next":
                        is_actual_next_button = True
                        break

        # Only apply the cooldown and "Processing..." visual to the actual "▶️ Next" button
        if is_actual_next_button:
            now = _time.monotonic()
            last_served = _next_video_cooldown.get(user_id, 0.0)
            elapsed = now - last_served

            if elapsed < _NEXT_VIDEO_COOLDOWN_SECONDS:
                # Silently ignore clicks within the cooldown
                return

            _next_video_cooldown[user_id] = now

        try:
            await query.answer()
        except Exception:
            pass

        try:
            if is_actual_next_button:
                try:
                    if query.message.reply_markup and query.message.reply_markup.inline_keyboard:
                        original_keyboard = []
                        new_keyboard = []
                        changed = False
                        for row in query.message.reply_markup.inline_keyboard:
                            new_row = []
                            orig_row = []
                            for btn in row:
                                orig_row.append(btn)
                                if btn.callback_data == 'next_video' and btn.text == "▶️ Next":
                                    new_row.append(InlineKeyboardButton("⏳ Processing...", callback_data='ignore'))
                                    changed = True
                                else:
                                    new_row.append(btn)
                            new_keyboard.append(new_row)
                            original_keyboard.append(orig_row)
                        
                        if changed:
                            await query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(new_keyboard))
                            # Schedule restoration of the button after cooldown
                            context.job_queue.run_once(
                                restore_button_callback,
                                when=_NEXT_VIDEO_COOLDOWN_SECONDS,
                                data={
                                    'chat_id': query.message.chat_id,
                                    'message_id': query.message.message_id,
                                    'original_keyboard': original_keyboard
                                },
                                name=f"restore_btn_{query.message.chat_id}_{query.message.message_id}"
                            )
                except Exception as e:
                    if "Message is not modified" not in str(e) and "Message can't be edited" not in str(e):
                        print(f"⚠️ Error updating next_video button: {e}")

            await rate_limited_send_video(
                update,
                context,
                chat_id=query.message.chat_id,
                user_id=user_id
            )
        except Exception:
            if is_actual_next_button:
                _next_video_cooldown.pop(user_id, None)
            raise
    
    elif query.data == 'unlock_premium':
        # User clicked "Unlock Premium" button - show Watch Ad option
        user_id = query.from_user.id
        
        keyboard = [[InlineKeyboardButton("📺 Watch Ad to Get Premium", callback_data='watch_ad')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        try:
            await query.message.edit_text(
                "💎 **Unlock Premium Access!**\n\n"
                "Get premium benefits:\n"
                "✅ **Unlimited** video watching\n"
                "✅ **20 downloads** per day (10x more!)\n"
                "✅ **12 hours** of premium access\n\n"
                "Click the button below to watch an ad and unlock premium! 🚀",
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
        except Exception as e:
            # If edit fails, send new message
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="💎 **Unlock Premium Access!**\n\n"
                     "Get premium benefits:\n"
                     "✅ **Unlimited** video watching\n"
                     "✅ **20 downloads** per day (10x more!)\n"
                     "✅ **12 hours** of premium access\n\n"
                     "Click the button below to watch an ad and unlock premium! 🚀",
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
    
    elif query.data == 'watch_ad':
        # Create unique shortened links from BOTH services concurrently
        user_id = query.from_user.id
        aro_url, vp_url = await create_shortened_links(user_id)

        if aro_url or vp_url:
            keyboard = []
            if aro_url:
                keyboard.append([InlineKeyboardButton("🔥 AroLinks — Watch Ad & Get Premium", url=aro_url)])
            if vp_url:
                keyboard.append([InlineKeyboardButton("⚡ VPLink — Watch Ad & Get Premium", url=vp_url)])
            keyboard.append([InlineKeyboardButton("❓ How to Open Link", callback_data='how_to_open')])
            reply_markup = InlineKeyboardMarkup(keyboard)

            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="📺 **Watch Ad to Get Premium Access**\n\n"
                     "👇 **Choose any link below** — both give you the same premium!\n\n"
                     "🔥 **AroLinks** — tap if VPLink doesn't work for you\n"
                     "⚡ **VPLink** — tap if AroLinks doesn't work for you\n\n"
                     "🎯 **Instructions**:\n"
                     "1. Tap any link button below\n"
                     "2. Complete the ad verification process\n"
                     "3. You'll be redirected back to the bot\n"
                     "4. **Premium will activate automatically!**\n\n"
                     "⚠️ **iOS Users**: Copy the link and open it in Chrome browser\n\n"
                     "💎 You'll get **12 hours of unlimited video access**!",
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
        else:
            # Both shorteners failed
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="❌ Error creating ad link. Please try again later."
            )
    
    elif query.data == 'how_to_open':
        # Show instructions on how to open the link
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="❓ **How to Open the Ad Link**:\n\n"
                 "**For iOS Users**:\n"
                 "1. Click on the 'Click Here to Watch Ad' button\n"
                 "2. Copy the link that appears\n"
                 "3. Open Chrome browser on your device\n"
                 "4. Paste and open the link\n"
                 "5. Complete the ad viewing process\n"
                 "6. You'll be redirected back automatically!\n\n"
                 "**For Android Users**:\n"
                 "1. Simply click the 'Click Here to Watch Ad' button\n"
                 "2. Complete the ad verification\n"
                 "3. You'll be redirected back automatically!\n\n"
                 "✅ Premium will activate automatically when you return!",
            parse_mode='Markdown'
        )
    
    elif query.data.startswith('download_'):
        # User clicked Download button - send unprotected copy
        # Extract message ID from callback data (format: "download_messageId")
        try:
            message_id = int(query.data.split('_')[1])
            user_id = query.from_user.id

            # Determine premium status (uses async cross-bot check)
            is_premium = await is_premium_user_async(user_id)
            download_limit = PREMIUM_DOWNLOADABLE_VIDEOS_LIMIT if is_premium else DOWNLOADABLE_VIDEOS_LIMIT

            # Read shared download count (counts downloads across ALL bots today)
            downloaded_count = await get_synced_download_count(user_id)

            # Check if user still has downloads remaining
            if downloaded_count >= download_limit:
                # Show different messages for premium vs free users
                if is_premium:
                    # Premium user reached their 20 download limit
                    await context.bot.send_message(
                        chat_id=query.message.chat_id,
                        text="❌ **Download Limit Reached!**\n\n"
                             f"You've already downloaded {download_limit} videos today.\n"
                             "Your download limit will reset at midnight (12:00 AM IST).\n\n"
                             "⏱️ Resets every **24 hours at midnight IST**"
                    )
                else:
                    # Free user reached their 1 download limit - show premium promotion
                    keyboard = [[InlineKeyboardButton("📺 Watch Ad for Premium", callback_data='watch_ad')]]
                    reply_markup = InlineKeyboardMarkup(keyboard)

                    await context.bot.send_message(
                        chat_id=query.message.chat_id,
                        text="❌ **Download Limit Reached!**\n\n"
                             f"You've already downloaded {download_limit} video today.\n\n"
                             "💎 **Want More Downloads?**\n"
                             "Watch an ad to get Premium Access:\n\n"
                             "✅ **Unlimited** video watching\n"
                             "✅ **20 downloads** per day (20x more!)\n"
                             "✅ **12 hours** of premium access\n\n"
                             "⏱️ Limits reset every **24 hours at midnight (12:00 AM IST)**\n\n"
                             "Click below to watch an ad and unlock premium! 🚀",
                        parse_mode='Markdown',
                        reply_markup=reply_markup
                    )
                return

            # Send unprotected copy of the video
            try:
                download_msg = await context.bot.copy_message(
                    chat_id=query.message.chat_id,
                    from_chat_id=CHANNEL_IDENTIFIER,
                    message_id=message_id,
                    protect_content=False  # Unprotected - can be downloaded/saved/forwarded
                )

                # ── Shared increment (writes to central DB — all bots see this) ──
                new_shared_count = await increment_synced_download(user_id)

                # ── Local increment (keeps local user_states in sync too) ────────
                if user_id in user_states:
                    user_states[user_id]['downloaded_count'] = new_shared_count
                    save_user_states(user_states, _hint_uid=user_id)

                remaining = download_limit - new_shared_count


                # Update the original message buttons
                keyboard = []
                
                # Add Like/Dislike buttons as first row back
                like_dislike_row = await _get_video_rating_buttons(message_id)
                keyboard.append(like_dislike_row)
                
                # Show updated download button or remove it if no downloads left
                if remaining > 0:
                    download_callback = f"download_{message_id}"
                    keyboard.append([InlineKeyboardButton(f"📥 Download ({remaining} left)", callback_data=download_callback)])
                
                # Always keep Next button
                keyboard.append([InlineKeyboardButton("▶️ Next", callback_data='next_video')])
                
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                # Update the message text
                if remaining > 0:
                    if is_premium:
                        new_text = (
                            f"👆 Enjoy the video!\n\n"
                            f"✅ Downloadable copy sent above!\n"
                            f"💎 Premium: Downloads remaining: {remaining}/{download_limit}\n\n"
                            f"⚠️ This video will be deleted after 20 minutes."
                        )
                    else:
                        new_text = (
                            f"👆 Enjoy the video!\n\n"
                            f"✅ Downloadable copy sent above!\n"
                            f"📥 Downloads remaining: {remaining}\n\n"
                            f"⚠️ This video will be deleted after 20 minutes."
                        )
                else:
                    new_text = (
                        f"👆 Enjoy the video!\n\n"
                        f"✅ Downloadable copy sent above!\n"
                        f"🔒 Download limit reached ({download_limit} videos).\n\n"
                        f"⚠️ This video will be deleted after 20 minutes."
                    )
                
                # ── BUG FIX: edit_caption not edit_text ──
                # query.message is the VIDEO message (caption + buttons). Telegram's API
                # does NOT allow editMessageText on media messages — it raises
                # "Bad Request: there is no text in the message". Must use edit_caption.
                try:
                    await query.message.edit_caption(
                        caption=new_text,
                        reply_markup=reply_markup
                    )
                except Exception as e:
                    if "Message is not modified" not in str(e):
                        print(f"⚠️ Error updating caption: {e}")

                # Schedule deletion of the download copy only (1 message, not 2)
                _dl_delete_at = _now_utc().timestamp() + 1200  # 20 min from now
                context.job_queue.run_once(
                    delete_messages,
                    when=1200,  # 20 minutes
                    data={'chat_id': query.message.chat_id, 'message_ids': [download_msg.message_id]},
                    name=f"delete_download_{query.message.chat_id}_{download_msg.message_id}"
                )
                # Persist so the deletion survives a bot restart
                asyncio.create_task(
                    _insert_pending_deletion(
                        query.message.chat_id, download_msg.message_id, _dl_delete_at
                    )
                )
                

                
            except Exception as e:
                print(f"❌ Error sending downloadable video: {e}")
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text="❌ **Error!**\n\n"
                         "Could not send downloadable copy. The video may no longer be available."
                )
        
        except Exception as e:
            print(f"❌ Error processing download request: {e}")
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="❌ **Error!**\n\n"
                     "Could not process download request. Please try again."
            )

    elif query.data.startswith('rzp_plan_'):
        # User tapped a plan → create Razorpay QR Code → send QR image (from Razorpay CDN directly)
        plan_key   = query.data[len('rzp_plan_'):]
        user_id    = query.from_user.id
        chat_id    = query.message.chat_id
        first_name = query.from_user.first_name or "there"

        try:
            await context.bot.answer_callback_query(callback_query_id=query.id)
        except Exception:
            pass

        plan = RZP_PLANS.get(plan_key)
        if not plan:
            await context.bot.send_message(chat_id=chat_id, text="❌ Unknown plan. Please try again.")
            return

        _, hours, amount_paise, plan_name, _ = plan
        amount_inr = amount_paise / 100
        is_life    = (hours >= 87600)
        days_str   = "Lifetime" if is_life else f"{hours // 24} day{'s' if hours // 24 != 1 else ''}"

        # Show placeholder while Razorpay creates the QR Code
        wait_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏳ Generating *{plan_name}* payment QR...\nPlease wait!",
            parse_mode='Markdown'
        )

        session = await _get_http_session()
        image_url, qr_id, pl_id = await create_qr_code_for_plan(
            user_id, chat_id, first_name, plan_key, session
        )

        # Delete placeholder
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=wait_msg.message_id)
        except Exception:
            pass

        if not image_url or not pl_id:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "❌ Could not generate payment QR. Please try again or contact admin.\n\n"
                    "_If the issue persists, use_ /help _to reach support._"
                ),
                parse_mode='Markdown'
            )
            return

        # Send Razorpay's own QR image (no external service — Razorpay CDN hosts it)
        try:
            qr_msg = await context.bot.send_photo(
                chat_id=chat_id,
                photo=image_url,
                caption=(
                    f"💳 *Pay ₹{amount_inr:.0f} — {plan_name}*\n\n"
                    f"1️⃣ Open GPay / PhonePe / Paytm\n"
                    f"2️⃣ Scan the QR code above\n"
                    f"3️⃣ Pay ₹{amount_inr:.0f}\n\n"
                    f"⚡ Premium activates *automatically* within seconds of payment!"
                ),
                parse_mode='Markdown'
            )
            await store_qr_message_id(pl_id, qr_msg.message_id)
            # Also store the plan-selection message ID so it's deleted on payment
            await store_limit_message_id(pl_id, query.message.message_id)
        except Exception as e:
            logger.error(f"[rzp_plan] QR photo failed: {e}")
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"💳 *{plan_name} — ₹{amount_inr:.0f}*\n"
                    f"QR ID: `{qr_id}`\n\n"
                    f"⚡ Premium activates automatically after payment!"
                ),
                parse_mode='Markdown'
            )


    elif query.data.startswith('vote_'):
        # Format: vote_like_{message_id} or vote_dislike_{message_id} or vote_none_{message_id}
        parts = query.data.split('_')
        if len(parts) >= 3:
            vote_type = parts[1]  # 'like', 'dislike', or 'none'

            if vote_type == 'none':
                try:
                    await context.bot.answer_callback_query(callback_query_id=query.id, text="Likes Percentage")
                except Exception:
                    pass
                return

            try:
                message_id = int(parts[2])
                user_id = query.from_user.id
                
                changed, msg = await _record_video_vote(user_id, message_id, vote_type)
                
                if changed:
                    # Optional: Popup toast instead of message box (show_alert=False)
                    await context.bot.answer_callback_query(callback_query_id=query.id, text=msg)
                    
                    # Update the keyboard with new percentages
                    try:
                        old_markup = query.message.reply_markup.inline_keyboard
                        # old_markup is a list of rows, we just overwrite the first row.
                        new_markup = list(old_markup)
                        new_markup[0] = await _get_video_rating_buttons(message_id)
                        
                        await query.message.edit_reply_markup(
                            reply_markup=InlineKeyboardMarkup(new_markup)
                        )
                    except Exception as e:
                        if "Message is not modified" not in str(e):
                            print(f"⚠️ Error updating vote keyboard: {e}")
                else:
                    await context.bot.answer_callback_query(callback_query_id=query.id, text=msg)

            except Exception as e:
                print(f"⚠️ Error processing vote: {e}")
                await context.bot.answer_callback_query(callback_query_id=query.id, text="Error. Please try again.")

async def _startup_pending_deletion_cleanup(application: Application):
    """On restart: delete overdue messages immediately; reschedule still-pending ones.

    This handles the case where the bot was stopped while 20-min timers were
    still running.  Any message whose deadline has passed is deleted right away;
    messages still within their window get a new job for their remaining time.
    """
    rows = await _get_all_pending_deletions()
    if not rows:
        return

    now_ts = _now_utc().timestamp()
    rescheduled = 0
    deleted_now = 0
    errors = 0

    for (row_id, chat_id, message_id, delete_at) in rows:
        remaining = delete_at - now_ts
        if remaining <= 0:
            # Overdue — delete immediately
            try:
                await application.bot.delete_message(chat_id=chat_id, message_id=message_id)
                deleted_now += 1
            except Exception:
                errors += 1  # already deleted or other error — ignore
            await _remove_pending_deletion(chat_id, message_id)
        else:
            # Still within window — reschedule for remaining seconds
            application.job_queue.run_once(
                delete_messages,
                when=remaining,
                data={'chat_id': chat_id, 'message_ids': [message_id]},
                name=f"delete_{chat_id}_{message_id}"
            )
            rescheduled += 1

    print(f"🧹 Startup cleanup: {deleted_now} overdue videos deleted, "
          f"{rescheduled} rescheduled, {errors} already gone")


async def post_init(application: Application):
    """Initialize bot after startup"""
    # 1. Init SQLite DB for user_states and start flush loop
    await init_db()
    start_flush_loop()

    # 2. Init bot_users table + pending_deletions table + ratings tables; load users into cache
    async with aiosqlite.connect("user_states.db") as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await _init_users_table(db)
        await _init_pending_deletions_table(db)
        await _init_ratings_table(db)
        await _load_users_from_db(db)
        # Migrate users.json if it exists and bot_users table is empty
        if not _users_cache and os.path.exists(OLD_USERS_JSON):
            await _migrate_users_json(db)

    print(f"👥 {len(_users_cache)} registered users loaded from DB")

    # 3. Init shared premium sync DB (cross-bot premium sharing)
    await init_premium_sync()
    if BOT_TOKEN:
        await register_bot_token(BOT_TOKEN, BOT_USERNAME)

    # 4. Load available video IDs from the channel
    await load_videos_from_channel(application)

    # 5. Start bot_users background flush loop
    global _bot_users_flush_task
    _bot_users_flush_task = asyncio.create_task(
        _bot_users_flush_loop(), name="bot_users_flush"
    )

    # 6. Handle orphaned pending-deletion jobs from previous run
    await _startup_pending_deletion_cleanup(application)

    # 7. Start Razorpay webhook server (background aiohttp server)
    await start_webhook_server(application, ADMIN_USER_ID, BOT_USERNAME)

async def post_shutdown(application: Application):
    """Clean shutdown: flush all dirty writes and close all DB/HTTP connections."""
    # Stop Razorpay webhook server first
    await stop_webhook_server()
    # Cancel both flush loops (each catches CancelledError and does final flush)
    stop_flush_loop()              # user_states flush loop
    if _bot_users_flush_task and not _bot_users_flush_task.done():
        _bot_users_flush_task.cancel()
    await asyncio.sleep(0.5)       # give both loops time to complete final flush
    await close_persistent_db()   # close user_states SQLite connection
    global _http_session, _users_persistent_db
    if _users_persistent_db:
        try:
            await _users_persistent_db.close()
        except Exception:
            pass
        _users_persistent_db = None
    if _http_session and not _http_session.closed:
        await _http_session.close()
        _http_session = None
    print("✅ Shutdown complete — all data flushed, connections closed")


async def handle_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Real-time membership enforcement via Telegram chat_member updates.

    When a user leaves or is kicked from a mandatory channel, Telegram pushes
    this event immediately. We evict their _membership_cache entry so the next
    video request triggers a fresh API check and blocks them instantly.
    """
    chat_member = update.chat_member
    if not chat_member:
        return

    # Only care about our two mandatory channels
    chat_id = chat_member.chat.id
    if chat_id not in (MANDATORY_CHANNEL, MANDATORY_CHANNEL_2):
        return

    user_id    = chat_member.new_chat_member.user.id
    new_status = chat_member.new_chat_member.status

    # User left or was kicked — drop their cache entry immediately
    if new_status in ('left', 'kicked', 'banned'):
        _membership_cache.pop(user_id, None)
        ch_name = "Channel 1" if chat_id == MANDATORY_CHANNEL else "Channel 2"
        print(f"🔔 User {user_id} left {ch_name} — cache evicted, will be blocked on next request")


# ──────────────────────────────────────────────────────────────
# ADMIN PANEL  (@Deep12048 only)
# ──────────────────────────────────────────────────────────────

def _is_admin(user_id: int) -> bool:
    """Return True if user_id belongs to the bot admin."""
    return user_id == ADMIN_USER_ID


def _get_today_stats() -> dict:
    """Compute live stats from in-memory user_states. Zero I/O.

    Returns:
        total_users         – all registered users
        active_today        – users who watched ≥1 video since midnight IST
        premium_today       – users who activated premium in the last 12 hours
        premium_active_now  – users whose premium has NOT expired yet
        total_videos_today  – sum of all hourly_count values for today
    """
    now = _now_utc()
    now_ist = now + _IST_OFFSET
    today_midnight_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    today_midnight_utc = today_midnight_ist - _IST_OFFSET

    total_users        = len(_users_cache)
    active_today       = 0
    premium_today      = 0
    premium_active_now = 0
    total_videos_today = 0

    for uid, state in user_states.items():
        # Active today: last_reset == today AND watched ≥1 video
        last_reset = state.get('last_reset')
        count      = state.get('hourly_count', 0)
        if last_reset and last_reset >= today_midnight_utc and count > 0:
            active_today += 1
            total_videos_today += count

        # Premium status
        premium_until = state.get('premium_until')
        if premium_until:
            if now < premium_until:
                premium_active_now += 1
            # Got premium today: activation time = premium_until - PREMIUM_DURATION_HOURS
            activated_at = premium_until - timedelta(hours=PREMIUM_DURATION_HOURS)
            if activated_at >= today_midnight_utc:
                premium_today += 1

    return {
        'total_users':        total_users,
        'active_today':       active_today,
        'premium_today':      premium_today,
        'premium_active_now': premium_active_now,
        'total_videos_today': total_videos_today,
    }


async def admin_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: /stats — show today’s live stats.
    Cooldown-protected: only 1 response per 5-second window, so pressing
    /stats many times rapidly results in exactly 1 message, not many.
    """
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ You are not authorized to use this command.")
        return

    # ── Cooldown: drop all duplicate calls within 5 seconds ──
    global _stats_last_ts
    async with _stats_lock:
        now_ts = _time.monotonic()
        if now_ts - _stats_last_ts < _STATS_COOLDOWN:
            return  # silently drop — a response was already sent moments ago
        _stats_last_ts = now_ts  # claim this 5-second window

    s = _get_today_stats()
    now_ist = (_now_utc() + _IST_OFFSET).strftime('%d %b %Y  %I:%M %p')
    sep = "━" * 28

    text = (
        f"📊 *Admin Stats — Live*\n"
        f"⏰ As of: `{now_ist} IST`\n"
        f"{sep}\n\n"
        f"👥 *Total registered users:* `{s['total_users']}`\n\n"
        f"📅 *Today\u2019s Activity*\n"
        f"  ▶️  Active users (watched video): `{s['active_today']}`\n"
        f"  🎬 Videos watched today: `{s['total_videos_today']}`\n\n"
        f"💎 *Premium*\n"
        f"  ⭐ Activated premium today: `{s['premium_today']}`\n"
        f"  ✅ Currently active premium: `{s['premium_active_now']}`\n"
    )
    await update.message.reply_text(text, parse_mode='Markdown')


async def admin_clean_videos_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: /cleanvideos — delete OVERDUE pending video messages for all users.

    Only deletes messages whose 20-minute window has already expired (i.e. the
    bot was restarted and those timers were lost).  Videos that are still within
    their active 20-minute window are left alone — their scheduled jobs handle them.

    Use this after a bot restart to clean up pre-restart stuck videos.
    """
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ You are not authorized to use this command.")
        return

    status_msg = await update.message.reply_text(
        "🧹 *Cleaning overdue video messages…*\n\n"
        "Deleting only pre-restart videos (overdue ones)…",
        parse_mode='Markdown'
    )

    rows = await _get_all_pending_deletions()
    if not rows:
        await status_msg.edit_text(
            "✅ *Nothing to clean!*\n\n"
            "No pending video deletions found in the database.",
            parse_mode='Markdown'
        )
        return

    now_ts = _now_utc().timestamp()
    deleted = 0
    already_gone = 0
    skipped_fresh = 0

    for (row_id, chat_id, message_id, delete_at) in rows:
        remaining = delete_at - now_ts
        if remaining > 0:
            # Still within the 20-min window — user is actively watching this
            # Don't touch it; the scheduled job will delete it normally
            skipped_fresh += 1
            continue

        # Overdue — 20-min window has passed, delete it now
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            deleted += 1
        except Exception:
            already_gone += 1  # message already gone — still clean the DB row
        await _remove_pending_deletion(chat_id, message_id)
        await asyncio.sleep(0.05)  # 50 ms rate-limit guard

    skip_note = (
        f"\n⏳ Skipped (still active): `{skipped_fresh}` — will auto-delete normally"
        if skipped_fresh > 0 else ""
    )

    await status_msg.edit_text(
        f"✅ *Video Cleanup Complete!*\n\n"
        f"🗑️ Overdue videos deleted: `{deleted}`\n"
        f"👻 Already deleted/gone: `{already_gone}`"
        f"{skip_note}\n\n"
        f"Only expired (pre-restart) videos were removed.",
        parse_mode='Markdown'
    )
    print(f"🧹 [admin_clean_videos] {deleted} deleted, {already_gone} already gone, "
          f"{skipped_fresh} skipped (still fresh)")


async def send_daily_admin_report(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled job: sends the day's stats to admin at 11:58 PM IST every day."""
    s       = _get_today_stats()
    now_ist = (_now_utc() + _IST_OFFSET).strftime('%d %b %Y')
    sep = "━" * 28

    text = (
        f"📊 *Daily Report — {now_ist}*\n"
        f"{sep}\n\n"
        f"👥 *Total registered users:* `{s['total_users']}`\n\n"
        f"📅 *Today\u2019s Activity*\n"
        f"  ▶️  Active users (watched video): `{s['active_today']}`\n"
        f"  🎬 Videos watched today: `{s['total_videos_today']}`\n\n"
        f"💎 *Premium*\n"
        f"  ⭐ Activated premium today: `{s['premium_today']}`\n"
        f"  ✅ Currently active premium: `{s['premium_active_now']}`\n\n"
        f"🔔 _Auto-report sent at 11:58 PM IST_"
    )
    try:
        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=text,
            parse_mode='Markdown'
        )
    except Exception as e:
        print(f"⚠️ Daily admin report failed: {e}")


async def check_expired_premiums_job(context: ContextTypes.DEFAULT_TYPE):
    """Background task to check for newly expired premiums in local state and notify the user."""
    now = _now_utc()
    expired_users = []
    
    for uid, state in list(user_states.items()):
        puntil = state.get("premium_until")
        if puntil and now >= puntil:
            # It just expired!
            expired_users.append(uid)
            
            # Clear local state so we don't notify again
            state["premium_until"] = None
            save_user_states(user_states, _hint_uid=uid)
            _membership_cache.pop(uid, None)

    if not expired_users:
        return

    # Broadcast notification to the user across all registered bots
    from premium_sync import get_all_bot_tokens
    all_tokens = await get_all_bot_tokens()
    
    if not all_tokens and BOT_TOKEN:
        all_tokens = [BOT_TOKEN]

    async with aiohttp.ClientSession() as session:
        for uid in expired_users:
            # Calculate time until next free video reset
            now_ist = now + _IST_OFFSET
            next_reset_ist = (now_ist + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            next_reset = next_reset_ist - _IST_OFFSET
            time_until_reset = next_reset - now
            minutes_until_reset = int(time_until_reset.total_seconds() / 60)
            hours_until_reset = int(time_until_reset.total_seconds() / 3600)
            if hours_until_reset > 0:
                time_msg = f"{hours_until_reset} hour(s) and {minutes_until_reset % 60} minute(s)"
            else:
                time_msg = f"{minutes_until_reset} minute(s)"
            
            # Build plan + ad keyboard
            aro_url, vp_url = await create_shortened_links(uid)
            keyboard = [[InlineKeyboardButton(lbl, callback_data=f'rzp_plan_{k}')]
                        for k, (lbl, *_) in RZP_PLANS.items()]
            if aro_url:
                keyboard.append([InlineKeyboardButton("🔥 AroLinks — Watch Ad & Get Premium", url=aro_url)])
            if vp_url:
                keyboard.append([InlineKeyboardButton("⚡ VPLink — Watch Ad & Get Premium", url=vp_url)])
            if not aro_url and not vp_url:
                keyboard.append([InlineKeyboardButton("🔥▶️ Watch Ad → Get FREE Premium! 🎁", callback_data='watch_ad')])
            
            user_text = (
                "🚫 **Video Limit Reached!**\n\n"
                "You've used your free video. Buy a plan or watch an ad to continue watching ⬇️\n\n"
                f"⏳ Next free video in: **{time_msg}**"
            )
            
            reply_markup_dict = {
                "inline_keyboard": [[{"text": btn.text, "callback_data": btn.callback_data} if btn.callback_data else {"text": btn.text, "url": btn.url}] for btn in row] for row in keyboard
            }

            for tk in all_tokens:
                try:
                    await session.post(
                        f"https://api.telegram.org/bot{tk}/sendMessage",
                        json={
                            "chat_id": uid,
                            "text": user_text,
                            "parse_mode": "Markdown",
                            "reply_markup": reply_markup_dict
                        }
                    )
                except Exception:
                    pass  # User might have blocked the bot


async def admin_video_likes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: /videolikes — show like/dislike stats for all rated videos.

    Reports:
      • Overall totals (total votes, total likes, total dislikes, overall %)
      • Top 5 most-liked videos (highest like %)
      • Bottom 5 least-liked videos (lowest like %)
    Only videos that have received at least one vote are included.
    """
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ You are not authorized to use this command.")
        return

    try:
        async with aiosqlite.connect("user_states.db") as db:
            async with db.execute(
                "SELECT message_id, likes, dislikes FROM video_ratings ORDER BY message_id"
            ) as cur:
                rows = await cur.fetchall()
    except Exception as e:
        await update.message.reply_text(f"❌ Database error: {e}")
        return

    if not rows:
        await update.message.reply_text(
            "📊 *Video Likes Stats*\n\n"
            "No votes have been recorded yet.\n"
            "Users need to like or dislike videos first!",
            parse_mode='Markdown'
        )
        return

    # Filter to only videos with at least 1 vote and compute percentage
    rated = []
    for (message_id, likes, dislikes) in rows:
        total = likes + dislikes
        if total == 0:
            continue
        pct = (likes / total) * 100
        rated.append({'id': message_id, 'likes': likes, 'dislikes': dislikes, 'total': total, 'pct': pct})

    if not rated:
        await update.message.reply_text(
            "📊 *Video Likes Stats*\n\nNo videos have received any votes yet.",
            parse_mode='Markdown'
        )
        return

    # Overall totals
    total_likes    = sum(v['likes']    for v in rated)
    total_dislikes = sum(v['dislikes'] for v in rated)
    total_votes    = total_likes + total_dislikes
    overall_pct    = (total_likes / total_votes * 100) if total_votes > 0 else 0
    rated_count    = len(rated)

    # Sort with tie-breaking:
    #   Top 5    — highest like% first; on tie, more likes wins
    #   Bottom 5 — lowest like% first; on tie, more dislikes wins
    sorted_desc = sorted(rated, key=lambda v: (v['pct'], v['likes']),    reverse=True)
    sorted_asc  = sorted(rated, key=lambda v: (v['pct'], -v['dislikes']))

    top5    = sorted_desc[:5]
    bottom5 = sorted_asc[:5]

    def fmt_row(rank: int, v: dict) -> str:
        bar_filled = int(v['pct'] / 10)   # 0-10 blocks
        bar = '🟩' * bar_filled + '⬛' * (10 - bar_filled)
        return (
            f"  {rank}\\. Video ID `{v['id']}`\n"
            f"     👍 {v['likes']}  👎 {v['dislikes']}  │  {v['pct']:.1f}% liked\n"
            f"     {bar}\n"
        )

    now_ist = (_now_utc() + _IST_OFFSET).strftime('%d %b %Y  %I:%M %p')

    text  = (
        f"📊 *Video Likes & Dislikes Report*\n"
        f"⏰ `{now_ist} IST`\n"
        f"{'━' * 28}\n\n"
        f"🎬 *Videos rated:* `{rated_count}`\n"
        f"👍 *Total likes:* `{total_likes}`\n"
        f"👎 *Total dislikes:* `{total_dislikes}`\n"
        f"🗳️ *Total votes:* `{total_votes}`\n"
        f"📈 *Overall like rate:* `{overall_pct:.1f}%`\n\n"
    )

    text += f"🏆 *Top 5 — Most Liked Videos*\n"
    for i, v in enumerate(top5, 1):
        text += fmt_row(i, v)
    text += "\n"

    text += f"💔 *Bottom 5 — Least Liked Videos*\n"
    for i, v in enumerate(bottom5, 1):
        text += fmt_row(i, v)

    await update.message.reply_text(text, parse_mode='Markdown')


async def admin_help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: /adminhelp — list every available admin command with a description."""
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ You are not authorized to use this command.")
        return

    text = (
        "🛠️ *Admin Command Reference*\n"
        f"{'━' * 28}\n\n"
        "📊 *Statistics & Reports*\n"
        "  /stats — Live user & activity stats\n"
        "  /videolikes — Like/dislike stats for all videos\n"
        "             (overall totals + top 5 & bottom 5)\n\n"
        "📢 *Broadcasts*\n"
        "  /broadcast — Send a message to ALL users\n"
        "  /deletebroadcast — Delete a previously sent broadcast\n\n"
        "🧹 *Maintenance*\n"
        "  /cleanvideos — Delete overdue video messages\n"
        "               (stuck after bot restart)\n\n"
        "👑 *Premium Management*\n"
        "  /checkpremium <uid> — Check if user has active premium\n"
        "  /approve <uid> <days> — Grant premium manually\n"
        "  /approvesec <uid> <sec> — Grant premium for seconds\n"
        "  /revoke <uid> — Remove premium manually\n\n"
        "ℹ️ *Help*\n"
        "  /adminhelp — Show this help message\n\n"
        "_All commands are restricted to the bot admin only._"
    )
    await update.message.reply_text(text, parse_mode='Markdown')


async def admin_broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: /broadcast  — step 1, ask admin to send the message."""
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ You are not authorized to use this command.")
        return ConversationHandler.END

    await update.message.reply_text(
        "📣 *Broadcast Mode*\n\n"
        "Send the message you want to broadcast to ALL users.\n"
        "You can send *text, photo, video, or any media*.\n\n"
        "Send /cancel to abort.",
        parse_mode='Markdown'
    )
    return _BROADCAST_WAITING


async def admin_broadcast_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the broadcast conversation."""
    await update.message.reply_text("❌ Broadcast cancelled.")
    return ConversationHandler.END


async def handle_broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: step 2 — receive the message and forward to all users."""
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END

    message  = update.message
    user_ids = get_all_user_ids()
    total    = len(user_ids)

    if not total:
        await message.reply_text("⚠️ No users in database to broadcast to.")
        return ConversationHandler.END

    # Generate a unique broadcast ID and a human-readable label
    ts         = int(_now_utc().timestamp())
    rnd        = random.randint(1000, 9999)
    bc_id      = f"bc_{ts}_{rnd}"
    bc_label   = (_now_utc() + _IST_OFFSET).strftime('%d %b %I:%M %p')

    status_msg = await message.reply_text(
        f"⏳ Broadcasting *#{bc_label}* to *{total}* users… Please wait.",
        parse_mode='Markdown'
    )

    # Evict oldest entry if history is full
    if len(_broadcast_history) >= _MAX_BROADCAST_HISTORY:
        oldest_key = next(iter(_broadcast_history))
        del _broadcast_history[oldest_key]

    _broadcast_history[bc_id] = {'ids': {}, 'label': bc_label, 'sent': 0}

    sent    = 0
    failed  = 0
    blocked = []

    for uid in user_ids:
        try:
            sent_msg = await context.bot.copy_message(
                chat_id=uid,
                from_chat_id=message.chat_id,
                message_id=message.message_id
            )
            _broadcast_history[bc_id]['ids'][uid] = sent_msg.message_id
            sent += 1
            await asyncio.sleep(0.05)   # 20 msg/sec — safely under Telegram limit
        except Exception as e:
            failed += 1
            err = str(e)
            if "Forbidden" in err or "blocked" in err.lower() or "deactivated" in err.lower():
                blocked.append(uid)

    _broadcast_history[bc_id]['sent'] = sent

    if blocked:
        batch_remove_users(blocked)

    # Each broadcast gets its OWN delete button embedding the broadcast ID
    delete_btn = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑️ Delete This Broadcast", callback_data=f'admin_delete_bc_{bc_id}')]
    ])
    await status_msg.edit_text(
        f"✅ *Broadcast \u2018{bc_label}\u2019 Complete!*\n\n"
        f"  📬 Sent: `{sent}`\n"
        f"  ❌ Failed: `{failed}`\n"
        f"  🔕 Removed blocked: `{len(blocked)}`\n\n"
        f"_Broadcast ID: `{bc_id}`_\n"
        f"Tap below to delete *this* broadcast from all chats.",
        parse_mode='Markdown',
        reply_markup=delete_btn
    )
    return ConversationHandler.END


async def admin_delete_broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: delete a specific broadcast by ID (inline button or /deletebroadcast).

    Inline button (callback_data = 'admin_delete_bc_{bc_id}'):
        Deletes the exact broadcast whose ID is embedded in the button.

    /deletebroadcast command:
        Lists all stored broadcasts with individual Delete buttons so admin
        can pick which one to delete.
    """

    # ── INLINE BUTTON PATH ──
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        if not _is_admin(query.from_user.id):
            await query.answer("⛔ Not authorized.", show_alert=True)
            return

        # Extract broadcast ID from callback data: 'admin_delete_bc_{bc_id}'
        bc_id = query.data.replace('admin_delete_bc_', '', 1)
        bc    = _broadcast_history.get(bc_id)

        if not bc:
            await query.message.edit_text(
                "⚠️ This broadcast is no longer in memory (bot may have restarted)."
            )
            return

        bc_label  = bc['label']
        ids_map   = bc['ids']

        # Update button to show progress
        try:
            await query.message.edit_text(
                f"⏳ Deleting broadcast \u2018*{bc_label}*\u2019 from `{len(ids_map)}` chats…",
                parse_mode='Markdown'
            )
        except Exception:
            pass

        deleted = 0
        failed  = 0
        for uid, msg_id in list(ids_map.items()):
            try:
                await context.bot.delete_message(chat_id=uid, message_id=msg_id)
                deleted += 1
                await asyncio.sleep(0.05)
            except Exception:
                failed += 1

        # Remove from history
        _broadcast_history.pop(bc_id, None)

        try:
            await query.message.edit_text(
                f"✅ *Broadcast \u2018{bc_label}\u2019 Deleted!*\n\n"
                f"  🗑️ Deleted from: `{deleted}` chats\n"
                f"  ⚠️ Could not delete: `{failed}` (already gone or expired)",
                parse_mode='Markdown'
            )
        except Exception:
            pass
        return

    # ── COMMAND PATH: /deletebroadcast ──
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ You are not authorized to use this command.")
        return

    if not _broadcast_history:
        await update.message.reply_text(
            "⚠️ No broadcasts in history.\n"
            "Send a broadcast first using /broadcast."
        )
        return

    # Build a list showing every stored broadcast with its own Delete button
    text = "🗑️ *Select a broadcast to delete:*\n\n"
    keyboard = []
    for bc_id, bc in list(_broadcast_history.items()):
        label = bc.get('label', bc_id)
        sent  = bc.get('sent', len(bc['ids']))
        text += f"  • *{label}* — {sent} users\n"
        keyboard.append([
            InlineKeyboardButton(
                f"🗑️ Delete \u2018{label}\u2019",
                callback_data=f'admin_delete_bc_{bc_id}'
            )
        ])

    await update.message.reply_text(
        text,
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ──────────────────────────────────────────────────────────────


def main():
    """Start the bot"""
    print("🤖 Starting Videos Bot System...")
    print("=" * 50)
    
    # Clean up expired data on startup
    print("\n🧹 Cleaning up expired data...")
    cleanup_expired_data(user_states)
    
    # Display statistics
    stats = get_stats(user_states)
    print(f"\n📊 User Statistics:")
    print(f"   Total users: {stats['total_users']}")
    print(f"   💎 Premium users: {stats['premium_users']}")
    print(f"   🆓 Free users: {stats['free_users']}")
    print(f"   🎬 Total videos watched: {stats['total_videos_watched']}")
    print(f"   📈 Avg videos per user: {stats['avg_videos_per_user']:.1f}")
    print()
    
    # Start the admin bot in a separate process (Windows only)
    # On Linux, use start_bots.sh script instead
    admin_bot_process = None
    if sys.platform == "win32":
        try:
            print("📱 [1/2] Starting Admin Support Bot...")
            admin_bot_path = os.path.join(os.path.dirname(__file__), "admin_bot.py")
            
            admin_bot_process = subprocess.Popen(
                [sys.executable, admin_bot_path],
                creationflags=subprocess.CREATE_NEW_CONSOLE
            )
            
            print("✅ Admin Bot started successfully!")
            print(f"   Bot: @videos69Admin_Bot")
            print(f"   Admin: ")
            print()
        except Exception as e:
            print(f"⚠️ Warning: Could not start admin bot: {e}")
            print("   You can start it manually: python admin_bot.py")
            print()
    else:
        print("ℹ️ On Linux, use './start_bots.sh' to start both bots")
        print()
    
    print("🎬 [2/2] Starting Main Videos Bot...")
    print("🌐 Connecting to Telegram API...")
    print()
    
    try:
        # Create the Application with custom request settings
        application = (
            Application.builder()
            .token(BOT_TOKEN)
            .request(request)
            .post_init(post_init)
            .post_shutdown(post_shutdown)
            .build()
        )
        
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("buy", buy_command))

        # ── Admin commands (@Deep12048 only) ──
        application.add_handler(CommandHandler("stats", admin_stats_command))
        application.add_handler(CommandHandler("deletebroadcast", admin_delete_broadcast_command))
        application.add_handler(CommandHandler("cleanvideos", admin_clean_videos_command))
        application.add_handler(CommandHandler("videolikes", admin_video_likes_command))
        application.add_handler(CommandHandler("adminhelp", admin_help_command))
        application.add_handler(CommandHandler("payments", admin_payments_command))
        application.add_handler(CommandHandler("revoke", admin_revoke_command))
        application.add_handler(CommandHandler("approve", admin_approve_command))
        application.add_handler(CommandHandler("approvesec", admin_approve_sec_command))
        application.add_handler(CommandHandler("checkpremium", admin_checkpremium_command))

        # Broadcast ConversationHandler in group=-1 (highest priority).
        # MUST be group=-1, not group=0. Reason:
        # When admin is in _BROADCAST_WAITING state and sends a command like /stats,
        # the ConversationHandler in group=0 would consume the update first, find no
        # matching state handler (due to ~filters.COMMAND), then let it fall through
        # to the SAME group=0 where CommandHandler("stats") ALSO fires — causing
        # duplicate responses. group=-1 gives it exclusive ownership of updates
        # it handles, and other handlers in group=0 process the rest independently.
        broadcast_conv = ConversationHandler(
            entry_points=[CommandHandler("broadcast", admin_broadcast_command)],
            states={
                _BROADCAST_WAITING: [
                    MessageHandler(
                        filters.ALL & ~filters.COMMAND & filters.User(user_id=ADMIN_USER_ID),
                        handle_broadcast_message
                    )
                ]
            },
            fallbacks=[CommandHandler("cancel", admin_broadcast_cancel)],
            per_user=True,
        )
        application.add_handler(broadcast_conv, group=-1)

        # Add callback query handler for buttons (handles both user and admin callbacks)
        # Admin delete-broadcast buttons: pattern matches 'admin_delete_bc_{any_id}'
        application.add_handler(CallbackQueryHandler(
            admin_delete_broadcast_command, pattern=r'^admin_delete_bc_'
        ))
        application.add_handler(CallbackQueryHandler(button_callback))

        # Add message handler for persistent menu buttons
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages))

        # Real-time membership enforcement:
        # When a user leaves or is kicked from a mandatory channel, Telegram sends
        # a chat_member update. We immediately evict their cache entry so the
        # next video request triggers a fresh API check and blocks them instantly.
        application.add_handler(ChatMemberHandler(
            handle_chat_member_update,
            ChatMemberHandler.CHAT_MEMBER
        ))
        # Schedule broadcast every 24 hours at midnight IST
        # Calculate seconds until next midnight IST for first broadcast
        now_utc = _now_utc()
        now_ist = now_utc + _IST_OFFSET
        next_broadcast_ist = (now_ist + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        next_broadcast_utc = next_broadcast_ist - _IST_OFFSET
        
        # Calculate seconds until next midnight
        seconds_until_next_broadcast = (next_broadcast_utc - now_utc).total_seconds()
        
        # Schedule for every 24 hours
        job_queue = application.job_queue

        # Run every 24 hours at midnight (00:00 IST)
        job_queue.run_repeating(
            broadcast_daily_reset,
            interval=86400,
            first=seconds_until_next_broadcast,
            name='daily_broadcast'
        )

        # ── Daily admin stats report at 11:58 PM IST ──
        # Calculate seconds until first 23:58 IST
        next_report_ist = now_ist.replace(hour=23, minute=58, second=0, microsecond=0)
        if now_ist >= next_report_ist:
            # Already past 11:58 PM today — schedule for tomorrow
            next_report_ist += timedelta(days=1)
        next_report_utc = next_report_ist - _IST_OFFSET
        seconds_until_report = (next_report_utc - now_utc).total_seconds()

        job_queue.run_repeating(
            send_daily_admin_report,
            interval=86400,          # repeat every 24 hours
            first=seconds_until_report,
            name='daily_admin_report'
        )
        print(f"📊 Daily admin report scheduled at 11:58 PM IST")
        print(f"   First report in: {int(seconds_until_report / 3600)}h {int((seconds_until_report % 3600) / 60)}m")

        # ── Premium expiration checker ──
        job_queue.run_repeating(
            check_expired_premiums_job,
            interval=60,             # check every 60 seconds
            name='premium_expiry_check'
        )
        print(f"💎 Premium expiration checker scheduled (runs every 60 seconds)")
        
        print(f"📢 Daily broadcast scheduler enabled!")
        print(f"   Current time: {now_ist.strftime('%I:%M %p')} IST")
        print(f"   Next broadcast: {next_broadcast_ist.strftime('%I:%M %p')} IST")
        print(f"   Waiting: {int(seconds_until_next_broadcast / 60)} minutes {int(seconds_until_next_broadcast % 60)} seconds")
        print(f"   Then every 24 hours at midnight (12:00 AM)")
        print()
        
        # Start the bot
        print("=" * 50)
        print("✅ BOTH BOTS ARE RUNNING!")
        print("=" * 50)
        print()
        print("📱 Main Bot: @Videos1_69_bot")
        print("   Purpose: Video distribution")
        print(f"   Channel ID: {CHANNEL_ID}")
        print("   Features: Videos, Premium, Daily Limits")
        print()
        print("💬 Admin Bot: @videos69Admin_Bot")
        print("   Purpose: User support")
        print("   Admin: ")
        print("   Features: Message forwarding, Admin replies")
        print()
        print("🎲 Videos will be sent randomly from the channel!")
        print("📞 Users can contact admin via /help command")
        print()
        
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            poll_interval=1.0,    # Poll every 1s — still fast; 0.5 doubles API calls for no benefit
            timeout=30,           # Long-poll timeout (seconds)
            drop_pending_updates=True,
        )
        
    except Exception as e:
        print(f"\n❌ Error starting bot: {e}")
        print("\n🔧 Troubleshooting tips:")
        print("1. Check your internet connection")
        print("2. If you're in a region where Telegram is blocked, try using a VPN")
        print("3. Verify your BOT_TOKEN in the .env file is correct")
        print("4. Make sure you have the latest version of python-telegram-bot installed")
        print("5. Try running: pip install --upgrade python-telegram-bot")
        print("\n💡 If the error is 'Timed out', it means the bot cannot reach Telegram servers.")
        print("   This is usually a network/firewall/proxy issue, not a code issue.")


if __name__ == '__main__':
    main()

