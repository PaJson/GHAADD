import requests
import sys
import time
import os

from config_manager import get_folder_settings, get_max_emails_to_process, get_recheck_intervals_minutes
from dry_run_mode import is_dry_run
from db_manager import (
    enqueue_job,
    get_jobs_by_ids,
    get_next_pending_job,
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
    get_release_data,
    move_processing_folder_to_complete,
    move_processing_folder_to_partial,
)
from lifecycle_logger import log_completed_move, log_cycle_summary, log_partial_move, log_warning
from mailbox_listener import (
    get_pending_notifications,
    mark_as_read_and_delete,
    move_unread_to_trash,
)
from mapping_manager import (
    get_repository_recheck_intervals_minutes,
    is_release_type_skipped,
    is_repository_paused,
    mark_repository_finalized,
    upsert_repository_mapping,
)
from payload_types import (
    DownloadReleaseResult,
    IngestCycleStats,
    NotificationPayload,
    QueuedItemInfo,
    QueuedNotificationPayload,
    QueueCycleStats,
    SkippedItemPayload,
)
from typing import Literal, Optional, Tuple, Union, cast, overload


GITHUB_API_VERSION = "2022-11-28"
_FOLDER_SETTINGS = get_folder_settings()
_COMPLETE_LABEL = _FOLDER_SETTINGS["complete"]
_PARTIAL_LABEL = _FOLDER_SETTINGS["partial"]


def _finalize_staged_release_folder(
    working_dir: Optional[str],
    repo: Optional[str] = None,
    tag: Optional[str] = None,
    commit: Optional[str] = None,
    write_complete_log: bool = True,
) -> Optional[str]:
    """Move a terminal job's staging folder from Processing to complete destination."""
    if not working_dir:
        return None

    try:
        done_dir = move_processing_folder_to_complete(working_dir, repo=repo)
    except OSError as exc:
        print(f"   ⚠️ Could not move staging folder to {_COMPLETE_LABEL}: {exc}")
        log_warning("MOVE", f"Could not move staging folder to {_COMPLETE_LABEL}: {exc}")
        return None

    if done_dir:
        print(f"   📁 Finalized artifacts: {done_dir}")
        if repo:
            mark_repository_finalized(repo)
        if write_complete_log and repo and tag:
            log_completed_move(repo, tag, commit, done_dir)

    return done_dir


def _handle_working_dir_relocation(
    previous_working_dir: Optional[str],
    new_working_dir: Optional[str],
    repo: str,
    tag: str,
    job_id: int,
) -> None:
    """Warn about and quarantine a Processing folder abandoned by a mid-flight rename.

    Re-check attempts recompute the staging folder name from live release metadata
    (title, prerelease flag). If that metadata changes upstream between attempts,
    a new folder is used and the previous attempt's folder is no longer referenced
    by the job - left alone, it would silently linger in Processing forever.
    """
    if not previous_working_dir or not new_working_dir:
        return
    if os.path.normcase(os.path.normpath(previous_working_dir)) == os.path.normcase(os.path.normpath(new_working_dir)):
        return
    if not os.path.isdir(previous_working_dir):
        return

    rename_message = (
        f"Job #{job_id} ({repo} {tag}): staging folder changed between attempts "
        "(release title/prerelease flag likely edited upstream), leaving the previous "
        f"attempt's folder unreferenced. Old folder: '{previous_working_dir}' -> "
        f"New folder: '{new_working_dir}'."
    )
    print(f"   ⚠️ {rename_message}")
    log_warning("FOLDER_RENAMED", rename_message)

    try:
        quarantined_dir = move_processing_folder_to_partial(previous_working_dir)
    except OSError as exc:
        move_failure_message = f"Could not move stale staging folder '{previous_working_dir}' to {_PARTIAL_LABEL}: {exc}"
        print(f"   ⚠️ {move_failure_message}")
        log_warning("FOLDER_RENAMED_MOVE", move_failure_message)
        return

    if quarantined_dir:
        print(f"   📁 Moved stale staging folder to {_PARTIAL_LABEL}: {quarantined_dir}")
        log_warning(
            "FOLDER_RENAMED_MOVED",
            f"Job #{job_id} ({repo} {tag}): moved stale staging folder to {_PARTIAL_LABEL}: {quarantined_dir}",
        )


