"""
Quick Verification Script for AroLinks Migration
This script verifies that all changes were applied correctly.
"""

import os
import sys

def verify_changes():
    """Verify all changes were applied correctly"""
    
    print("=" * 60)
    print("AROLINKS MIGRATION VERIFICATION")
    print("=" * 60)
    print()
    
    errors = []
    warnings = []
    success = []
    
    # Check if main bot file exists
    bot_file = "video69_bot.py"
    if not os.path.exists(bot_file):
        errors.append(f"[ERROR] {bot_file} not found!")
    else:
        success.append(f"[OK] {bot_file} exists")
        
        # Read bot file and check for AroLinks configuration
        with open(bot_file, 'r', encoding='utf-8') as f:
            content = f.read()
            
            # Check for AroLinks API configuration
            if 'AROLINKS_API_TOKEN' in content:
                success.append("[OK] AROLINKS_API_TOKEN found in bot file")
            else:
                errors.append("[ERROR] AROLINKS_API_TOKEN not found in bot file")
            
            if 'AROLINKS_API_URL' in content:
                success.append("[OK] AROLINKS_API_URL found in bot file")
            else:
                errors.append("[ERROR] AROLINKS_API_URL not found in bot file")
            
            # Check for new mandatory channel link
            if 'https://t.me/+A2FAI6TD5yZkN2Jl' in content:
                success.append("[OK] New mandatory channel link found")
            else:
                errors.append("[ERROR] New mandatory channel link not found")
            
            # Check that old VPLink references are removed
            if 'VPLINK_API_TOKEN' in content:
                errors.append("[ERROR] Old VPLINK_API_TOKEN still in bot file")
            else:
                success.append("[OK] Old VPLINK_API_TOKEN removed")
            
            if 'VPLINK_API_URL' in content:
                errors.append("[ERROR] Old VPLINK_API_URL still in bot file")
            else:
                success.append("[OK] Old VPLINK_API_URL removed")
            
            # Check for AroLinks in function
            if 'Created AroLinks shortened link' in content:
                success.append("[OK] AroLinks log message found")
            else:
                errors.append("[ERROR] AroLinks log message not found")
    
    # Check if old files were deleted
    old_files = ['VPLINK_SWITCH.md', 'GPLINKS_MIGRATION.md']
    for old_file in old_files:
        if os.path.exists(old_file):
            errors.append(f"[ERROR] Old file {old_file} still exists")
        else:
            success.append(f"[OK] Old file {old_file} deleted")
    
    # Check if new files were created
    new_files = ['AROLINKS_MIGRATION.md', 'CHANGES_SUMMARY.md']
    for new_file in new_files:
        if os.path.exists(new_file):
            success.append(f"[OK] New file {new_file} created")
        else:
            warnings.append(f"[WARN] New file {new_file} not found")
    
    # Print results
    print("\nVERIFICATION RESULTS:")
    print("-" * 60)
    
    if success:
        print("\nSUCCESSES:")
        for item in success:
            print(f"  {item}")
    
    if warnings:
        print("\nWARNINGS:")
        for item in warnings:
            print(f"  {item}")
    
    if errors:
        print("\nERRORS:")
        for item in errors:
            print(f"  {item}")
    
    print("\n" + "=" * 60)
    print("SUMMARY:")
    print(f"  Successes: {len(success)}")
    print(f"  Warnings: {len(warnings)}")
    print(f"  Errors: {len(errors)}")
    print("=" * 60)
    
    if errors:
        print("\nVERIFICATION FAILED - Please fix errors above")
        return False
    elif warnings:
        print("\nVERIFICATION PASSED WITH WARNINGS")
        return True
    else:
        print("\nVERIFICATION PASSED - All changes applied correctly!")
        return True

if __name__ == "__main__":
    success = verify_changes()
    sys.exit(0 if success else 1)
