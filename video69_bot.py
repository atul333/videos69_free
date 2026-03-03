import os
import random
import json
import subprocess
import sys
import time as _time
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, ChatMemberHandler, filters
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

# Load environment variables
load_dotenv()

BOT_TOKEN = os.getenv('BOT_TOKEN')
CHANNEL_USERNAME = os.getenv('CHANNEL_USERNAME')
CHANNEL_ID = int(os.getenv('CHANNEL_ID', '-1003067861005'))  # Private channel ID from .env

# Use channel ID for private channels, username for public channels
CHANNEL_IDENTIFIER = CHANNEL_ID if CHANNEL_ID else f"@{CHANNEL_USERNAME}"

# Admin configuration for support system
ADMIN_USER_ID = 7708376300  # Admin's Telegram user ID (https://t.me/Deep12048)

# Configure request — tuned for t4g.medium (2 ARM vCPU, 4 GB RAM)
request = HTTPXRequest(
    connection_pool_size=4,   # t4g ARM: 4 connections is optimal for 2 vCPUs
    connect_timeout=15.0,
    read_timeout=15.0,
    write_timeout=15.0,
    pool_timeout=5.0
)

# 8 concurrent handlers — Telegram bottlenecks at 30 msg/sec anyway;
# more coroutines = more memory, zero throughput gain
MAX_CONCURRENT_REQUESTS = 8
request_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)


# Load user states from persistent storage (replaces in-memory dictionary)
# Structure: {user_id: {'seen_videos': [], 'hourly_count': 0, 'last_reset': datetime, 'premium_until': datetime}}
user_states = load_user_states()

# Track used tokens to prevent link reuse (bounded to prevent unbounded memory growth)
used_tokens: set = set()
_MAX_USED_TOKENS = 10000  # Evict oldest when this limit is reached

# Video limit for free users (resets every 24 hours)
DAILY_VIDEO_LIMIT = 3  # Free users can watch 3 videos per day
# Premium access duration after watching ad (12 hours)
PREMIUM_DURATION_HOURS = 12
# Number of videos that can be downloaded/saved by normal users (every 24 hours)
DOWNLOADABLE_VIDEOS_LIMIT = 2
# Number of videos that can be downloaded by premium users (every 24 hours)
PREMIUM_DOWNLOADABLE_VIDEOS_LIMIT = 20

# VPLinks API Configuration
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

# Mandatory channels — users must join BOTH to use the bot
MANDATORY_CHANNEL        = -1003827947228           # Channel 1: Backup Videos69
MANDATORY_CHANNEL_LINK   = "https://t.me/+8MA7MsUSTD0zZDMx"
MANDATORY_CHANNEL_2      = -1003756311531           # Channel 2: Backup Videos69
MANDATORY_CHANNEL_2_LINK = "https://t.me/+FNojYwJtXSk2MGRl"

# ── Bot users registry (persisted in user_states.db, 'bot_users' table) ──────
# All reads come from _users_cache (instant, no I/O).
# Writes mark the user dirty; the shared flush loop in user_state_storage.py
# Writes mark the user dirty; _bot_users_flush_loop writes to SQLite every 30 seconds.
OLD_USERS_JSON = "users.json"      # migrated automatically if still present

_users_cache: dict[int, dict] = {}  # {user_id: {first_name, username, ...}}
_users_dirty: set[int] = set()      # user_ids needing a DB write


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
    """Return True if user has active premium. Uses cached _now_utc() — zero syscall."""
    if user_id not in user_states:
        init_user_state(user_id)
    premium_until = user_states[user_id].get('premium_until')
    return bool(premium_until) and _now_utc() < premium_until


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
    """Grant premium access for 12 hours after watching ad."""
    if user_id not in user_states:
        init_user_state(user_id)
    user_states[user_id]['premium_until'] = (
        _now_utc() + timedelta(hours=PREMIUM_DURATION_HOURS)
    )
    # Invalidate membership cache so next check re-evaluates premium state
    _membership_cache.pop(user_id, None)
    save_user_states(user_states, _hint_uid=user_id)


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