def _is_terminal_skip_reason(skip_reason: Optional[str]) -> bool:
    """Return True when a SKIP result should end the re-check plan immediately."""
    return skip_reason in {"release_not_found"}


def _preserve_best_known_counters(
    row,
    downloaded_count: int,
    skipped_count: int,
    total_items: int,
    working_dir: Optional[str],
) -> Tuple[int, int, int, Optional[str]]:
    """Avoid clobbering previously recorded progress with a worse-result attempt.

    An attempt that errors out or is skipped before reaching the asset list has no
    real counts of its own (0/0/0). Persisting those zeros would overwrite the
    genuine counts/working_dir left behind by an earlier successful attempt on the
    same job, which later makes a fully-staged release look incomplete.

    The same problem happens mid-run: if a prior attempt already accounted for
    every expected item (e.g. 16/16 downloaded+skipped), but this attempt gets
    interrupted partway through re-verification (a GitHub 404 because the release
    was replaced/deleted upstream mid-recheck), it reports fewer accounted-for
    items than before (e.g. 10/16). That regression is never true data loss -
    the previously downloaded files are still on disk - so it must not overwrite
    the last known-good, fully-accounted-for counters either.
    """
    previous_total_items = int(row["total_items"] or 0)
    if previous_total_items <= 0:
        return downloaded_count, skipped_count, total_items, working_dir

    previous_downloaded_count = int(row["downloaded_count"] or 0)
    previous_skipped_count = int(row["skipped_count"] or 0)
    previous_working_dir = str(row["working_dir"] or "").strip() or None
    previous_accounted = previous_downloaded_count + previous_skipped_count
    previously_fully_accounted = previous_accounted >= previous_total_items

    if total_items <= 0:
        return (
            previous_downloaded_count,
            previous_skipped_count,
            previous_total_items,
            working_dir or previous_working_dir,
        )

    current_accounted = downloaded_count + skipped_count
    regressed_from_complete = (
        previously_fully_accounted
        and total_items == previous_total_items
        and current_accounted < previous_accounted
    )
    if regressed_from_complete:
        return (
            previous_downloaded_count,
            previous_skipped_count,
            previous_total_items,
            working_dir or previous_working_dir,
        )

    return downloaded_count, skipped_count, total_items, working_dir


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
            log_warning("SUPERSEDE_MOVE", f"Could not move incomplete superseded staging folder: {exc}")
            return "none"

        if superseded_dir:
            print(
                "   [SUPERSEDE_FINALIZE] "
                f"{reason_code}: moved incomplete superseded job #{job_id} "
                f"to {_PARTIAL_LABEL}: {superseded_dir} "
                f"(files={downloaded_count}+{skipped_count}/{total_items})."
            )
            log_partial_move(repo, tag, row["expected_commit"], superseded_dir)
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

    done_dir = _finalize_staged_release_folder(
        working_dir,
        repo=repo,
        tag=tag,
        commit=row["expected_commit"],
        write_complete_log=False,
    )
    if not done_dir:
        print(
            "   [SUPERSEDE_FINALIZE] "
            f"{reason_code}: could not finalize staged artifacts for superseded pending job #{job_id}."
        )
        return "none"
    
    commit_hash = row["expected_commit"] or "unknown"
    
    supersede_finalize_message = (
        f"{reason_code}: finalized staged artifacts for superseded pending job #{job_id} "
        f"[{repo} {tag} ({commit_hash})] moved to [{done_dir}] "
        f"(files={downloaded_count}+{skipped_count}/{total_items})."
    )
    
    print(f"   [SUPERSEDE_FINALIZE] {supersede_finalize_message}")
    log_warning("SUPERSEDE_FINALIZE", supersede_finalize_message)
    return "finalized"


