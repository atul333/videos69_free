import os
import json
import copy
import asyncio
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)
from telegram.request import HTTPXRequest

load_dotenv()

ADMIN_BOT_TOKEN = "8655582748:AAEyQ7ircQeVJGOmBxiWKDkwZDfqNAUA25g"
ADMIN_USER_ID   = 7708376300  # @Deep12048

WORKING_BOTS_FILE = os.path.join(os.path.dirname(__file__), "working_bots.json")
MSG_MAP_FILE      = os.path.join(os.path.dirname(__file__), "msg_map.json")

# Default template data if working_bots.json is missing
DEFAULT_BOTS_DATA = {
    "header": "♨️ <b>Videos69 Working Bots List</b> 🤖",
    "channel_link": "https://t.me/+4vOykznly3A3ZDQ0",
    "bots": [],
    "footer": "💡 <i>Dear users aap inme se kisi bhi bot se unlimited videos access kr sakte hai. Jo bot band ho uska report hame yahan message kr dia kijie 👍</i>",
    "seen_users": []
}

# Distinct single emojis for bots (no words, no brackets, single clean emoji per bot)
UNIQUE_BOT_EMOJIS = [
    "⚡",
    "🔥",
    "💎",
    "🚀",
    "👑",
    "🌟",
    "🎯",
    "🔮",
    "💫",
    "🍿",
    "🏆",
    "🌀",
    "🛡️",
    "🤖",
    "💥",
    "✨",
    "🎬",
    "🌈"
]

# In-memory cache of forward map: admin-side fwd_message_id -> original user_id
_msg_map: dict[int, int] = {}

# Robust timeouts — prevents false "Timed out" on slow connections
request = HTTPXRequest(
    connection_pool_size=8,
    connect_timeout=20.0,
    read_timeout=20.0,
    write_timeout=20.0,
    pool_timeout=10.0,
)


