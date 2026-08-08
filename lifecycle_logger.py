import os
from datetime import datetime
from typing import Optional

from config_manager import get_logging_settings, load_config


def _append_lifecycle_line(file_name: str, message: str) -> None:
    """Append one timestamped line to a lifecycle log file."""
    config = load_config()
    log_dir = get_logging_settings(config).get("directory", "")
    if not isinstance(log_dir, str) or not log_dir.strip():
        return

    log_dir = os.path.expandvars(os.path.expanduser(log_dir))

    try:
        os.makedirs(log_dir, exist_ok=True)
        file_path = os.path.join(log_dir, file_name)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(file_path, "a", encoding="utf-8", buffering=1) as log_stream:
            log_stream.write(f"[{timestamp}] {message}\n")
    except OSError:
        # Never break queue processing because lifecycle logging failed.
        return


def log_completed_move(
    repo: str,
    tag: str,
    commit: Optional[str],
    destination_path: str,
) -> None:
    """Record a short completed-move line in Complete.log."""
    commit_label = (commit or "unknown")[:7]
    message = f"Completed [{repo} {tag} ({commit_label})] moved to [{destination_path}]"
    _append_lifecycle_line("Complete.log", message)


def log_partial_move(
    repo: str,
    tag: str,
    commit: Optional[str],
    destination_path: str,
) -> None:
    """Record a short superseded-partial move line in Partial.log."""
    commit_label = (commit or "unknown")[:7]
    message = f"Superseded [{repo} {tag} ({commit_label})] moved to [{destination_path}]"
    _append_lifecycle_line("Partial.log", message)


def log_warning(warning_type: str, message: str) -> None:
    """Record a typed warning line in Warning.log."""
    normalized_type = str(warning_type or "GENERAL").strip().upper() or "GENERAL"
    _append_lifecycle_line("Warning.log", f"- [{normalized_type}] - {message}")