def _finalize_terminal_skip_job(
    row,
    repo: str,
    tag: str,
    commit: Optional[str],
    current_downloaded_count: int,
    current_skipped_count: int,
    current_total_items: int,
    current_working_dir: Optional[str],
) -> Tuple[int, int, int]:
    """Finalize a job whose release/tag disappeared, moving any real staged files.

    A terminal SKIP (release_not_found) attempt never reaches the asset list, so it
    always reports 0/0/0 and no working_dir. The job's persisted counters and
    working_dir from earlier attempts are the only real record of what was staged,
    so those are used for the file-count checks and the actual folder move.
    Returns the (downloaded_count, skipped_count, total_items) to record for the job.
    """
    if current_total_items > 0:
        downloaded_count = current_downloaded_count
        skipped_count = current_skipped_count
        total_items = current_total_items
        working_dir = current_working_dir
    else:
        downloaded_count = int(row["downloaded_count"] or 0)
        skipped_count = int(row["skipped_count"] or 0)
        total_items = int(row["total_items"] or 0)
        working_dir = str(row["working_dir"] or "").strip() or None

    if working_dir and os.path.isdir(working_dir):
        if _has_all_release_items_accounted(downloaded_count, skipped_count, total_items):
            done_dir = _finalize_staged_release_folder(working_dir, repo=repo, tag=tag, commit=commit)
            if done_dir:
                premature_message = (
                    f"Release/tag {tag} for {repo} disappeared from GitHub (likely replaced/superseded "
                    "upstream) before the re-check schedule finished; finalized as complete using the "
                    f"previously recorded counts (files={downloaded_count}+{skipped_count}/{total_items})."
                )
                print(f"   ⚠️ {premature_message}")
                log_warning("PREMATURE_FINALIZE", premature_message)
        else:
            try:
                partial_dir = move_processing_folder_to_partial(working_dir)
            except OSError as exc:
                print(f"   ⚠️ Could not move incomplete staging folder to {_PARTIAL_LABEL}: {exc}")
                log_warning("MOVE", f"Could not move incomplete staging folder to {_PARTIAL_LABEL}: {exc}")
                partial_dir = None
            if partial_dir:
                print(f"   📁 Moved incomplete artifacts to {_PARTIAL_LABEL}: {partial_dir}")
                log_partial_move(repo, tag, commit, partial_dir)

    return downloaded_count, skipped_count, total_items