async def _shorten_via_vplinks(bot_deep_link: str, alias: str, user_id: int) -> str | None:
    """Try to shorten a link via VPLinks. Returns shortened URL or None."""
    api_url = f"{VPLINK_API_URL}?api={VPLINK_API_TOKEN}&url={bot_deep_link}&alias={alias}"
    try:
        session = await _get_http_session()
        async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=10)) as response:
            if response.status == 200:
                result = await response.json(content_type=None)
                if result.get("status") == "success":
                    shortened_url = result.get("shortenedUrl", "").strip()
                    print(f"✅ [VPLinks] Shortened link for user {user_id}: {shortened_url}")
                    return shortened_url
                else:
                    print(f"❌ [VPLinks] API error: {result.get('message', 'Unknown error')}")
            else:
                print(f"❌ [VPLinks] HTTP {response.status}")
    except Exception as e:
        print(f"❌ [VPLinks] Exception: {e}")
    return None


async def create_shortened_link(user_id):
    """Create a unique shortened link via VPLinks that opens the bot with encrypted token."""
    if user_id not in user_states:
        init_user_state(user_id)

    # Generate a unique verification token
    timestamp = int(_now_utc().timestamp())  # reuse cached now
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

    shortened_url = await _shorten_via_vplinks(bot_deep_link, alias, user_id)

    if shortened_url:
        user_states[user_id]['ad_link'] = shortened_url
        save_user_states(user_states, _hint_uid=user_id)
        return shortened_url

    print(f"❌ VPLinks failed for user {user_id}")
    return None



async def delete_messages(context: ContextTypes.DEFAULT_TYPE):
    """Delete messages after 20 minutes"""
    job_data = context.job.data
    chat_id = job_data['chat_id']
    message_ids = job_data['message_ids']
    
    # Delete the video and warning messages
    for message_id in message_ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            pass  # Silently ignore — message may already be deleted
    
    # No notification message - keep the chat clean!


