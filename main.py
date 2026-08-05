import sys
sys.dont_write_bytecode = True

import json
import os
import random
import requests
import time
from config_manager import get_max_emails_to_process, get_polling_settings, get_recheck_intervals_minutes
from datetime import datetime
from dotenv import load_dotenv

__version__ = "0.5.0-beta"

# Load environment variables from .env.
load_dotenv()
GITHUB_TOKEN = os.getenv("GITHUB_PAT")

# Import application modules.
from listener import get_pending_notifications, mark_as_read_and_delete
from db_manager import (
    purge_state_database,
    open_database,
    enqueue_job,
    get_due_jobs,
    mark_job_completed,
    mark_job_failed,
    reschedule_job,
)
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


def _format_timestamp(unix_timestamp):
    """Format a Unix timestamp for console output."""
    if unix_timestamp is None:
        return "-"
    try:
        return datetime.fromtimestamp(float(unix_timestamp)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return "-"


def _collect_queue_status_data():
    """Return queue status data for text or JSON rendering."""
    now_timestamp = time.time()

    with open_database() as connection:
        summary_rows = connection.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM job_queue
            GROUP BY status
            ORDER BY status ASC
            """
        ).fetchall()

        total_jobs = sum(int(row["count"]) for row in summary_rows)
        pending_due = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM job_queue
            WHERE status = 'PENDING' AND next_check_time <= ?
            """,
            (now_timestamp,),
        ).fetchone()

        next_pending = connection.execute(
            """
            SELECT
                id,
                repo,
                tag,
                release_type,
                attempt_count,
                next_check_time,
                expected_commit,
                downloaded_count,
                skipped_count,
                total_items,
                last_result
            FROM job_queue
            WHERE status = 'PENDING'
            ORDER BY next_check_time ASC, id ASC
            LIMIT 1
            """
        ).fetchone()

        recent_jobs = connection.execute(
            """
            SELECT
                id,
                repo,
                tag,
                release_type,
                status,
                attempt_count,
                next_check_time,
                expected_commit,
                downloaded_count,
                skipped_count,
                total_items,
                last_result
            FROM job_queue
            ORDER BY id DESC
            LIMIT 10
            """
        ).fetchall()

    status_counts = {row["status"]: int(row["count"]) for row in summary_rows}
    due_count = int(pending_due["count"]) if pending_due else 0

    next_pending_payload = None
    if next_pending is not None:
        next_pending_payload = {
            "id": int(next_pending["id"]),
            "repo": next_pending["repo"],
            "tag": next_pending["tag"],
            "release_type": next_pending["release_type"] or "Release",
            "attempt_count": int(next_pending["attempt_count"]),
            "next_check_time": float(next_pending["next_check_time"]),
            "next_check_time_readable": _format_timestamp(next_pending["next_check_time"]),
            "expected_commit": next_pending["expected_commit"] or "unknown",
            "downloaded_count": int(next_pending["downloaded_count"]),
            "skipped_count": int(next_pending["skipped_count"]),
            "total_items": int(next_pending["total_items"]),
            "last_result": next_pending["last_result"],
        }

    recent_jobs_payload = []
    for job in recent_jobs:
        recent_jobs_payload.append(
            {
                "id": int(job["id"]),
                "status": job["status"],
                "repo": job["repo"],
                "tag": job["tag"],
                "release_type": job["release_type"] or "Release",
                "attempt_count": int(job["attempt_count"]),
                "next_check_time": float(job["next_check_time"]),
                "next_check_time_readable": _format_timestamp(job["next_check_time"]),
                "expected_commit": job["expected_commit"] or "unknown",
                "downloaded_count": int(job["downloaded_count"]),
                "skipped_count": int(job["skipped_count"]),
                "total_items": int(job["total_items"]),
                "last_result": job["last_result"],
            }
        )

    return {
        "captured_at": _format_timestamp(now_timestamp),
        "captured_at_unix": float(now_timestamp),
        "total_jobs": int(total_jobs),
        "status_counts": status_counts,
        "pending_due_now": due_count,
        "next_pending_job": next_pending_payload,
        "recent_jobs": recent_jobs_payload,
    }


