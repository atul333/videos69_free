# GPLinks API Migration

## Change Summary
**Date:** December 16, 2025

### What Changed
Migrated from **VPLink** to **GPLinks** for URL shortening service.

## Why the Change?

Switched to GPLinks for better reliability and features.

## Changes Made

### 1. API Configuration (Lines 68-70)

**Before (VPLink):**
```python
# VPLink API Configuration
VPLINK_API_TOKEN = "602a4c7facf8ec279b28f8763cd0f5e246252d59"
VPLINK_API_URL = "https://vplink.in/api"
```

**After (GPLinks):**
```python
# GPLinks API Configuration (replaced VPLink)
GPLINK_API_TOKEN = "a1b06e56b6c9bf905ea9671e1e9ae508d5a302e5"
GPLINK_API_URL = "https://api.gplinks.com/api"
```

### 2. Link Creation Function (Lines 369-432)

**Key Changes:**

**API URL Format:**
- **Before:** `https://vplink.in/api?api=TOKEN&url=URL&alias=ALIAS&format=text`
- **After:** `https://api.gplinks.com/api?api=TOKEN&url=URL&alias=ALIAS`

**Response Format:**
- **Before:** Plain text response
- **After:** JSON response

**Response Handling:**

**Before (VPLink - Text):**
```python
shortened_url = await response.text()
shortened_url = shortened_url.strip()
return shortened_url
```

**After (GPLinks - JSON):**
```python
result = await response.json()

if result.get("status") == "success":
    shortened_url = result.get("shortenedUrl", "").strip()
    return shortened_url
else:
    error_msg = result.get("message", "Unknown error")
    print(f"❌ GPLinks API error: {error_msg}")
    return None
```

## GPLinks API Details

### API Endpoint
```
https://api.gplinks.com/api
```

### API Token
```
a1b06e56b6c9bf905ea9671e1e9ae508d5a302e5
```

### Request Format
```
GET https://api.gplinks.com/api?api={TOKEN}&url={URL}&alias={ALIAS}
```

### Parameters
- **api** (required): Your API token
- **url** (required): The destination URL to shorten
- **alias** (optional): Custom alias for the short link
- **format** (optional): Response format (default: JSON, can be "text")

### JSON Response Format

**Success:**
```json
{
  "status": "success",
  "shortenedUrl": "https://gplinks.com/xxxxx"
}
```

**Error:**
```json
{
  "status": "error",
  "message": "Error description"
}
```

## How It Works

### 1. User Clicks "Watch Ad"
```
User → Bot → create_shortened_link(user_id)
```

### 2. Bot Creates Deep Link
```python
bot_deep_link = f"https://t.me/{BOT_USERNAME}?start={full_token}"
# Example: https://t.me/Videos1_69_bot?start=123456_1734328339_a1b2c3d4
```

### 3. Bot Calls GPLinks API
```python
api_url = f"{GPLINK_API_URL}?api={GPLINK_API_TOKEN}&url={bot_deep_link}&alias={alias}"
# Example: https://api.gplinks.com/api?api=a1b06e56...&url=https://t.me/...&alias=v_123456_1734328339
```

### 4. GPLinks Returns Shortened URL
```json
{
  "status": "success",
  "shortenedUrl": "https://gplinks.com/v_123456_1734328339"
}
```

### 5. User Clicks Shortened Link
```
User → GPLinks (shows ads) → Redirects to Telegram bot → Bot verifies token → Grants premium
```

## Benefits of GPLinks

✅ **JSON Response** - Easier error handling
✅ **Status Field** - Clear success/error indication
✅ **Error Messages** - Detailed error information
✅ **Reliable Service** - Better uptime
✅ **Custom Aliases** - Same as VPLink

## Testing

### Test the Integration

1. **Start the bot**
2. **Reach video limit**
3. **Click "Watch Ad" button**
4. **Verify:**
   - ✅ Console shows: "✅ Created GPLinks shortened link for user X: https://gplinks.com/..."
   - ✅ User receives message with GPLinks URL
   - ✅ Clicking link shows ads
   - ✅ After ads, redirects to bot
   - ✅ Premium access granted

### Console Output

**Success:**
```
✅ Created GPLinks shortened link for user 123456: https://gplinks.com/v_123456_1734328339
   Token: 123456_1734328339_a1b2c3d4
```

**Error:**
```
❌ GPLinks API error: Invalid API token
```
or
```
❌ Failed to create shortened link: HTTP 500
```

## Comparison: VPLink vs GPLinks

| Feature | VPLink | GPLinks |
|---------|--------|---------|
| **API URL** | vplink.in/api | api.gplinks.com/api |
| **Response Format** | Text (with &format=text) | JSON (default) |
| **Success Check** | Check if text is URL | Check status field |
| **Error Handling** | No error details | Error message provided |
| **Alias Support** | ✅ Yes | ✅ Yes |
| **Reliability** | Good | Better |

## Migration Checklist

- ✅ Updated API token
- ✅ Updated API URL
- ✅ Changed response handling (text → JSON)
- ✅ Added error message handling
- ✅ Updated console logging
- ✅ Tested link creation
- ✅ Tested ad flow
- ✅ Tested premium grant

## Files Modified

1. **video69_bot.py**
   - Lines 68-70: API configuration
   - Lines 369-432: Link creation function

## Backward Compatibility

- ✅ **No data migration needed**
- ✅ **Existing tokens still work**
- ✅ **Same user flow**
- ✅ **Same premium grant logic**

Only the URL shortening service changed, everything else remains the same.

## Troubleshooting

### Issue: "GPLinks API error: Invalid API token"
**Solution:** Verify the API token is correct

### Issue: "Failed to create shortened link: HTTP 403"
**Solution:** Check if API token has expired or is invalid

### Issue: Link doesn't redirect properly
**Solution:** 
1. Check if bot username is correct
2. Verify token format
3. Test the deep link directly

## Summary

**What changed:**
- ✅ API provider: VPLink → GPLinks
- ✅ Response format: Text → JSON
- ✅ Error handling: Improved

**What stayed the same:**
- ✅ User experience
- ✅ Premium grant flow
- ✅ Token generation
- ✅ Deep link format

The migration is complete and the bot now uses GPLinks for URL shortening! 🎉
