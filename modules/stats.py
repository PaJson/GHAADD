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
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Optional, Sequence

from modules import db_manager, mapping_manager
from modules.app_info import __version__

TOP_COUNT = 10
# What the Stats window's "Show" drop-downs offer: (label, rows); None = every row.
TOP_CHOICES: tuple[tuple[str, Optional[int]], ...] = (("Top 10", 10), ("Top 25", 25), ("Top 50", 50), ("All", None))
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

# What the Stats window's "Period" drop-down offers: (label, last N days including today); None = everything.
PRESETS: tuple[tuple[str, Optional[int]], ...] = (
    ("All time", None), ("Today", 1), ("Last 7 days", 7), ("Last 30 days", 30), ("Last 365 days", 365),
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


MEASURED_MARK = "\x00"  # separates the tag from a job number in the key of a measured job (see build())


def _split_release_key(release_key: str) -> tuple[str, str]:
    """Split a "repo|tag" release key into (repo, tag); the job number of a measured job is dropped."""
    repo, _, tag = release_key.partition("|")
    return repo, tag.partition(MEASURED_MARK)[0]


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


@dataclass(frozen=True)
class DateRange:
    """A chosen window [start, end) in epoch seconds; None = open on that side. `label` names it in the tables."""

    start: Optional[float]
    end: Optional[float]
    label: str


def parse_date(text: str) -> Optional[date]:
    """Return the date of a YYYY-MM-DD text (None for empty text); raise ValueError with a plain message otherwise."""
    text = text.strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"'{text}' is not a date: write it as YYYY-MM-DD, for example 2026-09-30.") from None


def date_range(first: Optional[date], last: Optional[date]) -> Optional[DateRange]:
    """Build the window from the first and last day (both inclusive, either may be None); None when both are."""
    if first is None and last is None:
        return None
    if first is not None and last is not None and first > last:
        raise ValueError("The first date is after the last date.")
    start = None if first is None else datetime.combine(first, datetime.min.time()).timestamp()
    end = None if last is None else datetime.combine(last + timedelta(days=1), datetime.min.time()).timestamp()
    label = f"{first.isoformat() if first else 'the beginning'} to {last.isoformat() if last else 'now'}"
    return DateRange(start, end, label)


def preset_range(days: Optional[int], today: date) -> Optional[DateRange]:
    """The window for "last N days" including today (None = no window = everything)."""
    if days is None:
        return None
    return date_range(today - timedelta(days=days - 1), today)


def _period_rows(
    jobs: Sequence[tuple[str, str, str, float]],
    cycles: Sequence[float],
    release_first_job: Mapping[str, float],
    sizes: Mapping[str, Mapping[str, int]],
    now: float,
    history_start: Optional[float],
    date_range: Optional["DateRange"] = None,
) -> list[dict[str, Any]]:
    """Build the per-period figures (24 h, 7/30/365 days, lifetime, then the chosen range): jobs, polls, files, data."""
    history_days = 1.0 if history_start is None else max(1.0, (now - history_start) / DAY_SECONDS)

    def row(key: str, label: str, start: Optional[float], end: Optional[float], span: float) -> dict[str, Any]:
        """One table row for the window [start, end) (None = open); `span` is its length in days."""
        def inside(moment: float) -> bool:
            """True when a timestamp falls in the window."""
            return (start is None or moment >= start) and (end is None or moment < end)

        in_window = [job for job in jobs if inside(job[3])]
        by_status = Counter(job[2] for job in in_window)
        data = files = 0
        if start is None and end is None:  # lifetime also counts releases whose jobs were purged from the history
            data = sum(size["bytes"] for size in sizes.values())
            files = sum(size["files"] for size in sizes.values())
        else:
            for release_key, started in release_first_job.items():
                size = sizes.get(release_key)
                if size and inside(started):
                    data += size["bytes"]
                    files += size["files"]
        return {
            "key": key,
            "label": label,
            "jobs": len(in_window),
            "completed": by_status["COMPLETED"],
            "failed": by_status["FAILED"],
            "superseded": by_status["SUPERSEDED"],
            "pending": by_status["PENDING"],
            "cycles": sum(1 for cycle in cycles if inside(cycle)),
            "bytes": data,
            "files": files,
            "jobs_per_day": round(len(in_window) / span, 1),
        }

    rows = []
    for key, label, days in PERIODS:
        span = history_days if days is None else min(float(days), history_days)
        rows.append(row(key, label, None if days is None else now - days * DAY_SECONDS, None, span))
    if date_range is not None:
        first = date_range.start if date_range.start is not None else (history_start or now)
        last = min(date_range.end, now) if date_range.end is not None else now
        rows.append(row("range", date_range.label, date_range.start, date_range.end, max(1.0, (last - first) / DAY_SECONDS)))
    return rows


