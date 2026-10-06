"""Tail the daemon's terminal log files for the GUI's Live log tab (no Tk, no threads).

The GUI cannot read the daemon's stdout, so it follows the newest
YYYYMMDD_HHMMSS[_n].log file instead (see modules/log_files.py). `LogTailer.poll()`
is cheap (one `stat` when nothing changed) and returns only what is new:

- first call / switched file: the last `max_lines` lines (`reset=True`);
- afterwards: the lines appended since the previous call;
- a trailing partial line is held back until its newline arrives;
- a size rollover (the new file starts with "--- Log continued from X ---")
  is followed seamlessly: the rest of the old file, then the new one, without a
  reset; a brand-new run or a switched-on log starts a fresh view (`reset=True`);
- a file that shrank (replaced/truncated) is reloaded, and so is a backlog too
  large to replay line by line (e.g. after the tab was hidden for hours).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from modules.log_files import list_log_files

DEFAULT_MAX_LINES = 1000
MAX_CATCHUP_BYTES = 512 * 1024  # a bigger backlog is replaced by "the last max_lines lines"
_CHUNK_BYTES = 64 * 1024
_CONTINUED_HEADER = re.compile(r"^--- Log continued from (\S+\.log) ---$")

LEVEL_ERROR = "error"
LEVEL_WARNING = "warning"
_ERROR_MARKERS = ("❌", "Traceback (most recent call last)", "Error:", "ERROR")
_WARNING_MARKERS = ("⚠", "Warning", "WARNING")


@dataclass
class TailUpdate:
    """What changed since the last poll."""

    reset: bool = False  # clear the view first, then show `lines`
    lines: list[str] = field(default_factory=list)
    file_name: Optional[str] = None  # the file being followed (None: there is no log file)
    changed_file: bool = False


def line_level(line: str) -> Optional[str]:
    """Classify a log line for colouring: "error", "warning" or None."""
    if any(marker in line for marker in _ERROR_MARKERS):
        return LEVEL_ERROR
    if any(marker in line for marker in _WARNING_MARKERS):
        return LEVEL_WARNING
    return None


def _split_lines(data: bytes) -> list[str]:
    """Decode complete lines (data ends with a newline); undecodable bytes become U+FFFD."""
    lines = [line.rstrip("\r") for line in data.decode("utf-8", errors="replace").split("\n")]
    lines.pop()  # the empty piece after the final newline
    return lines


def read_last_lines(path: str, max_lines: int) -> tuple[list[str], int]:
    """Return (the last max_lines complete lines, byte offset just after the last newline).

    Reads backwards in chunks, so the cost does not depend on the file size. A
    trailing line without a newline is left out; the offset points at its start
    so a later poll picks it up once it is complete.
    """
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        data = b""
        # max_lines + 2 newlines: max_lines full lines, one cut-off line at the front, one trailing newline.
        while position > 0 and data.count(b"\n") < max_lines + 2:
            read_size = min(_CHUNK_BYTES, position)
            position -= read_size
            handle.seek(position)
            data = handle.read(read_size) + data

    last_newline = data.rfind(b"\n")
    if last_newline < 0:
        return [], position  # nothing complete yet (an empty file, or one unfinished line)
    offset = position + last_newline + 1
    lines = _split_lines(data[: last_newline + 1])
    if position > 0 and lines:
        lines.pop(0)  # the first line may be cut off by the chunk boundary
    return lines[-max_lines:], offset


class LogTailer:
    """Follows the newest log file in a directory; see the module docstring."""

    def __init__(self, directory: Callable[[], str], max_lines: int = DEFAULT_MAX_LINES) -> None:
        self._directory = directory
        self._max_lines = max_lines
        self._path: Optional[str] = None
        self._offset = 0
        self._reported_empty = False

    @property
    def path(self) -> Optional[str]:
        return self._path

    def poll(self) -> Optional[TailUpdate]:
        """Return what is new, or None when nothing changed since the last poll."""
        files = list_log_files(self._directory())
        newest = files[-1] if files else None

        if newest is None:
            if self._path is None and self._reported_empty:
                return None
            self._path, self._offset, self._reported_empty = None, 0, True
            return TailUpdate(reset=True, lines=[], file_name=None, changed_file=True)
        self._reported_empty = False

        if self._path is None:
            return self._load(newest, reset=True)

        update = self._read_new_lines(self._path)
        if update is None and not os.path.exists(self._path):
            return self._load(newest, reset=True)  # the followed file vanished

        if newest != self._path:
            return self._follow_newer(newest, update)
        return update

    # ----- internals -----

    def _load(self, path: str, reset: bool, prefill_continued: bool = True) -> Optional[TailUpdate]:
        """Start following `path`: show its last lines (plus the tail of the file it continues)."""
        try:
            lines, offset = read_last_lines(path, self._max_lines)
        except OSError:
            return None
        previous_path, self._path, self._offset = self._path, path, offset
        if prefill_continued and lines and len(lines) < self._max_lines:
            lines = self._prefill(path, lines)
        return TailUpdate(reset=reset, lines=lines, file_name=os.path.basename(path), changed_file=path != previous_path)

    def _prefill(self, path: str, lines: list[str]) -> list[str]:
        """Prepend the tail of the file a rollover continued from, so the view has context."""
        for _ in range(3):  # a few hops back at most
            match = _CONTINUED_HEADER.match(lines[0]) if lines else None
            if match is None:
                break
            previous = os.path.join(os.path.dirname(path), match.group(1))
            try:
                earlier, _ = read_last_lines(previous, self._max_lines - len(lines))
            except OSError:
                break
            lines = earlier + lines
            path = previous
            if len(lines) >= self._max_lines:
                break
        return lines[-self._max_lines:]

    def _read_new_lines(self, path: str) -> Optional[TailUpdate]:
        """Lines appended to the followed file since the last poll (None when none)."""
        try:
            size = os.stat(path).st_size
        except OSError:
            return None
        if size < self._offset or size - self._offset > MAX_CATCHUP_BYTES:
            return self._load(path, reset=True, prefill_continued=False)  # replaced/truncated, or far behind
        if size == self._offset:
            return None
        try:
            with open(path, "rb") as handle:
                handle.seek(self._offset)
                data = handle.read(size - self._offset)
        except OSError:
            return None
        last_newline = data.rfind(b"\n")
        if last_newline < 0:
            return None  # only a partial line so far: wait for its newline
        self._offset += last_newline + 1
        lines = _split_lines(data[: last_newline + 1])
        return TailUpdate(lines=lines, file_name=os.path.basename(path)) if lines else None

    def _follow_newer(self, newest: str, pending: Optional[TailUpdate]) -> Optional[TailUpdate]:
        """A newer file exists: finish the old one, then continue seamlessly or start a fresh view."""
        old_name = os.path.basename(self._path or "")
        try:
            lines, offset = read_last_lines(newest, self._max_lines)
        except OSError:
            return pending
        header = _CONTINUED_HEADER.match(lines[0]) if lines else None
        if header is not None and header.group(1) == old_name:
            # A size rollover of the file we were following: append, do not clear the view.
            self._path, self._offset = newest, offset
            carried = pending.lines if pending is not None and not pending.reset else []
            return TailUpdate(lines=carried + lines, file_name=os.path.basename(newest), changed_file=True)
        # A new run, a switched-on log, or several rollovers at once: show the new file from scratch.
        return self._load(newest, reset=True)
