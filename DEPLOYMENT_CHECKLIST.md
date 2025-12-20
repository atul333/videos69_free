# Production Deployment Checklist - 12-Hour Reset System

## Pre-Deployment
- [ ] Code changes reviewed and tested
- [ ] Backup current `user_states.json` file
- [ ] Backup current `users.json` file

## Deployment Steps

### Step 1: Stop the Bot
```bash
# Press Ctrl+C if bot is running
```
- [ ] Bot stopped successfully

### Step 2: Run Migration Script
```bash
python migrate_to_12hour.py
```
- [ ] Migration script completed successfully
- [ ] Verified number of users migrated
- [ ] Checked current period start time is correct

### Step 3: Restart the Bot
```bash
python video69_bot.py
```
- [ ] Bot started successfully
- [ ] Console shows "12-hour broadcast scheduler enabled"
- [ ] Next broadcast time is correct (12:00 AM or 12:00 PM IST)

### Step 4: Verify Functionality
- [ ] Test with a user account
- [ ] Confirm reset times work correctly
- [ ] Check broadcast messages arrive at correct times
- [ ] Verify normal users get 5 videos + 2 downloads per 12 hours

## Expected Console Output

When bot starts, you should see:
```
📢 12-hour broadcast scheduler enabled!
   Current time: XX:XX XX IST
   Next broadcast: 12:00 AM IST (or 12:00 PM IST)
   Waiting: XXX minutes XX seconds
   Then every 12 hours at: 12:00 AM, 12:00 PM
```

## Rollback Plan (If Needed)

If something goes wrong:
1. Stop the bot
2. Restore backup of `user_states.json`
3. Restore old version of code
4. Restart bot

## Post-Deployment Monitoring

Monitor for first 24 hours:
- [ ] Check at 12:00 AM IST - broadcast sent, users reset
- [ ] Check at 12:00 PM IST - broadcast sent, users reset
- [ ] Verify users can watch videos after reset
- [ ] Check for any error messages in console

## Notes

- Migration script only needs to be run ONCE
- All times are in IST (UTC+5:30)
- Normal users: 5 videos + 2 downloads per 12 hours
- Premium users: Unlimited videos + 20 downloads per 12 hours
