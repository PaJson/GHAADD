import requests
import sys
import time
import os

from config_manager import get_folder_settings, get_max_emails_to_process, get_recheck_intervals_minutes
from db_manager import (
    enqueue_job,
    get_jobs_by_ids,
    get_pending_jobs_for_release,
    get_pending_job_for_release,
    get_previous_successful_completed_job,
    get_due_jobs,
    mark_job_completed,
    mark_job_failed,
    mark_job_superseded,
    supersede_duplicate_pending_jobs,
    reschedule_job,
    save_job_skip_details,
    update_job_for_manual_check,
)
from asset_downloader import (
    download_release,
    move_processing_folder_to_complete,
    move_processing_folder_to_partial,
)
from mailbox_listener import get_pending_notifications, mark_as_read_and_delete
from mapping_manager import get_repository_recheck_intervals_minutes, upsert_repository_mapping
from payload_types import DownloadReleaseResult, NotificationPayload, QueuedNotificationPayload, SkippedItemPayload
from typing import Literal, Optional, Tuple, Union, cast, overload


GITHUB_API_VERSION = "2022-11-28"
_FOLDER_SETTINGS = get_folder_settings()
_COMPLETE_LABEL = _FOLDER_SETTINGS["complete"]
_PARTIAL_LABEL = _FOLDER_SETTINGS["partial"]


def _finalize_staged_release_folder(working_dir: Optional[str], repo: Optional[str] = None) -> None:
    """Move a terminal job's staging folder from Processing to complete destination."""
    if not working_dir:
        return

    try:
        done_dir = move_processing_folder_to_complete(working_dir, repo=repo)
    except OSError as exc:
        print(f"   ⚠️ Could not move staging folder to {_COMPLETE_LABEL}: {exc}")
        return

    if done_dir:
        print(f"   📁 Finalized artifacts: {done_dir}")


def _is_terminal_skip_reason(skip_reason: Optional[str]) -> bool:
    """Return True when a SKIP result should end the re-check plan immediately."""
    return skip_reason in {"release_not_found"}


def _has_all_release_items_accounted(
    downloaded_count: int,
    skipped_count: int,
    total_items: int,
) -> bool:
    """Return True when queued counters indicate all release items were handled."""
    if total_items <= 0:
        return False
    return (downloaded_count + skipped_count) >= total_items


def _handle_superseded_pending_job_artifacts(
    row,
    repo: str,
    tag: str,
    reason_code: str,
) -> str:
    """Return artifact handling outcome: finalized, quarantined, or none."""
    job_id = int(row["id"])
    downloaded_count = int(row["downloaded_count"] or 0)
    skipped_count = int(row["skipped_count"] or 0)
    total_items = int(row["total_items"] or 0)

    working_dir = str(row["working_dir"] or "").strip()

    if not _has_all_release_items_accounted(downloaded_count, skipped_count, total_items):
        if not working_dir:
            print(
                "   [SUPERSEDE_FINALIZE] "
                f"{reason_code}: pending job #{job_id} not finalized "
                f"because it is incomplete ({downloaded_count}+{skipped_count}/{total_items}) "
                "and has no working_dir to quarantine."
            )
            return "none"

        if not os.path.isdir(working_dir):
            print(
                "   [SUPERSEDE_FINALIZE] "
                f"{reason_code}: pending job #{job_id} not finalized "
                f"because it is incomplete ({downloaded_count}+{skipped_count}/{total_items}) "
                f"and working_dir is missing: {working_dir}"
            )
            return "none"

        try:
            superseded_dir = move_processing_folder_to_partial(working_dir)
        except OSError as exc:
            print(f"   ⚠️ Could not move incomplete superseded staging folder: {exc}")
            return "none"

        if superseded_dir:
            print(
                "   [SUPERSEDE_FINALIZE] "
                f"{reason_code}: moved incomplete superseded job #{job_id} "
                f"to {_PARTIAL_LABEL}: {superseded_dir} "
                f"(files={downloaded_count}+{skipped_count}/{total_items})."
            )
            return "quarantined"

        print(
            "   [SUPERSEDE_FINALIZE] "
            f"{reason_code}: pending job #{job_id} not finalized "
            f"because it is incomplete ({downloaded_count}+{skipped_count}/{total_items}) "
            f"and could not be moved to {_PARTIAL_LABEL}."
        )
        return "none"

    if not working_dir:
        print(
            "   [SUPERSEDE_FINALIZE] "
            f"{reason_code}: pending job #{job_id} appears complete "
            f"({downloaded_count}+{skipped_count}/{total_items}) but has no working_dir; "
            "cannot finalize staged artifacts."
        )
        return "none"

    if not os.path.isdir(working_dir):
        print(
            "   [SUPERSEDE_FINALIZE] "
            f"{reason_code}: pending job #{job_id} appears complete "
            f"({downloaded_count}+{skipped_count}/{total_items}) but working_dir is missing: {working_dir}"
        )
        return "none"

    _finalize_staged_release_folder(working_dir, repo=repo)
    print(
        "   [SUPERSEDE_FINALIZE] "
        f"{reason_code}: finalized staged artifacts for superseded pending job #{job_id} "
        f"({repo} {tag}, files={downloaded_count}+{skipped_count}/{total_items})."
    )
    return "finalized"


