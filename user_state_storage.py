"""
User State Storage Module
Handles persistent storage of user states including premium status, video history, etc.
"""

import json
import os
from datetime import datetime, timezone
from typing import Dict, Any, Optional

# File path for user states
USER_STATES_FILE = "user_states.json"

# Cache for user states (loaded once, updated in memory)
_states_cache = None
_states_cache_loaded = False


def serialize_datetime(dt):
    """Convert datetime to ISO format string"""
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    return dt


def deserialize_datetime(dt_str):
    """Convert ISO format string to datetime"""
    if dt_str is None:
        return None
    if isinstance(dt_str, str):
        return datetime.fromisoformat(dt_str)
    return dt_str


def load_user_states() -> Dict[int, Dict[str, Any]]:
    """Load user states from JSON file (with caching)"""
    global _states_cache, _states_cache_loaded
    
    # Return cached data if already loaded
    if _states_cache_loaded and _states_cache is not None:
        return _states_cache
    
    try:
        if os.path.exists(USER_STATES_FILE):
            with open(USER_STATES_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                
                # Convert string keys back to integers and deserialize datetimes
                _states_cache = {}
                for user_id_str, state in data.items():
                    user_id = int(user_id_str)
                    
                    # Deserialize datetime fields
                    if 'last_reset' in state:
                        state['last_reset'] = deserialize_datetime(state['last_reset'])
                    if 'premium_until' in state:
                        state['premium_until'] = deserialize_datetime(state['premium_until'])
                    
                    _states_cache[user_id] = state
                
                _states_cache_loaded = True
                print(f"📂 Loaded states for {len(_states_cache)} users from file")
                
                # Print premium users if any
                premium_count = sum(1 for state in _states_cache.values() 
                                  if state.get('premium_until') and 
                                  deserialize_datetime(state['premium_until']) > datetime.now(timezone.utc))
                if premium_count > 0:
                    print(f"💎 Found {premium_count} active premium users")
                
                return _states_cache
        else:
            print("📂 No user states file found, starting fresh")
            _states_cache = {}
            _states_cache_loaded = True
            return {}
    except Exception as e:
        print(f"⚠️ Error loading user states file: {e}")
        _states_cache = {}
        _states_cache_loaded = True
        return {}


def save_user_states(states: Dict[int, Dict[str, Any]]):
    """Save user states to JSON file"""
    global _states_cache
    try:
        # Update cache
        _states_cache = states
        
        # Serialize the data (convert datetime objects to strings)
        serialized_data = {}
        for user_id, state in states.items():
            serialized_state = state.copy()
            
            # Serialize datetime fields
            if 'last_reset' in serialized_state:
                serialized_state['last_reset'] = serialize_datetime(serialized_state['last_reset'])
            if 'premium_until' in serialized_state:
                serialized_state['premium_until'] = serialize_datetime(serialized_state['premium_until'])
            
            serialized_data[str(user_id)] = serialized_state
        
        # Write to file
        with open(USER_STATES_FILE, 'w', encoding='utf-8') as f:
            json.dump(serialized_data, f, indent=2, ensure_ascii=False)
        
        # Don't print on every save to reduce console spam
        # print(f"💾 Saved states for {len(states)} users to file")
    except Exception as e:
        print(f"⚠️ Error saving user states file: {e}")


def get_user_state(user_id: int, states: Dict[int, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Get a specific user's state"""
    return states.get(user_id)


def update_user_state(user_id: int, state: Dict[str, Any], states: Dict[int, Dict[str, Any]]):
    """Update a specific user's state and save to file"""
    states[user_id] = state
    save_user_states(states)


def init_user_state_data(user_id: int, states: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
    """Initialize user state with default values if not exists"""
    if user_id not in states:
        now = datetime.now(timezone.utc)
        # Set last_reset to the start of the current 12-hour period in IST
        # Periods: 00:00-12:00 IST, 12:00-24:00 IST
        # Convert to IST
        from datetime import timedelta
        ist_offset = timedelta(hours=5, minutes=30)
        now_ist = now + ist_offset
        
        # Calculate period start in IST
        current_hour_ist = now_ist.hour
        period_start_hour_ist = (current_hour_ist // 12) * 12  # 0 or 12
        period_start_ist = now_ist.replace(hour=period_start_hour_ist, minute=0, second=0, microsecond=0)
        
        # Convert back to UTC for storage
        last_reset = period_start_ist - ist_offset
        
        new_state = {
            'seen_videos': [],
            'hourly_count': 0,
            'last_reset': last_reset,
            'premium_until': None,
            'ad_link': None,
            'ad_token': None,
            'downloaded_count': 0  # Track number of videos downloaded
        }
        
        states[user_id] = new_state
        save_user_states(states)
        
        return new_state
    
    return states[user_id]


def cleanup_expired_data(states: Dict[int, Dict[str, Any]]) -> int:
    """
    Clean up expired premium memberships and old seen videos
    Returns number of users cleaned up
    """
    now = datetime.now(timezone.utc)
    cleaned_count = 0
    
    for user_id, state in states.items():
        modified = False
        
        # Clear expired premium status
        if state.get('premium_until'):
            premium_until = deserialize_datetime(state['premium_until'])
            if premium_until and now > premium_until:
                # Premium expired, but keep the record for history
                # Just clear the ad tokens
                if state.get('ad_token'):
                    state['ad_token'] = None
                    state['ad_link'] = None
                    modified = True
        
        # Optionally: Clear very old seen videos (e.g., older than 30 days)
        # to prevent the list from growing too large
        # This is optional - uncomment if needed
        # if len(state.get('seen_videos', [])) > 10000:
        #     state['seen_videos'] = state['seen_videos'][-5000:]  # Keep last 5000
        #     modified = True
        
        if modified:
            cleaned_count += 1
    
    if cleaned_count > 0:
        save_user_states(states)
        print(f"🧹 Cleaned up data for {cleaned_count} users")
    
    return cleaned_count


def get_premium_users_count(states: Dict[int, Dict[str, Any]]) -> int:
    """Get count of active premium users"""
    now = datetime.now(timezone.utc)
    count = 0
    
    for state in states.values():
        premium_until = state.get('premium_until')
        if premium_until:
            premium_until = deserialize_datetime(premium_until)
            if premium_until and now < premium_until:
                count += 1
    
    return count


def get_stats(states: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
    """Get statistics about user states"""
    now = datetime.now(timezone.utc)
    
    total_users = len(states)
    premium_users = 0
    total_videos_watched = 0
    
    for state in states.values():
        # Count premium users
        premium_until = state.get('premium_until')
        if premium_until:
            premium_until = deserialize_datetime(premium_until)
            if premium_until and now < premium_until:
                premium_users += 1
        
        # Count total videos watched
        total_videos_watched += len(state.get('seen_videos', []))
    
    return {
        'total_users': total_users,
        'premium_users': premium_users,
        'free_users': total_users - premium_users,
        'total_videos_watched': total_videos_watched,
        'avg_videos_per_user': total_videos_watched / total_users if total_users > 0 else 0
    }
