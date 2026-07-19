import os
import sys
from dotenv import load_dotenv

sys.dont_write_bytecode = True
__version__ = "0.1.0-beta"

# Load environment variables
load_dotenv()

# Import custom modules
from listener import get_pending_notifications, mark_as_read_and_delete
from downloader import download_release

def main():
    """
    Main orchestrator script that coordinates email listening and GitHub downloads.
    """
    # Get configuration from environment
    max_emails_to_process = int(os.getenv("MAX_EMAILS_TO_PROCESS", "0"))  # 0 = all, >0 = limit
    
    try:
        # Step 1: Get pending GitHub notifications from email
        print(f"Fetching pending GitHub notifications (limit: {'all' if max_emails_to_process == 0 else max_emails_to_process})...")
        notifications = get_pending_notifications(limit=max_emails_to_process if max_emails_to_process > 0 else None)
        
        if not notifications:
            print("No pending notifications found.")
            return

        unique_notifications = []
        notifications_by_release = {}
        duplicate_count = 0
        for notification in notifications:
            repo = notification.get("repo")
            tag = notification.get("tag")
            email_id = notification.get("email_id")
            release_key = (repo, tag)

            if release_key in notifications_by_release:
                duplicate_count += 1
                if email_id is not None:
                    notifications_by_release[release_key]["email_ids"].append(email_id)
                continue

            deduped_notification = dict(notification)
            deduped_notification["email_ids"] = [email_id] if email_id is not None else []
            notifications_by_release[release_key] = deduped_notification
            unique_notifications.append(deduped_notification)
        
        print(f"Found {len(unique_notifications)} unique notification(s) to process.")
        if duplicate_count:
            print(f"⏭️ Collapsed {duplicate_count} duplicate notification(s) for already-seen repo/tag pairs.")
        print()

        emails_to_delete = [] # NEW: List to track processed emails

        # Step 2: Process each notification
        for idx, notification in enumerate(unique_notifications, 1):
            repo = notification.get("repo")
            tag = notification.get("tag")
            email_ids = notification.get("email_ids", [])
            
            print(f"[{idx}/{len(unique_notifications)}] Processing: {repo} ({tag})")
            
            try:
                # Download the release
                result = download_release(repo, tag)
                
                # Handle different return states: True (success), "SKIP" (gracefully skipped), False (error)
                if result is True:
                    print(f"✓ Download successful for {repo} {tag}")
                    emails_to_delete.extend(email_ids) # Queue for deletion                    
                elif result == "SKIP":
                    print(f"⏭️ Skipped {repo} {tag} (release not found)")
                    emails_to_delete.extend(email_ids) # Queue for deletion                    
                else:
                    print(f"✗ Download failed for {repo} {tag}\n")
                    
            except Exception as e:
                print(f"✗ Error processing {repo} {tag}: {str(e)}\n")
                continue

        # NEW: Step 3 - Bulk cleanup after the loop completes
        if emails_to_delete:
            print(f"🧹 Cleaning up {len(emails_to_delete)} processed email(s)...")
            mark_as_read_and_delete(emails_to_delete)
            print("✓ Emails marked as read and moved to Trash.")

        print("All notifications processed.")
        
    except Exception as e:
        print(f"Fatal error: {str(e)}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()