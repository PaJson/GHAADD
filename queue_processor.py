import requests
import sys
import time

from config_manager import get_max_emails_to_process, get_recheck_intervals_minutes
from db_manager import (
    enqueue_job,
    get_due_jobs,
    mark_job_completed,
    mark_job_failed,
    reschedule_job,
    save_job_skip_details,
)
from downloader import download_release
from listener import get_pending_notifications, mark_as_read_and_delete
from payload_types import DownloadReleaseResult, NotificationPayload, QueuedNotificationPayload, SkippedItemPayload
from typing import Literal, Optional, Tuple, Union, overload


GITHUB_API_VERSION = "2022-11-28"


@overload
def get_current_commit_hash(
    repo: str,
    tag: str,
    github_token: Optional[str],
    include_reason: Literal[True],
) -> Tuple[Optional[str], Optional[str]]:
    ...


@overload
def get_current_commit_hash(
    repo: str,
    tag: str,
    github_token: Optional[str],
    include_reason: Literal[False] = False,
) -> Optional[str]:
    ...


def get_current_commit_hash(
    repo: str,
    tag: str,
    github_token: Optional[str],
    include_reason: bool = False,
) -> Union[Optional[str], Tuple[Optional[str], Optional[str]]]:
    """Return current 7-char commit hash, optionally including failure reason."""
    url = f"https://api.github.com/repos/{repo}/commits/{tag}"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    try:
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code != 200:
            reason = f"http_{response.status_code}"
            return (None, reason) if include_reason else None

        sha = response.json().get("sha")
        if not sha:
            reason = "missing_sha"
            return (None, reason) if include_reason else None

        commit_hash = str(sha)[:7]
        return (commit_hash, None) if include_reason else commit_hash
    except requests.RequestException as exc:
        reason = f"request_exception:{exc.__class__.__name__}"
        return (None, reason) if include_reason else None


def ingest_notifications_once(connection, github_token: Optional[str]) -> int:
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

        unique_notifications: list[QueuedNotificationPayload] = []
        notifications_by_release: dict[tuple[Optional[str], Optional[str], Optional[str]], QueuedNotificationPayload] = {}
        collapsed_notifications: list[NotificationPayload] = []

        for notification in notifications:
            repo = notification.get("repo")
            tag = notification.get("tag")
            release_type = notification.get("release_type")
            email_id = notification.get("email_id")
            release_key = (repo, tag, release_type)

            if release_key in notifications_by_release:
                collapsed_notifications.append(notification)
                if email_id is not None:
                    notifications_by_release[release_key]["email_ids"].append(email_id)
                continue

            deduped_notification: QueuedNotificationPayload = {
                "repo": repo,
                "tag": tag,
                "release_type": release_type,
                "email_id": email_id,
                "email_ids": [email_id] if email_id is not None else [],
            }
            notifications_by_release[release_key] = deduped_notification
            unique_notifications.append(deduped_notification)

        print(f"Found {len(unique_notifications)} unique notification(s) to ingest.")

        if collapsed_notifications:
            print(
                f"⏭️ Collapsed {len(collapsed_notifications)} duplicate notification(s) "
                "for already-seen repo/tag pairs:"
            )
            for duplicate in collapsed_notifications:
                print(
                    f"   - Repo: {duplicate.get('repo')} | Tag: {duplicate.get('tag')} "
                    f"| Type: {duplicate.get('release_type')}"
                )
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

            if repo is None or tag is None:
                print("✗ Skipping malformed notification: missing repo or tag.\n")
                emails_to_delete.extend(email_ids)
                continue

            try:
                expected_commit, commit_reason = get_current_commit_hash(
                    repo,
                    tag,
                    github_token,
                    include_reason=True,
                )
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
                if expected_commit:
                    print(f"✓ Queued as PENDING for {repo} {tag} (expected_commit={expected_commit})")
                else:
                    print(
                        f"✓ Queued as PENDING for {repo} {tag} "
                        f"(expected_commit=unknown, reason={commit_reason or 'unavailable'})"
                    )
            except Exception as exc:
                print(f"✗ Error queueing {repo} {tag}: {str(exc)}\n")
                continue

        if emails_to_delete:
            print(f"🧹 Cleaning up {len(emails_to_delete)} queued email(s)...")
            mark_as_read_and_delete(emails_to_delete)
            print("✓ Emails marked as read and moved to Trash.")

        print(f"Ingest complete. {queued_count} job(s) queued as PENDING.")
        return queued_count

    except Exception as exc:
        print(f"Fatal error: {str(exc)}", file=sys.stderr)
        return 0


def process_queue_once(connection, github_token: Optional[str]) -> None:
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

        current_commit, current_commit_reason = get_current_commit_hash(
            repo,
            tag,
            github_token,
            include_reason=True,
        )
        if expected_commit and current_commit and expected_commit != current_commit:
            print(f"   🔁 Commit changed: {expected_commit} -> {current_commit}")
        elif expected_commit and current_commit and expected_commit == current_commit:
            print(f"   ✅ Commit unchanged: {current_commit}")
        elif current_commit:
            print(f"   ℹ️ Commit baseline discovered: {current_commit}")
        else:
            print(
                "   ⚠️ Could not resolve current commit hash from GitHub API "
                f"(reason={current_commit_reason or 'unavailable'})."
            )

        result: DownloadReleaseResult
        try:
            result = download_release(repo, tag, release_type, include_stats=True)
        except Exception as exc:
            print(f"   ❌ Processor error while downloading: {exc}")
            result = {
                "status": "FAILED",
                "downloaded_count": 0,
                "skipped_count": 0,
                "total_items": 0,
                "skipped_items": [],
            }

        if isinstance(result, dict):
            result_status = result.get("status", "FAILED")
            downloaded_count = int(result.get("downloaded_count", 0) or 0)
            skipped_count = int(result.get("skipped_count", 0) or 0)
            total_items = int(result.get("total_items", 0) or 0)
            skipped_items: list[SkippedItemPayload] = result.get("skipped_items") or []
        else:
            result_status = "SUCCESS" if result is True else ("SKIP" if result == "SKIP" else "FAILED")
            downloaded_count = 0
            skipped_count = 0
            total_items = 0
            skipped_items: list[SkippedItemPayload] = []

        current_attempt_count = attempt_count + 1

        if result_status in ("SUCCESS", "SKIP"):
            mark_job_completed(
                connection,
                job_id,
                downloaded_count=downloaded_count,
                skipped_count=skipped_count,
                total_items=total_items,
                last_result=result_status,
            )
            save_job_skip_details(connection, job_id, current_attempt_count, skipped_items)
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
            save_job_skip_details(connection, job_id, current_attempt_count, skipped_items)
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
            save_job_skip_details(connection, job_id, current_attempt_count, skipped_items)
            print(
                "   ❌ Download failed; no retry intervals remaining. "
                f"Marked FAILED at attempt={next_attempt_count}."
            )
            print(f"   📊 Files: downloaded={downloaded_count}, skipped={skipped_count}, total={total_items}")


def run_ingest_and_queue_cycle(connection, github_token: Optional[str]) -> None:
    """Run one full cycle: ingest new emails, then process due queue jobs."""
    ingest_notifications_once(connection, github_token)
    process_queue_once(connection, github_token)
