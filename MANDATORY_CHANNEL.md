# Mandatory Channel Join Feature

## Overview
Users MUST join the specified channel before they can use the bot. This is a forced subscription feature.

## Channel Configuration

```python
# In bot.py
MANDATORY_CHANNEL = "@+A2FAI6TD5yZkN2Jl"
MANDATORY_CHANNEL_LINK = "https://t.me/+A2FAI6TD5yZkN2Jl"
```

## How It Works

### 1. User Starts Bot (`/start`)
```
User sends /start
→ Bot checks if user is a member of the mandatory channel
→ If NOT a member: Show join message with buttons
→ If IS a member: Show welcome message
```

### 2. Join Message (Not a Member)
```
👋 Welcome User!

🔒 To use this bot, you must join our channel first.

📢 Steps:
1. Click 'Join Channel' button below
2. Join the channel
3. Click 'Refresh' button to verify

After joining, you'll get full access to the bot! 🎬

[📢 Join Channel] [🔄 Refresh]
```

### 3. User Clicks "Refresh"
```
Bot checks membership again
→ If joined: Show verified message + welcome
→ If not joined: Show "not joined yet" message
```

### 4. Verified Message (After Joining)
```
✅ Verified!

Welcome User! 👋

You've successfully joined the channel!

🎥 You can watch 10 videos per day for free!

💎 Want unlimited access? Watch an ad to get 12 hours of premium!

Click below to start watching! 🍿

[🎬 Get First Video]
```

### 5. Not Joined Message (Still Not a Member)
```
❌ Not Joined Yet

You haven't joined the channel yet.

📢 Please:
1. Click 'Join Channel' button
2. Join the channel
3. Click 'Refresh' again

You must join to use this bot! 🔒

[📢 Join Channel] [🔄 Refresh]
```

## User Flow Diagram

```
┌─────────────────┐
│  User /start    │
└────────┬────────┘
         │
         ▼
┌─────────────────────┐
│ Check Membership    │
└────────┬────────────┘
         │
    ┌────┴────┐
    │         │
    ▼         ▼
 NOT      IS
MEMBER   MEMBER
    │         │
    │         ▼
    │    ┌──────────┐
    │    │ Welcome  │
    │    │ Message  │
    │    └──────────┘
    │
    ▼
┌──────────────┐
│ Join Message │
│ with Buttons │
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ User Clicks  │
│ Join Channel │
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ User Joins   │
│ Channel      │
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ User Clicks  │
│ Refresh      │
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ Bot Checks   │
│ Membership   │
└──────┬───────┘
       │
    ┌──┴──┐
    │     │
    ▼     ▼
 JOINED  NOT
         JOINED
    │     │
    │     ▼
    │  ┌────────────┐
    │  │ Show Error │
    │  │ Try Again  │
    │  └────────────┘
    │
    ▼
┌──────────┐
│ Verified │
│ Welcome  │
└──────────┘
```

## Code Implementation

### Check Membership Function
```python
async def check_channel_membership(context, user_id) -> bool:
    """Check if user is a member of the mandatory channel"""
    try:
        member = await context.bot.get_chat_member(
            chat_id=MANDATORY_CHANNEL,
            user_id=user_id
        )
        
        # Check status
        if member.status in ['member', 'administrator', 'creator']:
            return True
        else:
            return False
    except Exception as e:
        print(f"⚠️ Error checking membership: {e}")
        return False
```

### In start_command()
```python
# Check if user is a member
is_member = await check_channel_membership(context, user_id)

if not is_member:
    # Show join message with buttons
    keyboard = [
        [InlineKeyboardButton("📢 Join Channel", url=MANDATORY_CHANNEL_LINK)],
        [InlineKeyboardButton("🔄 Refresh", callback_data='check_membership')]
    ]
    # ... show message
    return  # Stop here
```

### In button_callback()
```python
if query.data == 'check_membership':
    # Check membership again
    is_member = await check_channel_membership(context, user_id)
    
    if is_member:
        # Show verified message
    else:
        # Show "not joined yet" message
```

## Member Status Types

The bot recognizes these statuses as "member":
- **member**: Regular channel member
- **administrator**: Channel admin
- **creator**: Channel owner/creator

These statuses are NOT recognized:
- **left**: User left the channel
- **kicked**: User was banned
- **restricted**: User is restricted

## Benefits

✅ **Forced Subscription**: Users must join to use bot
✅ **Channel Growth**: Automatically grows your channel
✅ **Verification**: Real-time membership checking
✅ **User-Friendly**: Clear instructions and buttons
✅ **Refresh Option**: Users can verify without restarting
✅ **No Bypass**: Cannot use bot without joining

## Important Notes

⚠️ **Bot Must Be Admin**
- The bot must be an admin in the mandatory channel
- Otherwise, it cannot check membership status
- Add bot as admin with at least "View Members" permission

⚠️ **Private Channels**
- Works with private channels (invite links)
- Works with public channels (@username)
- Channel ID format: `@+A2FAI6TD5yZkN2Jl` for private channels

⚠️ **Error Handling**
- If membership check fails, assumes user is not a member
- Logs error to console for debugging
- User can try again by clicking Refresh

## Testing

### Test 1: New User (Not a Member)
```
1. User sends /start
2. Should see join message with buttons
3. Should NOT be able to use bot
```

### Test 2: User Joins Channel
```
1. User clicks "Join Channel"
2. Joins the channel
3. Clicks "Refresh"
4. Should see verified message
5. Can now use bot
```

### Test 3: Existing Member
```
1. User already in channel
2. Sends /start
3. Should see welcome message directly
4. Can use bot immediately
```

### Test 4: User Leaves Channel
```
1. User was a member
2. Leaves the channel
3. Tries to use bot
4. Should be blocked (if checked again)
```

## Console Output

**User Not a Member:**
```
⚠️ User 123456789 not a member of mandatory channel
```

**User Joins:**
```
✅ User 123456789 verified as channel member
```

**Membership Check Error:**
```
⚠️ Error checking channel membership for user 123456789: [error details]
```

## Customization

### Change Channel
```python
# Update these constants
MANDATORY_CHANNEL = "@your_channel"
MANDATORY_CHANNEL_LINK = "https://t.me/your_channel"
```

### Change Messages
Edit the text in:
- `start_command()` - Join message
- `button_callback()` - Verified/Not joined messages

### Disable Feature
Comment out the membership check:
```python
# is_member = await check_channel_membership(context, user_id)
# if not is_member:
#     return
```

## Summary

The mandatory channel join feature:
- ✅ Forces users to join channel before using bot
- ✅ Provides clear instructions and buttons
- ✅ Verifies membership in real-time
- ✅ Allows users to refresh verification
- ✅ Grows your channel automatically
- ✅ Cannot be bypassed

Users MUST join the channel to use any bot features! 🔒
