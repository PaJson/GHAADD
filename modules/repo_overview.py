"""Toolkit-independent rows for the GUI's Mappings table.

Combines mapping.json entries with a per-repo summary of job_queue (see
db_manager.get_repo_job_summaries) into display rows, derives each repo's
status and orders the list most-recently-worked-on first. Pure functions: the
caller supplies the data and the current time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Collection, Mapping, Optional

from modules import gui_tooltips

STATUS_QUEUED = "Queued"
STATUS_WAITING = "Waiting"
STATUS_IDLE = "Idle"
STATUS_PAUSED = "Paused"
STATUS_FAILED = "Failed"
# "Running" needs a signal that is not in job_queue (the daemon does not record
# the job in progress); see ToDo step 3 (daemon status publishes the current job).
STATUS_RUNNING = "Running"

NO_VALUE = "-"


@dataclass(frozen=True)
class RepoRow:
    repo: str  # owner/repo, also the row id
    foldername: str
    destination: str
    status: str
    tag: str
    last_check: str
    step: str
    next_check: str
    files: str  # files held for the latest release: downloaded + already-present; "n / total" while incomplete
    limit: str
    last_activity: float  # epoch seconds; 0 = never worked on
    limit_warning: bool = False  # over its limit, or a folder-limit warning is on record
    limit_note: str = ""  # hover text for the Limit cell ("12 of 15 allowed folders, counted ...")


def release_marker(release_type: Any) -> str:
    """"(P)" for a Pre-release, "(R)" for a Release, "" when the type is unknown."""
    text = str(release_type or "").strip().lower().replace("_", "-")
    if text in ("pre-release", "prerelease"):
        return "(P)"
    if text == "release":
        return "(R)"
    return ""


def format_tag(tag: Any, release_type: Any) -> str:
    """The tag with its release marker in front, e.g. "(P) nightly"."""
    marker = release_marker(release_type)
    return f"{marker} {tag}" if marker and tag else str(tag or "")


def format_timestamp(epoch: Optional[float]) -> str:
    """Local "YYYY-MM-DD HH:MM:SS", or "-" when unset."""
    if not epoch:
        return NO_VALUE
    return datetime.fromtimestamp(float(epoch)).strftime("%Y-%m-%d %H:%M:%S")


def format_limit(limit: Any, counted: Optional[Mapping[str, Any]]) -> tuple[str, bool, str]:
    """(cell text, over the limit, hover note): "12 / 15" once the daemon has counted the folders, else just "15"."""
    text = "" if limit is None else str(limit)
    try:
        allowed = int(limit)
    except (TypeError, ValueError):
        return text, False, ""
    if allowed <= 0 or not counted:
        return text, False, ""
    count = int(counted.get("folder_count") or 0)
    when = format_timestamp(counted.get("counted_at"))
    return f"{count} / {allowed}", count > allowed, gui_tooltips.LIMIT_NOTE.format(count=count, allowed=allowed, when=when)


def format_files(summary: Optional[Mapping[str, Any]]) -> str:
    """Files held for the latest release, e.g. "17", or "8 / 16" while some are still missing.

    "Skipped" in job_queue means the file is already on disk and matches, so
    downloaded + skipped is stable across rechecks (a recheck that downloads
    nothing still shows 17).
    """
    if not summary or summary.get("latest_tag") is None:
        return NO_VALUE
    total = int(summary.get("latest_total") or 0)
    if total <= 0:
        return NO_VALUE
    accounted = int(summary.get("latest_downloaded") or 0) + int(summary.get("latest_skipped") or 0)
    return str(accounted) if accounted >= total else f"{accounted} / {total}"


def derive_status(
    paused: bool, summary: Optional[Mapping[str, Any]], now: float, running: bool = False
) -> str:
    """Running (job in progress) wins, then Paused; else Queued/Waiting from a pending job,
    Failed from the latest job, else Idle."""
    if running:
        return STATUS_RUNNING
    if paused:
        return STATUS_PAUSED
    if summary:
        if summary.get("pending_next_check") is not None:
            return STATUS_QUEUED if float(summary["pending_next_check"]) <= now else STATUS_WAITING
        if summary.get("latest_status") == "FAILED":
            return STATUS_FAILED
    return STATUS_IDLE


def build_rows(
    mapping_entries: list[Mapping[str, Any]],
    summaries: Mapping[str, Mapping[str, Any]],
    now: float,
    intervals_for: Callable[[Mapping[str, Any]], int],
    running_repo: Optional[str] = None,
    limit_warned: Collection[str] = frozenset(),
    folder_counts: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> list[RepoRow]:
    """Return table rows, most recently worked-on first, then by folder name.

    `summaries` is keyed by lower-cased owner/repo. `intervals_for(entry)` returns
    how many recheck steps the repo has (own list or the global default).
    `running_repo` is the owner/repo the daemon is processing right now, if any.
    `limit_warned` holds the lower-cased names that have a folder-limit warning on record; `folder_counts`
    the latest counted folders per lower-cased repo (the Limit column then reads "12 / 15").
    """
    running_key = running_repo.lower() if running_repo else None
    rows: list[RepoRow] = []
    for entry in mapping_entries:
        repo = str(entry.get("name") or "").strip()
        if not repo:
            continue
        summary = summaries.get(repo.lower())
        paused = entry.get("paused") is True
        status = derive_status(paused, summary, now, running=repo.lower() == running_key)

        pending_check = summary.get("pending_next_check") if summary else None
        if summary and pending_check is not None:
            step = f"{int(summary.get('pending_attempts') or 0)} / {intervals_for(entry)}"
        else:
            step = NO_VALUE

        files = format_files(summary)

        foldername = str(entry.get("foldername") or "").strip() or repo
        limit_text, over, limit_note = format_limit(entry.get("limit"), (folder_counts or {}).get(repo.lower()))
        rows.append(
            RepoRow(
                repo=repo,
                foldername=foldername,
                destination=str(entry.get("destination") or ""),
                status=status,
                tag=format_tag(summary.get("latest_tag"), summary.get("latest_release_type")) or NO_VALUE
                if summary else NO_VALUE,
                last_check=format_timestamp(summary.get("latest_updated_at") if summary else None),
                step=step,
                next_check=format_timestamp(pending_check),
                files=files,
                limit=limit_text,
                last_activity=float(summary.get("last_activity") or 0) if summary else 0.0,
                limit_warning=over or repo.lower() in limit_warned,
                limit_note=limit_note,
            )
        )

    rows.sort(key=lambda row: row.foldername.casefold())
    rows.sort(key=lambda row: row.last_activity, reverse=True)  # stable: ties stay by name
    return rows
