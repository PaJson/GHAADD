"""Fetch one release by its GitHub URL: queue a job for it, adding the repository when it is not mapped yet.

Toolkit-independent; used by the GUI's "Fetch…" button and the `--fetch-release` option. It acts like a notification
that arrived by hand, with these differences: the Skiplist and the Active flag are ignored (the user asked for this
release), and by default the job skips the waiting re-checks (a release picked by hand is normally finished already).
"""

from __future__ import annotations

import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import dataclass
from typing import Any, NamedTuple, Optional
from urllib.parse import unquote, urlsplit

from modules import asset_downloader, daemon_lock, db_manager, mapping_manager

_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
_HOSTS = ("github.com", "www.github.com")


class ReleaseUrlError(ValueError):
    """The text is not the link of one GitHub release; the message says what to paste instead."""


class ReleaseFetchError(RuntimeError):
    """The release could not be queued (not found on GitHub, no credentials, ...); the message is readable as is."""


class ReleaseRef(NamedTuple):
    """The repository (owner/repo) and tag a release link points at."""
    repo: str
    tag: str


@dataclass(frozen=True)
class FetchOutcome:
    """What a fetch did: the release, its type, the new job (None in a dry run) and whether the repo was added."""
    repo: str
    tag: str
    release_type: str
    job_id: Optional[int]
    mapping_created: bool
    finish_early: bool


def _valid_name(text: str) -> bool:
    """Return True for a GitHub owner or repository name (letters, digits, dash, underscore, dot)."""
    return bool(text) and set(text) <= _NAME_CHARS


def parse_release_url(text: str) -> ReleaseRef:
    """Return the repository and tag of a release link such as github.com/owner/repo/releases/tag/v1.0.

    Only a link to ONE release is accepted: the releases list, "latest" and a bare repository are refused, so a
    paste can never mean "download everything". A tag may contain slashes and percent-encoded characters.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        raise ReleaseUrlError("Paste the link of a release, for example https://github.com/owner/repo/releases/tag/v1.0")
    if "://" not in cleaned:
        cleaned = "https://" + cleaned
    parts = urlsplit(cleaned)
    if parts.scheme not in ("http", "https") or (parts.hostname or "").lower() not in _HOSTS:
        raise ReleaseUrlError("That is not a github.com link.")
    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) < 2 or not (_valid_name(segments[0]) and _valid_name(segments[1])):
        raise ReleaseUrlError("The link must start with github.com/owner/repo.")
    if len(segments) < 4 or segments[2] != "releases" or segments[3] != "tag":
        raise ReleaseUrlError(
            "That link does not point at one release. Open the release on GitHub and copy its link; it ends in "
            "/releases/tag/<tag>. (The releases list and \"latest\" are not accepted.)"
        )
    tag = unquote("/".join(segments[4:])).strip()
    if not tag:
        raise ReleaseUrlError("The link has no tag after /releases/tag/.")
    return ReleaseRef(f"{segments[0]}/{segments[1]}", tag)


def _canonical_repo(data: Any, fallback: str) -> str:
    """Return the repository name with GitHub's own capitalisation (from the release's page link)."""
    try:
        return parse_release_url(str(data.get("html_url") or "")).repo
    except ReleaseUrlError:
        return fallback


def _make_output_safe() -> None:
    """Make console output unable to raise: the queue code prints emoji, a cp1252 console cannot show them.

    The daemon forces UTF-8 at start; the GUI does not, and an error AFTER the job row was written would report a
    failure for a release that is in fact queued. Characters that do not fit become "?".
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (OSError, ValueError):
                pass


