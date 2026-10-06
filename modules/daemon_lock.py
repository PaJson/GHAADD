import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from typing import Optional, TypedDict

from filelock import FileLock, Timeout

_LOCK_FILE_NAME = "ghaadd.lock"
# True only inside acquire_daemon_lock(): status fields may be published only by the lock holder.
_lock_held = False
_STATUS_FILE_NAME = "ghaadd.daemon.status.json"


class DaemonStatus(TypedDict):
    running: bool
    pid: Optional[int]
    started_at: Optional[float]
    paused: bool
    next_poll_at: Optional[float]
    last_forced_poll_handled: Optional[float]
    current_job: Optional[dict]  # {"repo", "tag", "since"} while a job is being processed
    config_fingerprint: Optional[str]  # config_manager.get_config_fingerprint() the daemon started with
    log_active: Optional[bool]  # whether the terminal log file is really open (None: daemon does not say)


def get_daemon_lock_path() -> str:
    """Return the daemon singleton lock file path beside the app files."""
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(app_dir, _LOCK_FILE_NAME)


def get_daemon_status_path() -> str:
    """Return the daemon status sidecar file path beside the app files."""
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(app_dir, _STATUS_FILE_NAME)


def _read_status_payload() -> dict:
    """Return the status sidecar contents, or an empty dict when missing/unreadable."""
    try:
        with open(get_daemon_status_path(), "r", encoding="utf-8") as status_file:
            payload = json.load(status_file)
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_status_payload(payload: dict) -> None:
    """Atomically replace the status sidecar so probes never see a half-written file."""
    status_path = get_daemon_status_path()
    temp_path = None
    try:
        file_descriptor, temp_path = tempfile.mkstemp(
            dir=os.path.dirname(status_path), prefix=f"{_STATUS_FILE_NAME}.", suffix=".tmp"
        )
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temp_file:
            json.dump(payload, temp_file)
        os.replace(temp_path, status_path)
        temp_path = None
    except OSError:
        pass
    finally:
        if temp_path is not None:
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _write_daemon_status() -> None:
    """Record this process's PID and start time for status probes."""
    _write_status_payload({"pid": os.getpid(), "started_at": time.time()})


def update_daemon_status(**fields) -> None:
    """Merge fields (e.g. paused, next_poll_at) into the status sidecar, best-effort.

    Only the daemon that holds the singleton lock may call this.
    """
    payload = _read_status_payload()
    payload.update(fields)
    _write_status_payload(payload)


def _clear_daemon_status() -> None:
    """Remove the daemon status sidecar file, best-effort."""
    try:
        os.remove(get_daemon_status_path())
    except OSError:
        pass


def is_daemon_running() -> bool:
    """Return whether a mutating daemon instance currently holds the lock."""
    lock = FileLock(get_daemon_lock_path(), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        return True
    lock.release()
    return False


def get_daemon_status() -> DaemonStatus:
    """Return whether the daemon is running, plus PID/start time/poll state when known.

    Details come from a best-effort sidecar file written at lock acquisition,
    so they are only trusted once the lock probe itself confirms a daemon is
    actually running (the sidecar alone can go stale on a crash).
    """
    if not is_daemon_running():
        return {
            "running": False,
            "pid": None,
            "started_at": None,
            "paused": False,
            "next_poll_at": None,
            "last_forced_poll_handled": None,
            "current_job": None,
            "config_fingerprint": None,
            "log_active": None,
        }

    payload = _read_status_payload()
    return {
        "running": True,
        "pid": payload.get("pid"),
        "started_at": payload.get("started_at"),
        "paused": payload.get("paused") is True,
        "next_poll_at": payload.get("next_poll_at"),
        "last_forced_poll_handled": payload.get("last_forced_poll_handled"),
        "current_job": payload.get("current_job") if isinstance(payload.get("current_job"), dict) else None,
        "config_fingerprint": payload.get("config_fingerprint") if isinstance(payload.get("config_fingerprint"), str) else None,
        "log_active": payload.get("log_active") if isinstance(payload.get("log_active"), bool) else None,
    }


@contextmanager
def publishing_current_job(repo: str, tag: str):
    """Publish the job being processed so the GUI can show it as "Running".

    A no-op unless this process holds the daemon lock (tests, dry-run and
    read-only commands never touch the status file).
    """
    if not _lock_held:
        yield
        return
    update_daemon_status(current_job={"repo": repo, "tag": tag, "since": time.time()})
    try:
        yield
    finally:
        update_daemon_status(current_job=None)


@contextmanager
def acquire_daemon_lock():
    """Acquire the singleton daemon lock, exiting the process if already held.

    Guards only mutating run modes (default single run, --once, --poll,
    --drain-queue); read-only CLI commands never call this and remain free to
    run alongside an active daemon.
    """
    lock = FileLock(get_daemon_lock_path(), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        print(
            "Another GHAADD instance is already running (daemon lock is held). Exiting.",
            file=sys.stderr,
        )
        sys.exit(1)

    global _lock_held
    _write_daemon_status()
    _lock_held = True
    try:
        yield
    finally:
        _lock_held = False
        _clear_daemon_status()
        lock.release()
