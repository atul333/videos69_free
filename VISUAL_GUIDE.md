# Persistent Storage System - Visual Guide

## System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    TELEGRAM BOT                              │
│                   (video69_bot.py)                           │
└─────────────────────────────────────────────────────────────┘
                           │
                           │ imports
                           ▼
┌─────────────────────────────────────────────────────────────┐
│              USER STATE STORAGE MODULE                       │
│              (user_state_storage.py)                         │
│                                                              │
│  Functions:                                                  │
│  • load_user_states()    - Load from file                   │
│  • save_user_states()    - Save to file                     │
│  • init_user_state_data() - Initialize new user             │
│  • cleanup_expired_data() - Clean old data                  │
│  • get_stats()           - Get statistics                   │
└─────────────────────────────────────────────────────────────┘
                           │
                           │ reads/writes
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                  STORAGE FILES                               │
│                                                              │
│  📄 users.json          - Basic user info                   │
│  📄 user_states.json    - User states (NEW!)                │
└─────────────────────────────────────────────────────────────┘
```

## Data Flow

### When Bot Starts

```
1. Bot starts
   │
   ▼
2. Load user_states.json
   │
   ▼
3. Deserialize datetime objects
   │
   ▼
4. Store in memory (cache)
   │
   ▼
5. Clean up expired data
   │
   ▼
6. Display statistics
   │
   ▼
7. Bot ready! ✅
```

### When User Watches Video

```
1. User clicks "Next Video"
   │
   ▼
2. Check if user can watch
   │
   ▼
3. Send video
   │
   ▼
4. Update user_states:
   • Add to seen_videos
   • Increment hourly_count
   │
   ▼
5. Save to user_states.json ✅
```

### When User Gets Premium

```
1. User watches ad
   │
   ▼
2. User clicks ad link
   │
   ▼
3. Grant premium access
   │
   ▼
4. Update user_states:
   • Set premium_until
   • Clear ad_token
   │
   ▼
5. Save to user_states.json ✅
```

### When Bot Restarts

```
BEFORE (Old System):
┌──────────────┐
│ Bot Restart  │
└──────────────┘
       │
       ▼
❌ All data lost!
❌ Premium reset
❌ History cleared


AFTER (New System):
┌──────────────┐
│ Bot Restart  │
└──────────────┘
       │
       ▼
📂 Load user_states.json
       │
       ▼
✅ All data restored!
✅ Premium preserved
✅ History intact
```

## User State Structure

```
user_states = {
    123456789: {                                    // User ID
        "seen_videos": [1, 5, 12, 45, 67],         // Videos watched
        "hourly_count": 3,                          // Videos this hour
        "last_reset": datetime(2025, 12, 10, 4, 0), // Last reset time
        "premium_until": datetime(2025, 12, 10, 16, 30), // Premium expiry
        "ad_link": "https://arolinks.com/xyz",         // Ad link
        "ad_token": "123456789_1733812345_abc123"  // Token
    },
    987654321: {
        ...
    }
}
```

## File Format (user_states.json)

```json
{
  "123456789": {
    "seen_videos": [1, 5, 12, 45, 67],
    "hourly_count": 3,
    "last_reset": "2025-12-10T04:00:00+00:00",
    "premium_until": "2025-12-10T16:30:00+00:00",
    "ad_link": "https://arolinks.com/xyz",
    "ad_token": "123456789_1733812345_abc123"
  },
  "987654321": {
    "seen_videos": [2, 8, 15, 23],
    "hourly_count": 4,
    "last_reset": "2025-12-10T04:00:00+00:00",
    "premium_until": null,
    "ad_link": null,
    "ad_token": null
  }
}
```

## Timeline Example

```
Time: 1:00 PM
User watches 5 videos
┌─────────────────────┐
│ hourly_count: 5     │
│ premium_until: null │
└─────────────────────┘
         │
         │ Saved to file ✅
         ▼
┌─────────────────────┐
│ user_states.json    │
└─────────────────────┘

