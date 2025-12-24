# Video Repeat Issue - Fixed

## 🐛 Problem

Videos were repeating for users even though the bot was supposed to show all videos before looping.

### Root Cause

The issue was in the video tracking logic:

1. When a user requests a video, the bot tries a random message ID from the channel
2. Many message IDs are NOT videos (they could be text messages, photos, deleted messages, etc.)
3. **OLD BEHAVIOR**: When a message ID failed, it was marked as "seen"
4. This filled up the `seen_videos` list with non-video message IDs
5. User never actually saw those "videos" but they were counted as seen
6. Eventually the seen list would fill up and reset, causing actual videos to repeat

### Example of the Bug:
```
Channel has message IDs 1-100
Only IDs 10, 25, 40, 55, 70 are actual videos

User requests video:
- Try ID 5 → Text message → Fails → Marked as "seen" ❌
- Try ID 12 → Deleted message → Fails → Marked as "seen" ❌
- Try ID 25 → Video! → Success → Marked as "seen" ✅

After 10 requests, user's seen list: [5, 12, 25, 8, 33, 40, 15, 22, 55, 18]
- Only saw 3 actual videos (25, 40, 55)
- But 7 non-videos are marked as "seen"
- Seen list fills up quickly
- Resets and shows videos again (repeat!)
```

---

## ✅ Solution

### Changes Made:

1. **Separate Tracking Lists**:
   - `seen_videos`: Only actual videos that were successfully sent
   - `failed_message_ids`: Message IDs that failed (not videos)

2. **Updated `get_random_video()` Function**:
   - Now excludes BOTH seen videos AND failed message IDs
   - Only resets `seen_videos` when all actual videos are watched
   - Keeps `failed_message_ids` to avoid retrying non-videos

3. **Updated Error Handling**:
   - Failed messages are added to `failed_message_ids` list
   - NOT added to `seen_videos` list
   - Only successful video sends are marked as seen

### New Behavior:
```
Channel has message IDs 1-100
Only IDs 10, 25, 40, 55, 70 are actual videos

User requests video:
- Try ID 5 → Text message → Fails → Added to failed_message_ids ✅
- Try ID 12 → Deleted message → Fails → Added to failed_message_ids ✅
- Try ID 25 → Video! → Success → Added to seen_videos ✅

After 10 requests:
- seen_videos: [25, 40, 55, 70, 10] (5 actual videos) ✅
- failed_message_ids: [5, 12, 8, 33, 15, 22, 18, ...] (non-videos)

When all 5 videos are seen:
- Reset seen_videos to [] 
- Keep failed_message_ids (don't retry non-videos)
- User sees videos 25, 40, 55, 70, 10 again (proper loop!) ✅
```

---

## 📝 Code Changes

### 1. `video69_bot.py` - `get_random_video()` function

**Before:**
```python
def get_random_video(user_id):
    all_message_ids = list(range(MIN_MESSAGE_ID, MAX_MESSAGE_ID + 1))
    unseen_ids = [mid for mid in all_message_ids if mid not in user_state['seen_videos']]
    
    if not unseen_ids:
        user_state['seen_videos'] = []
        unseen_ids = all_message_ids
    
    return random.choice(unseen_ids)
```

**After:**
```python
def get_random_video(user_id):
    all_message_ids = list(range(MIN_MESSAGE_ID, MAX_MESSAGE_ID + 1))
    failed_ids = user_state.get('failed_message_ids', [])
    
    # Exclude BOTH seen videos AND failed message IDs
    unseen_ids = [mid for mid in all_message_ids 
                  if mid not in user_state['seen_videos'] 
                  and mid not in failed_ids]
    
    if not unseen_ids:
        print(f"🔄 User {user_id} has seen all videos! Resetting for loop.")
        user_state['seen_videos'] = []
        # Keep failed_ids to avoid retrying non-videos
        unseen_ids = [mid for mid in all_message_ids if mid not in failed_ids]
    
    return random.choice(unseen_ids)
```

