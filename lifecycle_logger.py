from datetime import datetime
from typing import Optional

from db_manager import get_lifecycle_events, insert_lifecycle_event, open_database


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
    """
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