def _daily(
    jobs: Sequence[tuple[str, str, str, float]], first_day: date, last_day: date
) -> list[dict[str, Any]]:
    """Count jobs per day from first_day to last_day inclusive (days without jobs count 0)."""
    per_day = Counter(_day(job[3]) for job in jobs)
    count = max((last_day - first_day).days + 1, 0)
    days = [first_day + timedelta(days=offset) for offset in range(count)]
    return [{"date": day.isoformat(), "jobs": per_day.get(day, 0)} for day in days]


def _busiest_repositories(
    jobs: Sequence[tuple[str, str, str, float]],
    sizes_by_repo: Mapping[str, int],
    now: float,
    limit: Optional[int],
) -> list[dict[str, Any]]:
    """Rank repositories by number of jobs and return the top `limit` (None = all) with their recent activity."""
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
    sizes: Mapping[str, Mapping[str, int]], limit: Optional[int]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """(top repositories by total size, top single releases, per-repository byte totals by lower-cased name).

    `limit` None = every repository and release; the byte totals always cover all of them.
    """
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


def limited(report: Mapping[str, Any], busiest: Optional[int] = TOP_COUNT, biggest: Optional[int] = TOP_COUNT) -> dict[str, Any]:
    """Return a copy of a report whose ranked lists are cut to the given sizes (None = keep all).

    The report must have been built with top=None to hold more than it already shows; the GUI collects everything once
    and re-cuts it here when the "Show" drop-down changes, so no new query is needed.
    """
    copy = dict(report)
    copy["busiest_repositories"] = list(report["busiest_repositories"])[:busiest]
    copy["biggest_repositories"] = list(report["biggest_repositories"])[:biggest]
    copy["biggest_releases"] = list(report["biggest_releases"])[:biggest]
    return copy


def daily_title(report: Mapping[str, Any], unit: str = "day") -> str:
    """Heading of the jobs series ("last 30 days", or the chosen window); `unit` is what one bar stands for."""
    count = len(report["daily"])
    if report.get("range"):
        return f"Jobs per {unit}, {report['range']}, {count} days"
    return f"Jobs per {unit}, last {count} days"


def bucket_daily(daily: Sequence[Mapping[str, Any]], max_bars: int) -> tuple[str, list[dict[str, Any]]]:
    """Combine the daily series into weeks, then months, until it has at most `max_bars` bars.

    Returns ("day" | "week" | "month", buckets); a bucket is {"date": its first day, "jobs": the sum, "days": count}.
    Weeks start on Monday; a week or month cut off by the range counts only the days that are in it.
    """
    days = [{"date": str(day["date"]), "jobs": int(day["jobs"]), "days": 1} for day in daily]
    if len(days) <= max_bars:
        return "day", days
    for unit in ("week", "month"):
        groups: dict[str, dict[str, Any]] = {}
        for day in days:
            moment = date.fromisoformat(day["date"])
            key = str(moment - timedelta(days=moment.weekday())) if unit == "week" else day["date"][:7]
            group = groups.setdefault(key, {"date": day["date"], "jobs": 0, "days": 0})
            group["jobs"] += day["jobs"]
            group["days"] += 1
        if len(groups) <= max_bars or unit == "month":
            return unit, list(groups.values())
    return "day", days  # not reached


