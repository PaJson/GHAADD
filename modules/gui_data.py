"""Read-only data loading for the GUI's Mappings table.

One call gathers mapping.json, the per-repo job summary from state.db and the
global recheck default, and returns display rows plus the raw entries (for the
edit form). The GUI never opens state.db itself.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from modules import config_manager, daemon_lock, db_manager, mapping_manager, repo_overview
from modules.file_cache import StatCache


@dataclass
class RepoTable:
    rows: list[repo_overview.RepoRow] = field(default_factory=list)
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)  # keyed by owner/repo as stored
    default_recheck: list[int] = field(default_factory=list)
    db_error: Optional[str] = None  # set when job data could not be read (rows then show no runtime info)


def _recheck_step_count(entry: Mapping[str, Any], default_count: int) -> int:
    """Number of recheck steps for a repo: its own list when set, else the global default."""
    own = entry.get("recheck_intervals_minutes")
    if isinstance(own, list):
        valid = {int(v) for v in own if isinstance(v, int) and not isinstance(v, bool) and v > 0}
        if valid:
            return len(valid)
    return default_count


def _load_summaries() -> dict[str, Any]:
    connection = db_manager.open_database()
    try:
        return db_manager.get_repo_job_summaries(connection)
    finally:
        connection.close()


def _state_db_files() -> list[str]:
    path = db_manager.get_state_db_path()
    return [path, f"{path}-wal"]  # in WAL mode the daemon's commits land in the -wal file first


# The queue summary (the costly part of a refresh) only changes when the daemon writes to
# state.db, so it is recomputed only when the db or its WAL file changed (or after 30 s as a
# safety net). Rows are still rebuilt every time: Queued vs Waiting depends on the clock.
_summaries_cache: StatCache[dict[str, Any]] = StatCache(_state_db_files, _load_summaries, max_age=30.0)


def load_repo_table(now: Optional[float] = None) -> RepoTable:
    """Load rows for the Mappings table, most recently worked-on repository first."""
    now = time.time() if now is None else now
    mapping = mapping_manager.load_mapping()
    default_recheck = config_manager.get_recheck_intervals_minutes()

    summaries: dict[str, Any] = {}
    db_error: Optional[str] = None
    try:
        summaries = _summaries_cache.get()
    except Exception as exc:  # a locked/corrupt db must not blank the mapping list
        db_error = f"state.db unavailable: {exc}"

    running_repo: Optional[str] = None
    try:
        current_job = daemon_lock.get_daemon_status()["current_job"]
        running_repo = current_job.get("repo") if isinstance(current_job, dict) else None
    except Exception:  # a status hiccup must not blank the mapping list
        pass

    entries = {str(e.get("name") or "").strip(): e for e in mapping["repositories"] if e.get("name")}
    rows = repo_overview.build_rows(
        list(entries.values()),
        summaries,
        now,
        lambda entry: _recheck_step_count(entry, len(default_recheck)),
        running_repo=running_repo,
    )
    return RepoTable(rows=rows, entries=entries, default_recheck=default_recheck, db_error=db_error)


def load_settings_form() -> dict[str, Any]:
    """Current config.json values as the strings/bool the Settings dialog shows.

    The download dir is shown as written in config.json (not the normalized
    "D:\\" form) so saving an untouched dialog does not rewrite it.
    """
    config = config_manager.load_config()
    polling = config_manager.get_polling_settings(config)
    terminal_log = config_manager.get_terminal_log_settings(config)
    paths = config.get("paths")
    raw_dir = paths.get("default_download_dir") if isinstance(paths, dict) else None
    download_dir = raw_dir if isinstance(raw_dir, str) and raw_dir.strip() else config_manager.get_default_download_dir(config)
    return {
        "recheck": ", ".join(str(m) for m in config_manager.get_recheck_intervals_minutes(config)),
        "max_emails": str(config_manager.get_max_emails_to_process(config)),
        "dest_check": str(config_manager.get_destination_check_every_n_polls(config)),
        "interval": str(polling["interval_seconds"]),
        "jitter_min": str(polling["jitter_min_seconds"]),
        "jitter_max": str(polling["jitter_max_seconds"]),
        "download_dir": download_dir,
        "log_enabled": terminal_log["enabled"],
        "log_max_mb": str(terminal_log["max_file_mb"]),
        "log_keep": str(terminal_log["keep_files"]),
    }
