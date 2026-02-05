import os
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Load environment variables
load_dotenv()

# Admin Bot Token
ADMIN_BOT_TOKEN = "8599538827:AAEByXuZDRmLP-8vP3TBaSajr7OGGV06bYI"

# Admin Telegram ID (who will receive messages)
ADMIN_USER_ID = 7708376300  # @Deep12048


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    user = update.effective_user
    user_id = user.id
    first_name = user.first_name or "there"
    
    # Check if user is admin
    if user_id == ADMIN_USER_ID:
        # Admin welcome message
        admin_message = f"""
👨‍💼 **Welcome Admin!**

Hello {first_name}! 👋

This is your admin support bot.

📨 **How it works:**
1. Users send messages here
2. Their messages are forwarded to you
3. Reply to the **User Info** message (the one with User ID)
4. Your reply is sent to the user

💡 **To reply to a user:**
- Wait for a user message (it will be forwarded to you)
- Click "Reply" on the **User Info** message (contains User ID)
- Type your response
- Send

✅ You're all set! Waiting for user messages...
        """
        
        await update.message.reply_text(
            admin_message,
            parse_mode='Markdown'
        )
        print(f"✅ Admin {user.id} started admin bot")
    else:
        # Regular user welcome message
        welcome_message = f"""
👋 **Welcome to Admin Support Bot!**

Hello {first_name}!

This is the support bot for Videos Bot.

📝 **How to use:**
Simply send your message here, and it will be forwarded to our admin.

💬 **Example:**
"I'm having trouble watching videos"
"How do I get premium access?"
"Videos are not loading"

The admin will reply to you as soon as possible!

👨‍💼 **Admin Support Available**

Just type your message below! 👇
        """
        
        await update.message.reply_text(
            welcome_message,
            parse_mode='Markdown'
        )
        print(f"✅ User {user.id} started admin bot")


async def handle_user_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forward user messages to admin"""
    user_id = update.effective_user.id
    message = update.message
    
    # Check if message is from admin
    if user_id == ADMIN_USER_ID:
        # Admin is replying to a user
        if message.reply_to_message:
            # Try to get user ID from the user info message
            original_user_id = None
            
            # Check if replying to the forwarded message (has forward_from)
            if message.reply_to_message.forward_from:
                original_user_id = message.reply_to_message.forward_from.id
            # Check if replying to the user info message (contains User ID)
            elif message.reply_to_message.text and "User ID:" in message.reply_to_message.text:
                # Extract user ID from the text
                try:
                    # Debug: Print the message text
                    print(f"📝 Processing reply to message: {message.reply_to_message.text}")
                    
                    lines = message.reply_to_message.text.split('\n')
                    for line in lines:
                        if "User ID:" in line:
                            print(f"📝 Found User ID line: {line}")
                            # Extract just the numbers from the line
                            import re
                            match = re.search(r'User ID:\s*`?(\d+)`?', line)
                            if match:
                                original_user_id = int(match.group(1))
                                print(f"✅ Extracted User ID: {original_user_id}")
                                break
                except Exception as e:
                    print(f"⚠️ Error extracting user ID: {e}")
                    print(f"⚠️ Message text was: {message.reply_to_message.text}")
            
            if original_user_id:
                try:
                    # Forward admin's reply to the user (without prefix)
                    await context.bot.send_message(
                        chat_id=original_user_id,
                        text=message.text
                    )
                    
                    # Confirm to admin
                    await message.reply_text("✅ Reply sent to user!")
                    print(f"✅ Admin replied to user {original_user_id}")
                    
                except Exception as e:
                    await message.reply_text(f"❌ Error sending reply: {e}")
                    print(f"❌ Error sending admin reply: {e}")
            else:
                await message.reply_text(
                    "⚠️ **Could not find user ID**\n\n"
                    "Please reply to the **User Info** message (the one that contains 'User ID: `xxxxx`')\n\n"
                    "💡 Tip: Reply to the message with the user's details, not the forwarded message."
                )
        else:
            # Admin sent a message but not replying to anyone
            await message.reply_text(
                "ℹ️ **How to reply to users:**\n\n"
                "1. Wait for a user message (it will be forwarded to you)\n"
                "2. Reply to the **User Info** message (contains User ID)\n"
                "3. Your reply will be sent to the user\n\n"
                "💡 You must reply to the User Info message, not send a new message."
            )
    else:
        # Regular user sending a message - Forward to admin
        try:
            # Forward the message to admin (no additional user info message)
            await context.bot.forward_message(
                chat_id=ADMIN_USER_ID,
                from_chat_id=message.chat_id,
                message_id=message.message_id
            )
            
            # Send success message to user that will auto-delete after 2 seconds
            success_msg = await message.reply_text(
                "✅ **Message sent successfully!**",
                parse_mode='Markdown'
            )
            
            # Schedule deletion of success message after 2 seconds
            import asyncio
            async def delete_success_message():
                await asyncio.sleep(2)
                try:
                    await context.bot.delete_message(
                        chat_id=message.chat_id,
                        message_id=success_msg.message_id
                    )
                    print(f"🗑️ Deleted success message for user {user_id}")
                except Exception as e:
                    print(f"⚠️ Could not delete success message: {e}")
            
            # Run deletion in background
            asyncio.create_task(delete_success_message())
            
            print(f"📨 Forwarded message from user {user_id} to admin")
            
        except Exception as e:
            await message.reply_text(
                "❌ **Error!**\n\n"
                "Sorry, there was an error sending your message to the admin.\n"
                "Please try again later.",
                parse_mode='Markdown'
            )
            print(f"❌ Error forwarding message to admin: {e}")


def main():
    """Start the admin bot"""
    print("🤖 Starting Admin Support Bot...")
    print(f"👨‍💼 Admin ID: {ADMIN_USER_ID}")
    
    try:
        # Create the Application
        application = Application.builder().token(ADMIN_BOT_TOKEN).build()
        
        # Add command handlers
        application.add_handler(CommandHandler("start", start_command))
        
        # Add message handler for all text messages
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_user_messages))
        
        # Start the bot
        print("✅ Admin Support Bot is running!")
        print("📱 Bot: @videos69Admin_Bot")
        print("💬 Users can send messages, admin can reply!")
        
        # Run the bot
        application.run_polling(allowed_updates=Update.ALL_TYPES)
        
    except Exception as e:
        print(f"\n❌ Error starting admin bot: {e}")
        print("\n🔧 Troubleshooting tips:")
        print("1. Check your internet connection")
        print("2. Verify the ADMIN_BOT_TOKEN is correct")
        print("3. Make sure the bot token is valid")


if __name__ == '__main__':
    main()