### 2. `video69_bot.py` - Error handling in `send_random_video()`

**Before:**
```python
except Exception as e:
    print(f"⚠️ Message ID {message_id} failed: {e}")
    mark_video_as_seen(user_id, message_id)  # ❌ Wrong!
    continue
```

**After:**
```python
except Exception as e:
    print(f"⚠️ Message ID {message_id} failed: {e}")
    # Add to failed list, NOT seen list
    if 'failed_message_ids' not in user_states[user_id]:
        user_states[user_id]['failed_message_ids'] = []
    if message_id not in user_states[user_id]['failed_message_ids']:
        user_states[user_id]['failed_message_ids'].append(message_id)
        save_user_states(user_states)
    continue
```

### 3. `user_state_storage.py` - Added new field

**Before:**
```python
new_state = {
    'seen_videos': [],
    'hourly_count': 0,
    'last_reset': last_reset,
    'premium_until': None,
    'ad_link': None,
    'ad_token': None,
    'downloaded_count': 0
}
```

**After:**
```python
new_state = {
    'seen_videos': [],
    'hourly_count': 0,
    'last_reset': last_reset,
    'premium_until': None,
    'ad_link': None,
    'ad_token': None,
    'downloaded_count': 0,
    'failed_message_ids': []  # NEW: Track non-video messages
}
```

---

## 🔄 Migration for Existing Users

Existing users in `user_states.json` don't have the `failed_message_ids` field. The code handles this gracefully:

```python
failed_ids = user_state.get('failed_message_ids', [])  # Returns [] if not exists
```

However, their `seen_videos` list may contain non-video message IDs. To clean this up, you can:

### Option 1: Let it fix itself naturally
- Over time, failed IDs will be added to `failed_message_ids`
- `seen_videos` will only contain actual videos going forward
- Old incorrect data will be cleared on next reset

### Option 2: Manual cleanup (optional)
Run the migration script (see `clean_user_states.py`)

---

## 🎯 Expected Behavior Now

1. **First time user**:
   - Sees random videos from the channel
   - Each video shown only once
   - Non-video messages are skipped and tracked

2. **After seeing all videos**:
   - Console shows: "🔄 User {id} has seen all videos! Resetting seen list for loop playback."
   - `seen_videos` list is cleared
   - Videos start from beginning (loop)
   - Still skips non-video messages

3. **Video repeat**:
   - Only happens AFTER all actual videos are watched
   - Not random repeats anymore
   - Proper loop behavior

---

## 🧪 Testing

To verify the fix works:

1. **Check console logs**:
   ```
   ✅ Sent video message ID 25 to user 123
   ⚠️ Message ID 5 failed: Bad Request: message to copy not found
   ⚠️ Message ID 12 failed: Bad Request: message to copy not found
   ✅ Sent video message ID 40 to user 123
   ```

2. **Check user_states.json**:
   ```json
   {
     "123": {
       "seen_videos": [25, 40, 55],  // Only successful videos
       "failed_message_ids": [5, 12, 8, 33],  // Failed attempts
       ...
     }
   }
   ```

3. **User experience**:
   - User clicks "Next Video" multiple times
   - Should see different videos each time
   - No repeats until all videos are watched
   - Then loops from beginning

---

## 📊 Benefits

| Before | After |
|--------|-------|
| Videos repeat randomly | Videos only repeat after all are watched |
| `seen_videos` contains non-videos | `seen_videos` only contains actual videos |
| Inefficient (retries failed IDs) | Efficient (skips known failed IDs) |
| Confusing for users | Predictable loop behavior |

---

## 🚀 Deployment

1. ✅ Code changes applied
2. ✅ Backward compatible (existing users work fine)
3. ✅ No database migration required
4. ✅ Restart bot to apply changes

```bash
# Stop current bot
# Restart with new code
python video69_bot.py
```

---

**Fixed**: 2024-12-24
**Status**: ✅ Ready for production
**Breaking Changes**: None
**Migration Required**: No (optional cleanup available)
