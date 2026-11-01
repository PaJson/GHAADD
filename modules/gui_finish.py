"""Logic of the Mappings tab's "Finish now" button: skip the waiting re-checks of one pending job.

Toolkit-independent: the dialog in main_gui.py only shows the choices and calls `finish`. The job is not completed
here; it is put on its last re-check step and made due, and the daemon's next poll does the real final check and
finalizes it (see db_manager.finish_job_now).
"""

from __future__ import annotations

import time
from typing import Any, NamedTuple, Optional, Sequence

from modules import db_manager, mapping_manager, repo_overview


class PendingChoice(NamedTuple):
    """One pending job the user can pick: its id and the line shown for it."""
    job_id: int
    label: str


def choices_from_rows(rows: Sequence[Any], steps: int, now: float) -> list[PendingChoice]:
    """Build the picker lines from pending job rows (kept in the order given: the newest job first)."""
    choices = []
    for row in rows:
        due = float(row["next_check_time"] or 0)
        when = "due now" if due <= now else repo_overview.format_timestamp(due)
        label = (
            f"{repo_overview.format_tag(row['tag'], row['release_type']) or repo_overview.NO_VALUE}"
            f"   -   step {int(row['attempt_count'] or 0)} / {steps}   -   next check {when}"
        )
        choices.append(PendingChoice(int(row["id"]), label))
    return choices


def load_choices(repo: str, now: Optional[float] = None) -> list[PendingChoice]:
    """Read the repository's pending jobs from state.db (own short-lived connection); newest first."""
    connection = db_manager.open_database()
    try:
        rows = db_manager.get_pending_jobs_for_repo(connection, repo)
    finally:
        connection.close()
    steps = len(mapping_manager.get_repository_recheck_intervals_minutes(repo))
    return choices_from_rows(rows, steps, time.time() if now is None else now)


def finish(repo: str, job_id: int) -> bool:
    """Put the job on its last re-check step and make it due now; False when it is not pending any more."""
    steps = len(mapping_manager.get_repository_recheck_intervals_minutes(repo))
    connection = db_manager.open_database()
    try:
        return db_manager.finish_job_now(connection, job_id, steps)
    finally:
        connection.close()


def blocked_reason(busy_repo: Optional[str], job_running: bool) -> str:
    """Return why the button must wait ("" when it may run): the daemon is in the middle of a poll cycle.

    The worker keeps the jobs of a cycle in memory and writes them back with the attempt number it read, so a change
    made during a cycle could be overwritten.
    """
    if not job_running:
        return ""
    where = f" with {busy_repo}" if busy_repo else ""
    return f"The daemon is busy{where}: try again when the job has finished."


def intro_text(repo: str, count: int) -> str:
    """Return the sentence above the list of jobs."""
    which = "this job" if count == 1 else "one of these jobs"
    return (
        f"Pick {which} of {repo} to finish now.\n\n"
        "The waiting re-checks are skipped: the daemon makes one more check at once (it downloads whatever is still "
        "missing) and then finalizes the release. A file that GitHub has not published yet will then be missing."
    )


def result_text(repo: str, daemon_running: bool, poll_error: Optional[str]) -> str:
    """Return the footer message after the job was set up to finish."""
    if not daemon_running:
        return f"{repo}: the job will finish when the daemon next runs (no daemon is running now)."
    if poll_error:
        return f"{repo}: the job is set to finish at the next poll, but Poll now failed: {poll_error}"
    return f"{repo}: finishing now (a poll was requested)."