async def broadcast_daily_reset(context: ContextTypes.DEFAULT_TYPE):
    """Broadcast message to all users about limit reset - sent every 24 hours at midnight"""
    
    print("\n" + "=" * 60)
    print("🔔 DAILY RESET BROADCAST TRIGGERED!")
    print("=" * 60)
    
    # Get current time in IST — use module-level cache and constant
    current_time_ist = _now_utc() + _IST_OFFSET
    time_str = current_time_ist.strftime('%I:00 %p')  # e.g., "12:00 AM"
    date_str = current_time_ist.strftime('%B %d, %Y')  # e.g., "February 6, 2026"
    
    print(f"⏰ Broadcasting at: {time_str} IST")
    print(f"📅 Date: {date_str}")
    
    # Create "Watch Now" button
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
    
    # Load user IDs from file (persistent storage)
    user_ids = get_all_user_ids()
    
    # Send to all users
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
            await asyncio.sleep(0.05)  # 50ms delay — Telegram allows ~30 msg/sec per token

            
        except Exception as e:
            failed += 1
            
            # Track blocked users to remove from file
            if "Forbidden" in str(e) or "blocked" in str(e).lower():
                blocked_users.append(user_id)
    
    # Remove blocked users from file (batch operation - very efficient)
    if blocked_users:
        batch_remove_users(blocked_users)
    
    print(f"📢 Daily reset broadcast complete! Sent to {successful} users, {failed} failed")
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
        
        # Compute is_premium ONCE — no double call into can_watch_video
        is_premium = is_premium_user(user_id)
        if not is_premium:
            check_and_reset_daily_limit(user_id)  # reset counter if new day
        can_watch = is_premium or user_states[user_id]['hourly_count'] < DAILY_VIDEO_LIMIT

        if not can_watch:
            # Check if user had premium that expired
            if not is_premium and user_states.get(user_id, {}).get('premium_until'):
                premium_until = user_states[user_id]['premium_until']
                now = _now_utc()
                if now >= premium_until:
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
                    # Generate shortened link upfront — 1-click direct ad access
                    shortened_link = await create_shortened_link(user_id)
                    if shortened_link:
                        keyboard = [[InlineKeyboardButton("🔥▶️ Watch Ad → Get FREE Premium! 🎁", url=shortened_link)]]
                    else:
                        keyboard = [[InlineKeyboardButton("🔥▶️ Watch Ad → Get FREE Premium! 🎁", callback_data='watch_ad')]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"⏰ **Premium Access Expired!**\n\n"
                             f"Your 12-hour premium has ended. To keep watching unlimited videos, you need to watch **1 short ad**.\n\n"
                             f"━━━━━━━━━━━━━━━━━━━━\n"
                             f"🎯 **HOW TO UNLOCK PREMIUM:**\n"
                             f"━━━━━━━━━━━━━━━━━━━━\n"
                             f"🔥 **Tap the button below** to watch the ad\n"
                             f"✅ **Premium unlocks instantly** after watching!\n\n"
                             f"🎁 **What you get:**\n"
                             f"• 🔓 Unlimited videos for **12 hours**\n"
                             f"• 📥 **20 downloads** (instead of 2)\n\n"
                             f"⏳ Or wait **{time_msg}** to get **3 free videos** again at midnight.\n\n"
                             f"⬇️ One tap → Instant premium access:",
                        parse_mode='Markdown',
                        reply_markup=reply_markup
                    )
                    return

            # Regular daily limit reached
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
            # Generate shortened link upfront — 1-click direct ad access
            shortened_link = await create_shortened_link(user_id)
            if shortened_link:
                keyboard = [[InlineKeyboardButton("🔥▶️ Watch Ad → Get FREE Premium! 🎁", url=shortened_link)]]
            else:
                keyboard = [[InlineKeyboardButton("🔥▶️ Watch Ad → Get FREE Premium! 🎁", callback_data='watch_ad')]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🚫 **You've Watched All 3 Free Videos!**\n\n"
                     f"To continue watching, you must **watch 1 short ad** to unlock Premium Access.\n\n"
                     f"━━━━━━━━━━━━━━━━━━━━\n"
                     f"🎯 **HOW TO UNLOCK PREMIUM:**\n"
                     f"━━━━━━━━━━━━━━━━━━━━\n"
                     f"🔥 **Tap the button below** to watch the ad\n"
                     f"✅ **Premium unlocks instantly** after watching!\n\n"
                     f"🎁 **What you get:**\n"
                     f"• 🔓 Unlimited videos for **12 hours**\n"
                     f"• 📥 **20 downloads** (instead of 2)\n\n"
                     f"⏳ Or wait **{time_msg}** to get **3 free videos** again at midnight.\n\n"
                     f"⬇️ One tap → Instant premium access:",
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
            return
        
        # Try to find a valid video (try up to 10 random message IDs)
        max_attempts = 10
        video_sent = False
        
        for attempt in range(max_attempts):
            try:
                message_id = get_random_video(user_id)

                # is_premium already computed above — reuse it, no extra syscall
                downloaded_count = user_states[user_id].get('downloaded_count', 0)
                download_limit = PREMIUM_DOWNLOADABLE_VIDEOS_LIMIT if is_premium else DOWNLOADABLE_VIDEOS_LIMIT
                can_show_download_button = downloaded_count < download_limit
                
                # Always send videos with protection enabled initially
                # Users must click the Download button to get an unprotected copy
                video_msg = await context.bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=CHANNEL_IDENTIFIER,
                    message_id=message_id,
                    protect_content=True  # Always protected initially
                )
                
                # If we got here, the message was sent successfully
                # Mark video as seen
                mark_video_as_seen(user_id, message_id)
                
                # Increment daily video count (for non-premium users)
                if not is_premium:
                    increment_video_count(user_id)
                
                # Create buttons based on download availability and premium status
                keyboard = []
                
                # Add Download button if user has downloads remaining
                if can_show_download_button:
                    remaining = download_limit - downloaded_count
                    # Store video info for download callback: format is "download_channelMsgId"
                    download_callback = f"download_{message_id}"
                    keyboard.append([InlineKeyboardButton(f"📥 Download ({remaining} left)", callback_data=download_callback)])
                else:
                    # Download limit reached - add Watch Ad button for free users
                    if not is_premium:
                        keyboard.append([InlineKeyboardButton("�▶️ Watch Ad → Get FREE Premium! 🎁", callback_data='watch_ad')])
                
                # Always add Next button
                keyboard.append([InlineKeyboardButton("▶️ Next", callback_data='next_video')])
                
                # Add "Unlock Premium" button for non-premium users (always shown)
                if not is_premium:
                    keyboard.append([InlineKeyboardButton("🔓 Unlock Premium", callback_data='unlock_premium')])
                
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                # Send message with appropriate text
                if can_show_download_button:
                    remaining = download_limit - downloaded_count
                    if is_premium:
                        warning_text = (
                            f"👆 Enjoy the video!\n\n"
                            f"💎 Premium User Benefits:\n"
                            f"📥 Click 'Download' button to get a downloadable copy\n"
                            f"📥 Downloads remaining: {remaining}/{download_limit}\n\n"
                            f"⚠️ This video will be deleted after 20 minutes."
                        )
                    else:
                        warning_text = (
                            f"👆 Enjoy the video!\n\n"
                            f"💾 Click 'Download' button to get a downloadable copy\n"
                            f"📥 Downloads remaining: {remaining}\n\n"
                            f"⚠️ This video will be deleted after 20 minutes."
                        )
                else:
                    # Different messages for premium vs free users when limit reached
                    if is_premium:
                        warning_text = (
                            f"👆 Enjoy the video!\n\n"
                            f"🔒 Download limit reached ({download_limit} videos).\n"
                            f"⚠️ This video will be deleted after 20 minutes."
                        )
                    else:
                        warning_text = (
                            f"👆 Enjoy the video!\n\n"
                            f"🔒 Download limit reached ({download_limit} videos).\n\n"
                            f"💎 **Want 20 downloads per day?**\n"
                            f"Watch an ad for Premium Access:\n"
                            f"✅ Unlimited videos + 20 downloads!\n\n"
                            f"⚠️ This video will be deleted after 20 minutes."
                        )
                
                warning_msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=warning_text,
                    reply_markup=reply_markup
                )
                
                # Schedule deletion of both video and warning message after 20 minutes (1200 seconds)
                context.job_queue.run_once(
                    delete_messages,
                    when=1200,  # 20 minutes in seconds
                    data={'chat_id': chat_id, 'message_ids': [video_msg.message_id, warning_msg.message_id]},
                    name=f"delete_{chat_id}_{video_msg.message_id}"
                )
                
                video_sent = True
                break  # Success
                
            except Exception:
                # Message ID doesn't exist or isn't a video — mark failed and retry
                failed_ids = user_states[user_id].setdefault('failed_message_ids', set())
                if message_id not in failed_ids:     # O(1) set check
                    failed_ids.add(message_id)       # O(1) set insert
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
                        # Valid token! Grant premium access
                        grant_premium_access(user_id)
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
    
    # Check if user already has premium
    if is_premium_user(user_id):
        premium_until = user_states[user_id]['premium_until']
        time_remaining = premium_until - _now_utc()  # use cached now
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