def busy_reason() -> str:
    """Return why a fetch must wait ("" when it may run): the daemon is in the middle of a poll cycle.

    Queueing replaces an identical pending job, and the worker keeps a cycle's jobs in memory and writes them back,
    so a change made during a cycle could be overwritten.
    """
    status = daemon_lock.get_daemon_status()
    if not status["running"] or not (status["queue_progress"] or status["current_job"]):
        return ""
    job = status["queue_progress"] or status["current_job"] or {}
    where = f" with {job['repo']}" if job.get("repo") else ""
    return f"The daemon is busy{where}: try again when the job has finished."


def fetch_release(
    url: str,
    finish_early: bool = True,
    connection: Optional[sqlite3.Connection] = None,
    now: Optional[float] = None,
) -> FetchOutcome:
    """Queue the release behind `url`; raise ReleaseUrlError or ReleaseFetchError with a readable message.

    The release must exist on GitHub (a draft or a bare git tag does not). Its type (Release/Pre-release) and commit
    come from GitHub, like the daemon does for a notification. An unmapped repository is added without a destination
    (the Unmapped tab and an UNMAPPED warning ask for one afterwards). `finish_early` puts the job on its last re-check
    step, so the next poll makes one final check and finalizes it (see db_manager.finish_job_now).
    """
    ref = parse_release_url(url)
    _make_output_safe()
    reason = busy_reason()
    if reason:
        raise ReleaseFetchError(reason)
    try:
        data = asset_downloader.get_release_data(ref.repo, ref.tag)
    except asset_downloader.ReleaseLookupError as exc:
        raise ReleaseFetchError(f"GitHub could not be asked about {ref.tag} of {ref.repo}: {exc}") from exc
    if not isinstance(data, dict):
        raise ReleaseFetchError(
            f"Release {ref.tag} of {ref.repo} was not found on GitHub. A draft, a private repository or a tag "
            "without a release cannot be fetched (or GitHub could not be reached: see the terminal log)."
        )
    tag = str(data.get("tag_name") or ref.tag)
    release_type = "Pre-release" if data.get("prerelease") else "Release"
    existing = mapping_manager.get_repository_mapping(ref.repo)
    repo = str(existing.get("repository") or ref.repo) if existing else _canonical_repo(data, ref.repo)
    created = False
    if existing is None:
        created, _updated = mapping_manager.upsert_repository_mapping(repo)

    try:
        from modules import queue_worker  # imported here: it needs the Gmail variables of .env, the GUI does not
    except ValueError as exc:
        raise ReleaseFetchError(f"The queue code could not be loaded: {exc}") from exc

    commit, reason = queue_worker.get_current_commit_hash(repo, tag, asset_downloader.GITHUB_TOKEN, include_reason=True)
    when = time.time() if now is None else now
    steps = len(mapping_manager.get_repository_recheck_intervals_minutes(repo))
    try:
        if connection is None:
            with closing(db_manager.open_database()) as own:
                job_id = queue_worker.queue_release_job(own, repo, tag, release_type, commit, reason, when)
                if finish_early and job_id is not None:
                    db_manager.finish_job_now(own, job_id, steps, now=when)
        else:
            job_id = queue_worker.queue_release_job(connection, repo, tag, release_type, commit, reason, when)
            if finish_early and job_id is not None:
                db_manager.finish_job_now(connection, job_id, steps, now=when)
    except sqlite3.Error as exc:
        raise ReleaseFetchError(f"The job could not be written to state.db: {exc}") from exc
    return FetchOutcome(repo, tag, release_type, job_id, created, finish_early)


def outcome_text(outcome: FetchOutcome) -> str:
    """Return the one-line report of a fetch for the footer and the console."""
    tag = f"{outcome.repo} {outcome.tag} ({outcome.release_type})"
    if outcome.job_id is None:
        return f"{tag}: nothing was queued (dry run)."
    text = f"{tag}: queued as job #{outcome.job_id}"
    text += ", one final check then finalize." if outcome.finish_early else ", with the usual re-checks."
    if outcome.mapping_created:
        text += f" {outcome.repo} was added without a destination: set one in the Mappings tab (see Unmapped)."
    return text
