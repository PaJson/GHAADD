"""Terminal log file management: naming, size-based rollover and retention.

The daemon mirrors its console output to one YYYYMMDD_HHMMSS.log file per run
(see main.setup_terminal_logging). A long-running daemon would grow a single
file without limit, so RollingLogFile starts a new file once the current one
reaches a size limit, and old files are pruned by count. Rolling over to a new,
newer-named file (instead of trimming one file in place) keeps readers that tail
the newest file simple: they just switch when a newer file appears.
"""
import os
import re
from datetime import datetime
from typing import Optional

from modules.dry_run_mode import is_dry_run

LOG_FILE_PATTERN = re.compile(r"^(\d{8}_\d{6})(?:_(\d+))?\.log$")


def _sort_key(file_name: str) -> tuple[str, int]:
    """Order log files oldest to newest: by timestamp, then rollover counter."""
    match = LOG_FILE_PATTERN.match(file_name)
    if match is None:
        return ("", 0)
    return (match.group(1), int(match.group(2) or 0))


def list_log_files(directory: str) -> list[str]:
    """Return full paths of GHAADD log files in directory, oldest first."""
    try:
        names = [name for name in os.listdir(directory) if LOG_FILE_PATTERN.match(name)]
    except OSError:
        return []
    return [os.path.join(directory, name) for name in sorted(names, key=_sort_key)]


def new_log_path(directory: str, now: Optional[datetime] = None) -> str:
    """Return an unused log path named after `now`, with a counter on collisions."""
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(directory, f"{stamp}.log")
    counter = 1
    while os.path.exists(path):
        counter += 1
        path = os.path.join(directory, f"{stamp}_{counter}.log")
    return path


def prune_log_files(directory: str, keep_files: int, protect: Optional[str] = None) -> int:
    """Delete the oldest log files beyond `keep_files`; return how many were removed.

    Only files matching the GHAADD log naming pattern are considered. `protect`
    (the file currently being written) is never deleted. keep_files <= 0 keeps
    everything. Does nothing in dry-run mode.
    """
    if keep_files <= 0 or is_dry_run():
        return 0

    protected = os.path.abspath(protect) if protect else None
    files = list_log_files(directory)
    removed = 0
    for path in files[: max(0, len(files) - keep_files)]:
        if protected is not None and os.path.abspath(path) == protected:
            continue
        try:
            os.remove(path)
            removed += 1
        except OSError:
            pass
    return removed


class RollingLogFile:
    """Text file-like object that rolls over to a new log file at a size limit.

    Used as the secondary stream of TeeStream. The roll happens at a line
    boundary, so no line is split across files. max_bytes <= 0 disables
    rollover. If a rollover fails, the current file keeps being used.
    """

    def __init__(self, directory: str, max_bytes: int, keep_files: int) -> None:
        self.directory = directory
        self.max_bytes = max_bytes
        self.keep_files = keep_files
        self.path = new_log_path(directory)
        self._stream = self._open(self.path)
        self._size = 0
        self._rollover_failed = False

    @staticmethod
    def _open(path: str):
        return open(path, "a", encoding="utf-8", buffering=1)

    def write(self, data: str) -> int:
        self._stream.write(data)
        self._size += len(data.encode("utf-8"))
        if (
            self.max_bytes > 0
            and not self._rollover_failed
            and self._size >= self.max_bytes
            and data.endswith("\n")
        ):
            self._roll()
        return len(data)

    def flush(self) -> None:
        self._stream.flush()

    def close(self) -> None:
        self._stream.close()

    def prune(self) -> int:
        """Apply the retention limit now (the current file is never removed)."""
        return prune_log_files(self.directory, self.keep_files, protect=self.path)

    def _roll(self) -> None:
        previous_path = self.path
        try:
            new_path = new_log_path(self.directory)
            new_stream = self._open(new_path)
        except OSError:
            self._rollover_failed = True
            return

        try:
            self._stream.close()
        except OSError:
            pass
        self._stream = new_stream
        self.path = new_path
        header = f"--- Log continued from {os.path.basename(previous_path)} ---\n"
        self._stream.write(header)
        self._size = len(header.encode("utf-8"))
        self.prune()
