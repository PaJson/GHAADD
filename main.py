import sys
sys.dont_write_bytecode = True

import random
import time
from config_manager import get_max_emails_to_process, get_polling_settings, is_state_persistence_disabled
from datetime import datetime
from dotenv import load_dotenv

__version__ = "0.4.0-beta"

# Load environment variables from .env.
load_dotenv()

# Import application modules.
from listener import get_pending_notifications, mark_as_read_and_delete
from db_manager import purge_state_database
from downloader import download_release


def run_internal_smoke_tests():
    """Run internal smoke tests for baseline release-download behavior."""
    print("🚀 Starting Internal Smoke Tests...")
    
    # Test graceful failure handling with a non-existent repository.
    print("\n--- Test 1: Verifying Failure Baseline (Invalid Repo) ---")
    fail_result = download_release("github/this-repo-does-not-exist", "v99.9.9")
    print(f"Result (Expected 'SKIP' or False): {fail_result}")
    
    # Test successful download and state database updates.
    print("\n--- Test 2: Verifying Success Baseline (Official GitHub Repo) ---")
    # Use a stable official repository and tag for predictable test behavior.
    # This release includes a representative mix of assets and source archives.
    success_result = download_release("cli/cli", "v2.30.0") 
    print(f"Result (Expected True): {success_result}")
    
    # Test duplicate guarding by repeating a previously successful request.
    print("\n--- Test 3: Verifying Duplicate Guard (Re-running Success Path) ---")
    repeat_result = download_release("cli/cli", "v2.30.0")
    print(f"Result (Expected True with 'Skipping' console logs): {repeat_result}")
    
    print("\n🎉 Smoke tests complete.")


def handle_cli_args(args):
    """Handle one-shot command-line operations."""
    if "--purge-state" in args:
        deleted = purge_state_database()
        if deleted:
            print("Deleted local state database: state.db")
        else:
            print("No local state database found to delete.")
        return True
        
    if "--smoke-test" in args:
        run_internal_smoke_tests()
        return True

    return False


def process_notifications_once():
    """Process pending notifications exactly once."""

    # Use 0 for all notifications; positive values apply a processing limit.
    max_emails_to_process = get_max_emails_to_process()
    state_disabled = is_state_persistence_disabled()
    
    try:
        if state_disabled:
            print("State persistence is disabled; duplicate detection will be in-memory only for this run.")

        # Step 1: Fetch pending GitHub notifications from email.
        print(f"Fetching pending GitHub notifications (limit: {'all' if max_emails_to_process == 0 else max_emails_to_process})...")
        notifications = get_pending_notifications(limit=max_emails_to_process if max_emails_to_process > 0 else None)
        
        if not notifications:
            print("No pending notifications found.")
            return

        unique_notifications = []
        notifications_by_release = {}
        collapsed_notifications = []  # Track notifications collapsed during deduplication.
        
        for notification in notifications:
            repo = notification.get("repo")
            tag = notification.get("tag")
            release_type = notification.get("release_type")
            email_id = notification.get("email_id")
            release_key = (repo, tag, release_type)

            if release_key in notifications_by_release:
                # Keep duplicate details for summary output.
                collapsed_notifications.append(notification)
                if email_id is not None:
                    notifications_by_release[release_key]["email_ids"].append(email_id)
                continue

            deduped_notification = dict(notification)
            deduped_notification["email_ids"] = [email_id] if email_id is not None else []
            notifications_by_release[release_key] = deduped_notification
            unique_notifications.append(deduped_notification)
        
        print(f"Found {len(unique_notifications)} unique notification(s) to process.")
        
        # Show details for notifications collapsed as duplicates.
        if collapsed_notifications:
            print(f"⏭️ Collapsed {len(collapsed_notifications)} duplicate notification(s) for already-seen repo/tag pairs:")
            for dup in collapsed_notifications:
                print(f"   - Repo: {dup.get('repo')} | Tag: {dup.get('tag')} | Type: {dup.get('release_type')}")
        print()

        emails_to_delete = []  # Track emails queued for cleanup.

        # Step 2: Process each unique notification.
        for idx, notification in enumerate(unique_notifications, 1):
            repo = notification.get("repo")
            tag = notification.get("tag")
            release_type = notification.get("release_type")
            email_ids = notification.get("email_ids", [])
            
            print(f"[{idx}/{len(unique_notifications)}] Processing: {repo} ({tag})")
            
            try:
                # Download the release.
                result = download_release(repo, tag, release_type)
                
                # Handle return states: True (success), "SKIP" (graceful skip), or False (error).
                if result is True:
                    print(f"✓ Download successful for {repo} {tag}")
                    emails_to_delete.extend(email_ids)  # Queue for cleanup.
                elif result == "SKIP":
                    print(f"⏭️ Skipped {repo} {tag} (release not found)")
                    emails_to_delete.extend(email_ids)  # Queue for cleanup.
                else:
                    print(f"✗ Download failed for {repo} {tag}\n")
                    
            except Exception as e:
                print(f"✗ Error processing {repo} {tag}: {str(e)}\n")
                continue

        # Step 3: Run bulk cleanup after processing completes.
        if emails_to_delete:
            print(f"🧹 Cleaning up {len(emails_to_delete)} processed email(s)...")
            mark_as_read_and_delete(emails_to_delete)
            print("✓ Emails marked as read and moved to Trash.")

        print("All notifications processed.")

    except Exception as e:
        print(f"Fatal error: {str(e)}", file=sys.stderr)


def _as_bool(value):
    return str(value).strip().lower() == "true"


def run_polling_loop(interval_seconds, jitter_min_seconds, jitter_max_seconds):
    """Run processing continuously with randomized jitter between cycles."""
    if jitter_min_seconds > jitter_max_seconds:
        jitter_min_seconds, jitter_max_seconds = jitter_max_seconds, jitter_min_seconds

    print(
        f"Polling enabled. Base interval: {interval_seconds}s, jitter: {jitter_min_seconds}-{jitter_max_seconds}s."
    )
    print("Press Ctrl+C to stop.\n")

    cycle = 1
    while True:
        started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"=== Poll cycle {cycle} @ {started} ===")
        process_notifications_once()

        jitter = random.randint(jitter_min_seconds, jitter_max_seconds)
        sleep_seconds = interval_seconds + jitter
        print(f"Next poll in {sleep_seconds}s ({interval_seconds}s + {jitter}s jitter).\n")
        time.sleep(sleep_seconds)
        cycle += 1


def main():
    """Run the main orchestration flow for email processing and downloads."""
    args = sys.argv[1:]
    if handle_cli_args(args):
        return

    polling_settings = get_polling_settings()

    # Force single-run mode with --once, even when polling is enabled in config.
    once_mode = "--once" in args
    poll_enabled = not once_mode and (
        "--poll" in args
        or polling_settings["enabled"]
    )
    if once_mode or not poll_enabled:
        process_notifications_once()
        return

    interval_seconds = polling_settings["interval_seconds"]
    jitter_min_seconds = polling_settings["jitter_min_seconds"]
    jitter_max_seconds = polling_settings["jitter_max_seconds"]

    try:
        run_polling_loop(interval_seconds, jitter_min_seconds, jitter_max_seconds)
    except KeyboardInterrupt:
        print("\nPolling stopped by user.")

if __name__ == "__main__":
    main()