def _warn_if_file_count_changed_from_previous_success(
    connection,
    job_id: int,
    repo: str,
    tag: str,
    release_type: Optional[str],
    current_total_items: int,
) -> None:
    """Print a warning when the final successful file count differs from the previous release."""
    previous_job = get_previous_successful_completed_job(
        connection,
        repo=repo,
        tag=tag,
        release_type=release_type,
        exclude_job_id=job_id,
    )
    if previous_job is None:
        return

    previous_total_items = int(previous_job["total_items"] or 0)
    if previous_total_items == int(current_total_items):
        return

    delta = int(current_total_items) - previous_total_items
    delta_sign = "+" if delta > 0 else ""
    previous_tag = previous_job["tag"] or "unknown"
    print(
        "   ⚠️ Sanity check: file count changed versus previous successful release "
        f"for {repo} ({release_type or 'Release'}). "
        f"Current={current_total_items}, Previous={previous_total_items} "
        f"(tag={previous_tag}, delta={delta_sign}{delta})."
    )


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
        notifications = cast(
            list[NotificationPayload],
            get_pending_notifications(
                limit=max_emails_to_process if max_emails_to_process > 0 else None
            ),
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

            mapping_created, mapping_updated = upsert_repository_mapping(
                repo,
            )
            release_type_label = release_type or "Release"
            if mapping_created:
                print(
                    "   🗺️ Added mapping skeleton entry for repository "
                    f"{repo} (notification tag={tag}, type={release_type_label})."
                )
            elif mapping_updated:
                print(
                    "   🗺️ Updated mapping active timestamp for repository "
                    f"{repo} (notification tag={tag}, type={release_type_label})."
                )

            try:
                expected_commit, commit_reason = get_current_commit_hash(
                    repo,
                    tag,
                    github_token,
                    include_reason=True,
                )

                pending_rows = get_pending_jobs_for_release(
                    connection,
                    repo,
                    tag,
                    release_type=release_type,
                )
                finalized_superseded_count = 0
                quarantined_superseded_count = 0
                superseded_count = 0
                for pending_row in pending_rows:
                    superseded_count += 1
                    artifact_outcome = _handle_superseded_pending_job_artifacts(
                        pending_row,
                        repo,
                        tag,
                        reason_code="NEW_NOTIFICATION",
                    )
                    if artifact_outcome == "finalized":
                        finalized_superseded_count += 1
                    elif artifact_outcome == "quarantined":
                        quarantined_superseded_count += 1

                    mark_job_superseded(
                        connection,
                        int(pending_row["id"]),
                        attempt_count=int(pending_row["attempt_count"] or 0),
                        expected_commit=expected_commit or pending_row["expected_commit"],
                        downloaded_count=int(pending_row["downloaded_count"] or 0),
                        skipped_count=int(pending_row["skipped_count"] or 0),
                        total_items=int(pending_row["total_items"] or 0),
                        last_result=(
                            "SUPERSEDED_REPLACED_BY_NEW_NOTIFICATION_FINALIZED"
                            if artifact_outcome == "finalized"
                            else (
                                "SUPERSEDED_REPLACED_BY_NEW_NOTIFICATION_INCOMPLETE_MOVED"
                                if artifact_outcome == "quarantined"
                                else "SUPERSEDED_REPLACED_BY_NEW_NOTIFICATION"
                            )
                        ),
                    )

                if superseded_count > 0:
                    print(
                        "🔁 Replaced older pending job(s) for this release identity "
                        f"before queueing new notification (superseded={superseded_count})."
                    )
                    if finalized_superseded_count > 0:
                        print(
                            "   [SUPERSEDE_FINALIZE] NEW_NOTIFICATION: "
                            f"finalized {finalized_superseded_count} superseded pending job folder(s)."
                        )
                    if quarantined_superseded_count > 0:
                        print(
                            "   [SUPERSEDE_FINALIZE] NEW_NOTIFICATION: "
                            f"moved {quarantined_superseded_count} incomplete superseded job folder(s) "
                            f"to {_PARTIAL_LABEL}."
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


def process_selected_pending_jobs(connection, github_token: Optional[str], job_ids) -> tuple[int, list[int], list[int]]:
    """Run selected pending jobs immediately without changing their retry schedule."""
    normalized_ids = sorted({int(job_id) for job_id in (job_ids or [])})
    if not normalized_ids:
        print("No pending job IDs were supplied.")
        return 0, [], []

    rows = get_jobs_by_ids(connection, normalized_ids)
    if not rows:
        print("No matching queue jobs found for the requested IDs.")
        return 0, [], normalized_ids

    rows_by_id = {int(row["id"]): row for row in rows}
    missing_ids = [job_id for job_id in normalized_ids if job_id not in rows_by_id]
    skipped_ids = [job_id for job_id in normalized_ids if job_id in rows_by_id and rows_by_id[job_id]["status"] != "PENDING"]

    print(f"Running manual check for {len(rows)} matching job(s)...")
    now_timestamp = time.time()

    processed_count = 0
    for row in rows:
        job_id = int(row["id"])
        if row["status"] != "PENDING":
            print(f"Skipping job #{job_id}: status={row['status']}")
            continue

        processed_count += 1
        repo = row["repo"]
        tag = row["tag"]
        release_type = row["release_type"]
        attempt_count = int(row["attempt_count"] or 0)
        expected_commit = row["expected_commit"]

        print(
            f"[{processed_count}/{len(rows)}] Manual check for job #{job_id}: {repo} {tag} "
            f"(attempt={attempt_count}, expected_commit={expected_commit or 'unknown'})"
        )

        retry_intervals_minutes = get_repository_recheck_intervals_minutes(repo)

        current_commit, current_commit_reason = get_current_commit_hash(
            repo,
            tag,
            github_token,
            include_reason=True,
        )
        if expected_commit and current_commit and expected_commit != current_commit:
            print(f"   🔁 Commit changed: {expected_commit} -> {current_commit}")

            existing_new_commit_job = get_pending_job_for_release(
                connection,
                repo,
                tag,
                release_type=release_type,
                expected_commit=current_commit,
                exclude_job_id=job_id,
            )

            if existing_new_commit_job is None:
                enqueue_job(
                    connection,
                    repo,
                    tag,
                    release_type=release_type,
                    next_check_time=now_timestamp,
                    expected_commit=current_commit,
                )
                print(
                    "   🆕 Created a new PENDING job for the updated commit "
                    f"({current_commit})."
                )
            else:
                print(
                    "   ℹ️ A PENDING job for the updated commit already exists "
                    f"(job_id={int(existing_new_commit_job['id'])}, commit={current_commit})."
                )

            artifact_outcome = _handle_superseded_pending_job_artifacts(
                row,
                repo,
                tag,
                reason_code="COMMIT_CHANGED_MANUAL",
            )
            mark_job_superseded(
                connection,
                job_id,
                attempt_count=attempt_count,
                expected_commit=current_commit,
                downloaded_count=int(row["downloaded_count"] or 0),
                skipped_count=int(row["skipped_count"] or 0),
                total_items=int(row["total_items"] or 0),
                last_result=(
                    "SUPERSEDED_COMMIT_CHANGED_FINALIZED"
                    if artifact_outcome == "finalized"
                    else (
                        "SUPERSEDED_COMMIT_CHANGED_INCOMPLETE_MOVED"
                        if artifact_outcome == "quarantined"
                        else "SUPERSEDED_COMMIT_CHANGED"
                    )
                ),
            )
            print("   ⏭️ Marked current job as SUPERSEDED; skipping download for this older commit baseline.")
            continue
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
                "working_dir": None,
                "skip_reason": None,
            }

        if isinstance(result, dict):
            result_status = result.get("status", "FAILED")
            downloaded_count = int(result.get("downloaded_count", 0) or 0)
            skipped_count = int(result.get("skipped_count", 0) or 0)
            total_items = int(result.get("total_items", 0) or 0)
            skipped_items: list[SkippedItemPayload] = result.get("skipped_items") or []
            working_dir = result.get("working_dir")
            skip_reason = result.get("skip_reason")
        else:
            result_status = "SUCCESS" if result is True else ("SKIP" if result == "SKIP" else "FAILED")
            downloaded_count = 0
            skipped_count = 0
            total_items = 0
            skipped_items: list[SkippedItemPayload] = []
            working_dir = None
            skip_reason = None

        latest_commit = current_commit or expected_commit

        if result_status == "SKIP" and _is_terminal_skip_reason(skip_reason):
            mark_job_completed(
                connection,
                job_id,
                attempt_count=attempt_count,
                downloaded_count=downloaded_count,
                skipped_count=skipped_count,
                total_items=total_items,
                last_result=result_status,
            )
            save_job_skip_details(connection, job_id, attempt_count, skipped_items)
            print(
                "   ⏹️ Release/tag not found; marked COMPLETED immediately "
                f"at attempt={attempt_count}."
            )
            print(f"   📊 Files: downloaded={downloaded_count}, skipped={skipped_count}, total={total_items}")
            _finalize_staged_release_folder(working_dir, repo=repo)
            continue

        if attempt_count < len(retry_intervals_minutes):
            update_job_for_manual_check(
                connection,
                job_id,
                expected_commit=latest_commit,
                downloaded_count=downloaded_count,
                skipped_count=skipped_count,
                total_items=total_items,
                working_dir=working_dir,
                last_result="MANUAL_CHECK",
            )
            save_job_skip_details(connection, job_id, attempt_count, skipped_items)
            print(
                f"   🔎 Manual check completed; job remains PENDING without changing retry schedule "
                f"(attempt={attempt_count}, last_status={result_status})."
            )
            print(f"   📊 Files: downloaded={downloaded_count}, skipped={skipped_count}, total={total_items}")
        else:
            if result_status in ("SUCCESS", "SKIP"):
                mark_job_completed(
                    connection,
                    job_id,
                    attempt_count=attempt_count,
                    downloaded_count=downloaded_count,
                    skipped_count=skipped_count,
                    total_items=total_items,
                    last_result=result_status,
                )
                if result_status == "SUCCESS":
                    _warn_if_file_count_changed_from_previous_success(
                        connection,
                        job_id=job_id,
                        repo=repo,
                        tag=tag,
                        release_type=release_type,
                        current_total_items=total_items,
                    )
            else:
                mark_job_failed(
                    connection,
                    job_id,
                    attempt_count=attempt_count,
                    expected_commit=latest_commit,
                    downloaded_count=downloaded_count,
                    skipped_count=skipped_count,
                    total_items=total_items,
                    last_result="FAILED",
                )
            save_job_skip_details(connection, job_id, attempt_count, skipped_items)
            if result_status in ("SUCCESS", "SKIP"):
                print(
                    "   ✅ Re-check plan complete; marked COMPLETED "
                    f"at attempt={attempt_count} (last_status={result_status})."
                )
            else:
                print(
                    "   ❌ Re-check plan complete; marked FAILED "
                    f"at attempt={attempt_count}."
                )
            print(f"   📊 Files: downloaded={downloaded_count}, skipped={skipped_count}, total={total_items}")
            _finalize_staged_release_folder(working_dir, repo=repo)

    return processed_count, skipped_ids, missing_ids


def process_queue_once(connection, github_token: Optional[str]) -> None:
    """Process due queue rows and re-check each job using configured intervals."""
    duplicate_count = supersede_duplicate_pending_jobs(connection)
    if duplicate_count > 0:
        print(
            "🧹 Queue cleanup: marked "
            f"{duplicate_count} duplicate pending job(s) as SUPERSEDED."
        )

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
        created_at = float(job["created_at"])
        expected_commit = job["expected_commit"]

        print(
            f"[{index}/{len(due_jobs)}] Job #{job_id}: {repo} {tag} "
            f"(attempt={attempt_count}, expected_commit={expected_commit or 'unknown'})"
        )

        retry_intervals_minutes = get_repository_recheck_intervals_minutes(repo)

        current_commit, current_commit_reason = get_current_commit_hash(
            repo,
            tag,
            github_token,
            include_reason=True,
        )
        if expected_commit and current_commit and expected_commit != current_commit:
            print(f"   🔁 Commit changed: {expected_commit} -> {current_commit}")

            existing_new_commit_job = get_pending_job_for_release(
                connection,
                repo,
                tag,
                release_type=release_type,
                expected_commit=current_commit,
                exclude_job_id=job_id,
            )

            if existing_new_commit_job is None:
                enqueue_job(
                    connection,
                    repo,
                    tag,
                    release_type=release_type,
                    next_check_time=now_timestamp,
                    expected_commit=current_commit,
                )
                print(
                    "   🆕 Created a new PENDING job for the updated commit "
                    f"({current_commit})."
                )
            else:
                print(
                    "   ℹ️ A PENDING job for the updated commit already exists "
                    f"(job_id={int(existing_new_commit_job['id'])}, commit={current_commit})."
                )

            artifact_outcome = _handle_superseded_pending_job_artifacts(
                job,
                repo,
                tag,
                reason_code="COMMIT_CHANGED_AUTO",
            )
            mark_job_superseded(
                connection,
                job_id,
                attempt_count=attempt_count,
                expected_commit=current_commit,
                downloaded_count=int(job["downloaded_count"] or 0),
                skipped_count=int(job["skipped_count"] or 0),
                total_items=int(job["total_items"] or 0),
                last_result=(
                    "SUPERSEDED_COMMIT_CHANGED_FINALIZED"
                    if artifact_outcome == "finalized"
                    else (
                        "SUPERSEDED_COMMIT_CHANGED_INCOMPLETE_MOVED"
                        if artifact_outcome == "quarantined"
                        else "SUPERSEDED_COMMIT_CHANGED"
                    )
                ),
            )
            print("   ⏭️ Marked current job as SUPERSEDED; skipping download for this older commit baseline.")
            continue
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
                "working_dir": None,
                "skip_reason": None,
            }

        if isinstance(result, dict):
            result_status = result.get("status", "FAILED")
            downloaded_count = int(result.get("downloaded_count", 0) or 0)
            skipped_count = int(result.get("skipped_count", 0) or 0)
            total_items = int(result.get("total_items", 0) or 0)
            skipped_items: list[SkippedItemPayload] = result.get("skipped_items") or []
            working_dir = result.get("working_dir")
            skip_reason = result.get("skip_reason")
        else:
            result_status = "SUCCESS" if result is True else ("SKIP" if result == "SKIP" else "FAILED")
            downloaded_count = 0
            skipped_count = 0
            total_items = 0
            skipped_items: list[SkippedItemPayload] = []
            working_dir = None
            skip_reason = None

        current_attempt_count = attempt_count + 1
        latest_commit = current_commit or expected_commit

        if result_status == "SKIP" and _is_terminal_skip_reason(skip_reason):
            mark_job_completed(
                connection,
                job_id,
                attempt_count=current_attempt_count,
                downloaded_count=downloaded_count,
                skipped_count=skipped_count,
                total_items=total_items,
                last_result=result_status,
            )
            save_job_skip_details(connection, job_id, current_attempt_count, skipped_items)
            print(
                "   ⏹️ Release/tag not found; marked COMPLETED immediately "
                f"at attempt={current_attempt_count}."
            )
            print(f"   📊 Files: downloaded={downloaded_count}, skipped={skipped_count}, total={total_items}")
            _finalize_staged_release_folder(working_dir, repo=repo)
            continue

        if current_attempt_count <= len(retry_intervals_minutes):
            target_age_minutes = retry_intervals_minutes[current_attempt_count - 1]
            next_check_time = created_at + (target_age_minutes * 60)

            # Calculate remaining delay relative to right now
            remaining_minutes = max(0, int(round((next_check_time - time.time()) / 60)))

            reschedule_job(
                connection,
                job_id,
                next_check_time=next_check_time,
                attempt_count=current_attempt_count,
                expected_commit=latest_commit,
                downloaded_count=downloaded_count,
                skipped_count=skipped_count,
                total_items=total_items,
                working_dir=working_dir,
                last_result="RETRY",
            )
            save_job_skip_details(connection, job_id, current_attempt_count, skipped_items)
            print(
                f"   🔄 Re-check scheduled in {remaining_minutes} minute(s) "
                f"(target task age: {target_age_minutes}m, attempt={current_attempt_count}, last_status={result_status})."
            )
            print(f"   📊 Files: downloaded={downloaded_count}, skipped={skipped_count}, total={total_items}")
        else:
            if result_status in ("SUCCESS", "SKIP"):
                mark_job_completed(
                    connection,
                    job_id,
                    attempt_count=current_attempt_count,
                    downloaded_count=downloaded_count,
                    skipped_count=skipped_count,
                    total_items=total_items,
                    last_result=result_status,
                )
                if result_status == "SUCCESS":
                    _warn_if_file_count_changed_from_previous_success(
                        connection,
                        job_id=job_id,
                        repo=repo,
                        tag=tag,
                        release_type=release_type,
                        current_total_items=total_items,
                    )
            else:
                mark_job_failed(
                    connection,
                    job_id,
                    attempt_count=current_attempt_count,
                    expected_commit=latest_commit,
                    downloaded_count=downloaded_count,
                    skipped_count=skipped_count,
                    total_items=total_items,
                    last_result="FAILED",
                )
            save_job_skip_details(connection, job_id, current_attempt_count, skipped_items)
            if result_status in ("SUCCESS", "SKIP"):
                print(
                    "   ✅ Re-check plan complete; marked COMPLETED "
                    f"at attempt={current_attempt_count} (last_status={result_status})."
                )
            else:
                print(
                    "   ❌ Re-check plan complete; marked FAILED "
                    f"at attempt={current_attempt_count}."
                )
            print(f"   📊 Files: downloaded={downloaded_count}, skipped={skipped_count}, total={total_items}")
            _finalize_staged_release_folder(working_dir, repo=repo)


def run_ingest_and_queue_cycle(connection, github_token: Optional[str]) -> None:
    """Run one full cycle: ingest new emails, then process due queue jobs."""
    ingest_notifications_once(connection, github_token)
    process_queue_once(connection, github_token)
