"""Toolkit-independent rows for the GUI's Mappings table.

Combines mapping.json entries with a per-repo summary of job_queue (see
db_manager.get_repo_job_summaries) into display rows, derives each repo's
status and orders the list most-recently-worked-on first. Pure functions: the
caller supplies the data and the current time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Optional

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


def format_timestamp(epoch: Optional[float]) -> str:
    """Local "YYYY-MM-DD HH:MM", or "-" when unset."""
    if not epoch:
        return NO_VALUE
    return datetime.fromtimestamp(float(epoch)).strftime("%Y-%m-%d %H:%M")


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


def derive_status(paused: bool, summary: Optional[Mapping[str, Any]], now: float) -> str:
    """Paused wins; else Queued/Waiting from a pending job, Failed from the latest job, else Idle."""
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
) -> list[RepoRow]:
    """Return table rows, most recently worked-on first, then by folder name.

    `summaries` is keyed by lower-cased owner/repo. `intervals_for(entry)` returns
    how many recheck steps the repo has (own list or the global default).
    """
    rows: list[RepoRow] = []
    for entry in mapping_entries:
        repo = str(entry.get("name") or "").strip()
        if not repo:
            continue
        summary = summaries.get(repo.lower())
        paused = entry.get("paused") is True
        status = derive_status(paused, summary, now)

        pending_check = summary.get("pending_next_check") if summary else None
        if summary and pending_check is not None:
            step = f"{int(summary.get('pending_attempts') or 0)} / {intervals_for(entry)}"
        else:
            step = NO_VALUE

        files = format_files(summary)

        foldername = str(entry.get("foldername") or "").strip() or repo
        rows.append(
            RepoRow(
                repo=repo,
                foldername=foldername,
                destination=str(entry.get("destination") or ""),
                status=status,
                tag=str(summary.get("latest_tag") or NO_VALUE) if summary else NO_VALUE,
                last_check=format_timestamp(summary.get("latest_updated_at") if summary else None),
                step=step,
                next_check=format_timestamp(pending_check),
                files=files,
                limit=str(entry.get("limit", "")),
                last_activity=float(summary.get("last_activity") or 0) if summary else 0.0,
            )
        )

    rows.sort(key=lambda row: row.foldername.casefold())
    rows.sort(key=lambda row: row.last_activity, reverse=True)  # stable: ties stay by name
    return rows