def print_queue_status(as_json=False):
    """Print queue counts and scheduling details without processing jobs."""
    data = _collect_queue_status_data()

    if as_json:
        print(json.dumps(data, indent=2, ensure_ascii=True))
        return

    print("Queue status")
    print("------------")
    print(f"Total jobs: {data['total_jobs']}")

    if data["total_jobs"] == 0:
        print("No queue rows found.")
        return

    for status in sorted(data["status_counts"].keys()):
        print(f"- {status}: {data['status_counts'][status]}")

    print(f"- PENDING due now: {data['pending_due_now']}")

    next_pending = data["next_pending_job"]
    if next_pending is None:
        print("Next pending job: -")
    else:
        print(
            "Next pending job: "
            f"#{next_pending['id']} {next_pending['repo']} {next_pending['tag']} "
            f"({next_pending['release_type']}, attempt={next_pending['attempt_count']}, "
            f"next_check={next_pending['next_check_time_readable']}, "
            f"expected_commit={next_pending['expected_commit']}, "
            f"downloaded={next_pending['downloaded_count']}, "
            f"skipped={next_pending['skipped_count']}, "
            f"total={next_pending['total_items']}, "
            f"last_result={next_pending['last_result'] or '-'})"
        )

    if data["recent_jobs"]:
        print("\nRecent jobs (newest first):")
        for job in data["recent_jobs"]:
            print(
                f"- #{job['id']} {job['status']} {job['repo']} {job['tag']} "
                f"({job['release_type']}, attempt={job['attempt_count']}, "
                f"next_check={job['next_check_time_readable']}, "
                f"expected_commit={job['expected_commit']}, "
                f"downloaded={job['downloaded_count']}, "
                f"skipped={job['skipped_count']}, "
                f"total={job['total_items']}, "
                f"last_result={job['last_result'] or '-'})"
            )


def handle_cli_args(args):
    """Handle one-shot command-line operations."""
    if "--help" in args or "-h" in args:
        print(
            f"GHAADD v{__version__}\n"
            "\nUsage: python main.py [OPTIONS]\n"
            "\nOptions:\n"
            "  --help, -h        Show this help message and exit.\n"
            "  --once            Run a single ingest-and-process cycle, then exit.\n"
            "  --poll            Force polling mode even if disabled in config.\n"
            "  --queue-status    Print current queue counts and scheduling details.\n"
            "    --json          Output --queue-status as JSON.\n"
            "  --purge-state     Delete the local state database (state.db).\n"
            "  --smoke-test      Run internal smoke tests for download behavior.\n"
            "\nWith no options, behaviour is determined by config.json (poll or single run)."
        )
        return True

    if "--queue-status" in args:
        print_queue_status(as_json="--json" in args)
        return True

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


def get_current_commit_hash(repo, tag):
    """Return the current 7-char commit hash for repo/tag, or None on error."""
    url = f"https://api.github.com/repos/{repo}/commits/{tag}"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    try:
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code != 200:
            return None

        sha = response.json().get("sha")
        if not sha:
            return None

        return str(sha)[:7]
    except requests.RequestException:
        return None


def ingest_notifications_once(connection):
    """Ingest unseen notifications into job_queue and delete emails immediately."""
    max_emails_to_process = get_max_emails_to_process()

    try:
        print(
            f"Fetching pending GitHub notifications (limit: "
            f"{'all' if max_emails_to_process == 0 else max_emails_to_process})..."
        )
        notifications = get_pending_notifications(
            limit=max_emails_to_process if max_emails_to_process > 0 else None
        )

        if not notifications:
            print("No pending notifications found.")
            return 0

        unique_notifications = []
        notifications_by_release = {}
        collapsed_notifications = []

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

        print(f"Found {len(unique_notifications)} unique notification(s) to ingest.")

        if collapsed_notifications:
            print(f"⏭️ Collapsed {len(collapsed_notifications)} duplicate notification(s) for already-seen repo/tag pairs:")
            for dup in collapsed_notifications:
                print(f"   - Repo: {dup.get('repo')} | Tag: {dup.get('tag')} | Type: {dup.get('release_type')}")
        print()

        emails_to_delete = []
        queued_count = 0
        now_timestamp = time.time()

        for idx, notification in enumerate(unique_notifications, 1):
            repo = notification.get("repo")
            tag = notification.get("tag")
            release_type = notification.get("release_type")
            email_ids = notification.get("email_ids", [])

            print(f"[{idx}/{len(unique_notifications)}] Queueing: {repo} ({tag})")

            try:
                expected_commit = get_current_commit_hash(repo, tag)
                enqueue_job(
                    connection,
                    repo,
                    tag,
                    release_type=release_type,
                    next_check_time=now_timestamp,
                    expected_commit=expected_commit,
                )
                emails_to_delete.extend(email_ids)
                queued_count += 1
                print(f"✓ Queued as PENDING for {repo} {tag} (expected_commit={expected_commit or 'unknown'})")
            except Exception as e:
                print(f"✗ Error queueing {repo} {tag}: {str(e)}\n")
                continue

        if emails_to_delete:
            print(f"🧹 Cleaning up {len(emails_to_delete)} queued email(s)...")
            mark_as_read_and_delete(emails_to_delete)
            print("✓ Emails marked as read and moved to Trash.")

        print(f"Ingest complete. {queued_count} job(s) queued as PENDING.")
        return queued_count

    except Exception as e:
        print(f"Fatal error: {str(e)}", file=sys.stderr)
        return 0