def load_msg_map() -> dict[str, int]:
    if os.path.exists(MSG_MAP_FILE):
        try:
            with open(MSG_MAP_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_msg_map(m_map: dict[str, int]):
    try:
        with open(MSG_MAP_FILE, "w", encoding="utf-8") as f:
            json.dump(m_map, f, indent=2)
    except Exception as e:
        print(f"⚠️ Error saving {MSG_MAP_FILE}: {e}")


def record_forward_map(fwd_msg_id: int, user_id: int):
    """Store forwarded message ID to user ID mapping in memory and disk."""
    _msg_map[fwd_msg_id] = user_id
    try:
        m_map = load_msg_map()
        m_map[str(fwd_msg_id)] = user_id
        if len(m_map) > 2000:
            keys = list(m_map.keys())
            for k in keys[:-2000]:
                del m_map[k]
        save_msg_map(m_map)
    except Exception as e:
        print(f"⚠️ Failed to save forward map: {e}")


def get_user_from_map(fwd_msg_id: int) -> int | None:
    """Retrieve original user ID from in-memory cache or persistent disk map."""
    if fwd_msg_id in _msg_map:
        return _msg_map[fwd_msg_id]
    m_map = load_msg_map()
    uid = m_map.get(str(fwd_msg_id))
    if uid:
        _msg_map[fwd_msg_id] = uid
        return uid
    return None


def extract_forward_sender_id(msg) -> int | None:
    """Safely extract user ID from forwarded message across all PTB versions."""
    if not msg:
        return None

    # Check forward_from (PTB < 21.0)
    try:
        forward_from = getattr(msg, 'forward_from', None)
        if forward_from and getattr(forward_from, 'id', None):
            return forward_from.id
    except Exception:
        pass

    # Check forward_origin (PTB >= 21.0)
    try:
        origin = getattr(msg, 'forward_origin', None)
        if origin:
            sender_user = getattr(origin, 'sender_user', None)
            if sender_user and getattr(sender_user, 'id', None):
                return sender_user.id
            user = getattr(origin, 'user', None)
            if user and getattr(user, 'id', None):
                return user.id
            chat = getattr(origin, 'chat', None) or getattr(origin, 'sender_chat', None)
            if chat and getattr(chat, 'id', None):
                return chat.id
    except Exception:
        pass

    return None


def load_bots_data() -> dict:
    if os.path.exists(WORKING_BOTS_FILE):
        try:
            with open(WORKING_BOTS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ Error loading {WORKING_BOTS_FILE}: {e}")
    # Initialize default
    data = copy.deepcopy(DEFAULT_BOTS_DATA)
    save_bots_data(data)
    return data


def save_bots_data(data: dict):
    try:
        with open(WORKING_BOTS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"❌ Error saving {WORKING_BOTS_FILE}: {e}")


def is_first_time_user(user_id: int) -> bool:
    data = load_bots_data()
    seen = set(data.get("seen_users", []))
    return user_id not in seen


def mark_user_seen(user_id: int):
    data = load_bots_data()
    seen = set(data.get("seen_users", []))
    if user_id not in seen:
        seen.add(user_id)
        data["seen_users"] = list(seen)
        save_bots_data(data)


def format_bots_message(extra_top_msg: str = "", extra_bottom_msg: str = "") -> str:
    data = load_bots_data()
    header = data.get("header", "♨️ <b>Videos69 Working Bots List</b> 🤖")
    bots = data.get("bots", [])
    footer = data.get("footer", "")

    lines = []
    if extra_top_msg:
        lines.append(extra_top_msg.strip() + "\n")

    lines.append(header)
    lines.append("")  # 1 blank line after header

    if bots:
        for idx, b in enumerate(bots, start=1):
            uname = b.get("username", "").strip()
            tag = b.get("tag", "").strip()
            if not uname.startswith("@"):
                uname = f"@{uname}"

            # Clean emoji: if tag is empty or has brackets/words, use single clean emoji
            if not tag or "(" in tag or ")" in tag:
                tag = UNIQUE_BOT_EMOJIS[(idx - 1) % len(UNIQUE_BOT_EMOJIS)]

            lines.append(f"{idx}. {uname} {tag}")
            lines.append("")  # 1 space between each bot
    else:
        lines.append("<i>(Bots will be listed here. Use the button below to join our official channel!)</i>")
        lines.append("")

    if footer:
        lines.append(footer)

    if extra_bottom_msg:
        lines.append("\n━━━━━━━━━━━━━━━━━━━━\n" + extra_bottom_msg.strip())

    return "\n".join(lines).strip()


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user       = update.effective_user
    user_id    = user.id
    first_name = user.first_name or "there"

    if user_id == ADMIN_USER_ID:
        await update.message.reply_text(
            f"👨‍💼 <b>Welcome Admin!</b>\n\n"
            f"Hello {first_name}! 🙏\n\n"
            f"📨 <b>How user support works:</b>\n"
            f"1. Users send questions or error screenshots here\n"
            f"2. They are forwarded directly to you\n"
            f"3. <b>Reply</b> to any forwarded message to respond\n\n"
            f"🤖 <b>Bot Management Commands:</b>\n"
            f"➕ <code>/addbot @bot [tag]</code> — Add/update bot\n"
            f"🔄 <code>/replacebot @old @new [tag]</code> — Replace banned bot\n"
            f"➖ <code>/delbot @bot</code> — Remove a bot\n"
            f"📋 <code>/bots</code> — View working bots list\n"
            f"🔗 <code>/setlink [url]</code> — Change button channel link\n"
            f"🏷️ <code>/setheader [text]</code> — Change list header\n"
            f"📝 <code>/setfooter [text]</code> — Change list footer\n"
            f"🔄 <code>/resetbots</code> — Reset to default list\n\n"
            f"✅ Waiting for user messages…",
            parse_mode='HTML'
        )
    else:
        mark_user_seen(user_id)
        support_help_text = (
            "💬 <b>Need Admin Support?</b>\n"
            "📩 Send your message or 📸 <b>error screenshot of facing issue</b> below, and our admin will reply shortly! 👇"
        )
        msg_text = format_bots_message(
            extra_top_msg=f"🙏 <b>Welcome {first_name}!</b> ✨\n",
            extra_bottom_msg=support_help_text
        )
        data = load_bots_data()
        channel_link = data.get("channel_link", "https://t.me/+4vOykznly3A3ZDQ0")
        reply_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Join Official Channel 🚀", url=channel_link)]
        ])

        await update.message.reply_text(
            msg_text,
            parse_mode='HTML',
            reply_markup=reply_markup,
            disable_web_page_preview=True
        )