def list_titles(report: Mapping[str, Any]) -> tuple[str, str, str]:
    """Headings of the busiest / biggest repositories and biggest releases lists ("Top 10 ..." or "All 143 ...")."""
    totals = report["totals"]

    def heading(shown: int, total: int, what: str) -> str:
        """Say "All N" when more than the default top is listed in full, else "Top N"."""
        return f"{'All' if shown == total and total > TOP_COUNT else 'Top'} {shown} {what}"

    return (
        heading(len(report["busiest_repositories"]), totals["busiest_repositories"], "busiest repositories (most jobs)"),
        heading(len(report["biggest_repositories"]), totals["biggest_repositories"],
                "biggest repositories (total size of the releases on record)"),
        heading(len(report["biggest_releases"]), totals["biggest_releases"], "biggest single releases"),
    )


def build(
    entries: Sequence[Mapping[str, Any]],
    jobs: Sequence[tuple[str, str, str, float]],
    cycles: Sequence[float],
    sizes: Mapping[str, Mapping[str, int]],
    folder_counts: Mapping[str, Mapping[str, Any]],
    now: float,
    top: Optional[int] = TOP_COUNT,
    date_range: Optional[DateRange] = None,
    job_sizes: Sequence[tuple[str, str, float, int, int]] = (),
) -> dict[str, Any]:
    """Assemble the report from already loaded data (no file or database access; this is what the tests feed).

    With a `date_range` the Busiest, Biggest and Per day figures cover only that window and the Activity table gets
    one more row for it. Sizes come from `job_sizes` (repo, tag, created_at, bytes, files: the finished folder of each
    job, dated by the job); a release without any falls back to asset_state, dated by its first job (or its newest
    file for a reused tag), so releases whose jobs were purged drop out. Mapping, history and reliability always
    describe everything on record.
    """
    first_job = jobs[0][3] if jobs else None
    last_job = jobs[-1][3] if jobs else None

    release_first_job: dict[str, float] = {}
    for repo, tag, _status, created in jobs:
        key = f"{repo}|{tag}"
        if key not in release_first_job or created < release_first_job[key]:
            release_first_job[key] = created
    # A rolling tag ("latest", "continuous", "nightly") is reused by hundreds of jobs while asset_state holds only its
    # newest build: dating that by the first job ever would put today's download weeks back. So a release counts for
    # the day of its newest file when that is later than its first job (ordinary releases keep the first job's day).
    for key in release_first_job:
        size = sizes.get(key)
        if size:
            release_first_job[key] = max(release_first_job[key], float(size.get("newest", 0)))

    # A release with measured jobs is counted from those (each finished job's folder, dated by the job); only the
    # releases without any use the asset_state estimate above. A measured job gets its own unique key (MEASURED_MARK).
    measured = {f"{repo}|{tag}" for repo, tag, _created, _bytes, _files in job_sizes}
    dated_sizes: dict[str, Mapping[str, int]] = {key: size for key, size in sizes.items() if key not in measured}
    release_first_job = {key: moment for key, moment in release_first_job.items() if key not in measured}
    for number, (repo, tag, created, folder_bytes, folder_files) in enumerate(job_sizes):
        key = f"{repo}|{tag}{MEASURED_MARK}{number}"
        dated_sizes[key] = {"bytes": folder_bytes, "files": folder_files}
        release_first_job[key] = created

    def inside(moment: float) -> bool:
        """True when a timestamp is in the chosen window (always, without one)."""
        return date_range is None or (
            (date_range.start is None or moment >= date_range.start) and (date_range.end is None or moment < date_range.end)
        )

    ranked_jobs = [job for job in jobs if inside(job[3])]
    ranked_sizes = dated_sizes if date_range is None else {
        key: size for key, size in dated_sizes.items() if key in release_first_job and inside(release_first_job[key])
    }
    biggest_repos, biggest_releases, bytes_by_repo = _biggest(ranked_sizes, top)
    busiest_repositories = _busiest_repositories(ranked_jobs, bytes_by_repo, now, top)
    totals = {
        "busiest_repositories": len({job[0].lower() for job in ranked_jobs}),
        "biggest_repositories": len(bytes_by_repo),
        "biggest_releases": len(ranked_sizes),
    }
    today = _day(now)
    if date_range is None:
        daily = _daily(jobs, today - timedelta(days=DAILY_DAYS - 1), today)
    else:
        first_day = _day(date_range.start) if date_range.start is not None else (_day(first_job) if first_job else today)
        last_day = min(_day(date_range.end - 1), today) if date_range.end is not None else today
        daily = _daily(jobs, first_day, last_day)
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
        "periods": _period_rows(jobs, cycles, release_first_job, dated_sizes, now, first_job, date_range),
        "range": None if date_range is None else date_range.label,
        "daily": daily,
        "busiest_repositories": busiest_repositories,
        "biggest_repositories": biggest_repos,
        "biggest_releases": biggest_releases,
        "totals": totals,
        "tracked": {
            "bytes": sum(size["bytes"] for size in sizes.values()),
            "files": sum(size["files"] for size in sizes.values()),
            "releases": len(sizes),
        },
        "storage": {},
    }


