import os
import re
import asyncio
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
from telegram.request import HTTPXRequest

# Load environment variables
load_dotenv()

# Admin Bot Token
ADMIN_BOT_TOKEN = "8599538827:AAEByXuZDRmLP-8vP3TBaSajr7OGGV06bYI"

# Admin Telegram ID (who will receive messages)
ADMIN_USER_ID = 7708376300  # @Deep12048

# Robust HTTP request settings (prevents "Timed out" on slow connections)
request = HTTPXRequest(
    connection_pool_size=8,
    connect_timeout=20.0,
    read_timeout=20.0,
    write_timeout=20.0,
    pool_timeout=10.0,
)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    user = update.effective_user
    user_id = user.id
    first_name = user.first_name or "there"

    if user_id == ADMIN_USER_ID:
        await update.message.reply_text(
            f"👨‍💼 *Welcome Admin!*\n\n"
            f"Hello {first_name}! 👋\n\n"
            f"📨 *How it works:*\n"
            f"1. Users send messages here\n"
            f"2. Their message + a *User Info* card are forwarded to you\n"
            f"3. Reply to the *User Info* card (contains User ID)\n"
            f"4. Your reply is sent directly to the user\n\n"
            f"✅ Waiting for user messages…",
            parse_mode='Markdown'
        )
        print(f"✅ Admin {user_id} started admin bot")
    else:
        await update.message.reply_text(
            f"👋 *Welcome to Admin Support Bot!*\n\n"
            f"Hello {first_name}!\n\n"
            f"This is the support channel for Videos Bot.\n\n"
            f"📝 Just type your message below and our admin will reply as soon as possible!\n\n"
            f"👨‍💼 *Admin Support Available* 👇",
            parse_mode='Markdown'
        )
        print(f"✅ User {user_id} started admin bot")


async def handle_user_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forward user messages to admin, or deliver admin replies to users."""
    user = update.effective_user
    user_id = user.id
    message = update.message

    # ── ADMIN PATH ────────────────────────────────────────────────────────────
    if user_id == ADMIN_USER_ID:
        if not message.reply_to_message:
            await message.reply_text(
                "ℹ️ *How to reply to users:*\n\n"
                "1. Wait for a user message\n"
                "2. Reply to the *User Info* card (the message with `User ID:`)\n"
                "3. Your reply will be sent directly to the user\n\n"
                "💡 You must reply to the User Info card, not send a new message.",
                parse_mode='Markdown'
            )
            return

        # Extract user ID from the User Info card text
        reply_text = message.reply_to_message.text or ""
        original_user_id = None

        if "User ID:" in reply_text:
            match = re.search(r'User ID:\s*`?(\d+)`?', reply_text)
            if match:
                original_user_id = int(match.group(1))

        # Also try forward_from as a fallback (only works if user has no privacy)
        if not original_user_id and message.reply_to_message.forward_from:
            original_user_id = message.reply_to_message.forward_from.id

        if not original_user_id:
            # ── User ID could not be found ──
            # This happens when:
            #   • Admin replied to the forwarded message instead of the User Info card
            #   • User has privacy settings that hide their ID from forward_from
            # In either case, we cannot message the user, so we log silently.
            print(
                f"⚠️ [admin_bot] Admin replied but no user ID found. "
                f"Reply was to: '{reply_text[:80]}'"
            )
            await message.reply_text(
                "⚠️ *Cannot send reply.*\n\n"
                "This user has privacy settings that hide their identity, "
                "so it's not possible to message them directly.\n\n"
                "💡 Please reply to the *User Info* card (the message that starts with "
                "`👤 User:`), not the forwarded message.",
                parse_mode='Markdown'
            )
            return

        # Send admin's reply to the user
        sent_ok = False
        try:
            await context.bot.send_message(
                chat_id=original_user_id,
                text=f"👨‍💼 *Reply from Admin:*\n\n{message.text}",
                parse_mode='Markdown'
            )
            sent_ok = True
            print(f"✅ Admin reply delivered to user {original_user_id}")
        except Exception as e:
            err = str(e)
            print(f"❌ [admin_bot] Failed to deliver reply to {original_user_id}: {err}")
            if "bot was blocked" in err.lower() or "user is deactivated" in err.lower():
                note = "The user has either blocked this bot or their account is deactivated."
            elif "chat not found" in err.lower():
                note = "This user has never started a conversation with this bot."
            else:
                note = f"`{err}`"
            # Only show error if the send actually failed
            try:
                await message.reply_text(
                    f"❌ *Failed to send reply.*\n\n{note}",
                    parse_mode='Markdown'
                )
            except Exception:
                pass  # confirmation itself timed out — ignore
            return

        # Confirm to admin — in its own try so a timeout here never looks like a send failure
        if sent_ok:
            try:
                await message.reply_text("✅ Reply sent to user!")
            except Exception:
                pass  # confirmation timed out but message was already delivered

    # ── USER PATH ─────────────────────────────────────────────────────────────
    else:
        first_name = user.first_name or "Unknown"
        username   = f"@{user.username}" if user.username else "_(hidden — cannot reply)_"
        can_reply  = bool(user.username) or True  # we use user_id to reply, not username

        try:
            # Step 1: Forward the actual message so admin sees the content
            await context.bot.forward_message(
                chat_id=ADMIN_USER_ID,
                from_chat_id=message.chat_id,
                message_id=message.message_id
            )
        except Exception as e:
            print(f"❌ [admin_bot] forward_message failed for user {user_id}: {e}")
            try:
                await message.reply_text(
                    "❌ *Error sending message.*\n\nPlease try again later.",
                    parse_mode='Markdown'
                )
            except Exception:
                pass
            return

        # Step 2: Send User Info card (separate try — forward already succeeded)
        privacy_note = (
            "\n⚠️ _Username hidden — reply via User Info card below_"
            if not user.username else ""
        )
        try:
            await context.bot.send_message(
                chat_id=ADMIN_USER_ID,
                text=(
                    f"👤 *User Info* — reply to THIS message to respond\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🏷 Name: {first_name}\n"
                    f"🔗 Username: {username}\n"
                    f"User ID: `{user_id}`\n"
                    f"━━━━━━━━━━━━━━━━━━━━━"
                    f"{privacy_note}"
                ),
                parse_mode='Markdown'
            )
        except Exception as e:
            print(f"⚠️ [admin_bot] User Info card failed for user {user_id}: {e}")
            # Non-fatal — forward already went through

        # Step 3: Confirm to user — separate try so a timeout here never shows an error
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
            pass  # confirmation timed out — message was already forwarded

        print(f"📨 Forwarded message from {first_name} ({user_id}) to admin")


def main():
    """Start the admin bot"""
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
        application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_user_messages)
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
        print("\n🔧 Troubleshooting:")
        print("1. Check your internet connection")
        print("2. Verify ADMIN_BOT_TOKEN is correct")


if __name__ == '__main__':
    main()
