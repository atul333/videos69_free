import os
import random
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest
import aiohttp
import hashlib
import aiosqlite

# Import persistent user state storage (SQLite-backed)
from user_state_storage import (
    load_user_states,
    save_user_states,
    save_user_state,
    init_db,
    start_flush_loop,
    stop_flush_loop,
    init_user_state_data,
    cleanup_expired_data,
    get_premium_users_count,
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

# Configure request with optimized settings for a Telegram bot
# Keep pool small — large pools consume OS resources and cause CPU overhead
request = HTTPXRequest(
    connection_pool_size=8,   # 8 is plenty for a single-bot setup
    connect_timeout=15.0,
    read_timeout=15.0,
    write_timeout=15.0,
    pool_timeout=5.0
)

# Store video message IDs
video_messages = []

# Rate limiting: keep low so the event loop stays responsive
from asyncio import Semaphore
MAX_CONCURRENT_REQUESTS = 8   # Only 8 handlers at once; more = CPU contention
request_semaphore = Semaphore(MAX_CONCURRENT_REQUESTS)


# Load user states from persistent storage (replaces in-memory dictionary)
# Structure: {user_id: {'seen_videos': [], 'hourly_count': 0, 'last_reset': datetime, 'premium_until': datetime}}
user_states = load_user_states()

# Track used tokens to prevent link reuse (this can be in-memory as tokens are single-use)
used_tokens = set()

# Track all users who have started the bot (for broadcasting)
all_users = set()

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
# AroLinks API Configuration
AROLINK_API_TOKEN = "cf0c76a4739d65e08d5990b0db1865b680f1de4a"
AROLINK_API_URL = "https://arolinks.com/api"
# Round-robin counter: 0 = VPLinks next, 1 = AroLinks next
_link_service_counter = 0
# Bot username for deep links (from .env)
BOT_USERNAME = os.getenv('BOT_USERNAME', 'Test_videos69_bot')
# Secret key for encrypting verification tokens
SECRET_KEY = "your_secret_key_here_change_this"  # Change this to a random secret

# Message ID range configuration (from .env)
MIN_MESSAGE_ID = 1
MAX_MESSAGE_ID = int(os.getenv('MAX_MESSAGE_ID', '10'))  # Maximum message ID in your channel

# Mandatory channel to join (forced subscription)
# For private channels, you need to use the numeric chat ID (get it from @userinfobot)
# For public channels, use @channelname
MANDATORY_CHANNEL = -1003827947228  # Backup Videos69
MANDATORY_CHANNEL_LINK = "https://t.me/+8MA7MsUSTD0zZDMx"

# ── Bot users registry (persisted in user_states.db, 'bot_users' table) ──────
# All reads come from _users_cache (instant, no I/O).
# Writes mark the user dirty; the shared flush loop in user_state_storage.py
# writes dirty users to SQLite every 10 seconds.
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
                "joined_date": u.get("joined_date", datetime.now(timezone.utc).isoformat()),
                "last_seen":   u.get("last_seen",   datetime.now(timezone.utc).isoformat()),
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


async def _flush_users_to_db():
    """Write dirty users to bot_users table (called by flush loop)."""
    if not _users_dirty:
        return
    to_save = list(_users_dirty)
    _users_dirty.clear()
    try:
        async with aiosqlite.connect("user_states.db") as db:
            await db.execute("PRAGMA journal_mode=WAL")
            for uid in to_save:
                if uid not in _users_cache:
                    continue
                u = _users_cache[uid]
                await db.execute("""
                    INSERT INTO bot_users
                        (user_id, first_name, username, joined_date, last_seen)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        first_name  = excluded.first_name,
                        username    = excluded.username,
                        last_seen   = excluded.last_seen
                """, (uid, u["first_name"], u["username"],
                      u["joined_date"], u["last_seen"]))
            await db.commit()
    except Exception as e:
        _users_dirty.update(to_save)
        print(f"⚠️ users DB flush error: {e}")


# ── Public functions (same signatures as before) ─────────────────────────────
def load_users_from_file():
    """Return in-memory cache (populated at startup by init_db)."""
    return list(_users_cache.values())


def save_users_to_file(users_data):
    """
    Legacy compatibility shim — marks all provided users dirty.
    Prefer add_user_to_file() for single-user updates.
    """
    for u in users_data:
        uid = u.get("user_id")
        if uid:
            _users_cache[uid] = u
            _users_dirty.add(uid)
    # Schedule async flush (non-blocking)
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_flush_users_to_db())
    except RuntimeError:
        pass   # No running loop yet (startup) — init_db handles the flush


