# Downloaded Video Auto-Deletion

## Change Summary
**Date:** December 16, 2025

### What Changed
Downloaded videos are now automatically deleted after 20 minutes, along with the confirmation message.

## Feature Overview

### Before
- Downloaded videos stayed in chat forever
- No deletion warning
- Chat could get cluttered

### After
- Downloaded videos auto-delete after 20 minutes
- Confirmation message shows deletion warning
- Both video and message deleted together
- Clean chat experience

## How It Works

### User Flow

**Step 1: User clicks Download button**
```
[📥 Download (2 left)]  ← User clicks
```

**Step 2: Downloadable video sent**
```
🎬 Video (unprotected - can be saved)
```

**Step 3: Confirmation message**
```
✅ Download Successful!

The video above can be saved/forwarded.
📥 You have 1 download(s) remaining.

⚠️ This video will be deleted after 20 minutes.  ← NEW WARNING!
```

**Step 4: After 20 minutes**
```
🗑️ Both video and confirmation message deleted automatically
```

## Implementation Details

### 1. Confirmation Message (Lines 1368-1393)

**Added deletion warning:**
```python
confirmation_msg = await context.bot.send_message(
    chat_id=query.message.chat_id,
    text=f"✅ **Download Successful!**\n\n"
         f"The video above can be saved/forwarded.\n"
         f"📥 You have {remaining} download(s) remaining.\n\n"
         f"⚠️ This video will be deleted after 20 minutes."  # ← NEW!
)
```

### 2. Deletion Scheduling (Lines 1395-1401)

**Schedule deletion:**
```python
# Schedule deletion of downloaded video and confirmation message after 20 minutes
context.job_queue.run_once(
    delete_messages,
    when=1200,  # 20 minutes in seconds
    data={'chat_id': query.message.chat_id, 'message_ids': [download_msg.message_id, confirmation_msg.message_id]},
    name=f"delete_download_{query.message.chat_id}_{download_msg.message_id}"
)
```

### 3. Console Logging

**Added log:**
```python
print(f"🗑️ Scheduled deletion of downloaded video {download_msg.message_id} in 20 minutes")
```

## Messages Updated

### Premium User (Has Downloads)
```
✅ Download Successful!

The video above can be saved/forwarded.
💎 Premium: You have 19/20 download(s) remaining.

⚠️ This video will be deleted after 20 minutes.
```

### Free User (Has Downloads)
```
✅ Download Successful!

The video above can be saved/forwarded.
📥 You have 1 download(s) remaining.

⚠️ This video will be deleted after 20 minutes.
```

### Last Download
```
✅ Download Successful!

The video above can be saved/forwarded.
🔒 This was your last download. Future videos will be protected.

⚠️ This video will be deleted after 20 minutes.
```

## Deletion Behavior

### What Gets Deleted
1. **Downloaded video** (the unprotected copy)
2. **Confirmation message** (the success message)

### When
- **Exactly 20 minutes** (1200 seconds) after download

### Why Both Messages
- Keeps chat clean
- Removes clutter
- User has already saved the video if they wanted it

## User Experience

### Timeline

**00:00** - User clicks Download
**00:01** - Video sent (unprotected)
**00:02** - Confirmation message sent with warning
**00:02-20:00** - User can save/forward the video
**20:00** - Both messages automatically deleted

### User Benefits
✅ **Clear warning** - Knows video will be deleted
✅ **Enough time** - 20 minutes to save the video
✅ **Clean chat** - Old downloads don't clutter
✅ **Same as regular videos** - Consistent 20-minute deletion

## Console Output

### When Download Happens
```
📥 User 123456 downloaded video 42 (1 downloads remaining)
🗑️ Scheduled deletion of downloaded video 789 in 20 minutes
```

### When Deletion Happens (20 minutes later)
```
🗑️ Deleted 2 messages in chat 123456
```

## Comparison: Regular vs Downloaded Videos

| Aspect | Regular Video | Downloaded Video |
|--------|--------------|------------------|
| **Protection** | Protected | Unprotected |
| **Can Save** | ❌ No | ✅ Yes |
| **Deletion Time** | 20 minutes | 20 minutes |
| **Warning Message** | ✅ Yes | ✅ Yes |
| **Messages Deleted** | Video + Warning | Video + Confirmation |

## Technical Details

### Deletion Function Used
```python
delete_messages()  # Same function used for regular videos
```

### Job Queue
```python
context.job_queue.run_once(
    delete_messages,
    when=1200,  # 20 minutes
    data={'chat_id': chat_id, 'message_ids': [video_id, message_id]},
    name=f"delete_download_{chat_id}_{video_id}"
)
```

### Message IDs Tracked
- `download_msg.message_id` - The downloaded video
- `confirmation_msg.message_id` - The success message

## Files Modified

**video69_bot.py:**
- Lines 1368-1393: Updated confirmation messages with deletion warning
- Lines 1395-1401: Added deletion scheduling
- Lines 1403-1404: Added console logging

## Testing

### Test Scenario 1: Download and Wait
1. Download a video
2. ✅ Verify confirmation shows "deleted after 20 minutes"
3. Save the video to your device
4. Wait 20 minutes
5. ✅ Verify both video and message are deleted

### Test Scenario 2: Multiple Downloads
1. Download 2 videos
2. ✅ Verify both show deletion warning
3. Wait 20 minutes
4. ✅ Verify all 4 messages deleted (2 videos + 2 confirmations)

### Test Scenario 3: Last Download
1. Use your last download
2. ✅ Verify message says "last download"
3. ✅ Verify deletion warning shown
4. Wait 20 minutes
5. ✅ Verify messages deleted

## Benefits

### For Users
✅ **Clear expectations** - Know video will be deleted
✅ **Enough time** - 20 minutes to save
✅ **Clean chat** - No clutter from old downloads
✅ **Consistent** - Same deletion time as regular videos

### For Bot
✅ **Storage management** - Old messages cleaned up
✅ **Consistent behavior** - All videos deleted after 20 min
✅ **Better UX** - Clean chat experience
✅ **Clear communication** - Users warned about deletion

## Summary

**What was added:**
- ✅ Deletion warning in confirmation messages
- ✅ Automatic deletion after 20 minutes
- ✅ Both video and confirmation deleted
- ✅ Console logging for deletion scheduling

**Impact:**
- ⬆️ **Cleaner chats** - Old downloads removed
- ⬆️ **Better UX** - Users know what to expect
- ⬆️ **Consistent** - Same 20-minute deletion for all videos
- ⬆️ **Clear communication** - Warning message shown

Downloaded videos now auto-delete after 20 minutes! 🗑️
