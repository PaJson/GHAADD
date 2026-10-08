"""Statistics about the mapped repositories and the work GHAADD has done (CLI `--stats`, GUI "Stats" button).

Read-only: it reads mapping.json and state.db and never writes either. The figures come from three places:
  * mapping.json: how many repositories are mapped, active, without a destination, ...
  * job_queue: one row per release GHAADD was told about (status COMPLETED / FAILED / SUPERSEDED / PENDING),
    counted per period and per repository;
  * asset_state: the size of every file kept per release, joined to the job times, for the data volumes.
    ("Data" is what the releases weigh on disk when they were fetched; it is not network traffic and a file
    that was fetched twice counts once.)

collect() builds a plain dict (also what `--stats --json` prints); format_report() renders it as text. The window
only fills the dict, so the dialog and the CLI show the same numbers. Tests: tests/test_stats.py.
"""
from __future__ import annotations

import os
import time
from collections import Counter
from contextlib import closing
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Optional, Sequence

from modules import db_manager, mapping_manager
from modules.app_info import __version__

TOP_COUNT = 10
DAILY_DAYS = 30
DAY_SECONDS = 86400

# (key, label, window in days); None = since the beginning of the history.
PERIODS: tuple[tuple[str, str, Optional[int]], ...] = (
    ("day", "Last 24 hours", 1),
    ("week", "Last 7 days", 7),
    ("month", "Last 30 days", 30),
    ("year", "Last 365 days", 365),
    ("lifetime", "Lifetime", None),
)

SPARK_BLOCKS = "▁▂▃▄▅▆▇█"


def format_bytes(size: Optional[float]) -> str:
    """1536 -> '1.5 KB' (binary units, the way the file explorer shows sizes)."""
    if size is None:
        return "n/a"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            return f"{value:,.0f} {unit}" if unit == "B" else f"{value:,.1f} {unit}"
        value /= 1024
    return f"{value:,.1f} TB"  # pragma: no cover - the loop always returns


def _day(timestamp: float) -> date:
    """Convert an epoch timestamp to a local date."""
    return datetime.fromtimestamp(timestamp).date()


def _stamp(timestamp: Optional[float]) -> Optional[str]:
    """Format an epoch timestamp as "YYYY-MM-DD HH:MM", or None."""
    return None if timestamp is None else datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def _split_release_key(release_key: str) -> tuple[str, str]:
    """Split a "repo|tag" release key into (repo, tag)."""
    repo, _, tag = release_key.partition("|")
    return repo, tag


def mapping_stats(entries: Sequence[Mapping[str, Any]], folder_counts: Mapping[str, Mapping[str, Any]]) -> dict[str, int]:
    """Counts over the mapping.json entries (`folder_counts` is {lower-cased repo: {"folder_count": n}})."""
    active = [entry for entry in entries if entry.get("active", True) is not False]
    over_limit = 0
    for entry in entries:
        limit = entry.get("limit")
        counted = folder_counts.get(str(entry.get("repository", "")).strip().lower())
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0 and counted:
            if int(counted.get("folder_count", 0)) > limit:
                over_limit += 1
    return {
        "total": len(entries),
        "active": len(active),
        "inactive": len(entries) - len(active),
        "without_destination": sum(1 for entry in entries if not str(entry.get("destination") or "").strip()),
        "with_skiplist": sum(1 for entry in entries if entry.get("skiplist")),
        "with_own_recheck": sum(1 for entry in entries if entry.get("recheck_intervals")),
        "shared_destination": sum(1 for entry in entries if entry.get("shared_destination")),
        "over_limit": over_limit,
    }


def _period_rows(
    jobs: Sequence[tuple[str, str, str, float]],
    cycles: Sequence[float],
    release_first_job: Mapping[str, float],
    sizes: Mapping[str, Mapping[str, int]],
    now: float,
    history_start: Optional[float],
) -> list[dict[str, Any]]:
    """Build the per-period figures (24 h, 7/30/365 days, lifetime): jobs by outcome, polls, files and data."""
    rows = []
    history_days = 1.0 if history_start is None else max(1.0, (now - history_start) / DAY_SECONDS)
    for key, label, days in PERIODS:
        cutoff = None if days is None else now - days * DAY_SECONDS
        in_window = [job for job in jobs if cutoff is None or job[3] >= cutoff]
        by_status = Counter(job[2] for job in in_window)
        data = files = 0
        if cutoff is None:  # lifetime also counts releases whose jobs were purged from the history
            data = sum(size["bytes"] for size in sizes.values())
            files = sum(size["files"] for size in sizes.values())
        else:
            for release_key, started in release_first_job.items():
                size = sizes.get(release_key)
                if size and started >= cutoff:
                    data += size["bytes"]
                    files += size["files"]
        span = history_days if days is None else min(float(days), history_days)
        rows.append({
            "key": key,
            "label": label,
            "jobs": len(in_window),
            "completed": by_status["COMPLETED"],
            "failed": by_status["FAILED"],
            "superseded": by_status["SUPERSEDED"],
            "pending": by_status["PENDING"],
            "cycles": sum(1 for cycle in cycles if cutoff is None or cycle >= cutoff),
            "bytes": data,
            "files": files,
            "jobs_per_day": round(len(in_window) / span, 1),
        })
    return rows