Time: 1:30 PM
User watches ad → Gets premium
┌─────────────────────────────┐
│ hourly_count: 5             │
│ premium_until: 1:30 AM next │
└─────────────────────────────┘
         │
         │ Saved to file ✅
         ▼
┌─────────────────────┐
│ user_states.json    │
└─────────────────────┘

Time: 2:00 PM
🔄 BOT RESTARTS
         │
         │ Load from file
         ▼
┌─────────────────────────────┐
│ hourly_count: 0 (reset)     │
│ premium_until: 1:30 AM next │ ✅ PRESERVED!
└─────────────────────────────┘

Time: 3:00 PM
User watches 10 more videos
┌─────────────────────────────┐
│ hourly_count: 10            │
│ premium_until: 1:30 AM next │
└─────────────────────────────┘
         │
         │ Saved to file ✅
         ▼
┌─────────────────────┐
│ user_states.json    │
└─────────────────────┘

Time: 1:30 AM (next day)
Premium expires
┌─────────────────────────────┐
│ hourly_count: 0             │
│ premium_until: EXPIRED      │
└─────────────────────────────┘
         │
         │ Cleanup on next restart
         ▼
┌─────────────────────┐
│ user_states.json    │
└─────────────────────┘
```

## Key Functions and When They're Called

```
┌──────────────────────────┬─────────────────────────────────┐
│ Function                 │ When Called                     │
├──────────────────────────┼─────────────────────────────────┤
│ load_user_states()       │ • Bot startup                   │
│                          │ • Test script                   │
├──────────────────────────┼─────────────────────────────────┤
│ save_user_states()       │ • After watching video          │
│                          │ • After granting premium        │
│                          │ • After hourly reset            │
│                          │ • After creating ad link        │
│                          │ • After clearing token          │
├──────────────────────────┼─────────────────────────────────┤
│ init_user_state_data()   │ • New user starts bot           │
│                          │ • User state doesn't exist      │
├──────────────────────────┼─────────────────────────────────┤
│ cleanup_expired_data()   │ • Bot startup                   │
├──────────────────────────┼─────────────────────────────────┤
│ get_stats()              │ • Bot startup                   │
│                          │ • Test script                   │
└──────────────────────────┴─────────────────────────────────┘
```

## Performance Optimization

```
┌─────────────────────────────────────────────────────────┐
│                    CACHING SYSTEM                        │
└─────────────────────────────────────────────────────────┘

First Load:
  File → Deserialize → Cache → Return
  (Slow)              (Fast)

Subsequent Reads:
  Cache → Return
  (Very Fast! ⚡)

Writes:
  Update Cache → Serialize → File
  (Async-safe)
```

## Error Handling

```
┌─────────────────────────────────────────────────────────┐
│ If file doesn't exist:                                   │
│   → Create new empty state                              │
│   → Continue normally                                   │
├─────────────────────────────────────────────────────────┤
│ If file is corrupted:                                    │
│   → Log error                                           │
│   → Start with empty state                              │
│   → Continue normally                                   │
├─────────────────────────────────────────────────────────┤
│ If write fails:                                          │
│   → Log error                                           │
│   → Keep data in memory                                 │
│   → Retry on next save                                  │
└─────────────────────────────────────────────────────────┘
```

## Summary

```
┌─────────────────────────────────────────────────────────┐
│                    BEFORE vs AFTER                       │
├─────────────────────────────────────────────────────────┤
│                                                          │
│  BEFORE (In-Memory Only):                               │
│  ❌ Data lost on restart                                │
│  ❌ Premium reset                                       │
│  ❌ History cleared                                     │
│                                                          │
│  AFTER (Persistent Storage):                            │
│  ✅ Data survives restart                               │
│  ✅ Premium preserved                                   │
│  ✅ History intact                                      │
│  ✅ Statistics available                                │
│  ✅ Automatic cleanup                                   │
│                                                          │
└─────────────────────────────────────────────────────────┘
```
