"""Read-only performance baseline (`python main.py --perf-report`).

Measures the things the daemon and the GUI do over and over (config/mapping loads, the state.db
queries, the status probes, the log tail) against the data you actually have, plus how big and how fast
that data grows. Nothing is written or changed, so it is safe to run while the daemon is working.
Run it now and again after a few weeks of use: T5.5 ("performance pass on measured bottlenecks only")
starts from the difference.

GUI drawing (Tk) is not measured here, only the data work behind it.
"""

from __future__ import annotations

import os
import platform
import statistics
import time
from contextlib import closing
from datetime import datetime
from typing import Any, Callable, Optional

from modules import (
    config_manager,
    daemon_lock,
    db_manager,
    gui_daemon,
    gui_data,
    log_files,
    log_tail,
    mapping_manager,
    status_tabs,
)
from modules.app_info import __version__

DEFAULT_REPEAT = 15
SLOW_MS = 50.0  # a single step slower than this is flagged in the notes
LOG_TAIL_LINES = log_tail.DEFAULT_MAX_LINES


class _MemoryStore:
    """Read marks that live and die inside the measurement (the real config.json is never written)."""

    def load(self) -> dict[str, int]:
        return {}

    def save(self, seen: dict[str, int]) -> None:
        pass


def _time_it(name: str, work: Callable[[], Any], repeat: int, group: str) -> dict[str, Any]:
    """Run `work` `repeat` times (after one warm-up) and return the timing in milliseconds."""
    try:
        work()
        samples = []
        for _ in range(repeat):
            started = time.perf_counter()
            work()
            samples.append((time.perf_counter() - started) * 1000)
    except Exception as exc:  # one broken probe must not hide the others
        return {"group": group, "name": name, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "group": group,
        "name": name,
        "median_ms": round(statistics.median(samples), 3),
        "min_ms": round(min(samples), 3),
        "max_ms": round(max(samples), 3),
        "runs": repeat,
    }


def _lazy(keep: dict[str, Any], key: str, factory: Callable[[], Any]) -> Any:
    """The object kept under `key`, created by `factory` on first use (setdefault would build one every call)."""
    if key not in keep:
        keep[key] = factory()
    return keep[key]


def _file_size(path: str) -> Optional[int]:
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def _collect_sizes() -> dict[str, Any]:
    state_db = db_manager.get_state_db_path()
    log_directory = config_manager.get_terminal_log_settings()["directory"]
    log_paths = log_files.list_log_files(os.path.expandvars(os.path.expanduser(log_directory or "")))
    log_sizes = [size for size in (_file_size(path) for path in log_paths) if size is not None]
    return {
        "state_db_bytes": _file_size(state_db),
        "state_db_wal_bytes": _file_size(f"{state_db}-wal"),
        "mapping_json_bytes": _file_size(mapping_manager._mapping_file_path()),
        "config_json_bytes": _file_size(config_manager._config_file_path()),
        "log_directory": log_directory,
        "log_files": len(log_paths),
        "log_bytes_total": sum(log_sizes),
        "log_bytes_largest": max(log_sizes, default=0),
    }


def _growth(stats: dict[str, Any], sizes: dict[str, Any], now: float) -> dict[str, Any]:
    """Per-day rates from the history that exists (rough: the history may be shorter than a week)."""
    oldest = min((t for t in (stats["oldest_job_at"], stats["oldest_event_at"]) if t), default=None)
    days = max((now - oldest) / 86400, 1 / 24) if oldest else None
    db_bytes = sizes["state_db_bytes"]
    return {
        "history_days": round(days, 1) if days else None,
        "jobs_per_day_last_7": round(stats["jobs_last_7_days"] / 7, 1),
        "events_per_day_last_7": round(stats["events_last_7_days"] / 7, 1),
        "state_db_bytes_per_day": round(db_bytes / days) if days and db_bytes else None,
    }


def collect(repeat: int = DEFAULT_REPEAT) -> dict[str, Any]:
    """Gather sizes, growth and timings into a plain dict (JSON-friendly)."""
    now = time.time()
    sizes = _collect_sizes()
    timings: list[dict[str, Any]] = []
    stats: dict[str, Any] = {}

    def add(group: str, name: str, work: Callable[[], Any]) -> None:
        timings.append(_time_it(name, work, repeat, group))

    with closing(db_manager.open_database()) as connection:
        stats = db_manager.get_storage_stats(connection)

        add("files", "load config.json", config_manager.load_config)
        add("files", "config fingerprint (read + hash)", config_manager.get_config_fingerprint)
        add("files", "load mapping.json", mapping_manager.load_mapping)

        add("state.db", "open + close state.db", lambda: db_manager.open_database().close())
        add("state.db", "queue summary per repo", lambda: db_manager.get_repo_job_summaries(connection))
        add("state.db", "all pending jobs", lambda: db_manager.get_pending_jobs(connection))
        add("state.db", "jobs due now", lambda: db_manager.get_due_jobs(connection, time.time()))
        for tab in status_tabs.TAB_DEFS:
            if tab.kind == status_tabs.KIND_EVENTS:
                add(
                    "state.db",
                    f"status tab '{tab.title}' (latest {status_tabs.DEFAULT_ROW_LIMIT})",
                    lambda tab=tab: db_manager.get_events_for_tab(
                        connection, tab.event_types, tab.categories, tab.exclude_categories, None,
                        status_tabs.DEFAULT_ROW_LIMIT,
                    ),
                )

    add("daemon probes", "daemon lock + status file", daemon_lock.get_daemon_status)
    add("daemon probes", "GUI daemon snapshot (cold, reads control table)",
        lambda: gui_daemon.SnapshotReader().read(force_control=True))
    # The "warm" probes keep one long-lived object, created lazily inside the guarded timing (the warm-up
    # run in _time_it is its first, cold use), so a failure while setting one up cannot abort the report.
    keep: dict[str, Any] = {}
    add("daemon probes", "GUI daemon snapshot (every 1 s tick)",
        lambda: _lazy(keep, "reader", gui_daemon.SnapshotReader).read())

    def table_cold() -> Any:
        gui_data._summaries_cache.invalidate()
        return gui_data.load_repo_table()

    add("GUI data", "Mappings table rows (changed database)", table_cold)
    add("GUI data", "Mappings table rows (unchanged database)", gui_data.load_repo_table)
    add("GUI data", "status tabs, first load of all four",
        lambda: gui_data.StatusFeed(store=_MemoryStore()).refresh())
    add("GUI data", "status tabs (unchanged database)",
        lambda: _lazy(keep, "feed", lambda: gui_data.StatusFeed(store=_MemoryStore())).refresh())
    add("GUI data", "unmapped list",
        lambda: _lazy(keep, "feed", lambda: gui_data.StatusFeed(store=_MemoryStore())).unmapped())

    def log_directory() -> str:
        return os.path.expandvars(os.path.expanduser(config_manager.get_terminal_log_settings()["directory"] or ""))

    add("log tail", f"open the live log (last {LOG_TAIL_LINES} lines)",
        lambda: log_tail.LogTailer(log_directory).poll())
    add("log tail", "live log poll, nothing new",
        lambda: _lazy(keep, "tailer", lambda: log_tail.LogTailer(log_directory)).poll())

    report = {
        "generated_at": datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
        "version": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "daemon_running": daemon_lock.is_daemon_running(),
        "repeat": repeat,
        "sizes": sizes,
        "storage": stats,
        "growth": _growth(stats, sizes, now),
        "timings": timings,
    }
    report["notes"] = _notes(report)
    return report


