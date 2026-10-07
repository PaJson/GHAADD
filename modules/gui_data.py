"""Read-only data loading for the GUI's Mappings table.

One call gathers mapping.json, the per-repo job summary from state.db and the
global recheck default, and returns display rows plus the raw entries (for the
edit form). The GUI never opens state.db itself.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

from modules import config_manager, daemon_lock, db_manager, mapping_manager, repo_overview, status_tabs
from modules.file_cache import StatCache, file_signature


@dataclass
class RepoTable:
    rows: list[repo_overview.RepoRow] = field(default_factory=list)
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)  # keyed by owner/repo as stored
    default_recheck: list[int] = field(default_factory=list)
    db_error: Optional[str] = None  # set when job data could not be read (rows then show no runtime info)


def _recheck_step_count(entry: Mapping[str, Any], default_count: int) -> int:
    """Number of recheck steps for a repo: its own list when set, else the global default."""
    own = entry.get("recheck_intervals")
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


def _load_limit_warned() -> frozenset[str]:
    connection = db_manager.open_database()
    try:
        return frozenset(db_manager.get_repos_with_limit_warnings(connection))
    finally:
        connection.close()


def _load_folder_counts() -> dict[str, dict[str, Any]]:
    connection = db_manager.open_database()
    try:
        return db_manager.get_folder_counts(connection)
    finally:
        connection.close()


def _state_db_files() -> list[str]:
    path = db_manager.get_state_db_path()
    return [path, f"{path}-wal"]  # in WAL mode the daemon's commits land in the -wal file first


# The queue summary (the costly part of a refresh) only changes when the daemon writes to
# state.db, so it is recomputed only when the db or its WAL file changed (or after 30 s as a
# safety net). Rows are still rebuilt every time: Queued vs Waiting depends on the clock.
_summaries_cache: StatCache[dict[str, Any]] = StatCache(_state_db_files, _load_summaries, max_age=30.0)

_limit_cache: StatCache[frozenset[str]] = StatCache(_state_db_files, _load_limit_warned, max_age=30.0)
_counts_cache: StatCache[dict[str, dict[str, Any]]] = StatCache(_state_db_files, _load_folder_counts, max_age=30.0)


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

    limit_warned: frozenset[str] = frozenset()
    try:
        limit_warned = _limit_cache.get()
    except Exception:  # only the warning marker is lost
        pass

    folder_counts: dict[str, dict[str, Any]] = {}
    try:
        folder_counts = _counts_cache.get()
    except Exception:  # the counts are optional decoration
        pass

    running_repo: Optional[str] = None
    try:
        current_job = daemon_lock.get_daemon_status()["current_job"]
        running_repo = current_job.get("repo") if isinstance(current_job, dict) else None
    except Exception:  # a status hiccup must not blank the mapping list
        pass

    entries = {str(e.get("repository") or "").strip(): e for e in mapping["repositories"] if e.get("repository")}
    rows = repo_overview.build_rows(
        list(entries.values()),
        summaries,
        now,
        lambda entry: _recheck_step_count(entry, len(default_recheck)),
        running_repo=running_repo,
        limit_warned=limit_warned,
        folder_counts=folder_counts,
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


# ----- status tabs (Warnings / Completed / Folder limits / Unmapped) -----

STATUS_SEEN_SECTION = "gui.status_tabs"
STATUS_REFRESH_MAX_AGE_SECONDS = 30.0


class ConfigSeenStore:
    """Per-tab last-seen event ids, kept in config.json under gui.status_tabs (GUI-side only)."""

    def load(self) -> dict[str, int]:
        gui = config_manager.load_config().get("gui")
        tabs = gui.get("status_tabs") if isinstance(gui, dict) else None
        if not isinstance(tabs, dict):
            return {}
        return {str(key): value for key, value in tabs.items() if isinstance(value, int) and not isinstance(value, bool)}

    def save(self, seen: dict[str, int]) -> None:
        try:
            config_manager.set_config_values({f"{STATUS_SEEN_SECTION}.{key}": value for key, value in seen.items()})
        except (config_manager.ConfigLockTimeout, config_manager.ConfigUnreadableError, OSError):
            pass  # the read marks just are not remembered this time; never break the GUI over it


class StatusFeed:
    """Loads the status tabs' data cheaply enough to poll every few seconds.

    The event tabs only look at state.db when it (or its WAL file) changed, plus a safety refresh
    every 30 s; the Unmapped list only re-reads mapping.json when that file changed.
    """

    def __init__(
        self,
        defs: tuple[status_tabs.TabDef, ...] = status_tabs.TAB_DEFS,
        store: Optional[status_tabs.SeenStore] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._mapping_cache: StatCache[dict[str, list[dict[str, Any]]]] = StatCache(
            lambda: [mapping_manager._mapping_file_path()], mapping_manager.load_mapping, clock=clock
        )
        self._connection: Any = None
        self._signature: Optional[tuple[Any, ...]] = None
        self._refreshed_at = 0.0
        self.model = status_tabs.StatusTabsModel(
            defs, self._fetch, self._max_id, store if store is not None else ConfigSeenStore(),
            known_repos=self._known_repos,
            existing_ids=self._existing_ids,
        )
        self.defs = defs

    def refresh(self) -> set[str]:
        """Update the event rows if the database changed; returns the keys of tabs that got new rows."""
        signature = file_signature(_state_db_files())
        now = self._clock()
        if (
            self._signature is not None
            and signature == self._signature
            and now - self._refreshed_at < STATUS_REFRESH_MAX_AGE_SECONDS
        ):
            return set()
        self._connection = db_manager.open_database()
        try:
            changed = self.model.refresh()
        finally:
            self._connection.close()
            self._connection = None
        self._signature, self._refreshed_at = signature, now
        return changed

    def count_repo_limit_warnings(self, repo: str) -> int:
        return self._purge_repo(repo, dry_run=True)

    def clear_repo_limit_warnings(self, repo: str) -> int:
        """Delete one repository's folder-limit warnings and drop them from the Folder limits tab."""
        removed = self._purge_repo(repo, dry_run=False)
        self.model.drop_repo("limits", repo)
        self._signature = None
        return removed

    def _purge_repo(self, repo: str, dry_run: bool) -> int:
        connection = db_manager.open_database()
        try:
            return db_manager.purge_limit_warnings_for_repo(connection, repo, dry_run=dry_run)
        finally:
            connection.close()

    def count_tab(self, key: str) -> int:
        """How many stored events the tab lists (all of them, not only the rows on screen)."""
        return self._purge(key, dry_run=True)

    def clear_tab(self, key: str) -> int:
        """Delete the events the tab lists from state.db and empty the tab; returns how many were removed."""
        removed = self._purge(key, dry_run=False)
        self.model.clear(key)
        self._signature = None  # the next refresh looks at the database again
        return removed

    def _purge(self, key: str, dry_run: bool) -> int:
        tab = next(definition for definition in self.defs if definition.key == key)
        connection = db_manager.open_database()
        try:
            return db_manager.purge_events_for_tab(
                connection, tab.event_types, tab.categories, tab.exclude_categories, dry_run=dry_run
            )
        finally:
            connection.close()

    def unmapped(self) -> list[status_tabs.UnmappedRow]:
        return status_tabs.unmapped_rows(self._mapping_cache.get()["repositories"])

    # ----- callbacks for the model -----

    def _fetch(self, tab: status_tabs.TabDef, after_id: Optional[int], limit: int) -> list[dict[str, Any]]:
        return db_manager.get_events_for_tab(
            self._connection, tab.event_types, tab.categories, tab.exclude_categories, after_id, limit
        )

    def _existing_ids(self, ids: list[int]) -> set[int]:
        return db_manager.get_existing_event_ids(self._connection, ids)

    def _max_id(self) -> int:
        return db_manager.get_max_event_id(self._connection)

    def _known_repos(self) -> dict[str, str]:
        entries = self._mapping_cache.get()["repositories"]
        return {str(e["repository"]).strip().lower(): str(e["repository"]).strip() for e in entries if e.get("repository")}
