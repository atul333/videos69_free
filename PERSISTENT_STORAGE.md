# Persistent User Data Storage

## Overview

The bot now uses **persistent storage** to save all user data, including:
- ✅ Premium membership status and expiry time
- ✅ Videos watched history (seen_videos)
- ✅ Hourly video count and last reset time
- ✅ Ad tokens and links

**This means all user data will be preserved even after the bot restarts!**

## How It Works

### Storage Files

The bot uses two JSON files for persistent storage:

1. **`users.json`** - Stores basic user information:
   - User ID
   - First name
   - Username
   - Join date
   - Last seen date

2. **`user_states.json`** - Stores user state data (NEW):
   - Premium membership status and expiry time
   - Videos watched (seen_videos list)
   - Hourly video count
   - Last hourly reset time
   - Ad tokens and links

### Automatic Saving

The bot automatically saves user state changes to `user_states.json` whenever:
- A user watches a video (updates seen_videos and hourly_count)
- Premium access is granted (updates premium_until)
- Hourly limit is reset (updates last_reset and hourly_count)
- Ad link is created (updates ad_token and ad_link)
- Token is cleared after premium activation

### Automatic Loading

When the bot starts:
1. Loads all user states from `user_states.json`
2. Cleans up expired data (old tokens, etc.)
3. Displays statistics:
   - Total users
   - Premium users count
   - Free users count
   - Total videos watched
   - Average videos per user

## Benefits

### 1. **Premium Membership Persists**
If a user has premium access and the bot restarts, they will still have premium access when the bot comes back online. Their premium expiry time is preserved.

### 2. **Video History Persists**
Users won't see the same videos again after a bot restart. Their watch history is saved.

### 3. **Hourly Limits Persist**
If a user has watched 5 videos this hour and the bot restarts, they will still have only 5 videos remaining for that hour (not reset to 10).

### 4. **No Data Loss**
All user progress is preserved across bot restarts, updates, and crashes.

## Example Scenarios

### Scenario 1: Premium User
1. User watches an ad and gets 12 hours of premium access
2. Bot restarts after 2 hours
3. ✅ User still has 10 hours of premium access remaining
4. ✅ User can continue watching unlimited videos

### Scenario 2: Free User
1. User watches 7 videos this hour
2. Bot restarts
3. ✅ User still has 3 videos remaining for this hour
4. ✅ Hourly limit will reset at the next hour mark (e.g., 2:00 PM)

### Scenario 3: Video History
1. User watches 50 videos over several days
2. Bot restarts
3. ✅ User won't see those 50 videos again
4. ✅ Bot will only show new, unseen videos

## Technical Details

### Data Structure

The `user_states.json` file stores data in this format:

```json
{
  "123456789": {
    "seen_videos": [1, 5, 12, 45, 67],
    "hourly_count": 3,
    "last_reset": "2025-12-10T04:00:00+00:00",
    "premium_until": "2025-12-10T16:30:00+00:00",
    "ad_link": "https://arolinks.com/xyz",
    "ad_token": "123456789_1733812345_abc123"
  }
}
```

### DateTime Serialization

DateTime objects are automatically converted to ISO format strings when saving and converted back to datetime objects when loading.

### Caching

The bot uses in-memory caching to minimize file I/O:
- User states are loaded once on startup
- Changes are saved immediately to file
- Cache is updated in memory for fast access

### Performance

The persistent storage system is optimized for performance:
- Fast in-memory access for reads
- Efficient JSON serialization for writes
- Minimal file I/O operations
- No impact on bot response time

## Maintenance

### Cleanup

The bot automatically cleans up expired data on startup:
- Clears expired ad tokens
- (Optional) Trims very large seen_videos lists

### Backup

It's recommended to backup the following files regularly:
- `users.json`
- `user_states.json`

You can simply copy these files to a backup location.

### Reset User Data

To reset a specific user's data:
1. Stop the bot
2. Edit `user_states.json`
3. Remove the user's entry or modify their data
4. Start the bot

To reset all user data:
1. Stop the bot
2. Delete `user_states.json`
3. Start the bot (will create a fresh file)

## Monitoring

On bot startup, you'll see statistics like:

```
📊 User Statistics:
   Total users: 150
   💎 Premium users: 12
   🆓 Free users: 138
   🎬 Total videos watched: 3,450
   📈 Avg videos per user: 23.0
```

This helps you monitor:
- How many users have premium access
- Total engagement (videos watched)
- User activity patterns

## Troubleshooting

### Issue: Premium status lost after restart

**Solution**: This should no longer happen! If it does:
1. Check if `user_states.json` exists
2. Check if the file contains the user's data
3. Check if `premium_until` is set correctly

### Issue: Users seeing same videos again

**Solution**: This should no longer happen! If it does:
1. Check if `user_states.json` exists
2. Check if `seen_videos` list is being saved
3. Check console logs for save errors

### Issue: File permission errors

**Solution**: Make sure the bot has write permissions in its directory.

## Migration from Old System

If you're upgrading from the old in-memory system:
1. The bot will automatically create `user_states.json` on first run
2. Existing users will get fresh state (no migration needed)
3. Premium users will need to watch ads again (one-time only)

## Summary

✅ **Premium membership persists across restarts**
✅ **Video watch history persists across restarts**
✅ **Hourly limits persist across restarts**
✅ **No data loss on bot restart**
✅ **Automatic cleanup of expired data**
✅ **Statistics on startup**
✅ **Optimized for performance**

Your bot is now fully persistent! 🎉