def _notes(report: dict[str, Any]) -> list[str]:
    """Plain-language observations a reader would otherwise have to work out from the numbers."""
    notes: list[str] = []
    for timing in report["timings"]:
        if "error" in timing:
            notes.append(f"'{timing['name']}' could not be measured: {timing['error']}")
        elif timing["median_ms"] >= SLOW_MS:
            notes.append(f"'{timing['name']}' takes {timing['median_ms']:.0f} ms (the threshold for a closer look is {SLOW_MS:.0f} ms).")
    tables = report["storage"]["tables"]
    indexes = report["storage"]["indexes"]
    if not any(name.startswith("job_queue.") and "repo" in name for name in indexes):
        notes.append(
            f"job_queue has no index on repo; the per-repo queue summary scans all {tables['job_queue']:,} rows "
            "(fine today; watch its timing as the table grows)."
        )
    biggest = max(tables, key=lambda name: tables[name])
    notes.append(f"The largest table is {biggest} with {tables[biggest]:,} rows.")
    if report["storage"]["freelist_pages"]:
        notes.append(f"{report['storage']['freelist_pages']:,} unused pages could be reclaimed (VACUUM).")
    return notes


def _mb(size: Optional[int]) -> str:
    return "n/a" if size is None else f"{size / (1024 * 1024):,.1f} MB"


def format_report(report: dict[str, Any]) -> str:
    """Human-readable text version of collect()."""
    sizes, storage, growth = report["sizes"], report["storage"], report["growth"]
    lines = [
        f"GHAADD {report['version']} performance report ({report['generated_at']})",
        f"Python {report['python']} on {report['platform']}; daemon {'running' if report['daemon_running'] else 'not running'}",
        "",
        "Data",
        f"  state.db            {_mb(sizes['state_db_bytes'])}  (WAL {_mb(sizes['state_db_wal_bytes'])}; "
        f"{storage['page_count']:,} pages of {storage['page_size']} bytes, {storage['freelist_pages']:,} unused)",
        f"  mapping.json        {_mb(sizes['mapping_json_bytes'])}      config.json  {_mb(sizes['config_json_bytes'])}",
        f"  log files           {sizes['log_files']} files, {_mb(sizes['log_bytes_total'])} total, "
        f"largest {_mb(sizes['log_bytes_largest'])}",
    ]
    for table, count in storage["tables"].items():
        detail = ""
        if table == "job_queue":
            detail = "  (" + ", ".join(f"{k} {v:,}" for k, v in sorted(storage["jobs_by_status"].items())) + ")"
        elif table == "lifecycle_events":
            detail = "  (" + ", ".join(f"{k} {v:,}" for k, v in sorted(storage["events_by_type"].items())) + ")"
        lines.append(f"  {table:<19} {count:>9,} rows{detail}")

    lines += [
        "",
        "Growth",
        f"  history covers      {growth['history_days']} days" if growth["history_days"] else "  history covers      (no data yet)",
        f"  jobs / events       {growth['jobs_per_day_last_7']} / {growth['events_per_day_last_7']} per day (last 7 days)",
    ]
    if growth["state_db_bytes_per_day"]:
        lines.append(f"  state.db            about {growth['state_db_bytes_per_day'] / (1024 * 1024):.2f} MB per day of history")

    lines += ["", f"Timings (median of {report['repeat']} runs, min - max)"]
    group = None
    for timing in report["timings"]:
        if timing["group"] != group:
            group = timing["group"]
            lines.append(f"  [{group}]")
        if "error" in timing:
            lines.append(f"    {timing['name']:<52} ERROR {timing['error']}")
        else:
            lines.append(
                f"    {timing['name']:<52} {timing['median_ms']:>8.2f} ms   ({timing['min_ms']:.2f} - {timing['max_ms']:.2f})"
            )

    lines += ["", "Notes"]
    lines += [f"  - {note}" for note in report["notes"]]
    return "\n".join(lines)
