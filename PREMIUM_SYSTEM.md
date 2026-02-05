# Premium Ad System - Complete Flow

## Overview
The bot now has a fully automated premium access system with ad verification. Users watch ads to get 12 hours of unlimited video access.

## Key Features

### 1. **Automatic Premium Activation**
- When users click the ad link, they're redirected back to the bot with a unique token
- Premium is activated **automatically** - no manual "I Watched the Ad" button needed
- Users see a premium activation message instead of the welcome message

### 2. **Link Reuse Prevention**
- Each ad link can only be used **once**
- Tokens are tracked in the `used_tokens` set
- If someone tries to reuse a link, they get an error message
- Links are user-specific - can't share links with others

### 3. **Premium Status Detection**
- When premium users type `/start`, they see their remaining time
- Non-premium users see the regular welcome message
- Premium users don't see the welcome message repeatedly

### 4. **Daily Limits for Free Users**
- Free users: **2 videos per day**
- Premium users: **Unlimited videos for 12 hours**
- Daily limit resets every 24 hours

## User Flow

### For New Users (No Premium)
1. User types `/start`
2. Sees welcome message: "You can watch 2 videos per day for free!"
3. Clicks "Get First Video"
4. After 2 videos, sees "Daily Limit Reached" with "Watch Ad" button

### For Users Watching Ad
1. User clicks "Watch Ad" button
2. Gets a unique shortened link (via AroLinks)
3. Clicks the link, completes ad verification
4. Gets redirected back to bot with token in URL
5. **Premium activates automatically!**
6. Sees message: "✅ Premium Access Activated! You now have unlimited access for 12 hours!"
7. Can immediately start watching videos

### For Premium Users
1. User types `/start`
2. Sees: "Welcome Back! You have Premium Access! Time remaining: Xh Ym"
3. Can watch unlimited videos
4. After 12 hours, premium expires and they're back to 2 videos/day

### For Users Trying to Reuse Links
1. User clicks an already-used ad link
2. Gets redirected to bot
3. Sees error: "❌ This link has already been used!"
4. Must watch a new ad to get premium

## Technical Implementation

### Token System
```
Format: {user_id}_{timestamp}_{encrypted_hash}
Example: 123456789_1732683600_a1b2c3d4e5f6g7h8
```

- **user_id**: Identifies who the link belongs to
- **timestamp**: When the link was created
- **encrypted_hash**: SHA256 hash for security

### Token Validation
1. Check if token is in `used_tokens` set (prevent reuse)
2. Parse token to extract user_id
3. Verify token belongs to the current user
4. Verify token matches the one stored in user's state
5. If all checks pass: grant premium + mark token as used

### State Management
```python
user_states = {
    user_id: {
        'seen_videos': [],           # Videos already watched
        'daily_count': 0,             # Videos watched today
        'last_reset': datetime,       # Last daily reset time
        'premium_until': datetime,    # Premium expiry time
        'ad_link': str,              # Current ad link (cleared after use)
        'ad_token': str              # Current token (cleared after use)
    }
}

used_tokens = set()  # All tokens that have been used
```

## Messages

### Premium Activation Message
```
✅ Premium Access Activated!

Hello [Name]! 👋

🎉 You now have unlimited access for the next 12 hours!

⏰ Your premium access will expire at: [TIME]

🎥 Click below to start watching unlimited videos!

Enjoy! 🍿
```

### Premium User Welcome Back
```
🎬 Welcome Back, [Name]! 🎬

✅ You have Premium Access!

⏰ Time remaining: [X]h [Y]m

🎥 Click below to watch unlimited videos!

Enjoy! 🍿
```

### Daily Limit Reached
```
❌ Daily Limit Reached!

You've exceeded the 2 link access limit for today. Watch an ad to unlock full bot access or try again tomorrow at 12:00 AM IST
```

### Link Already Used
```
❌ This link has already been used!

Each ad link can only be used once. Please watch a new ad to get premium access.
```

### Wrong User Trying to Use Link
```
❌ This link was created for a different user!

Please watch the ad yourself to get premium access.
```

## Security Features

1. **Encrypted Tokens**: SHA256 hash prevents tampering
2. **User-Specific Links**: Tokens contain user_id
3. **One-Time Use**: Tokens tracked in `used_tokens` set
4. **Expiry Tracking**: Premium access has clear expiry time
5. **Token Validation**: Multiple checks before granting premium

## Benefits

✅ **No Manual Verification**: Premium activates automatically
✅ **No Link Sharing**: Each link works only for the creator
✅ **No Link Reuse**: Each link can only be used once
✅ **Clear Status**: Users always know their premium status
✅ **Smooth UX**: No confusing "I Watched the Ad" buttons
✅ **Secure**: Multiple validation layers

## Configuration

```python
DAILY_VIDEO_LIMIT = 2              # Free videos per day
PREMIUM_DURATION_HOURS = 12        # Premium duration
AROLINKS_API_TOKEN = "..."           # AroLinks API token
BOT_USERNAME = "Videos1_69_bot"    # Bot username for deep links
SECRET_KEY = "..."                 # Secret for token encryption
```

## Testing Checklist

- [ ] New user can watch 2 videos
- [ ] Daily limit message appears after 2 videos
- [ ] Ad link is generated correctly
- [ ] Clicking ad link activates premium automatically
- [ ] Premium activation message shows correct expiry time
- [ ] Premium users can watch unlimited videos
- [ ] Premium expires after 12 hours
- [ ] Used ad links show error message
- [ ] Different users can't use each other's links
- [ ] Premium users see welcome back message with time remaining
