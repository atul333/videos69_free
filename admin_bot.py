import os
import asyncio
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)
from telegram.request import HTTPXRequest

load_dotenv()

ADMIN_BOT_TOKEN = "8599538827:AAEByXuZDRmLP-8vP3TBaSajr7OGGV06bYI"
ADMIN_USER_ID   = 7708376300  # @Deep12048

# Map: admin-side forwarded message_id → original user_id
_msg_map: dict[int, int] = {}

# Robust timeouts — prevents false "Timed out" on slow connections
request = HTTPXRequest(
    connection_pool_size=8,
    connect_timeout=20.0,
    read_timeout=20.0,
    write_timeout=20.0,
    pool_timeout=10.0,
)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user       = update.effective_user
    user_id    = user.id
    first_name = user.first_name or "there"

    if user_id == ADMIN_USER_ID:
        await update.message.reply_text(
            f"👨‍💼 *Welcome Admin!*\n\n"
            f"Hello {first_name}! 👋\n\n"
            f"📨 *How it works:*\n"
            f"1. Users send messages/photos here\n"
            f"2. They are forwarded directly to you\n"
            f"3. *Reply* to any forwarded message (text or photo) to respond\n\n"
            f"✅ Waiting for user messages…",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(
            f"👋 *Welcome to Admin Support Bot!*\n\n"
            f"Hello {first_name}!\n\n"
            f"📝 Send your message or 📷 photo and our admin will reply shortly.\n\n"
            f"👨‍💼 *Admin Support Available* 👇",
            parse_mode='Markdown'
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text and photo messages from both users and admin."""
    user    = update.effective_user
    user_id = user.id
    message = update.message

    # Determine message type
    is_photo = bool(message.photo)
    is_text  = bool(message.text)

    # ── ADMIN PATH ────────────────────────────────────────────────────────────
    if user_id == ADMIN_USER_ID:
        if not message.reply_to_message:
            await message.reply_text(
                "ℹ️ *How to reply to a user:*\n\n"
                "Simply *Reply* to their forwarded message (text or photo).\n"
                "Your reply will be sent directly to them.",
                parse_mode='Markdown'
            )
            return

        replied_msg_id = message.reply_to_message.message_id

        # Look up user from forward map
        target_user_id = _msg_map.get(replied_msg_id)

        # Fallback: forward_from (only if user has no privacy shield)
        if not target_user_id and message.reply_to_message.forward_from:
            target_user_id = message.reply_to_message.forward_from.id

        if not target_user_id:
            print(f"⚠️ [admin_bot] Reply to msg {replied_msg_id} — not in map")
            try:
                await message.reply_text(
                    "⚠️ *Cannot send reply.*\n\n"
                    "Could not identify the user for this message.\n"
                    "Please reply directly to their forwarded message.",
                    parse_mode='Markdown'
                )
            except Exception:
                pass
            return

        # Deliver admin reply (photo or text)
        sent_ok = False
        try:
            if is_photo:
                await context.bot.send_photo(
                    chat_id=target_user_id,
                    photo=message.photo[-1].file_id,
                    caption=(
                        f"👨‍💼 *Reply from Admin:*\n\n{message.caption}"
                        if message.caption else "👨‍💼 *Reply from Admin:*"
                    ),
                    parse_mode='Markdown'
                )
            else:
                await context.bot.send_message(
                    chat_id=target_user_id,
                    text=f"👨‍💼 *Reply from Admin:*\n\n{message.text}",
                    parse_mode='Markdown'
                )
            sent_ok = True
            print(f"✅ Admin {'photo' if is_photo else 'text'} reply → user {target_user_id}")

        except Exception as e:
            err = str(e)
            print(f"❌ [admin_bot] Delivery failed to {target_user_id}: {err}")
            if "bot was blocked" in err.lower() or "user is deactivated" in err.lower():
                note = "The user has blocked this bot or their account is deactivated."
            elif "chat not found" in err.lower():
                note = "This user has never started a conversation with this bot."
            else:
                note = f"`{err}`"
            try:
                await message.reply_text(
                    f"❌ *Failed to send reply.*\n\n{note}",
                    parse_mode='Markdown'
                )
            except Exception:
                pass
            return

        # Confirm to admin (separate try — timeout here ≠ send failure)
        if sent_ok:
            try:
                await message.reply_text("✅ Reply sent!")
            except Exception:
                pass

    # ── USER PATH ─────────────────────────────────────────────────────────────
    else:
        first_name = user.first_name or "Unknown"

        # Users with hidden username can't be replied to — skip
        if not user.username:
            print(f"⚠️ [admin_bot] Skipped {first_name} ({user_id}) — no username")
            try:
                await message.reply_text(
                    "⚠️ *Unable to forward your message.*\n\n"
                    "Your Telegram username is hidden, so the admin cannot reply to you.\n\n"
                    "Please set a public username in *Telegram Settings → Username* and try again.",
                    parse_mode='Markdown'
                )
            except Exception:
                pass
            return

        # Forward message to admin (works for both text and photos)
        try:
            fwd = await context.bot.forward_message(
                chat_id=ADMIN_USER_ID,
                from_chat_id=message.chat_id,
                message_id=message.message_id
            )
            # Store map so admin reply can be routed back
            _msg_map[fwd.message_id] = user_id
            # Keep map bounded — evict oldest entry beyond 500
            if len(_msg_map) > 500:
                oldest = next(iter(_msg_map))
                del _msg_map[oldest]

            kind = "photo" if is_photo else "message"
            print(f"📨 Forwarded {kind} from {first_name} ({user_id}) → admin msg {fwd.message_id}")

        except Exception as e:
            print(f"❌ [admin_bot] forward_message failed for {user_id}: {e}")
            try:
                await message.reply_text(
                    "❌ *Error sending message.*\n\nPlease try again later.",
                    parse_mode='Markdown'
                )
            except Exception:
                pass
            return

        # Confirm to user and auto-delete after 3 s
        try:
            success_msg = await message.reply_text(
                "✅ *Message sent!* The admin will reply shortly.",
                parse_mode='Markdown'
            )

            async def _delete_later():
                await asyncio.sleep(3)
                try:
                    await context.bot.delete_message(
                        chat_id=message.chat_id,
                        message_id=success_msg.message_id
                    )
                except Exception:
                    pass

            asyncio.create_task(_delete_later())
        except Exception:
            pass  # timeout on confirm is fine — forward already succeeded


def main():
    print("🤖 Starting Admin Support Bot…")
    print(f"👨‍💼 Admin ID: {ADMIN_USER_ID}")

    try:
        application = (
            Application.builder()
            .token(ADMIN_BOT_TOKEN)
            .request(request)
            .build()
        )

        application.add_handler(CommandHandler("start", start_command))

        # Handle TEXT messages (excluding commands)
        application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
        )
        # Handle PHOTO messages
        application.add_handler(
            MessageHandler(filters.PHOTO, handle_message)
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
