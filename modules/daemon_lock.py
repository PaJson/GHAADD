import json
import os
import sys
import time
from contextlib import contextmanager
from typing import Optional, TypedDict

from filelock import FileLock, Timeout

_LOCK_FILE_NAME = "ghaadd.lock"
_STATUS_FILE_NAME = "ghaadd.daemon.status.json"


class DaemonStatus(TypedDict):
    running: bool
    pid: Optional[int]
    started_at: Optional[float]


def get_daemon_lock_path() -> str:
    """Return the daemon singleton lock file path beside the app files."""
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(app_dir, _LOCK_FILE_NAME)


def get_daemon_status_path() -> str:
    """Return the daemon status sidecar file path beside the app files."""
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(app_dir, _STATUS_FILE_NAME)


def _write_daemon_status() -> None:
    """Record this process's PID and start time for status probes."""
    payload = {"pid": os.getpid(), "started_at": time.time()}
    try:
        with open(get_daemon_status_path(), "w", encoding="utf-8") as status_file:
            json.dump(payload, status_file)
    except OSError:
        pass


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
    """Return whether the daemon is running, plus PID/start time when known.

    PID/start time come from a best-effort sidecar file written at lock
    acquisition, so they are only trusted once the lock probe itself confirms
    a daemon is actually running (the sidecar alone can go stale on a crash).
    """
    if not is_daemon_running():
        return {"running": False, "pid": None, "started_at": None}

    try:
        with open(get_daemon_status_path(), "r", encoding="utf-8") as status_file:
            payload = json.load(status_file)
        return {
            "running": True,
            "pid": payload.get("pid"),
            "started_at": payload.get("started_at"),
        }
    except (OSError, json.JSONDecodeError, AttributeError):
        return {"running": True, "pid": None, "started_at": None}


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

    _write_daemon_status()
    try:
        yield
    finally:
        _clear_daemon_status()
        lock.release()