🎥 You can watch **3 free videos per day**!
⏰ Your limit resets every **24 hours at midnight IST**

💾 You can **download 2 videos per day**!

💎 Want unlimited access? Watch an ad to get **12 hours of premium**!

Enjoy! 🍿
    """
    
    # Create \"Get First Video\" inline button
    inline_keyboard = [[InlineKeyboardButton("🎬 Get First Video", callback_data='next_video')]]
    inline_markup = InlineKeyboardMarkup(inline_keyboard)
    
    # Send welcome message and clear any old persistent menus
    await update.message.reply_text(
        welcome_message,
        parse_mode='Markdown',
        reply_markup=_REMOVE_KEYBOARD
    )
    
    # Send inline button separately
    await update.message.reply_text(
        "👇 Click below to get your first video:",
        reply_markup=inline_markup
    )



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
    try:
        await query.answer()
    except Exception:
        pass  # Silently ignore expired/old queries
    
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
                    f"🎥 You can watch **3 free videos** per day!\n"
                    f"📥 You can download **2 videos** per day!\n"
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
    
    elif query.data == 'next_video':
        # Use rate-limited version to prevent overload
        await rate_limited_send_video(
            update,
            context,
            chat_id=query.message.chat_id,
            user_id=query.from_user.id
        )
    
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
        # Create a unique shortened link for this user
        user_id = query.from_user.id
        shortened_link = await create_shortened_link(user_id)
        
        if shortened_link:
            # Show ad watching instructions with the shortened link
            keyboard = [
                [InlineKeyboardButton("📺 Click Here to Watch Ad", url=shortened_link)],
                [InlineKeyboardButton("❓ How to Open Link", callback_data='how_to_open')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="📺 **Watch Ad to Get Premium Access**\n\n"
                     "🎯 **Instructions**:\n"
                     "1. Click the 'Click Here to Watch Ad' button below\n"
                     "2. Complete the ad verification process\n"
                     "3. You'll be automatically redirected back to the bot\n"
                     "4. **Premium will activate automatically!**\n\n"
                     "⚠️ **iOS Users**: Copy the link and open it in Chrome browser\n\n"
                     "💎 You'll get **12 hours of unlimited video access**!",
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
        else:
            # Error creating link
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
            
            # Initialize downloaded_count if it doesn't exist
            if 'downloaded_count' not in user_states[user_id]:
                user_states[user_id]['downloaded_count'] = 0
            
            downloaded_count = user_states[user_id]['downloaded_count']
            
            # Determine download limit based on premium status
            is_premium = is_premium_user(user_id)
            download_limit = PREMIUM_DOWNLOADABLE_VIDEOS_LIMIT if is_premium else DOWNLOADABLE_VIDEOS_LIMIT
            
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
                    # Free user reached their 2 download limit - show premium promotion
                    keyboard = [[InlineKeyboardButton("📺 Watch Ad for Premium", callback_data='watch_ad')]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    await context.bot.send_message(
                        chat_id=query.message.chat_id,
                        text="❌ **Download Limit Reached!**\n\n"
                             f"You've already downloaded {download_limit} videos today.\n\n"
                             "💎 **Want More Downloads?**\n"
                             "Watch an ad to get Premium Access:\n\n"
                             "✅ **Unlimited** video watching\n"
                             "✅ **20 downloads** per day (10x more!)\n"
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
                
                # Increment download count
                user_states[user_id]['downloaded_count'] += 1
                save_user_states(user_states, _hint_uid=user_id)
                
                remaining = download_limit - user_states[user_id]['downloaded_count']
                
                # Update the original message buttons
                keyboard = []
                
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
                
                # Try to update the original message
                try:
                    await query.message.edit_text(
                        text=new_text,
                        reply_markup=reply_markup
                    )
                except Exception as e:
                    if "Message is not modified" not in str(e):
                        print(f"⚠️ Error updating message: {e}")
                
                # Send confirmation message with deletion warning
                if remaining > 0:
                    if is_premium:
                        confirmation_msg = await context.bot.send_message(
                            chat_id=query.message.chat_id,
                            text=f"✅ **Download Successful!**\n\n"
                                 f"The video above can be saved/forwarded.\n"
                                 f"💎 Premium: You have {remaining}/{download_limit} download(s) remaining.\n\n"
                                 f"⚠️ This video will be deleted after 20 minutes."
                        )
                    else:
                        confirmation_msg = await context.bot.send_message(
                            chat_id=query.message.chat_id,
                            text=f"✅ **Download Successful!**\n\n"
                                 f"The video above can be saved/forwarded.\n"
                                 f"📥 You have {remaining} download(s) remaining.\n\n"
                                 f"⚠️ This video will be deleted after 20 minutes."
                        )
                else:
                    confirmation_msg = await context.bot.send_message(
                        chat_id=query.message.chat_id,
                        text=f"✅ **Download Successful!**\n\n"
                             f"The video above can be saved/forwarded.\n"
                             f"🔒 This was your last download. Future videos will be protected.\n\n"
                             f"⚠️ This video will be deleted after 20 minutes."
                    )
                
                # Schedule deletion of downloaded video and confirmation message after 20 minutes (1200 seconds)
                context.job_queue.run_once(
                    delete_messages,
                    when=1200,  # 20 minutes in seconds
                    data={'chat_id': query.message.chat_id, 'message_ids': [download_msg.message_id, confirmation_msg.message_id]},
                    name=f"delete_download_{query.message.chat_id}_{download_msg.message_id}"
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



async def post_init(application: Application):
    """Initialize bot after startup"""
    # 1. Init SQLite DB for user_states and start flush loop
    await init_db()
    start_flush_loop()

    # 2. Init bot_users table in the same DB and load users into cache
    async with aiosqlite.connect("user_states.db") as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await _init_users_table(db)
        await _load_users_from_db(db)
        # Migrate users.json if it exists and bot_users table is empty
        if not _users_cache and os.path.exists(OLD_USERS_JSON):
            await _migrate_users_json(db)

    print(f"👥 {len(_users_cache)} registered users loaded from DB")

    # 3. Load available video IDs from the channel
    await load_videos_from_channel(application)

    # 4. Start bot_users background flush loop
    global _bot_users_flush_task
    _bot_users_flush_task = asyncio.create_task(
        _bot_users_flush_loop(), name="bot_users_flush"
    )

async def post_shutdown(application: Application):
    """Clean shutdown: flush all dirty writes and close all DB/HTTP connections."""
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

        # Add callback query handler for buttons
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
            interval=86400,  # Every 24 hours in seconds (24 * 3600)
            first=seconds_until_next_broadcast,  # Wait until next midnight
            name='daily_broadcast'
        )
        
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

