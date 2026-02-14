# VPLinks API Switch - February 14, 2026

## Overview
Switched back from AroLinks API to VPLinks API with updated credentials.

---

## API Configuration Changes

### Before (AroLinks):
```python
AROLINKS_API_TOKEN = "cf0c76a4739d65e08d5990b0db1865b680f1de4a"
AROLINKS_API_URL = "https://arolinks.com/api"
```

### After (VPLinks):
```python
VPLINK_API_TOKEN = "686ddc23e543449e56029d3de0a59e19677617de"
VPLINK_API_URL = "https://vplink.in/api"
```

---

## VPLinks API Details

### Endpoint:
```
https://vplink.in/api
```

### API Token:
```
686ddc23e543449e56029d3de0a59e19677617de
```

### Request Format:
```
GET https://vplink.in/api?api={TOKEN}&url={URL}&alias={ALIAS}
```

### Response Format (JSON):
```json
{
  "status": "success",
  "shortenedUrl": "https://vplink.in/xxxxx"
}
```

### Response Format (TEXT):
```
Add &format=text to get plain text response
https://vplink.in/api?api={TOKEN}&url={URL}&alias={ALIAS}&format=text
```

---

## Code Changes

### Main Bot File (`video69_bot.py`)

**Lines 75-77:** API Configuration
```python
# BEFORE:
# AroLinks API Configuration
AROLINKS_API_TOKEN = "cf0c76a4739d65e08d5990b0db1865b680f1de4a"
AROLINKS_API_URL = "https://arolinks.com/api"

# AFTER:
# VPLinks API Configuration
VPLINK_API_TOKEN = "686ddc23e543449e56029d3de0a59e19677617de"
VPLINK_API_URL = "https://vplink.in/api"
```

**Lines 420-455:** Function Implementation
- Updated comments from "AroLinks" to "VPLinks"
- Changed API URL construction to use VPLinks
- Updated response handling comments
- Changed console log messages to show "VPLinks" instead of "AroLinks"

---

## Files Modified

1. ✅ `video69_bot.py` - API configuration and function implementation

---

## Files Deleted

1. ✅ `AROLINKS_MIGRATION.md` - AroLinks migration documentation
2. ✅ `CHANGES_SUMMARY.md` - AroLinks changes summary

---

## Files Created

1. ✅ `VPLINKS_SWITCH.md` - This file (VPLinks switch documentation)

---

## Expected Console Output

### Success Message:
```
✅ Created VPLinks shortened link for user 123456: https://vplink.in/v_123456_1739529058
   Token: 123456_1739529058_abc123...
```

### Error Messages:
```
❌ VPLinks API error: Invalid API token
❌ Failed to create shortened link: HTTP 403
❌ Error creating shortened link: Connection timeout
```

---

## Testing Checklist

### VPLinks API:
- [ ] Bot starts without errors
- [ ] "Watch Ad" button generates VPLinks URL
- [ ] Console shows "Created VPLinks shortened link..."
- [ ] Shortened URL format is `https://vplink.in/...`
- [ ] Clicking shortened link shows VPLinks ads
- [ ] After ads, user is redirected to bot
- [ ] Premium access is granted correctly
- [ ] Token validation works as expected

### Functionality:
- [ ] Free users can watch videos
- [ ] Daily limits work correctly
- [ ] Premium activation works
- [ ] Download limits enforced
- [ ] Broadcast messages sent correctly

---

## Configuration Summary

### Current Configuration:
```python
# VPLinks API
VPLINK_API_TOKEN = "686ddc23e543449e56029d3de0a59e19677617de"
VPLINK_API_URL = "https://vplink.in/api"

# Bot Settings
BOT_USERNAME = "Test_videos69_bot"
DAILY_VIDEO_LIMIT = 3                      # Free videos per day
PREMIUM_DURATION_HOURS = 12                # Premium duration
DOWNLOADABLE_VIDEOS_LIMIT = 2              # Free downloads per day
PREMIUM_DOWNLOADABLE_VIDEOS_LIMIT = 20     # Premium downloads per day

# Mandatory Channel
MANDATORY_CHANNEL = -1003363678621
MANDATORY_CHANNEL_LINK = "https://t.me/+A2FAI6TD5yZkN2Jl"
```

---

## PHP Usage Examples

### Using JSON Response:
```php
$long_url = urlencode('yourdestinationlink.com');
$api_token = '686ddc23e543449e56029d3de0a59e19677617de';
$api_url = "https://vplink.in/api?api={$api_token}&url={$long_url}&alias=CustomAlias";
$result = @json_decode(file_get_contents($api_url),TRUE);
if($result["status"] === 'error') {
    echo $result["message"];
} else {
    echo $result["shortenedUrl"];
}
```

### Using Plain Text Response:
```php
$long_url = urlencode('yourdestinationlink.com');
$api_token = '686ddc23e543449e56029d3de0a59e19677617de';
$api_url = "https://vplink.in/api?api={$api_token}&url={$long_url}&alias=CustomAlias&format=text";
$result = @file_get_contents($api_url);
if( $result ){
    echo $result;
}
```

---

## Backward Compatibility

### User States:
- ✅ Existing user states remain valid
- ✅ Premium access preserved
- ✅ Video history intact
- ✅ Download counts maintained

### Old Links:
- ⚠️ Old AroLinks URLs (if any) will continue to work until they expire
- ✅ New links will use VPLinks
- ✅ Token verification unchanged

---

## Summary

✅ **Successfully switched from AroLinks to VPLinks**  
✅ **API configuration updated with new token**  
✅ **All code references updated**  
✅ **AroLinks documentation removed**  
✅ **Ready for testing and deployment**

---

**Date:** February 14, 2026  
**Status:** ✅ Complete
