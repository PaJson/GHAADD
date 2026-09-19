from datetime import datetime
from typing import Optional

from db_manager import get_lifecycle_events, insert_lifecycle_event, open_database
from dry_run_mode import is_dry_run
from payload_types import IngestCycleStats, QueueCycleStats


def _record_lifecycle_event(
    event_type: str,
    message: str,
    category: Optional[str] = None,
    repo: Optional[str] = None,
    tag: Optional[str] = None,
    commit_hash: Optional[str] = None,
    destination_path: Optional[str] = None,
) -> None:
    """Insert one lifecycle event using a short-lived connection.

    Never raises, so a logging failure can never break queue processing.
    Skipped entirely in dry-run mode so it leaves no trace in state.db.
    """
    if is_dry_run():
        return

    connection = None
    try:
        connection = open_database()
        insert_lifecycle_event(
            connection,
            event_type,
            message,
            category=category,
            repo=repo,
            tag=tag,
            commit_hash=commit_hash,
            destination_path=destination_path,
        )
    except Exception:
        return
    finally:
        if connection is not None:
            connection.close()


def log_completed_move(
    repo: str,
    tag: str,
    commit: Optional[str],
    destination_path: str,
) -> None:
    """Record a completed-move lifecycle event."""
    commit_label = (commit or "unknown")[:7]
    message = f"Completed [{repo} {tag} ({commit_label})] moved to [{destination_path}]"
    _record_lifecycle_event(
        "COMPLETED_MOVE",
        message,
        repo=repo,
        tag=tag,
        commit_hash=commit,
        destination_path=destination_path,
    )


def log_partial_move(
    repo: str,
    tag: str,
    commit: Optional[str],
    destination_path: str,
) -> None:
    """Record a superseded-partial-move lifecycle event."""
    commit_label = (commit or "unknown")[:7]
    message = f"Superseded [{repo} {tag} ({commit_label})] moved to [{destination_path}]"
    _record_lifecycle_event(
        "PARTIAL_MOVE",
        message,
        repo=repo,
        tag=tag,
        commit_hash=commit,
        destination_path=destination_path,
    )


def log_warning(warning_type: str, message: str) -> None:
    """Record a typed warning lifecycle event."""
    normalized_type = str(warning_type or "GENERAL").strip().upper() or "GENERAL"
    _record_lifecycle_event("WARNING", message, category=normalized_type)


def list_lifecycle_events(
    limit: Optional[int] = 20,
    event_type: Optional[str] = None,
    repo_filter: Optional[str] = None,
) -> list[dict]:
    """Return recent lifecycle events as plain dicts, newest first."""
    connection = None
    try:
        connection = open_database()
        rows = get_lifecycle_events(
            connection,
            limit=limit,
            event_type=event_type,
            repo_filter=repo_filter,
        )
    finally:
        if connection is not None:
            connection.close()

    events = []
    for row in rows:
        created_at = float(row["created_at"])
        events.append(
            {
                "id": int(row["id"]),
                "event_type": row["event_type"],
                "category": row["category"],
                "repo": row["repo"],
                "tag": row["tag"],
                "commit_hash": row["commit_hash"],
                "destination_path": row["destination_path"],
                "message": row["message"],
                "created_at": created_at,
                "created_at_readable": datetime.fromtimestamp(created_at).strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
    return events


def log_cycle_summary(
    ingest_stats: Optional[IngestCycleStats] = None,
    queue_stats: Optional[QueueCycleStats] = None,
) -> str:
    """Record a one-line summary for one ingest/process cycle and return it."""
    ingest_values: dict = dict(ingest_stats or {})
    queue_values: dict = dict(queue_stats or {})

    notifications_part = (
        "notifications: found={found} queued={queued} "
        "(malformed={malformed}, paused={paused}, skiplist={skiplist}, "
        "errors={errors}, duplicates_collapsed={duplicates})"
    ).format(
        found=ingest_values.get("notifications_found", 0),
        queued=ingest_values.get("notifications_queued", 0),
        malformed=ingest_values.get("notifications_skipped_malformed", 0),
        paused=ingest_values.get("notifications_skipped_paused", 0),
        skiplist=ingest_values.get("notifications_skipped_skiplist", 0),
        errors=ingest_values.get("notifications_errors", 0),
        duplicates=ingest_values.get("notifications_collapsed_duplicates", 0),
    )

    queue_part = (
        "queue: due={due} completed={completed} failed={failed} "
        "retried={retried} superseded={superseded} "
        "files(downloaded={downloaded}, skipped={skipped})"
    ).format(
        due=queue_values.get("due_jobs", 0),
        completed=queue_values.get("completed", 0),
        failed=queue_values.get("failed", 0),
        retried=queue_values.get("retried", 0),
        superseded=queue_values.get("superseded", 0),
        downloaded=queue_values.get("downloaded_files", 0),
        skipped=queue_values.get("skipped_files", 0),
    )

    message = f"Cycle summary - {notifications_part} | {queue_part}"
    _record_lifecycle_event("CYCLE_SUMMARY", message)
    return message