def _daily(jobs: Sequence[tuple[str, str, str, float]], today: date) -> list[dict[str, Any]]:
    """Count jobs per day for the last DAILY_DAYS days (days without jobs count 0)."""
    per_day = Counter(_day(job[3]) for job in jobs)
    days = [today - timedelta(days=offset) for offset in range(DAILY_DAYS - 1, -1, -1)]
    return [{"date": day.isoformat(), "jobs": per_day.get(day, 0)} for day in days]


def _busiest_repositories(
    jobs: Sequence[tuple[str, str, str, float]],
    sizes_by_repo: Mapping[str, int],
    now: float,
    limit: int,
) -> list[dict[str, Any]]:
    """Rank repositories by number of jobs and return the top `limit` with their recent activity."""
    grouped: dict[str, dict[str, Any]] = {}
    month_ago = now - 30 * DAY_SECONDS
    for repo, _tag, status, created in jobs:
        entry = grouped.setdefault(repo.lower(), {
            "repo": repo, "jobs": 0, "completed": 0, "failed": 0, "jobs_30d": 0, "last_job": 0.0})
        entry["jobs"] += 1
        entry["completed"] += status == "COMPLETED"
        entry["failed"] += status == "FAILED"
        entry["jobs_30d"] += created >= month_ago
        entry["last_job"] = max(entry["last_job"], created)
    ranked = sorted(grouped.values(), key=lambda e: (-e["jobs"], -e["last_job"], e["repo"].lower()))[:limit]
    for entry in ranked:
        entry["bytes"] = sizes_by_repo.get(entry["repo"].lower(), 0)
        entry["last_job"] = _stamp(entry["last_job"])
    return ranked