def process_queue_once(connection):
    """Process due queue rows, compare commit hashes, and apply retry intervals."""
    retry_intervals_minutes = get_recheck_intervals_minutes()
    now_timestamp = time.time()
    due_jobs = get_due_jobs(connection, now_timestamp)

    if not due_jobs:
        print("No due queue jobs found.")
        return

    print(f"Processing {len(due_jobs)} due queue job(s)...")

    for index, job in enumerate(due_jobs, 1):
        job_id = job["id"]
        repo = job["repo"]
        tag = job["tag"]
        release_type = job["release_type"]
        attempt_count = int(job["attempt_count"])
        expected_commit = job["expected_commit"]

        print(
            f"[{index}/{len(due_jobs)}] Job #{job_id}: {repo} {tag} "
            f"(attempt={attempt_count}, expected_commit={expected_commit or 'unknown'})"
        )

        current_commit = get_current_commit_hash(repo, tag)
        if expected_commit and current_commit and expected_commit != current_commit:
            print(f"   🔁 Commit changed: {expected_commit} -> {current_commit}")
        elif expected_commit and current_commit and expected_commit == current_commit:
            print(f"   ✅ Commit unchanged: {current_commit}")
        elif current_commit:
            print(f"   ℹ️ Commit baseline discovered: {current_commit}")
        else:
            print("   ⚠️ Could not resolve current commit hash from GitHub API.")

        try:
            result = download_release(repo, tag, release_type, include_stats=True)
        except Exception as e:
            print(f"   ❌ Processor error while downloading: {e}")
            result = {
                "status": "FAILED",
                "downloaded_count": 0,
                "skipped_count": 0,
                "total_items": 0,
            }

        if isinstance(result, dict):
            result_status = result.get("status", "FAILED")
            downloaded_count = int(result.get("downloaded_count", 0) or 0)
            skipped_count = int(result.get("skipped_count", 0) or 0)
            total_items = int(result.get("total_items", 0) or 0)
        else:
            result_status = "SUCCESS" if result is True else ("SKIP" if result == "SKIP" else "FAILED")
            downloaded_count = 0
            skipped_count = 0
            total_items = 0

        if result_status in ("SUCCESS", "SKIP"):
            mark_job_completed(
                connection,
                job_id,
                downloaded_count=downloaded_count,
                skipped_count=skipped_count,
                total_items=total_items,
                last_result=result_status,
            )
            if result_status == "SUCCESS":
                print("   ✅ Job completed successfully.")
            else:
                print("   ⏭️ Job completed with SKIP (release not found).")
            print(f"   📊 Files: downloaded={downloaded_count}, skipped={skipped_count}, total={total_items}")
            continue

        next_attempt_count = attempt_count + 1
        latest_commit = current_commit or expected_commit

        if next_attempt_count <= len(retry_intervals_minutes):
            delay_minutes = retry_intervals_minutes[next_attempt_count - 1]
            next_check_time = time.time() + (delay_minutes * 60)
            reschedule_job(
                connection,
                job_id,
                next_check_time=next_check_time,
                attempt_count=next_attempt_count,
                expected_commit=latest_commit,
                downloaded_count=downloaded_count,
                skipped_count=skipped_count,
                total_items=total_items,
                last_result="RETRY",
            )
            print(
                f"   🔄 Download failed; rescheduled in {delay_minutes} minute(s) "
                f"(attempt={next_attempt_count})."
            )
            print(f"   📊 Files: downloaded={downloaded_count}, skipped={skipped_count}, total={total_items}")
        else:
            mark_job_failed(
                connection,
                job_id,
                attempt_count=next_attempt_count,
                expected_commit=latest_commit,
                downloaded_count=downloaded_count,
                skipped_count=skipped_count,
                total_items=total_items,
                last_result="FAILED",
            )
            print(
                "   ❌ Download failed; no retry intervals remaining. "
                f"Marked FAILED at attempt={next_attempt_count}."
            )
            print(f"   📊 Files: downloaded={downloaded_count}, skipped={skipped_count}, total={total_items}")


def run_ingest_and_queue_cycle(connection):
    """Run one full cycle: ingest new emails, then process due queue jobs."""
    ingest_notifications_once(connection)
    process_queue_once(connection)


def run_polling_loop(interval_seconds, jitter_min_seconds, jitter_max_seconds):
    """Run processing continuously with randomized jitter between cycles."""
    if jitter_min_seconds > jitter_max_seconds:
        jitter_min_seconds, jitter_max_seconds = jitter_max_seconds, jitter_min_seconds

    print(
        f"Polling enabled. Base interval: {interval_seconds}s, jitter: {jitter_min_seconds}-{jitter_max_seconds}s."
    )
    print("Press Ctrl+C to stop.\n")

    cycle = 1
    with open_database() as connection:
        while True:
            started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"=== Poll cycle {cycle} @ {started} ===")
            run_ingest_and_queue_cycle(connection)

            jitter = random.randint(jitter_min_seconds, jitter_max_seconds)
            sleep_seconds = interval_seconds + jitter
            print(f"Next poll in {sleep_seconds}s ({interval_seconds}s + {jitter}s jitter).\n")
            time.sleep(sleep_seconds)
            cycle += 1


def main():
    """Run the main orchestration flow for ingest and queue processing."""
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
        with open_database() as connection:
            run_ingest_and_queue_cycle(connection)
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