def load() -> dict[str, Any]:
    """Read mapping.json and state.db into plain data (nothing is written); report_from() turns it into a report."""
    entries = mapping_manager.load_mapping().get("repositories", [])
    with closing(db_manager.open_database()) as connection:
        data = {
            "entries": entries,
            "jobs": db_manager.get_job_history(connection),
            "cycles": db_manager.get_cycle_times(connection),
            "sizes": db_manager.get_release_sizes(connection),
            "job_sizes": db_manager.get_job_folder_sizes(connection),
            "folder_counts": db_manager.get_folder_counts(connection),
        }
        storage = db_manager.get_storage_stats(connection)
    state_db = db_manager.get_state_db_path()
    oldest = [value for value in (storage["oldest_job_at"], storage["oldest_event_at"]) if value]
    data["storage"] = {
        "state_db_bytes": os.path.getsize(state_db) if os.path.exists(state_db) else None,
        "rows": storage["tables"],
        "oldest_record": _stamp(min(oldest)) if oldest else None,
    }
    return data


def report_from(
    data: Mapping[str, Any],
    now: Optional[float] = None,
    top: Optional[int] = TOP_COUNT,
    date_range: Optional[DateRange] = None,
) -> dict[str, Any]:
    """Build the report from load()'s data; the GUI calls this again for every new window or "now"."""
    report = build(
        data["entries"], data["jobs"], data["cycles"], data["sizes"], data["folder_counts"],
        time.time() if now is None else now, top, date_range, data.get("job_sizes", ()),
    )
    report["storage"] = data["storage"]
    return report


def collect(now: Optional[float] = None, top: Optional[int] = TOP_COUNT, date_range: Optional[DateRange] = None) -> dict[str, Any]:
    """Read mapping.json and state.db and build the report (nothing is written)."""
    return report_from(load(), now, top, date_range)


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
    lines += ["", f"{daily_title(report)} (oldest first, busiest day = {max((d['jobs'] for d in daily), default=0)})",
              "  " + sparkline([day["jobs"] for day in daily])]

    busiest, biggest, releases = busiest_rows(report), biggest_rows(report), release_rows(report)
    busiest_title, biggest_title, releases_title = list_titles(report)
    lines += ["", busiest_title, *_table(BUSIEST_HEADERS, busiest)]
    lines += ["", biggest_title, *_table(BIGGEST_HEADERS, biggest)]
    lines += ["", releases_title, *_table(RELEASE_HEADERS, releases)]

    storage = storage_lines(report)
    if storage:
        lines += ["", "State database", *["  " + line for line in storage]]
    return "\n".join(lines)