async def bots_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View working bots list."""
    user = update.effective_user
    user_id = user.id
    msg_text = format_bots_message()

    data = load_bots_data()
    channel_link = data.get("channel_link", "https://t.me/+4vOykznly3A3ZDQ0")
    reply_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Official Channel 🚀", url=channel_link)]
    ])

    await update.message.reply_text(
        msg_text,
        parse_mode='HTML',
        reply_markup=reply_markup,
        disable_web_page_preview=True
    )

    if user_id == ADMIN_USER_ID:
        await update.message.reply_text(
            "👨‍💼 <b>Admin Quick Actions:</b>\n"
            "➕ <code>/addbot @bot [tag]</code>\n"
            "🔄 <code>/replacebot @old @new [tag]</code>\n"
            "➖ <code>/delbot @bot</code>",
            parse_mode='HTML'
        )


async def addbot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: Add a new bot or update existing bot."""
    if update.effective_user.id != ADMIN_USER_ID:
        return

    if not context.args:
        await update.message.reply_text(
            "⚠️ <b>Usage:</b>\n"
            "<code>/addbot @bot_username [emoji]</code>\n\n"
            "<b>Examples:</b>\n"
            "<code>/addbot @Videos1_69_bot ⚡</code>\n"
            "<code>/addbot @Videos2_69_bot 🔥</code>\n"
            "<code>/addbot @Videos3_69_bot</code> <i>(auto-assigns distinct emoji)</i>",
            parse_mode='HTML'
        )
        return

    bot_username = context.args[0].strip()
    if not bot_username.startswith("@"):
        bot_username = f"@{bot_username}"

    data = load_bots_data()
    bots = data.get("bots", [])

    tag = " ".join(context.args[1:]).strip() if len(context.args) > 1 else UNIQUE_BOT_EMOJIS[len(bots) % len(UNIQUE_BOT_EMOJIS)]

    updated = False
    for b in bots:
        if b.get("username", "").lower() == bot_username.lower():
            b["username"] = bot_username
            b["tag"] = tag
            updated = True
            break

    if not updated:
        bots.append({"username": bot_username, "tag": tag})

    data["bots"] = bots
    save_bots_data(data)

    action = "Updated" if updated else "Added"
    await update.message.reply_text(
        f"✅ <b>{action} working bot:</b>\n"
        f"<code>{bot_username} {tag}</code>\n\n"
        f"📊 Total working bots: <b>{len(bots)}</b>\n"
        f"Type /bots to preview the updated list.",
        parse_mode='HTML'
    )


