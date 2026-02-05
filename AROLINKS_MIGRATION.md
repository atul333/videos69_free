# AroLinks API Migration

## Overview

Migrated from **VPLink** to **AroLinks** for URL shortening service.

---

## Changes Made

### 1. API Configuration Update

**Before (VPLink):**
```python
# VPLink API Configuration
VPLINK_API_TOKEN = "602a4c7facf8ec279b28f8763cd0f5e246252d59"
VPLINK_API_URL = "https://vplink.in/api"
```

**After (AroLinks):**
```python
# AroLinks API Configuration
AROLINKS_API_TOKEN = "cf0c76a4739d65e08d5990b0db1865b680f1de4a"
AROLINKS_API_URL = "https://arolinks.com/api"
```

### 2. Mandatory Channel Link Update

**Before:**
```python
MANDATORY_CHANNEL_LINK = "https://t.me/+Pkij1IcrzhJjMDBl"
```

**After:**
```python
MANDATORY_CHANNEL_LINK = "https://t.me/+A2FAI6TD5yZkN2Jl"
```

### 3. Function Updates

Updated `create_shortened_link()` function to use AroLinks API:
- Changed API URL construction
- Updated log messages to reference AroLinks
- Maintained same JSON response format

---

## AroLinks API Details

### API Endpoint
```
https://arolinks.com/api
```

### API Token
```
cf0c76a4739d65e08d5990b0db1865b680f1de4a
```

### Request Format
```
GET https://arolinks.com/api?api={TOKEN}&url={URL}&alias={ALIAS}
```

### Response Format (JSON)
```json
{
  "status": "success",
  "shortenedUrl": "https://arolinks.com/xxxxx"
}
```

### Response Format (TEXT)
Add `&format=text` to get plain text response:
```
https://arolinks.com/xxxxx
```

---

## Implementation Details

### How It Works

1. **User Clicks "Watch Ad" Button**
   ```
   User → Bot → create_shortened_link(user_id)
   ```

2. **Bot Creates Deep Link**
   ```python
   bot_deep_link = f"https://t.me/{BOT_USERNAME}?start={full_token}"
   # Example: https://t.me/Test_videos69_bot?start=123456_1734501584_abc123
   ```

3. **Bot Calls AroLinks API**
   ```python
   api_url = f"{AROLINKS_API_URL}?api={AROLINKS_API_TOKEN}&url={bot_deep_link}&alias={alias}"
   # Example: https://arolinks.com/api?api=cf0c76a4...&url=https://t.me/...&alias=v_123456_1734501584
   ```

4. **AroLinks Returns Shortened URL**
   ```json
   {
     "status": "success",
     "shortenedUrl": "https://arolinks.com/v_123456_1734501584"
   }
   ```

5. **User Flow**
   ```
   User → Clicks shortened link
        → AroLinks shows ads
        → Redirects to bot deep link
        → Bot verifies token
        → Grants premium access
   ```

---

## Code Example

```python
async def create_shortened_link(user_id):
    # Generate token
    timestamp = int(datetime.now().timestamp())
    full_token = f"{user_id}_{timestamp}_{encrypted_token}"
    
    # Create bot deep link
    bot_deep_link = f"https://t.me/{BOT_USERNAME}?start={full_token}"
    
    # Generate alias
    alias = f"v_{user_id}_{timestamp}"
    
    # Call AroLinks API
    api_url = f"{AROLINKS_API_URL}?api={AROLINKS_API_TOKEN}&url={bot_deep_link}&alias={alias}"
    
    async with aiohttp.ClientSession() as session:
        async with session.get(api_url) as response:
            if response.status == 200:
                result = await response.json()
                if result.get("status") == "success":
                    shortened_url = result.get("shortenedUrl", "").strip()
                    return shortened_url
```

---

## Expected Console Output

### Success
```
✅ Created AroLinks shortened link for user 123456: https://arolinks.com/v_123456_1734501584
   Token: 123456_1734501584_abc123def456
```

### Error Cases
```
❌ AroLinks API error: Invalid API token
```
or
```
❌ Failed to create shortened link: HTTP 500
```

---

## Testing Checklist

1. ✅ Start the bot
2. ✅ Click "Watch Ad" or "Unlock Premium" button
3. ✅ Verify shortened link is generated
   - ✅ Console shows: "✅ Created AroLinks shortened link..."
   - ✅ User receives message with AroLinks URL
   - ✅ URL format: `https://arolinks.com/...`
4. ✅ Click the shortened link
5. ✅ Verify ads are shown (AroLinks ad page)
6. ✅ Verify redirect to bot works
7. ✅ Verify premium access is granted

---

## Files Modified

- `video69_bot.py`:
  - Lines 75-77: API configuration (VPLink → AroLinks)
  - Line 91: Mandatory channel link updated
  - Lines 423-451: `create_shortened_link()` function updated

---

## Files Deleted

- `VPLINK_SWITCH.md` - Old VPLink documentation
- `GPLINKS_MIGRATION.md` - Old GPLinks migration documentation

---

## Benefits of AroLinks

- ✅ Reliable API with JSON/TEXT response formats
- ✅ Custom alias support for tracking
- ✅ Same response structure as VPLink (easy migration)
- ✅ Ad revenue generation for bot monetization

---

## Backward Compatibility

- ✅ Existing user states remain valid
- ✅ Only URL shortening service changed
- ✅ Premium access flow unchanged
- ✅ Token verification unchanged
- New URLs will use AroLinks
- Old VPLink URLs (if any) will continue to work until they expire

---

## Summary

- ✅ API provider: VPLink → AroLinks
- ✅ API URL: vplink.in → arolinks.com
- ✅ Mandatory channel link updated
- ✅ All references updated in code
- ✅ Console logs updated
- ✅ Old documentation files removed

---

The bot now uses AroLinks for URL shortening! 🎉
