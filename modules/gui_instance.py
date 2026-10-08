"""Only one GHAADD window at a time.

The first GUI holds a lock file for as long as it runs. A second start finds the lock held, leaves a small
"show yourself" note next to it and exits; the running window sees the note (it looks every half second, also
while hidden in the tray or minimized) and brings itself to the front. The OS drops the lock when the process
ends, however it ends, so a crash never leaves the GUI locked out.

Toolkit-independent; the paths are parameters so the tests never touch the real files.
"""

from __future__ import annotations

import os
import tempfile
from typing import Optional

from filelock import FileLock, Timeout

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK_FILE_NAME = "ghaadd.gui.lock"
SHOW_FILE_NAME = "ghaadd.gui.show"

_held: Optional[FileLock] = None  # kept for the life of the process


def lock_path(directory: Optional[str] = None) -> str:
    """Return the path of the GUI lock file."""
    return os.path.join(directory or _APP_DIR, LOCK_FILE_NAME)


def show_path(directory: Optional[str] = None) -> str:
    """Return the path of the "show yourself" note a second start leaves behind."""
    return os.path.join(directory or _APP_DIR, SHOW_FILE_NAME)


def acquire(directory: Optional[str] = None) -> bool:
    """Become the one GUI. False when another GUI already holds the lock (nothing is changed then)."""
    global _held
    lock = FileLock(lock_path(directory), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        return False
    except OSError:
        return True  # a read-only folder or the like must not keep the GUI from starting
    _held = lock
    clear_show_request(directory)  # a note left by a start that raced with the last window's exit is stale
    return True


def release() -> None:
    """Release the GUI lock (the OS also frees it when the process ends)."""
    global _held
    lock, _held = _held, None
    if lock is not None:
        try:
            lock.release()
        except OSError:
            pass


def request_show(directory: Optional[str] = None) -> bool:
    """Ask the running GUI to come to the front. Best effort: True when the note was written."""
    path = show_path(directory)
    temp_path = None
    try:
        handle, temp_path = tempfile.mkstemp(dir=os.path.dirname(path), prefix=SHOW_FILE_NAME + ".", suffix=".tmp")
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write("show\n")
        os.replace(temp_path, path)
        return True
    except OSError:
        if temp_path is not None:
            try:
                os.remove(temp_path)
            except OSError:
                pass
        return False


def clear_show_request(directory: Optional[str] = None) -> None:
    """Delete the "show yourself" note, if present."""
    try:
        os.remove(show_path(directory))
    except OSError:
        pass


def take_show_request(directory: Optional[str] = None) -> bool:
    """True once per note: the note is removed as it is read."""
    try:
        os.remove(show_path(directory))
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return True