def add_user_to_file(user_id: int, first_name: str, username=None) -> bool:
    """Add or update a user in cache + mark dirty for DB write (instant return)."""
    now_iso = datetime.now(timezone.utc).isoformat()

    if user_id in _users_cache:
        u = _users_cache[user_id]
        # Only update (and mark dirty) if something actually changed
        if u["first_name"] != first_name or u.get("username") != username:
            u["first_name"] = first_name
            u["username"]   = username
            u["last_seen"]  = now_iso
            _users_dirty.add(user_id)
            # Schedule async flush
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_flush_users_to_db())
            except RuntimeError:
                pass
        return False  # Existing user

    # New user
    _users_cache[user_id] = {
        "user_id":     user_id,
        "first_name":  first_name,
        "username":    username,
        "joined_date": now_iso,
        "last_seen":   now_iso,
    }
    _users_dirty.add(user_id)
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_flush_users_to_db())
    except RuntimeError:
        pass
    print(f"✅ New user: {first_name} (ID: {user_id})")
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
        try:
            async with aiosqlite.connect("user_states.db") as db:
                await db.executemany(
                    "DELETE FROM bot_users WHERE user_id = ?",
                    [(uid,) for uid in remove_set]
                )
                await db.commit()
            print(f"🗑️ Removed {len(remove_set)} users from DB")
        except Exception as e:
            print(f"⚠️ Error removing users from DB: {e}")

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_delete_from_db())
    except RuntimeError:
        pass