def _biggest(
    sizes: Mapping[str, Mapping[str, int]], limit: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """(top repositories by total size, top single releases, per-repository byte totals by lower-cased name)."""
    per_repo: dict[str, dict[str, Any]] = {}
    for release_key, size in sizes.items():
        repo, tag = _split_release_key(release_key)
        entry = per_repo.setdefault(repo.lower(), {"repo": repo, "bytes": 0, "releases": 0, "files": 0, "largest": 0})
        entry["bytes"] += size["bytes"]
        entry["files"] += size["files"]
        entry["releases"] += 1
        entry["largest"] = max(entry["largest"], size["bytes"])
    repos = sorted(per_repo.values(), key=lambda e: (-e["bytes"], e["repo"].lower()))[:limit]
    for entry in repos:
        entry["average_release_bytes"] = entry["bytes"] // entry["releases"] if entry["releases"] else 0
    releases = []
    for release_key, size in sorted(sizes.items(), key=lambda item: (-item[1]["bytes"], item[0]))[:limit]:
        repo, tag = _split_release_key(release_key)
        releases.append({"repo": repo, "tag": tag, "bytes": size["bytes"], "files": size["files"]})
    return repos, releases, {name: entry["bytes"] for name, entry in per_repo.items()}


def build(
    entries: Sequence[Mapping[str, Any]],
    jobs: Sequence[tuple[str, str, str, float]],
    cycles: Sequence[float],
    sizes: Mapping[str, Mapping[str, int]],
    folder_counts: Mapping[str, Mapping[str, Any]],
    now: float,
    top: int = TOP_COUNT,
) -> dict[str, Any]:
    """Assemble the report from already loaded data (no file or database access; this is what the tests feed)."""
    first_job = jobs[0][3] if jobs else None
    last_job = jobs[-1][3] if jobs else None

    release_first_job: dict[str, float] = {}
    for repo, tag, _status, created in jobs:
        key = f"{repo}|{tag}"
        if key not in release_first_job or created < release_first_job[key]:
            release_first_job[key] = created

    biggest_repos, biggest_releases, bytes_by_repo = _biggest(sizes, top)
    per_day = Counter(_day(job[3]) for job in jobs)
    busiest_day = max(per_day.items(), key=lambda item: (item[1], item[0])) if per_day else None

    finished = sum(1 for job in jobs if job[2] in ("COMPLETED", "FAILED"))
    completed = sum(1 for job in jobs if job[2] == "COMPLETED")
    active_30d = {job[0].lower() for job in jobs if job[3] >= now - 30 * DAY_SECONDS}
    mapped = {str(entry.get("repository", "")).strip().lower() for entry in entries}

    mapping = mapping_stats(entries, folder_counts)
    mapping["quiet_30_days"] = sum(
        1 for entry in entries
        if entry.get("active", True) is not False and str(entry.get("repository", "")).strip().lower() not in active_30d
    )
    mapping["seen_in_jobs_not_mapped"] = len({job[0].lower() for job in jobs} - mapped)

    return {
        "generated_at": datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
        "version": __version__,
        "mapping": mapping,
        "history": {
            "first_job": _stamp(first_job),
            "last_job": _stamp(last_job),
            "days": None if first_job is None else max(1, int((now - first_job) // DAY_SECONDS) + 1),
            "jobs": len(jobs),
            "busiest_day": None if busiest_day is None else {"date": busiest_day[0].isoformat(), "jobs": busiest_day[1]},
        },
        "reliability": {
            "completed": completed,
            "failed": sum(1 for job in jobs if job[2] == "FAILED"),
            "superseded": sum(1 for job in jobs if job[2] == "SUPERSEDED"),
            "pending": sum(1 for job in jobs if job[2] == "PENDING"),
            "success_percent": round(100.0 * completed / finished, 1) if finished else None,
        },
        "periods": _period_rows(jobs, cycles, release_first_job, sizes, now, first_job),
        "daily": _daily(jobs, _day(now)),
        "busiest_repositories": _busiest_repositories(jobs, bytes_by_repo, now, top),
        "biggest_repositories": biggest_repos,
        "biggest_releases": biggest_releases,
        "tracked": {
            "bytes": sum(size["bytes"] for size in sizes.values()),
            "files": sum(size["files"] for size in sizes.values()),
            "releases": len(sizes),
        },
        "storage": {},
    }


def collect(now: Optional[float] = None, top: int = TOP_COUNT) -> dict[str, Any]:
    """Read mapping.json and state.db and build the report (nothing is written)."""
    now = time.time() if now is None else now
    entries = mapping_manager.load_mapping().get("repositories", [])
    with closing(db_manager.open_database()) as connection:
        report = build(
            entries,
            db_manager.get_job_history(connection),
            db_manager.get_cycle_times(connection),
            db_manager.get_release_sizes(connection),
            db_manager.get_folder_counts(connection),
            now,
            top,
        )
        storage = db_manager.get_storage_stats(connection)
    state_db = db_manager.get_state_db_path()
    oldest = [value for value in (storage["oldest_job_at"], storage["oldest_event_at"]) if value]
    report["storage"] = {
        "state_db_bytes": os.path.getsize(state_db) if os.path.exists(state_db) else None,
        "rows": storage["tables"],
        "oldest_record": _stamp(min(oldest)) if oldest else None,
    }
    return report


def sparkline(values: Sequence[int]) -> str:
    """One block per value, scaled to the largest (a flat row of the lowest block when everything is 0)."""
    peak = max(values, default=0)
    if peak <= 0:
        return SPARK_BLOCKS[0] * len(values)
    return "".join(SPARK_BLOCKS[min(len(SPARK_BLOCKS) - 1, int(value / peak * (len(SPARK_BLOCKS) - 1) + 0.5))] for value in values)


PERIOD_HEADERS = ("Period", "Jobs", "Done", "Failed", "Replaced", "Per day", "Polls", "Files", "Data")
BUSIEST_HEADERS = ("Repository", "Jobs", "30 days", "Failed", "Data", "Latest job")
BIGGEST_HEADERS = ("Repository", "Total", "Releases", "Files", "Average", "Largest")
RELEASE_HEADERS = ("Release", "Size", "Files")


def period_rows(report: Mapping[str, Any]) -> list[tuple[str, ...]]:
    """Turn the period figures into table rows of display strings (CLI and GUI share them)."""
    return [
        (
            row["label"], f"{row['jobs']:,}", f"{row['completed']:,}", f"{row['failed']:,}", f"{row['superseded']:,}",
            f"{row['jobs_per_day']:g}", f"{row['cycles']:,}", f"{row['files']:,}", format_bytes(row["bytes"]),
        )
        for row in report["periods"]
    ]


def busiest_rows(report: Mapping[str, Any]) -> list[tuple[str, ...]]:
    """Turn the busiest repositories into table rows of display strings."""
    return [
        (r["repo"], f"{r['jobs']:,}", f"{r['jobs_30d']:,}", f"{r['failed']:,}", format_bytes(r["bytes"]), r["last_job"])
        for r in report["busiest_repositories"]
    ]


def biggest_rows(report: Mapping[str, Any]) -> list[tuple[str, ...]]:
    """Turn the biggest repositories into table rows of display strings."""
    return [
        (r["repo"], format_bytes(r["bytes"]), f"{r['releases']:,}", f"{r['files']:,}",
         format_bytes(r["average_release_bytes"]), format_bytes(r["largest"]))
        for r in report["biggest_repositories"]
    ]


def release_rows(report: Mapping[str, Any]) -> list[tuple[str, ...]]:
    """Turn the biggest releases into table rows of display strings."""
    return [
        (f"{r['repo']} {r['tag']}", format_bytes(r["bytes"]), f"{r['files']:,}") for r in report["biggest_releases"]
    ]


def summary_lines(report: Mapping[str, Any]) -> list[str]:
    """The Repositories and History paragraphs, ready to print (headings included, blank line between)."""
    mapping, history, reliability = report["mapping"], report["history"], report["reliability"]
    lines = ["Repositories"]
    lines.append(
        f"  {mapping['total']} mapped: {mapping['active']} active, {mapping['inactive']} inactive"
        + (f", {mapping['without_destination']} without a destination" if mapping["without_destination"] else "")
    )
    details = [
        f"{mapping['quiet_30_days']} active ones without a job in 30 days",
        f"{mapping['with_skiplist']} with a skiplist",
        f"{mapping['with_own_recheck']} with their own recheck intervals",
        f"{mapping['shared_destination']} sharing a destination",
    ]
    if mapping["over_limit"]:
        details.append(f"{mapping['over_limit']} over their folder limit")
    lines.append("  " + "; ".join(details))
    if mapping["seen_in_jobs_not_mapped"]:
        lines.append(f"  {mapping['seen_in_jobs_not_mapped']} repositories in the job history are no longer in mapping.json")

    lines += ["", "History"]
    if history["first_job"] is None:
        lines.append("  No jobs recorded yet.")
    else:
        lines.append(f"  Jobs recorded since {history['first_job']} ({history['days']} days); latest {history['last_job']}")
        busiest = history["busiest_day"]
        lines.append(f"  Busiest day: {busiest['date']} with {busiest['jobs']} jobs")
        success = "n/a" if reliability["success_percent"] is None else f"{reliability['success_percent']}%"
        lines.append(
            f"  {reliability['completed']:,} completed, {reliability['failed']:,} failed ({success} of the finished ones "
            f"succeeded), {reliability['superseded']:,} superseded by a newer release, {reliability['pending']:,} waiting"
        )
    tracked = report["tracked"]
    lines.append(
        f"  Kept on record: {tracked['releases']:,} releases, {tracked['files']:,} files, {format_bytes(tracked['bytes'])}"
    )
    return lines


def storage_lines(report: Mapping[str, Any]) -> list[str]:
    """Return the text lines about state.db's size and row counts (empty when unknown)."""
    storage = report["storage"]
    if not storage:
        return []
    rows = ", ".join(f"{name} {count:,}" for name, count in storage["rows"].items())
    return [
        f"state.db {format_bytes(storage['state_db_bytes'])}; oldest record {storage['oldest_record'] or 'n/a'}",
        f"rows: {rows}",
    ]


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]], left: int = 1) -> list[str]:
    """Aligned text table; the first `left` columns are left aligned, the rest right aligned."""
    widths = [max(len(str(cell)) for cell in column) for column in zip(headers, *rows)] if rows else [len(h) for h in headers]

    def line(cells: Sequence[str]) -> str:
        """Format one table row: first column(s) left-aligned, the rest right-aligned."""
        return "  " + "  ".join(
            str(cell).ljust(width) if index < left else str(cell).rjust(width)
            for index, (cell, width) in enumerate(zip(cells, widths))
        ).rstrip()

    return [line(headers), "  " + "  ".join("-" * width for width in widths), *[line(row) for row in rows]]


def format_report(report: Mapping[str, Any]) -> str:
    """Human-readable text version of collect()."""
    lines = [f"GHAADD {report['version']} statistics ({report['generated_at']})", "", *summary_lines(report)]

    lines += ["", "Activity", *_table(PERIOD_HEADERS, period_rows(report))]
    daily = report["daily"]
    lines += ["", f"Jobs per day, last {len(daily)} days (oldest first, busiest day = {max((d['jobs'] for d in daily), default=0)})",
              "  " + sparkline([day["jobs"] for day in daily])]

    busiest, biggest, releases = busiest_rows(report), biggest_rows(report), release_rows(report)
    lines += ["", f"Top {len(busiest)} busiest repositories (most jobs)", *_table(BUSIEST_HEADERS, busiest)]
    lines += ["", f"Top {len(biggest)} biggest repositories (total size of the releases on record)",
              *_table(BIGGEST_HEADERS, biggest)]
    lines += ["", f"Top {len(releases)} biggest single releases", *_table(RELEASE_HEADERS, releases)]

    storage = storage_lines(report)
    if storage:
        lines += ["", "State database", *["  " + line for line in storage]]
    return "\n".join(lines)
