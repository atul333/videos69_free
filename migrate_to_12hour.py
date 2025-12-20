"""
Migration Script: Fix User Reset Times for 12-Hour Periods
This script updates all existing users' last_reset timestamps to align with 12-hour periods.
Run this once after changing from 6-hour to 12-hour reset periods.

IMPORTANT: Run this script BEFORE restarting your bot in production!
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta

# Fix encoding for Windows console
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')

USER_STATES_FILE = "user_states.json"

def migrate_user_states():
    """Migrate all user states to align with 12-hour periods in IST"""
    
    if not os.path.exists(USER_STATES_FILE):
        print("❌ No user_states.json file found. Nothing to migrate.")
        return
    
    print("🔄 Starting migration to 12-hour reset periods...")
    print("=" * 60)
    
    # Load existing user states
    with open(USER_STATES_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    total_users = len(data)
    migrated_count = 0
    
    # Get current time
    now = datetime.now(timezone.utc)
    
    # Convert to IST for period calculation
    ist_offset = timedelta(hours=5, minutes=30)
    now_ist = now + ist_offset
    
    # Calculate current 12-hour period start in IST
    current_hour_ist = now_ist.hour
    period_start_hour_ist = (current_hour_ist // 12) * 12  # 0 or 12
    period_start_ist = now_ist.replace(hour=period_start_hour_ist, minute=0, second=0, microsecond=0)
    
    # Convert back to UTC for storage
    new_period_start = period_start_ist - ist_offset
    
    print(f"📊 Found {total_users} users to migrate")
    print(f"⏰ Current time (UTC): {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"⏰ Current time (IST): {now_ist.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"🔧 Current 12-hour period starts at:")
    print(f"   IST: {period_start_ist.strftime('%Y-%m-%d %I:%M %p')}")
    print(f"   UTC: {new_period_start.strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    
    # Update each user's last_reset to the current 12-hour period start
    for user_id_str, state in data.items():
        old_last_reset = state.get('last_reset')
        
        # Set to current 12-hour period start (in UTC)
        state['last_reset'] = new_period_start.isoformat()
        
        # Reset their counts since we're aligning to a new period
        # This gives all users a fresh start with the new system
        state['hourly_count'] = 0
        state['downloaded_count'] = 0
        
        migrated_count += 1
        
        if migrated_count <= 5:  # Show first 5 as examples
            print(f"✅ User {user_id_str}:")
            print(f"   Old last_reset: {old_last_reset}")
            print(f"   New last_reset: {state['last_reset']}")
            print(f"   Reset counts: hourly_count=0, downloaded_count=0")
            print()
    
    if migrated_count > 5:
        print(f"... and {migrated_count - 5} more users")
        print()
    
    # Save the updated states
    with open(USER_STATES_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    
    print("=" * 60)
    print(f"✅ Migration complete!")
    print(f"📊 Total users migrated: {migrated_count}")
    print(f"💾 Updated file: {USER_STATES_FILE}")
    print()
    print("🎉 All users now aligned to 12-hour reset periods!")
    print("⏰ Reset times: 12:00 AM and 12:00 PM IST")
    print()
    print("⚠️  IMPORTANT: Restart your bot for changes to take effect!")

if __name__ == "__main__":
    migrate_user_states()
