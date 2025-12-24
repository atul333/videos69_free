"""
Clean User States - Migration Script
Cleans up existing user states to add failed_message_ids field
and optionally reset seen_videos for a fresh start
"""
import json
import os
from datetime import datetime

USER_STATES_FILE = "user_states.json"
BACKUP_FILE = f"user_states_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

def load_user_states():
    """Load user states from file"""
    try:
        with open(USER_STATES_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"❌ File not found: {USER_STATES_FILE}")
        return None
    except Exception as e:
        print(f"❌ Error loading file: {e}")
        return None

def save_user_states(states):
    """Save user states to file"""
    try:
        with open(USER_STATES_FILE, 'w', encoding='utf-8') as f:
            json.dump(states, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"❌ Error saving file: {e}")
        return False

def backup_user_states(states):
    """Create a backup of current user states"""
    try:
        with open(BACKUP_FILE, 'w', encoding='utf-8') as f:
            json.dump(states, f, indent=2, ensure_ascii=False)
        print(f"✅ Backup created: {BACKUP_FILE}")
        return True
    except Exception as e:
        print(f"❌ Error creating backup: {e}")
        return False

def clean_user_states(reset_seen_videos=False):
    """
    Clean user states:
    1. Add failed_message_ids field if missing
    2. Optionally reset seen_videos for a fresh start
    """
    print("\n" + "="*60)
    print("🧹 USER STATES CLEANUP SCRIPT")
    print("="*60)
    
    # Load current states
    print("\n📂 Loading user states...")
    states = load_user_states()
    if states is None:
        return False
    
    print(f"✅ Loaded {len(states)} users")
    
    # Create backup
    print("\n💾 Creating backup...")
    if not backup_user_states(states):
        print("⚠️  Backup failed! Aborting to prevent data loss.")
        return False
    
    # Clean states
    print("\n🔧 Cleaning user states...")
    cleaned_count = 0
    reset_count = 0
    
    for user_id, state in states.items():
        modified = False
        
        # Add failed_message_ids if missing
        if 'failed_message_ids' not in state:
            state['failed_message_ids'] = []
            modified = True
            cleaned_count += 1
        
        # Optionally reset seen_videos
        if reset_seen_videos and state.get('seen_videos'):
            old_count = len(state['seen_videos'])
            state['seen_videos'] = []
            modified = True
            reset_count += 1
            print(f"   🔄 Reset user {user_id}: cleared {old_count} seen videos")
        
        if modified:
            states[user_id] = state
    
    # Save cleaned states
    print("\n💾 Saving cleaned states...")
    if save_user_states(states):
        print("✅ User states cleaned successfully!")
        print(f"\n📊 Summary:")
        print(f"   - Total users: {len(states)}")
        print(f"   - Added failed_message_ids field: {cleaned_count} users")
        if reset_seen_videos:
            print(f"   - Reset seen_videos: {reset_count} users")
        print(f"   - Backup saved: {BACKUP_FILE}")
        return True
    else:
        print("❌ Failed to save cleaned states")
        print(f"⚠️  Your data is safe in backup: {BACKUP_FILE}")
        return False

def show_stats():
    """Show statistics about current user states"""
    print("\n" + "="*60)
    print("📊 USER STATES STATISTICS")
    print("="*60)
    
    states = load_user_states()
    if states is None:
        return
    
    total_users = len(states)
    users_with_failed_ids = 0
    total_seen_videos = 0
    total_failed_ids = 0
    max_seen = 0
    max_failed = 0
    
    for user_id, state in states.items():
        seen_count = len(state.get('seen_videos', []))
        failed_count = len(state.get('failed_message_ids', []))
        
        total_seen_videos += seen_count
        total_failed_ids += failed_count
        
        if failed_count > 0:
            users_with_failed_ids += 1
        
        max_seen = max(max_seen, seen_count)
        max_failed = max(max_failed, failed_count)
    
    print(f"\n👥 Total Users: {total_users}")
    print(f"\n📺 Seen Videos:")
    print(f"   - Total: {total_seen_videos}")
    print(f"   - Average per user: {total_seen_videos / total_users if total_users > 0 else 0:.1f}")
    print(f"   - Maximum for one user: {max_seen}")
    print(f"\n❌ Failed Message IDs:")
    print(f"   - Total: {total_failed_ids}")
    print(f"   - Users with failed IDs: {users_with_failed_ids}")
    print(f"   - Average per user: {total_failed_ids / total_users if total_users > 0 else 0:.1f}")
    print(f"   - Maximum for one user: {max_failed}")
    
    # Check for users missing failed_message_ids field
    missing_field = sum(1 for state in states.values() if 'failed_message_ids' not in state)
    if missing_field > 0:
        print(f"\n⚠️  {missing_field} users missing 'failed_message_ids' field")
        print("   Run cleanup to add this field")

def main():
    """Main function"""
    print("\n" + "="*60)
    print("🔧 USER STATES MANAGEMENT TOOL")
    print("="*60)
    print("\nOptions:")
    print("1. Show statistics")
    print("2. Clean states (add failed_message_ids field)")
    print("3. Clean states AND reset seen_videos (fresh start)")
    print("4. Exit")
    
    choice = input("\nEnter your choice (1-4): ").strip()
    
    if choice == "1":
        show_stats()
    elif choice == "2":
        confirm = input("\n⚠️  This will modify user_states.json. Continue? (yes/no): ").strip().lower()
        if confirm == "yes":
            clean_user_states(reset_seen_videos=False)
        else:
            print("❌ Cancelled")
    elif choice == "3":
        print("\n⚠️  WARNING: This will reset all users' seen_videos lists!")
        print("   Users will see videos from the beginning again.")
        confirm = input("\nAre you sure? (yes/no): ").strip().lower()
        if confirm == "yes":
            clean_user_states(reset_seen_videos=True)
        else:
            print("❌ Cancelled")
    elif choice == "4":
        print("\n👋 Goodbye!")
    else:
        print("\n❌ Invalid choice")
    
    print("\n" + "="*60)

if __name__ == "__main__":
    main()
