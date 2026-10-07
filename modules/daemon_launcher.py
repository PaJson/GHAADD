"""Start the polling daemon as a fully detached process (used by the GUI's Start button).

The GUI does not own the daemon: it must keep running after the GUI closes, so
it gets no console, no inherited standard streams and its own process group
(Windows) or session (POSIX).
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STDERR_FILE_NAME = "ghaadd.daemon.stderr.log"

# subprocess.DETACHED_PROCESS / CREATE_NEW_PROCESS_GROUP exist only on Windows builds of Python.
_WINDOWS_DETACH_FLAGS = 0x00000008 | 0x00000200


def get_main_script_path() -> str:
    return os.path.join(_APP_DIR, "main.py")


def get_python_executable(executable: Optional[str] = None) -> str:
    """Return a console interpreter for the daemon (pythonw.exe has no stdout, which the daemon prints to)."""
    executable = executable or sys.executable
    directory, name = os.path.split(executable)
    if name.lower() == "pythonw.exe":
        console_python = os.path.join(directory, "python.exe")
        if os.path.isfile(console_python):
            return console_python
    return executable


def get_stderr_path() -> str:
    """Where a GUI-started daemon's error output (e.g. a crash traceback) goes."""
    return os.path.join(_APP_DIR, STDERR_FILE_NAME)


def build_start_command() -> list[str]:
    """Command line that starts the polling daemon (--daemon: polls, or idles when polling.enabled is false)."""
    return [get_python_executable(), get_main_script_path(), "--daemon"]


def start_daemon() -> int:
    """Launch the daemon detached from this process and return its PID. Raises OSError on failure."""
    # Nobody watches a detached daemon's console, so stdout is dropped (the terminal log mirrors it
    # when switched on) but stderr is kept in a file: a crash must leave a trace.
    stderr_target = None
    try:
        stderr_target = open(get_stderr_path(), "wb")
    except OSError:
        pass
    options: dict = {
        "cwd": _APP_DIR,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": stderr_target if stderr_target is not None else subprocess.DEVNULL,
        "close_fds": True,
        # A redirected stdout would otherwise use the Windows locale encoding and choke on emoji.
        "env": {**os.environ, "PYTHONIOENCODING": "utf-8"},
    }
    if sys.platform == "win32":
        options["creationflags"] = _WINDOWS_DETACH_FLAGS
    else:
        options["start_new_session"] = True
    try:
        process = subprocess.Popen(build_start_command(), **options)
    finally:
        if stderr_target is not None:
            stderr_target.close()  # the child holds its own copy of the handle
    return process.pid
