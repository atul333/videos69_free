# VPLink API Switch (Back from GPLinks)

## Change Summary
**Date:** December 18, 2025

### What Changed
Switched back from **GPLinks** to **VPLink** for URL shortening service.

## API Details

### VPLink Configuration

**API Endpoint:**
```
https://vplink.in/api
```

**API Token:**
```
602a4c7facf8ec279b28f8763cd0f5e246252d59
```

**Request Format:**
```
GET https://vplink.in/api?api={TOKEN}&url={URL}&alias={ALIAS}
```

**Response Format (JSON):**
```json
{
  "status": "success",
  "shortenedUrl": "https://vplink.in/xxxxx"
}
```

**Response Format (Text):**
```
https://vplink.in/xxxxx
```
(Add `&format=text` to get plain text response)

## Changes Made

### 1. API Configuration (Lines 68-70)

**Before (GPLinks):**
```python
# GPLinks API Configuration (replaced VPLink)
GPLINK_API_TOKEN = "a1b06e56b6c9bf905ea9671e1e9ae508d5a302e5"
GPLINK_API_URL = "https://api.gplinks.com/api"
```

**After (VPLink):**
```python
# VPLink API Configuration (switched back from GPLinks)
VPLINK_API_TOKEN = "602a4c7facf8ec279b28f8763cd0f5e246252d59"
VPLINK_API_URL = "https://vplink.in/api"
```

### 2. Link Creation Function (Lines 395-431)

**Updated references:**
- `GPLINK_API_URL` → `VPLINK_API_URL`
- `GPLINK_API_TOKEN` → `VPLINK_API_TOKEN`
- `"GPLinks"` → `"VPLink"` (in comments and logs)

**API URL:**
```python
# Before
api_url = f"{GPLINK_API_URL}?api={GPLINK_API_TOKEN}&url={bot_deep_link}&alias={alias}"

# After
api_url = f"{VPLINK_API_URL}?api={VPLINK_API_TOKEN}&url={bot_deep_link}&alias={alias}"
```

**Console Logging:**
```python
# Before
print(f"✅ Created GPLinks shortened link for user {user_id}: {shortened_url}")
print(f"❌ GPLinks API error: {error_msg}")

# After
print(f"✅ Created VPLink shortened link for user {user_id}: {shortened_url}")
print(f"❌ VPLink API error: {error_msg}")
```

## API Comparison

| Feature | GPLinks | VPLink |
|---------|---------|--------|
| **API URL** | api.gplinks.com/api | vplink.in/api |
| **Response Format** | JSON (default) | JSON (default) or Text |
| **Success Field** | `status: "success"` | `status: "success"` |
| **URL Field** | `shortenedUrl` | `shortenedUrl` |
| **Error Handling** | `message` field | `message` field |
| **Alias Support** | ✅ Yes | ✅ Yes |

## How It Works

### 1. User Clicks "Watch Ad"
```
User → Bot → create_shortened_link(user_id)
```

### 2. Bot Creates Deep Link
```python
bot_deep_link = f"https://t.me/{BOT_USERNAME}?start={full_token}"
# Example: https://t.me/Videos1_69_bot?start=123456_1734501584_a1b2c3d4
```

### 3. Bot Calls VPLink API
```python
api_url = f"{VPLINK_API_URL}?api={VPLINK_API_TOKEN}&url={bot_deep_link}&alias={alias}"
# Example: https://vplink.in/api?api=602a4c7f...&url=https://t.me/...&alias=v_123456_1734501584
```

### 4. VPLink Returns Shortened URL
```json
{
  "status": "success",
  "shortenedUrl": "https://vplink.in/v_123456_1734501584"
}
```

### 5. User Flow
```
User → Clicks shortened link
     → VPLink shows ads
     → Redirects to Telegram bot
     → Bot verifies token
     → Grants premium access
```

## Response Handling

### JSON Response (Current Implementation)
```python
result = await response.json()

if result.get("status") == "success":
    shortened_url = result.get("shortenedUrl", "").strip()
    return shortened_url
else:
    error_msg = result.get("message", "Unknown error")
    print(f"❌ VPLink API error: {error_msg}")
    return None
```

### Alternative: Text Response
If you want plain text response instead of JSON, add `&format=text`:
```python
api_url = f"{VPLINK_API_URL}?api={VPLINK_API_TOKEN}&url={bot_deep_link}&alias={alias}&format=text"

shortened_url = await response.text()
shortened_url = shortened_url.strip()
```

## Console Output

### Success
```
✅ Created VPLink shortened link for user 123456: https://vplink.in/v_123456_1734501584
   Token: 123456_1734501584_a1b2c3d4
```

### Error
```
❌ VPLink API error: Invalid API token
```
or
```
❌ Failed to create shortened link: HTTP 500
```

## Testing

### Test the Integration

1. **Start the bot**
2. **Reach video limit**
3. **Click "Watch Ad" button**
4. **Verify:**
   - ✅ Console shows: "✅ Created VPLink shortened link..."
   - ✅ User receives message with VPLink URL
   - ✅ URL format: `https://vplink.in/...`
   - ✅ Clicking link shows ads
   - ✅ After ads, redirects to bot
   - ✅ Premium access granted

## Files Modified

**video69_bot.py:**
- Lines 68-70: API configuration (GPLinks → VPLink)
- Lines 395-431: Link creation function (updated API calls and logging)

## Benefits of VPLink

✅ **Reliable Service** - Proven track record
✅ **JSON Response** - Easy error handling
✅ **Text Option** - Can use plain text if needed
✅ **Custom Aliases** - Supports custom short URLs
✅ **Error Messages** - Detailed error information

## Migration Notes

### No Data Migration Needed
- ✅ Existing tokens still work
- ✅ Same user flow
- ✅ Same premium grant logic
- ✅ Only URL shortening service changed

### Backward Compatibility
- Old GPLinks URLs (if any exist) will continue to work
- New URLs will use VPLink
- No user impact

## Summary

**What changed:**
- ✅ API provider: GPLinks → VPLink
- ✅ API URL: api.gplinks.com → vplink.in
- ✅ Console logging updated

**What stayed the same:**
- ✅ JSON response format
- ✅ Error handling
- ✅ User experience
- ✅ Premium grant flow
- ✅ Token generation

The bot now uses VPLink for URL shortening! 🎉
