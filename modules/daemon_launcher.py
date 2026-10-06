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


def build_start_command() -> list[str]:
    """Command line that starts the polling daemon (--poll forces polling even if config disables it)."""
    return [get_python_executable(), get_main_script_path(), "--poll"]


def start_daemon() -> int:
    """Launch the daemon detached from this process and return its PID. Raises OSError on failure."""
    options: dict = {
        "cwd": _APP_DIR,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        options["creationflags"] = _WINDOWS_DETACH_FLAGS
    else:
        options["start_new_session"] = True
    process = subprocess.Popen(build_start_command(), **options)
    return process.pid