async def replacebot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: Replace a banned bot with a new bot."""
    if update.effective_user.id != ADMIN_USER_ID:
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "⚠️ <b>Usage:</b>\n"
            "<code>/replacebot @old_bot @new_bot [emoji]</code>\n\n"
            "<b>Example:</b>\n"
            "<code>/replacebot @old_bot @new_bot ⚡</code>",
            parse_mode='HTML'
        )
        return

    old_bot = context.args[0].strip()
    if not old_bot.startswith("@"):
        old_bot = f"@{old_bot}"

    new_bot = context.args[1].strip()
    if not new_bot.startswith("@"):
        new_bot = f"@{new_bot}"

    data = load_bots_data()
    bots = data.get("bots", [])

    custom_tag = " ".join(context.args[2:]).strip() if len(context.args) > 2 else None

    found = False
    for b in bots:
        if b.get("username", "").lower() == old_bot.lower():
            b["username"] = new_bot
            if custom_tag is not None:
                b["tag"] = custom_tag
            found = True
            break

    if not found:
        tag = custom_tag if custom_tag is not None else UNIQUE_BOT_EMOJIS[len(bots) % len(UNIQUE_BOT_EMOJIS)]
        bots.append({"username": new_bot, "tag": tag})

    data["bots"] = bots
    save_bots_data(data)

    if found:
        status_txt = f"Replaced <code>{old_bot}</code> ➔ <code>{new_bot}</code>"
    else:
        status_txt = f"Added <code>{new_bot}</code> (old bot <code>{old_bot}</code> was not in list)"

    await update.message.reply_text(
        f"✅ <b>{status_txt}</b>\n\n"
        f"📊 Total working bots: <b>{len(bots)}</b>\n"
        f"Type /bots to preview the updated list.",
        parse_mode='HTML'
    )


async def delbot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: Remove a bot from the working list."""
    if update.effective_user.id != ADMIN_USER_ID:
        return

    if not context.args:
        await update.message.reply_text(
            "⚠️ <b>Usage:</b>\n"
            "<code>/delbot @bot_username</code>\n\n"
            "<b>Example:</b>\n"
            "<code>/delbot @old_bot</code>",
            parse_mode='HTML'
        )
        return

    bot_username = context.args[0].strip()
    if not bot_username.startswith("@"):
        bot_username = f"@{bot_username}"

    data = load_bots_data()
    bots = data.get("bots", [])

    new_bots = [b for b in bots if b.get("username", "").lower() != bot_username.lower()]

    if len(new_bots) == len(bots):
        await update.message.reply_text(
            f"⚠️ Bot <code>{bot_username}</code> was not found in the list.",
            parse_mode='HTML'
        )
        return

    data["bots"] = new_bots
    save_bots_data(data)

    await update.message.reply_text(
        f"🗑️ <b>Removed bot:</b> <code>{bot_username}</code>\n\n"
        f"📊 Total remaining working bots: <b>{len(new_bots)}</b>\n"
        f"Type /bots to preview the updated list.",
        parse_mode='HTML'
    )


async def setlink_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: Set channel link for the button."""
    if update.effective_user.id != ADMIN_USER_ID:
        return

    if not context.args:
        await update.message.reply_text(
            "⚠️ <b>Usage:</b> <code>/setlink https://t.me/+...</code>",
            parse_mode='HTML'
        )
        return

    link = context.args[0].strip()
    data = load_bots_data()
    data["channel_link"] = link
    save_bots_data(data)
    await update.message.reply_text(f"✅ Channel button link updated to:\n{link}")


async def setheader_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: Set list header."""
    if update.effective_user.id != ADMIN_USER_ID:
        return

    text = update.message.text.partition(" ")[2].strip()
    if not text:
        await update.message.reply_text(
            "⚠️ <b>Usage:</b> <code>/setheader <header text></code>",
            parse_mode='HTML'
        )
        return

    data = load_bots_data()
    data["header"] = text
    save_bots_data(data)
    await update.message.reply_text("✅ Header updated successfully!")


