# -*- coding: utf-8 -*-
"""
reset_seen_videos.py
====================
Resets the seen_videos (and failed_message_ids) for ALL users
in the SQLite database so they start fresh with a new channel.

Run this script WHILE THE BOT IS STOPPED, then restart the bot.
"""

import sys
# Force UTF-8 output on Windows (avoids emoji encoding errors)
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import sqlite3
import json
import os
import shutil
from datetime import datetime

DB_FILE = "user_states.db"
BACKUP_FILE = f"user_states_BACKUP_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"


def show_stats(conn):
    cursor = conn.execute("SELECT COUNT(*) FROM users")
    total = cursor.fetchone()[0]

    cursor = conn.execute("SELECT seen_videos FROM users")
    total_seen = 0
    for (sv,) in cursor:
        try:
            total_seen += len(json.loads(sv or "[]"))
        except Exception:
            pass

    avg = f"{total_seen / total:.1f}" if total else "N/A"
    print(f"\n[Stats]")
    print(f"  Total users in DB       : {total}")
    print(f"  Total seen-video entries: {total_seen}")
    print(f"  Average per user        : {avg}")


def reset_seen_videos(conn):
    """Set seen_videos = '[]' and failed_message_ids = '[]' for every user."""
    conn.execute("UPDATE users SET seen_videos = '[]', failed_message_ids = '[]'")
    conn.commit()


def main():
    print("\n" + "=" * 60)
    print("  SEEN-VIDEOS RESET TOOL  (SQLite edition)")
    print("=" * 60)

    if not os.path.exists(DB_FILE):
        print(f"\nERROR: Database not found: {DB_FILE}")
        print("Make sure you run this script from the bot directory.")
        input("\nPress Enter to exit...")
        return

    print(f"\nFound database: {DB_FILE}")

    conn = sqlite3.connect(DB_FILE)
    show_stats(conn)

    print("\nWARNING:")
    print("  This will clear ALL users' watched-video history.")
    print("  Every user will start seeing channel videos from the beginning.")
    print("  Premium status, daily limits, and user accounts are NOT affected.")
    print(f"\n  A backup will be saved automatically as: {BACKUP_FILE}")

    confirm = input("\nType  YES  to continue, anything else to cancel: ").strip()

    if confirm != "YES":
        print("\nCancelled. No changes were made.")
        conn.close()
        input("\nPress Enter to exit...")
        return

    # --- Create backup first ---
    conn.close()   # close before file copy
    print(f"\nCreating backup -> {BACKUP_FILE} ...")
    try:
        shutil.copy2(DB_FILE, BACKUP_FILE)
        print(f"Backup created: {BACKUP_FILE}")
    except Exception as e:
        print(f"ERROR - Backup failed: {e}")
        print("Aborting to protect your data.")
        input("\nPress Enter to exit...")
        return

    # --- Do the reset ---
    conn = sqlite3.connect(DB_FILE)
    print("\nResetting seen_videos and failed_message_ids for all users...")
    try:
        reset_seen_videos(conn)
        # Count affected rows
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        print(f"\nDone! Reset {total} user record(s).")
        show_stats(conn)
        print("\nNext steps:")
        print("  1. Update your .env  with the new CHANNEL_ID and MAX_MESSAGE_ID")
        print("  2. Start the bot normally (start_bots.bat or python video69_bot.py)")
        print("  3. Users will now receive new-channel videos from message #1 onwards")
    except Exception as e:
        print(f"\nERROR - Reset failed: {e}")
        print(f"Your original data is safe in the backup: {BACKUP_FILE}")
    finally:
        conn.close()

    input("\nPress Enter to exit...")


if __name__ == "__main__":
    main()