async def check_channel_membership(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Check if user is a member of the mandatory channel"""
    try:
        # Get user's status in the channel
        member = await context.bot.get_chat_member(chat_id=MANDATORY_CHANNEL, user_id=user_id)
        
        # Check if user is a member (member, administrator, or creator)
        status = member.status
        print(f"📋 Membership check for user {user_id}: status = '{status}'")
        
        if status in ['member', 'administrator', 'creator']:
            return True
        elif status in ['left', 'kicked', 'banned']:
            return False
        else:
            # Unknown status - treat as not member
            print(f"⚠️ Unknown membership status '{status}' for user {user_id}")
            return False
    except Exception as e:
        error_msg = str(e)
        print(f"❌ MEMBERSHIP CHECK ERROR for user {user_id}: {error_msg}")
        # If the error is that the bot is not an admin / can't check,
        # return True to avoid wrongly blocking users
        if "bot is not a member" in error_msg.lower() or \
           "chat not found" in error_msg.lower() or \
           "bot was kicked" in error_msg.lower() or \
           "forbidden" in error_msg.lower() or \
           "have no rights" in error_msg.lower():
            print(f"⚠️ Bot may not be admin in the mandatory channel! Add bot as admin in Backup Videos69.")
            return True  # Don't block users if bot can't check
        return False


def get_main_menu_keyboard():
    """Create the persistent menu keyboard with main buttons"""
    keyboard = [
        [KeyboardButton("🎬 Get New Video"), KeyboardButton("💬 Contact Us")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


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


def get_random_video(user_id):
    """Generate a random video message ID that the user hasn't seen yet"""
    
    if user_id not in user_states:
        init_user_state(user_id)
    
    user_state = user_states[user_id]
    
    # Use global configuration from .env file
    # MIN_MESSAGE_ID and MAX_MESSAGE_ID are defined at the top of the file
    
    # Generate a list of all possible message IDs
    all_message_ids = list(range(MIN_MESSAGE_ID, MAX_MESSAGE_ID + 1))
    
    # Get failed message IDs (messages that aren't videos or don't exist)
    failed_ids = user_state.get('failed_message_ids', [])
    
    # Filter out videos the user has already seen AND failed message IDs
    unseen_ids = [mid for mid in all_message_ids 
                  if mid not in user_state['seen_videos'] 
                  and mid not in failed_ids]
    
    # If user has seen all valid videos, reset their history and start over
    if not unseen_ids:
        print(f"🔄 User {user_id} has seen all videos! Resetting seen list for loop playback.")
        user_state['seen_videos'] = []
        # Keep failed_ids so we don't retry non-video messages
        unseen_ids = [mid for mid in all_message_ids if mid not in failed_ids]
        
        # If still no videos (all messages failed), reset failed_ids too
        if not unseen_ids:
            print(f"⚠️ All message IDs failed for user {user_id}. Resetting failed list.")
            user_state['failed_message_ids'] = []
            unseen_ids = all_message_ids
    
    # Return a random unseen message ID
    return random.choice(unseen_ids)




def mark_video_as_seen(user_id, video_id):
    """Mark a video as seen by the user"""
    if user_id not in user_states:
        init_user_state(user_id)
    
    if video_id not in user_states[user_id]['seen_videos']:
        user_states[user_id]['seen_videos'].append(video_id)
        # Save to persistent storage
        save_user_states(user_states)


def init_user_state(user_id):
    """Initialize user state with default values"""
    global user_states
    if user_id not in user_states:
        # Use the persistent storage module to initialize
        init_user_state_data(user_id, user_states)
        print(f"✨ Initialized new user state for user {user_id}")


def is_premium_user(user_id):
    """Check if user has active premium access"""
    if user_id not in user_states:
        init_user_state(user_id)
    
    premium_until = user_states[user_id].get('premium_until')
    if premium_until and datetime.now(timezone.utc) < premium_until:
        return True
    return False


def check_and_reset_daily_limit(user_id):
    """Check if limit needs to be reset (every 24 hours at midnight IST)"""
    if user_id not in user_states:
        init_user_state(user_id)
    
    user_state = user_states[user_id]
    now = datetime.now(timezone.utc)
    
    # Convert to IST for period calculation
    now_ist = utc_to_ist(now)
    
    # Calculate the start of the current day in IST (00:00 IST)
    current_day_start_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    
    # Convert back to UTC for storage
    current_day_start = current_day_start_ist - timedelta(hours=5, minutes=30)
    
    # Reset if we've moved to a new day
    if current_day_start > user_state['last_reset']:
        user_state['hourly_count'] = 0
        user_state['downloaded_count'] = 0  # Reset download count every 24 hours
        user_state['last_reset'] = current_day_start
        # Save to persistent storage
        save_user_states(user_states)
        print(f"🔄 Daily reset for user {user_id}: watch count and download count reset")
        return True  # Indicate that reset occurred
    return False  # No reset occurred


def can_watch_video(user_id):
    """Check if user can watch a video (premium or within daily limit)"""
    if is_premium_user(user_id):
        return True
    
    check_and_reset_daily_limit(user_id)
    return user_states[user_id]['hourly_count'] < DAILY_VIDEO_LIMIT


def increment_video_count(user_id):
    """Increment user's hourly video count"""
    if user_id not in user_states:
        init_user_state(user_id)
    
    user_states[user_id]['hourly_count'] += 1
    # Save to persistent storage
    save_user_states(user_states)


def utc_to_ist(utc_time):
    """Convert UTC datetime to IST (UTC+5:30)"""
    ist_offset = timedelta(hours=5, minutes=30)
    return utc_time + ist_offset


def grant_premium_access(user_id):
    """Grant premium access for 12 hours after watching ad"""
    if user_id not in user_states:
        init_user_state(user_id)
    
    user_states[user_id]['premium_until'] = datetime.now(timezone.utc) + timedelta(hours=PREMIUM_DURATION_HOURS)
    # Save to persistent storage
    save_user_states(user_states)
    print(f"💎 Premium access granted to user {user_id} until {user_states[user_id]['premium_until']}")


async def _shorten_via_vplinks(bot_deep_link: str, alias: str, user_id: int) -> str | None:
    """Try to shorten a link via VPLinks. Returns shortened URL or None."""
    api_url = f"{VPLINK_API_URL}?api={VPLINK_API_TOKEN}&url={bot_deep_link}&alias={alias}"
    try:
        async with aiohttp.ClientSession() as session:
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


async def _shorten_via_arolinks(bot_deep_link: str, alias: str, user_id: int) -> str | None:
    """Try to shorten a link via AroLinks. Returns shortened URL or None."""
    api_url = f"{AROLINK_API_URL}?api={AROLINK_API_TOKEN}&url={bot_deep_link}&alias={alias}"
    try:
        async with aiohttp.ClientSession() as session:
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


async def create_shortened_link(user_id):
    """Create a unique shortened link that opens the bot with encrypted token.
    Alternates between VPLinks and AroLinks on each call (round-robin).
    Falls back to the other service if the selected one fails.
    """
    global _link_service_counter

    if user_id not in user_states:
        init_user_state(user_id)
    
    # Generate a unique verification token
    timestamp = int(datetime.now().timestamp())
    random_part = random.randint(10000, 99999)
    
    # Create token: user_id + timestamp + random
    token_data = f"{user_id}_{timestamp}_{random_part}"
    
    # Encrypt the token using SHA256
    encrypted_token = hashlib.sha256(f"{token_data}_{SECRET_KEY}".encode()).hexdigest()[:16]
    
    # Create the full token (we'll verify this later)
    full_token = f"{user_id}_{timestamp}_{encrypted_token}"
    
    # Store the token in user state for verification
    user_states[user_id]['ad_token'] = full_token
    save_user_states(user_states)
    
    # Create Telegram bot deep link
    bot_deep_link = f"https://t.me/{BOT_USERNAME}?start={full_token}"
    
    # Unique alias for the short link
    alias = f"v_{user_id}_{timestamp}"
    
    # --- Round-robin: pick which service goes first this request ---
    use_vplinks_first = (_link_service_counter % 2 == 0)
    _link_service_counter += 1
    service_order = ("VPLinks", "AroLinks") if use_vplinks_first else ("AroLinks", "VPLinks")
    print(f"🔗 Link service this request: {service_order[0]} (fallback: {service_order[1]}) | counter={_link_service_counter}")
    
    # Try primary service
    if use_vplinks_first:
        shortened_url = await _shorten_via_vplinks(bot_deep_link, alias, user_id)
        if not shortened_url:
            print(f"⚠️ VPLinks failed, trying AroLinks as fallback...")
            shortened_url = await _shorten_via_arolinks(bot_deep_link, alias + "a", user_id)
    else:
        shortened_url = await _shorten_via_arolinks(bot_deep_link, alias, user_id)
        if not shortened_url:
            print(f"⚠️ AroLinks failed, trying VPLinks as fallback...")
            shortened_url = await _shorten_via_vplinks(bot_deep_link, alias + "v", user_id)
    
    if shortened_url:
        # Store the shortened link in user state
        user_states[user_id]['ad_link'] = shortened_url
        save_user_states(user_states)
        print(f"   Token: {full_token}")
        return shortened_url
    
    print(f"❌ Both link services failed for user {user_id}")
    return None



def validate_ad_link(user_id, clicked_link):
    """Validate if the clicked link matches the user's assigned link"""
    if user_id not in user_states:
        return False
    
    stored_link = user_states[user_id].get('ad_link')
    if stored_link and stored_link == clicked_link:
        return True
    return False

async def delete_messages(context: ContextTypes.DEFAULT_TYPE):
    """Delete messages after 20 minutes"""
    job_data = context.job.data
    chat_id = job_data['chat_id']
    message_ids = job_data['message_ids']
    
    # Delete the video and warning messages
    for message_id in message_ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception as e:
            print(f"⚠️ Could not delete message {message_id}: {e}")
    
    print(f"🗑️ Deleted {len(message_ids)} messages in chat {chat_id}")
    
    # No notification message - keep the chat clean!


async def broadcast_daily_reset(context: ContextTypes.DEFAULT_TYPE):
    """Broadcast message to all users about limit reset - sent every 24 hours at midnight"""
    
    print("\n" + "=" * 60)
    print("🔔 DAILY RESET BROADCAST TRIGGERED!")
    print("=" * 60)
    
    # Get current time in IST
    from datetime import datetime, timedelta, timezone
    ist_offset = timedelta(hours=5, minutes=30)
    current_time_ist = datetime.now(timezone.utc) + ist_offset
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
            
            # Add delay to avoid hitting rate limits (increased for stability)
            import asyncio
            await asyncio.sleep(0.2)  # 200ms delay between messages (safer under load)

            
        except Exception as e:
            failed += 1
            # User might have blocked the bot or deleted their account
            print(f"⚠️ Could not send broadcast to user {user_id}: {e}")
            
            # Track blocked users to remove from file
            if "Forbidden" in str(e) or "blocked" in str(e).lower():
                blocked_users.append(user_id)
                # Also remove from in-memory set
                all_users.discard(user_id)
    
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
    import time
    
    async with request_semaphore:
        start_time = time.time()
        try:
            await send_random_video(update, context, chat_id, user_id)
            duration = time.time() - start_time
            
            # Log slow requests for monitoring
            if duration > 2.0:
                print(f"⚠️ Slow request: {duration:.2f}s for user {user_id}")
                
        except Exception as e:
            print(f"⚠️ Error in rate-limited send: {e}")
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
        # FIRST: Check if user is still a member of the mandatory channel
        is_member = await check_channel_membership(context, user_id)
        
        if not is_member:
            # User hasn't joined or left the channel - show join message
            keyboard = [
                [InlineKeyboardButton("📢 Join Channel", url=MANDATORY_CHANNEL_LINK)],
                [InlineKeyboardButton("🔄 Refresh", callback_data='check_membership')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await context.bot.send_message(
                chat_id=chat_id,
                text="🔒 **Channel Membership Required!**\n\n"
                     "You must join our channel to use this bot.\n\n"
                     "📢 **Steps**:\n"
                     "1. Click 'Join Channel' button below\n"
                     "2. Join the channel\n"
                     "3. Click 'Refresh' button to verify\n\n"
                     "After joining, you can watch videos! 🎬",
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
            return  # Stop here - don't send video
        
        # Check if user can watch videos (daily limit or premium)
        if not can_watch_video(user_id):
            # Check if user had premium that expired
            if user_id in user_states and user_states[user_id].get('premium_until'):
                premium_until = user_states[user_id]['premium_until']
                if datetime.now(timezone.utc) >= premium_until:
                    # Premium has expired - calculate next daily reset
                    now = datetime.now(timezone.utc)
                    now_ist = utc_to_ist(now)
                    
                    # Calculate next day at midnight (00:00 IST)
                    next_reset_ist = (now_ist + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                    
                    # Convert back to UTC for time calculation
                    next_reset = next_reset_ist - timedelta(hours=5, minutes=30)
                    
                    time_until_reset = next_reset - now
                    minutes_until_reset = int(time_until_reset.total_seconds() / 60)
                    hours_until_reset = int(time_until_reset.total_seconds() / 3600)
                    
                    # Format time until reset
                    if hours_until_reset > 0:
                        time_msg = f"{hours_until_reset} hour(s) and {minutes_until_reset % 60} minute(s)"
                    else:
                        time_msg = f"{minutes_until_reset} minute(s)"
                    
                    keyboard = [[InlineKeyboardButton("📺 Watch Ad", callback_data='watch_ad')]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"⏰ **Premium Access Expired!**\n\n"
                             f"Your premium access has ended.\n\n"
                             f"**Options**:\n"
                             f"• 📺 Watch an ad to get **12 more hours of premium**\n"
                             f"• ⏰ Wait **{time_msg}** for your limit to reset at **midnight (12:00 AM) IST**\n\n"
                             f"⏱️ Limits reset every **24 hours at midnight**\n\n"
                             f"Choose an option below:",
                        parse_mode='Markdown',
                        reply_markup=reply_markup
                    )
                    return
            
            # Regular limit reached (no premium or never had premium)
            # Calculate next daily reset time (midnight)
            now = datetime.now(timezone.utc)
            now_ist = utc_to_ist(now)
            
            # Calculate next day at midnight (00:00 IST)
            next_reset_ist = (now_ist + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            
            # Convert back to UTC for time calculation
            next_reset = next_reset_ist - timedelta(hours=5, minutes=30)
            
            time_until_reset = next_reset - now
            minutes_until_reset = int(time_until_reset.total_seconds() / 60)
            hours_until_reset = int(time_until_reset.total_seconds() / 3600)
            
            keyboard = [[InlineKeyboardButton("📺 Watch Ad", callback_data='watch_ad')]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            # Format time until reset
            if hours_until_reset > 0:
                time_msg = f"{hours_until_reset} hour(s) and {minutes_until_reset % 60} minute(s)"
            else:
                time_msg = f"{minutes_until_reset} minute(s)"
            
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ **Daily Limit Reached!**\n\n"
                     f"You've watched all **3 free videos** for today.\n\n"
                     f"**Options**:\n"
                     f"• 📺 Watch an ad to get **12 hours of unlimited access**\n"
                     f"• ⏰ Wait **{time_msg}** for your limit to reset at **midnight (12:00 AM) IST**\n\n"
                     f"⏱️ Limits reset every **24 hours at midnight**\n\n"
                     f"Choose an option below:",
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
            return
        
        # Try to find a valid video (try up to 10 random message IDs)
        max_attempts = 10
        video_sent = False
        
        for attempt in range(max_attempts):
            try:
                # Get a random message ID
                message_id = get_random_video(user_id)
                
                # Initialize downloaded_count if it doesn't exist (for existing users)
                if 'downloaded_count' not in user_states[user_id]:
                    user_states[user_id]['downloaded_count'] = 0
                    save_user_states(user_states)
                
                downloaded_count = user_states[user_id]['downloaded_count']
                
                # Determine download limit based on premium status
                is_premium = is_premium_user(user_id)
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
                        keyboard.append([InlineKeyboardButton("💎 Watch Ad for Premium", callback_data='watch_ad')])
                
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
                            f"💎 **Want 20 downloads per hour?**\n"
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
                print(f"✅ Sent video message ID {message_id} to user {user_id} (protected, downloads remaining: {DOWNLOADABLE_VIDEOS_LIMIT - downloaded_count})")
                break  # Success! Exit the loop
                
            except Exception as e:
                # This message ID doesn't exist or isn't a video, try another
                print(f"⚠️ Message ID {message_id} failed: {e}")
                # DON'T mark as seen - only mark successful videos as seen
                # Instead, add to a separate failed_ids list to avoid retrying
                if 'failed_message_ids' not in user_states[user_id]:
                    user_states[user_id]['failed_message_ids'] = []
                if message_id not in user_states[user_id]['failed_message_ids']:
                    user_states[user_id]['failed_message_ids'].append(message_id)
                    save_user_states(user_states)
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
    
    # Track this user for broadcasting (in-memory)
    all_users.add(user_id)
    
    # Save user to file (persistent storage)
    add_user_to_file(user_id, first_name, username)
    
    # Check if user is a member of the mandatory channel
    is_member = await check_channel_membership(context, user_id)
    
    if not is_member:
        # User hasn't joined the channel - show join message
        keyboard = [
            [InlineKeyboardButton("📢 Join Channel", url=MANDATORY_CHANNEL_LINK)],
            [InlineKeyboardButton("🔄 Refresh", callback_data='check_membership')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"👋 Welcome {first_name}!\n\n"
            f"🔒 To use this bot, you must join our channel first.\n\n"
            f"📢 **Steps**:\n"
            f"1. Click 'Join Channel' button below\n"
            f"2. Join the channel\n"
            f"3. Click 'Refresh' button to verify\n\n"
            f"After joining, you'll get full access to the bot! 🎬",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
        return  # Stop here until user joins
    
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
                        
                        # Clear the stored token
                        user_states[user_id]['ad_token'] = None
                        user_states[user_id]['ad_link'] = None
                        # Save to persistent storage
                        save_user_states(user_states)
                        
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
        time_remaining = premium_until - datetime.now(timezone.utc)
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
        print(f"✅ Premium user returned: {first_name} (ID: {user_id})")
        return
    
    # Regular welcome message for new/non-premium users
    welcome_message = f"""
🎬 **Welcome to Videos Bot!** 🎬

Hello {first_name}! 👋

I'm here to share amazing videos with you from our collection.

🎥 You can watch **10 videos per hour** for free!
⏰ Your limit resets every hour (1:00 PM, 2:00 PM, etc.)

💎 Want unlimited access? Watch an ad to get **12 hours of premium**!

Enjoy! 🍿
    """
    
    # Create \"Get First Video\" inline button
    inline_keyboard = [[InlineKeyboardButton("🎬 Get First Video", callback_data='next_video')]]
    inline_markup = InlineKeyboardMarkup(inline_keyboard)
    
    # Get persistent menu keyboard
    menu_keyboard = get_main_menu_keyboard()
    
    await update.message.reply_text(
        welcome_message,
        parse_mode='Markdown',
        reply_markup=menu_keyboard  # Show persistent menu
    )
    
    # Send inline button separately
    await update.message.reply_text(
        "👇 Click below to get your first video:",
        reply_markup=inline_markup
    )
    
    print(f"✅ New user started the bot: {first_name} (ID: {user_id})")


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
    print(f"📞 User {user.id} requested help - directed to admin bot")


async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle regular messages - Forward to/from admin OR handle menu buttons"""
    # Ignore messages without a user (e.g., channel posts)
    if not update.effective_user:
        return
    
    user_id = update.effective_user.id
    message = update.message
    
    # Check if it's a menu button press
    if message.text:
        if message.text == "🎬 Get New Video":
            # User clicked "Get New Video" button
            await send_random_video(
                update,
                context,
                chat_id=message.chat_id,
                user_id=user_id
            )
            return
        
        elif message.text == "💬 Contact Us":
            # User clicked "Contact Us" button - show admin bot link
            first_name = update.effective_user.first_name or "there"
            
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
            
            await message.reply_text(
                help_message,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
            print(f"📞 User {user_id} requested help via menu button")
            return
    
    # If not a menu button, ignore other text messages (no forwarding to admin from main bot)
    # Admin communication happens through the separate admin bot


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button callbacks"""
    query = update.callback_query
    user_id = query.from_user.id
    user_name = query.from_user.first_name or "Unknown"
    button_action = query.data
    
    # Track if this is an old/expired callback query
    is_old_query = False
    
    # Try to answer the query, but silently ignore timeout/expired errors
    try:
        await query.answer()
    except Exception as e:
        # Silently ignore "Query is too old" and timeout errors
        # These are expected when users click old broadcast buttons
        if "too old" in str(e).lower() or "timeout" in str(e).lower() or "query id is invalid" in str(e).lower():
            is_old_query = True
            print(f"🕐 Old button clicked by {user_name} (ID: {user_id}) - Action: '{button_action}'")
            print(f"   ✅ Processing anyway (callback query expired but action will execute)")
        else:
            # Log unexpected errors
            print(f"⚠️ Unexpected callback query error: {e}")
    
    # Log successful fresh button clicks
    if not is_old_query:
        print(f"🔘 Button clicked by {user_name} (ID: {user_id}) - Action: '{button_action}'")
    
    if query.data == 'check_membership':
        # User clicked refresh button - check if they joined the channel
        user_id = query.from_user.id
        first_name = query.from_user.first_name or "there"
        
        is_member = await check_channel_membership(context, user_id)
        
        if is_member:
            # User has joined! Show welcome message
            keyboard = [[InlineKeyboardButton("🎬 Get First Video", callback_data='next_video')]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            try:
                await query.message.edit_text(
                    f"✅ **Verified!**\n\n"
                    f"Welcome {first_name}! 👋\n\n"
                    f"You've successfully joined the channel!\n\n"
                    f"🎥 You can watch **3 free videos** per day!\n"
                    f"📥 You can download **2 videos** per day!\n"
                    f"⏰ Your limits reset every **24 hours at midnight (12:00 AM IST)**!\n\n"
                    f"💎 Want unlimited access? Watch an ad to get **12 hours of premium**!\n\n"
                    f"Click below to start watching! 🍿",
                    parse_mode='Markdown',
                    reply_markup=reply_markup
                )
            except Exception as e:
                # Ignore "Message is not modified" errors
                if "Message is not modified" not in str(e):
                    print(f"⚠️ Error editing message: {e}")
        else:
            # User still hasn't joined
            keyboard = [
                [InlineKeyboardButton("📢 Join Channel", url=MANDATORY_CHANNEL_LINK)],
                [InlineKeyboardButton("🔄 Refresh", callback_data='check_membership')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            try:
                await query.message.edit_text(
                    f"❌ **Not Joined Yet**\n\n"
                    f"You haven't joined the channel yet.\n\n"
                    f"📢 **Please**:\n"
                    f"1. Click 'Join Channel' button\n"
                    f"2. Join the channel\n"
                    f"3. Click 'Refresh' again\n\n"
                    f"You must join to use this bot! 🔒",
                    parse_mode='Markdown',
                    reply_markup=reply_markup
                )
            except Exception as e:
                # Ignore "Message is not modified" errors
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
                "✅ **20 downloads** every 6 hours (10x more!)\n"
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
                     "✅ **20 downloads** every 6 hours (10x more!)\n"
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
                save_user_states(user_states)
            
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
                save_user_states(user_states)
                
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
                
                print(f"📥 User {user_id} downloaded video {message_id} ({remaining} downloads remaining)")
                print(f"🗑️ Scheduled deletion of downloaded video {download_msg.message_id} in 20 minutes")
                
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
            .request(request)  # Use custom request with longer timeouts
            .post_init(post_init)
            .build()
        )
        
        # Add command handlers
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("help", help_command))
        
        # Add callback query handler for buttons
        application.add_handler(CallbackQueryHandler(button_callback))
        
        # Add message handler for persistent menu buttons
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages))
        
        # Channel post handler disabled - using random message IDs
        
        # Schedule broadcast every 6 hours (00:00, 06:00, 12:00, 18:00)
        from datetime import time
        import pytz
        
        # IST timezone
        ist_tz = pytz.timezone('Asia/Kolkata')
        
        # Calculate when to run the first broadcast (at the next 12-hour period start)
        now_utc = datetime.now(timezone.utc)
        now_ist = now_utc + timedelta(hours=5, minutes=30)
        
        # Calculate next midnight (00:00 IST)
        next_broadcast_ist = (now_ist + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        
        next_broadcast_utc = next_broadcast_ist - timedelta(hours=5, minutes=30)
        
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
            poll_interval=0.5,    # Poll every 0.5s — fast enough, avoids CPU burn from constant hammering
            timeout=30,           # Long-poll: 30s open connection reduces API call count
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