async def setfooter_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: Set list footer."""
    if update.effective_user.id != ADMIN_USER_ID:
        return

    text = update.message.text.partition(" ")[2].strip()
    if not text:
        await update.message.reply_text(
            "⚠️ <b>Usage:</b> <code>/setfooter <footer text></code>",
            parse_mode='HTML'
        )
        return

    data = load_bots_data()
    data["footer"] = text
    save_bots_data(data)
    await update.message.reply_text("✅ Footer updated successfully!")


async def resetbots_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: Reset bots list to default."""
    if update.effective_user.id != ADMIN_USER_ID:
        return

    data = copy.deepcopy(DEFAULT_BOTS_DATA)
    save_bots_data(data)
    await update.message.reply_text(
        "✅ Reset working bots list to default!\nType /bots to view.",
        parse_mode='HTML'
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text, photo, and media messages from both users and admin."""
    user    = update.effective_user
    user_id = user.id
    message = update.message

    if not message:
        return

    # Determine message type
    is_photo = bool(message.photo)
    is_text  = bool(message.text)

    # ── ADMIN PATH ────────────────────────────────────────────────────────────
    if user_id == ADMIN_USER_ID:
        if not message.reply_to_message:
            await message.reply_text(
                "ℹ️ <b>How to reply to a user:</b>\n\n"
                "Simply <b>Reply</b> to their forwarded message (text, photo, or media).\n"
                "Your reply will be sent directly to them.",
                parse_mode='HTML'
            )
            return

        replied_msg_id = message.reply_to_message.message_id

        # 1. Look up user from persistent forward map
        target_user_id = get_user_from_map(replied_msg_id)

        # 2. Safe Fallback: extract from forward_from / forward_origin
        if not target_user_id:
            target_user_id = extract_forward_sender_id(message.reply_to_message)

        if not target_user_id:
            print(f"⚠️ [admin_bot] Reply to msg {replied_msg_id} — not in map")
            try:
                await message.reply_text(
                    "⚠️ <b>Cannot send reply.</b>\n\n"
                    "Could not identify the user for this message.\n"
                    "Please reply directly to their forwarded message.",
                    parse_mode='HTML'
                )
            except Exception:
                pass
            return

        # Deliver admin reply (photo, text, or any other media)
        sent_ok = False
        try:
            if is_photo:
                await context.bot.send_photo(
                    chat_id=target_user_id,
                    photo=message.photo[-1].file_id,
                    caption=(
                        f"👨‍💼 <b>Reply from Admin:</b>\n\n{message.caption}"
                        if message.caption else "👨‍💼 <b>Reply from Admin:</b>"
                    ),
                    parse_mode='HTML'
                )
            elif is_text:
                await context.bot.send_message(
                    chat_id=target_user_id,
                    text=f"👨‍💼 <b>Reply from Admin:</b>\n\n{message.text}",
                    parse_mode='HTML'
                )
            else:
                await context.bot.copy_message(
                    chat_id=target_user_id,
                    from_chat_id=message.chat_id,
                    message_id=message.message_id
                )
            sent_ok = True
            print(f"✅ Admin reply → user {target_user_id}")

        except Exception as e:
            err = str(e)
            print(f"❌ [admin_bot] Delivery failed to {target_user_id}: {err}")
            if "bot was blocked" in err.lower() or "user is deactivated" in err.lower():
                note = "The user has blocked this bot or their account is deactivated."
            elif "chat not found" in err.lower():
                note = "This user has never started a conversation with this bot."
            else:
                note = f"<code>{err}</code>"
            try:
                await message.reply_text(
                    f"❌ <b>Failed to send reply.</b>\n\n{note}",
                    parse_mode='HTML'
                )
            except Exception:
                pass
            return

        # Confirm to admin
        if sent_ok:
            try:
                await message.reply_text("✅ Reply sent!")
            except Exception:
                pass

    # ── USER PATH ─────────────────────────────────────────────────────────────
    else:
        first_name = user.first_name or "Unknown"
        data = load_bots_data()
        channel_link = data.get("channel_link", "https://t.me/+4vOykznly3A3ZDQ0")
        reply_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Join Official Channel 🚀", url=channel_link)]
        ])

        # Users with hidden username can't be replied to — skip
        if not user.username:
            print(f"⚠️ [admin_bot] Skipped {first_name} ({user_id}) — no username")
            try:
                msg_text = format_bots_message(
                    extra_top_msg="⚠️ <b>Unable to forward your message to Admin.</b>\n"
                                  "Your Telegram username is hidden, so the admin cannot reply back to you.\n\n"
                                  "👉 Please set a public username in <b>Telegram Settings → Username</b> and send your message or 📸 <b>error screenshot of facing issue</b> again!\n"
                )
                await message.reply_text(
                    msg_text,
                    parse_mode='HTML',
                    reply_markup=reply_markup,
                    disable_web_page_preview=True
                )
            except Exception:
                pass
            return

        # Forward message to admin (works for text, photos, documents, etc.)
        try:
            fwd = await context.bot.forward_message(
                chat_id=ADMIN_USER_ID,
                from_chat_id=message.chat_id,
                message_id=message.message_id
            )
            # Store map persistently so admin reply can ALWAYS be routed back
            record_forward_map(fwd.message_id, user_id)

            kind = "photo" if is_photo else ("text" if is_text else "media")
            print(f"📨 Forwarded {kind} from {first_name} ({user_id}) → admin msg {fwd.message_id}")

        except Exception as e:
            print(f"❌ [admin_bot] forward_message failed for {user_id}: {e}")
            try:
                await message.reply_text(
                    "❌ <b>Error sending message.</b>\n\nPlease try again later.",
                    parse_mode='HTML'
                )
            except Exception:
                pass
            return

        # Check if 1st time user or returning user
        first_time = is_first_time_user(user_id)
        mark_user_seen(user_id)

        try:
            if first_time:
                # 1st time user: send full working bots list + confirmation
                full_promo_msg = format_bots_message(
                    extra_bottom_msg="✅ <b>Your message & screenshot have been sent to Admin!</b> 📩\n"
                                     "⏳ <i>Our support team will review and reply to you shortly.</i> 👨‍💼🙏"
                )
                await message.reply_text(
                    full_promo_msg,
                    parse_mode='HTML',
                    reply_markup=reply_markup,
                    disable_web_page_preview=True
                )
            else:
                # Returning user: concise confirmation with /bots tip
                await message.reply_text(
                    "✅ <b>Your message & screenshot have been sent to Admin!</b> 📩\n"
                    "⏳ <i>Please wait patiently, our support team will reply shortly.</i> 👨‍💼🙏\n\n"
                    "💡 <i>Tip: Type /bots anytime to view all working bots!</i>",
                    parse_mode='HTML',
                    reply_markup=reply_markup,
                    disable_web_page_preview=True
                )
        except Exception as e:
            print(f"⚠️ [admin_bot] Error sending confirmation to user {user_id}: {e}")


def main():
    print("🤖 Starting Admin Support Bot…")
    print(f"👨‍💼 Admin ID: {ADMIN_USER_ID}")

    # Ensure working_bots.json is initialized
    load_bots_data()

    try:
        application = (
            Application.builder()
            .token(ADMIN_BOT_TOKEN)
            .request(request)
            .build()
        )

        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("bots", bots_command))
        application.add_handler(CommandHandler("botlist", bots_command))
        application.add_handler(CommandHandler("addbot", addbot_command))
        application.add_handler(CommandHandler("replacebot", replacebot_command))
        application.add_handler(CommandHandler("delbot", delbot_command))
        application.add_handler(CommandHandler("removebot", delbot_command))
        application.add_handler(CommandHandler("setlink", setlink_command))
        application.add_handler(CommandHandler("setheader", setheader_command))
        application.add_handler(CommandHandler("setfooter", setfooter_command))
        application.add_handler(CommandHandler("resetbots", resetbots_command))

        # Handle TEXT messages (excluding commands)
        application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
        )
        # Handle PHOTO messages
        application.add_handler(
            MessageHandler(filters.PHOTO, handle_message)
        )
        # Handle OTHER media messages (document, video, audio, sticker, voice)
        application.add_handler(
            MessageHandler(filters.ALL & ~filters.COMMAND & ~filters.TEXT & ~filters.PHOTO, handle_message)
        )

        print("✅ Admin Support Bot is running!")
        print("📱 Bot: @videos69Admin_Bot")

        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            poll_interval=1.0,
            timeout=30,
            drop_pending_updates=True,
        )

    except Exception as e:
        print(f"\n❌ Error starting admin bot: {e}")
        print("1. Check internet connection")
        print("2. Verify ADMIN_BOT_TOKEN is correct")


if __name__ == '__main__':
    main()