def _warn_if_file_count_changed_from_previous_success(
    connection,
    job_id: int,
    repo: str,
    tag: str,
    release_type: Optional[str],
    current_total_items: int,
    working_dir: Optional[str] = None,
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
    folder = working_dir or "unknown"
    warning_message = (
        "Sanity check: file count changed versus previous successful release "
        f"for {repo} ({release_type or 'Release'}). "
        f"Current={current_total_items}, Previous={previous_total_items} "
        f"(tag={previous_tag}, delta={delta_sign}{delta}, folder={folder})."
    )
    print(f"   ⚠️ {warning_message}")
    log_warning("SANITY_CHECK", warning_message)


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


def _empty_ingest_cycle_stats() -> IngestCycleStats:
    """Return a zeroed ingest-cycle stats payload."""
    return {
        "notifications_found": 0,
        "notifications_collapsed_duplicates": 0,
        "notifications_queued": 0,
        "notifications_skipped_malformed": 0,
        "notifications_skipped_paused": 0,
        "notifications_skipped_skiplist": 0,
        "notifications_errors": 0,
    }


def ingest_notifications_once(
    connection,
    github_token: Optional[str],
    notification_limit: Optional[int] = None,
) -> Tuple[IngestCycleStats, Optional[QueuedItemInfo]]:
    """Ingest unseen notifications into job_queue and delete emails immediately.

    notification_limit, when given, overrides processing.max_emails_to_process
    for this call only (used by --single to fetch at most one notification).
    Returns cycle stats plus the identity of the one item just queued, if any
    (used by --single to immediately process that same item).
    """
    max_emails_to_process = notification_limit if notification_limit is not None else get_max_emails_to_process()

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
            return _empty_ingest_cycle_stats(), None

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
        paused_emails_to_trash = []
        queued_count = 0
        malformed_count = 0
        paused_count = 0
        skiplist_count = 0
        error_count = 0
        queued_item_info: Optional[QueuedItemInfo] = None
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
                malformed_count += 1
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

            if is_repository_paused(repo):
                print(
                    f"⏸️ Repository is paused; moved unread notification to Trash and skipped queueing {repo} {tag}."
                )
                paused_emails_to_trash.extend(email_ids)
                paused_count += 1
                continue

            # The email subject only reflects the release's state when GitHub sent the
            # notification; confirm against the live API before acting on it (skiplist, folder).
            live_release_data = get_release_data(repo, tag)
            if live_release_data is not None:
                live_release_type = "Pre-release" if live_release_data.get("prerelease") else "Release"
                if live_release_type != (release_type or "Release"):
                    print(
                        f"   ℹ️ Notification reported '{release_type_label}' but GitHub currently shows "
                        f"'{live_release_type}' for {repo} {tag}; using the live value."
                    )
                release_type = live_release_type
                release_type_label = release_type

            if is_release_type_skipped(repo, release_type):
                skip_message = (
                    f"{repo} {tag} ({release_type_label}) matched the repository skiplist; "
                    "notification was not queued."
                )
                print(f"⏭️ [SKIPPED] {skip_message}")
                log_warning("SKIPPED", skip_message)
                emails_to_delete.extend(email_ids)
                skiplist_count += 1
                continue

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

                    superseded_last_result = (
                        "SUPERSEDED_REPLACED_BY_NEW_NOTIFICATION_FINALIZED"
                        if artifact_outcome == "finalized"
                        else (
                            "SUPERSEDED_REPLACED_BY_NEW_NOTIFICATION_INCOMPLETE_MOVED"
                            if artifact_outcome == "quarantined"
                            else "SUPERSEDED_REPLACED_BY_NEW_NOTIFICATION"
                        )
                    )
                    if is_dry_run():
                        print(
                            f"   🧪 [DRY-RUN] Would mark pending job #{int(pending_row['id'])} "
                            f"as SUPERSEDED ({superseded_last_result})."
                        )
                    else:
                        mark_job_superseded(
                            connection,
                            int(pending_row["id"]),
                            attempt_count=int(pending_row["attempt_count"] or 0),
                            expected_commit=expected_commit or pending_row["expected_commit"],
                            downloaded_count=int(pending_row["downloaded_count"] or 0),
                            skipped_count=int(pending_row["skipped_count"] or 0),
                            total_items=int(pending_row["total_items"] or 0),
                            last_result=superseded_last_result,
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

                queued_job_id: Optional[int] = None
                if is_dry_run():
                    print(
                        f"   🧪 [DRY-RUN] Would queue as PENDING for {repo} {tag} "
                        f"(expected_commit={expected_commit or 'unknown'})."
                    )
                else:
                    queued_job_id = enqueue_job(
                        connection,
                        repo,
                        tag,
                        release_type=release_type,
                        next_check_time=now_timestamp,
                        expected_commit=expected_commit,
                    )
                    if expected_commit:
                        print(f"✓ Queued as PENDING for {repo} {tag} (expected_commit={expected_commit})")
                    else:
                        print(
                            f"✓ Queued as PENDING for {repo} {tag} "
                            f"(expected_commit=unknown, reason={commit_reason or 'unavailable'})"
                        )
                emails_to_delete.extend(email_ids)
                queued_count += 1
                queued_item_info = {
                    "repo": repo,
                    "tag": tag,
                    "release_type": release_type,
                    "job_id": queued_job_id,
                }
            except Exception as exc:
                print(f"✗ Error queueing {repo} {tag}: {str(exc)}\n")
                error_count += 1
                continue

        if emails_to_delete:
            print(f"🧹 Cleaning up {len(emails_to_delete)} queued email(s)...")
            if mark_as_read_and_delete(emails_to_delete):
                print("✓ Emails marked as read and moved to Trash.")
            else:
                warning_message = (
                    f"Could not mark/move {len(emails_to_delete)} queued email(s) to Trash after retrying; "
                    "they remain read but not deleted and may be re-queued next poll if marked unread again."
                )
                print(f"⚠️ {warning_message}")
                log_warning("MAILBOX", warning_message)

        if paused_emails_to_trash:
            print(f"🗑️ Moving {len(paused_emails_to_trash)} paused unread email(s) to Trash...")
            if move_unread_to_trash(paused_emails_to_trash):
                print("✓ Paused emails remain unread and were moved to Trash.")
            else:
                warning_message = (
                    f"Could not move {len(paused_emails_to_trash)} paused unread email(s) to Trash after retrying; "
                    "they remain in the mailbox and will be checked again on the next poll."
                )
                print(f"⚠️ {warning_message}")
                log_warning("MAILBOX", warning_message)

        print(f"Ingest complete. {queued_count} job(s) queued as PENDING.")
        return {
            "notifications_found": len(unique_notifications),
            "notifications_collapsed_duplicates": len(collapsed_notifications),
            "notifications_queued": queued_count,
            "notifications_skipped_malformed": malformed_count,
            "notifications_skipped_paused": paused_count,
            "notifications_skipped_skiplist": skiplist_count,
            "notifications_errors": error_count,
        }, queued_item_info

    except Exception as exc:
        print(f"Fatal error: {str(exc)}", file=sys.stderr)
        return _empty_ingest_cycle_stats(), None


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
            warning_message = (
                f"Could not resolve current commit hash from GitHub API for {repo} @ {tag} "
                f"(expected_commit={expected_commit or 'unknown'}, "
                f"reason={current_commit_reason or 'unavailable'})."
            )
            print(f"   ⚠️ {warning_message}")
            log_warning("API", warning_message)

        previous_working_dir = str(row["working_dir"] or "").strip() or None

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

        downloaded_count, skipped_count, total_items, working_dir = _preserve_best_known_counters(
            row, downloaded_count, skipped_count, total_items, working_dir
        )
        _handle_working_dir_relocation(previous_working_dir, working_dir, repo, tag, job_id)

        latest_commit = current_commit or expected_commit

        if result_status == "SKIP" and _is_terminal_skip_reason(skip_reason):
            downloaded_count, skipped_count, total_items = _finalize_terminal_skip_job(
                row,
                repo,
                tag,
                latest_commit,
                downloaded_count,
                skipped_count,
                total_items,
                working_dir,
            )
            mark_job_completed(
                connection,
                job_id,
                attempt_count=attempt_count,
                downloaded_count=downloaded_count,
                skipped_count=skipped_count,
                total_items=total_items,
                last_result=result_status,
            )
            _warn_if_file_count_changed_from_previous_success(
                connection,
                job_id=job_id,
                repo=repo,
                tag=tag,
                release_type=release_type,
                current_total_items=total_items,
                working_dir=working_dir,
            )
            save_job_skip_details(connection, job_id, attempt_count, skipped_items)
            print(
                "   ⏹️ Release/tag not found; marked COMPLETED immediately "
                f"at attempt={attempt_count}."
            )
            print(f"   📊 Files: downloaded={downloaded_count}, skipped={skipped_count}, total={total_items}")
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
                        working_dir=working_dir,
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
            _finalize_staged_release_folder(
                working_dir,
                repo=repo,
                tag=tag,
                commit=latest_commit,
            )

    return processed_count, skipped_ids, missing_ids


def _empty_queue_cycle_stats() -> QueueCycleStats:
    """Return a zeroed queue-cycle stats payload."""
    return {
        "due_jobs": 0,
        "completed": 0,
        "failed": 0,
        "retried": 0,
        "superseded": 0,
        "downloaded_files": 0,
        "skipped_files": 0,
    }


def process_queue_once(
    connection,
    github_token: Optional[str],
    limit: Optional[int] = None,
    job_filter_ids: Optional[list[int]] = None,
) -> QueueCycleStats:
    """Process due queue rows and re-check each job using configured intervals.

    limit caps how many due jobs are fetched this call (ignored when
    job_filter_ids is given). job_filter_ids, when given, processes exactly
    those job rows regardless of due time (used by --single to run a job
    that was just queued in the same cycle).
    """
    if is_dry_run():
        print("   🧪 [DRY-RUN] Skipping duplicate-pending-job cleanup (no writes in dry-run).")
    else:
        duplicate_count = supersede_duplicate_pending_jobs(connection)
        if duplicate_count > 0:
            print(
                "🧹 Queue cleanup: marked "
                f"{duplicate_count} duplicate pending job(s) as SUPERSEDED."
            )

    now_timestamp = time.time()
    if job_filter_ids:
        due_jobs = get_jobs_by_ids(connection, job_filter_ids)
    else:
        due_jobs = get_due_jobs(connection, now_timestamp, limit=limit)

    if not due_jobs:
        print("No due queue jobs found.")
        return _empty_queue_cycle_stats()

    print(f"Processing {len(due_jobs)} due queue job(s)...")

    completed_count = 0
    failed_count = 0
    retried_count = 0
    superseded_count = 0
    downloaded_files_total = 0
    skipped_files_total = 0

    for index, job in enumerate(due_jobs, 1):
        job_id = job["id"]
        repo = job["repo"]
        tag = job["tag"]
        release_type = job["release_type"]
        attempt_count = int(job["attempt_count"])
        created_at = float(job["created_at"])
        expected_commit = job["expected_commit"]

        retry_intervals_minutes = get_repository_recheck_intervals_minutes(repo)

        if 0 < attempt_count <= len(retry_intervals_minutes):
            attempt_schedule = (
                f"attempt={attempt_count} of {len(retry_intervals_minutes)}, "
                f"target task age: {retry_intervals_minutes[attempt_count - 1]}m"
            )
        else:
            attempt_schedule = f"attempt=0, initial run; {len(retry_intervals_minutes)} re-check(s) planned"

        print(
            f"[{index}/{len(due_jobs)}] Job #{job_id}: {repo} {tag} "
            f"({attempt_schedule}, "
            f"expected_commit={expected_commit or 'unknown'})"
        )

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
                if is_dry_run():
                    print(
                        "   🧪 [DRY-RUN] Would create a new PENDING job for the updated commit "
                        f"({current_commit})."
                    )
                else:
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
            commit_changed_last_result = (
                "SUPERSEDED_COMMIT_CHANGED_FINALIZED"
                if artifact_outcome == "finalized"
                else (
                    "SUPERSEDED_COMMIT_CHANGED_INCOMPLETE_MOVED"
                    if artifact_outcome == "quarantined"
                    else "SUPERSEDED_COMMIT_CHANGED"
                )
            )
            if is_dry_run():
                print(
                    f"   🧪 [DRY-RUN] Would mark job #{job_id} as SUPERSEDED "
                    f"({commit_changed_last_result}); skipping download for this older commit baseline."
                )
            else:
                mark_job_superseded(
                    connection,
                    job_id,
                    attempt_count=attempt_count,
                    expected_commit=current_commit,
                    downloaded_count=int(job["downloaded_count"] or 0),
                    skipped_count=int(job["skipped_count"] or 0),
                    total_items=int(job["total_items"] or 0),
                    last_result=commit_changed_last_result,
                )
                print("   ⏭️ Marked current job as SUPERSEDED; skipping download for this older commit baseline.")
            continue
        elif expected_commit and current_commit and expected_commit == current_commit:
            print(f"   ✅ Commit unchanged: {current_commit}")
        elif current_commit:
            print(f"   ℹ️ Commit baseline discovered: {current_commit}")
        else:
            warning_message = (
                f"Could not resolve current commit hash from GitHub API for {repo} @ {tag} "
                f"(expected_commit={expected_commit or 'unknown'}, "
                f"reason={current_commit_reason or 'unavailable'})."
            )
            print(f"   ⚠️ {warning_message}")
            log_warning("API", warning_message)

        previous_working_dir = str(job["working_dir"] or "").strip() or None

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

        downloaded_count, skipped_count, total_items, working_dir = _preserve_best_known_counters(
            job, downloaded_count, skipped_count, total_items, working_dir
        )
        _handle_working_dir_relocation(previous_working_dir, working_dir, repo, tag, job_id)

        current_attempt_count = attempt_count + 1
        latest_commit = current_commit or expected_commit
        is_terminal_skip = result_status == "SKIP" and _is_terminal_skip_reason(skip_reason)

        if current_attempt_count <= len(retry_intervals_minutes):
            target_age_minutes = retry_intervals_minutes[current_attempt_count - 1]
            next_check_time = created_at + (target_age_minutes * 60)

            # Calculate remaining delay relative to right now
            remaining_minutes = max(0, int(round((next_check_time - time.time()) / 60)))

            if is_dry_run():
                print(
                    f"   🧪 [DRY-RUN] Would reschedule job #{job_id} "
                    f"(attempt={current_attempt_count}, last_status={result_status}); no skip-detail rows written."
                )
            else:
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
            if is_terminal_skip:
                print(
                    "   ⏳ Release/tag not found yet; keeping job PENDING and re-checking "
                    f"per the full re-check schedule instead of completing immediately "
                    f"(attempt={current_attempt_count})."
                )
            print(
                f"   🔄 Re-check scheduled in {remaining_minutes} minute(s) "
                f"(target task age: {target_age_minutes}m, attempt={current_attempt_count}, last_status={result_status})."
            )
            print(f"   📊 Files: downloaded={downloaded_count}, skipped={skipped_count}, total={total_items}")
            retried_count += 1
            downloaded_files_total += downloaded_count
            skipped_files_total += skipped_count
        else:
            if is_terminal_skip:
                downloaded_count, skipped_count, total_items = _finalize_terminal_skip_job(
                    job,
                    repo,
                    tag,
                    latest_commit,
                    downloaded_count,
                    skipped_count,
                    total_items,
                    working_dir,
                )

            if result_status in ("SUCCESS", "SKIP"):
                if is_dry_run():
                    print(f"   🧪 [DRY-RUN] Would mark job #{job_id} as COMPLETED (last_status={result_status}).")
                else:
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
                            working_dir=working_dir,
                        )
            else:
                if is_dry_run():
                    print(f"   🧪 [DRY-RUN] Would mark job #{job_id} as FAILED.")
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
            if not is_dry_run():
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
            if not is_terminal_skip:
                _finalize_staged_release_folder(
                    working_dir,
                    repo=repo,
                    tag=tag,
                    commit=latest_commit,
                )

            downloaded_files_total += downloaded_count
            skipped_files_total += skipped_count
            if result_status in ("SUCCESS", "SKIP"):
                completed_count += 1
            else:
                failed_count += 1

    return {
        "due_jobs": len(due_jobs),
        "completed": completed_count,
        "failed": failed_count,
        "retried": retried_count,
        "superseded": superseded_count,
        "downloaded_files": downloaded_files_total,
        "skipped_files": skipped_files_total,
    }


def run_ingest_and_queue_cycle(connection, github_token: Optional[str]) -> None:
    """Run one full cycle: ingest new emails, then process due queue jobs."""
    ingest_stats, _queued_item_info = ingest_notifications_once(connection, github_token)
    queue_stats = process_queue_once(connection, github_token)

    summary_message = log_cycle_summary(ingest_stats, queue_stats)
    print(f"📋 {summary_message}")

    next_pending_job = get_next_pending_job(connection)
    if next_pending_job is None:
        print("Next pending job: NONE")
        return

    next_check_time_readable = time.strftime(
        "%Y-%m-%d %H:%M:%S",
        time.localtime(float(next_pending_job["next_check_time"])),
    )
    print(
        "Next pending job: "
        f"{next_pending_job['repo']} {next_pending_job['tag']} @ {next_check_time_readable}"
    )


def run_single_cycle(connection, github_token: Optional[str]) -> None:
    """Ingest at most one new notification and process at most one queue item.

    Prefers the just-ingested notification's job when one was queued; falls
    back to the oldest due job already in the queue otherwise. Fully honors
    dry-run mode (is_dry_run()) throughout the call chain: when dry-run and a
    notification was found, nothing was actually enqueued, so the would-be
    download is previewed directly instead of looking up a real job id.
    """
    ingest_stats, queued_item = ingest_notifications_once(connection, github_token, notification_limit=1)

    if queued_item is not None and queued_item.get("job_id") is not None:
        queued_job_id = int(cast(int, queued_item["job_id"]))
        queue_stats = process_queue_once(connection, github_token, job_filter_ids=[queued_job_id])
    elif queued_item is not None:
        print(
            "🧪 [DRY-RUN] Would process newly-ingested item now: "
            f"{queued_item['repo']} {queued_item['tag']} ({queued_item.get('release_type') or 'Release'})"
        )
        download_release(queued_item["repo"], queued_item["tag"], queued_item.get("release_type"))
        queue_stats = _empty_queue_cycle_stats()
    else:
        queue_stats = process_queue_once(connection, github_token, limit=1)

    summary_message = log_cycle_summary(ingest_stats, queue_stats)
    print(f"📋 {summary_message}")

    next_pending_job = get_next_pending_job(connection)
    if next_pending_job is None:
        print("Next pending job: NONE")
        return

    next_check_time_readable = time.strftime(
        "%Y-%m-%d %H:%M:%S",
        time.localtime(float(next_pending_job["next_check_time"])),
    )
    print(
        "Next pending job: "
        f"{next_pending_job['repo']} {next_pending_job['tag']} @ {next_check_time_readable}"
    )
