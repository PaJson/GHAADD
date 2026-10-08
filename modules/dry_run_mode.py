"""Process-wide dry-run flag (--dry-run): code checks is_dry_run() before every write, delete or download."""

_dry_run_enabled = False


def set_dry_run(enabled: bool) -> None:
    """Enable or disable dry-run mode for the remainder of this process."""
    global _dry_run_enabled
    _dry_run_enabled = bool(enabled)


def is_dry_run() -> bool:
    """Return whether dry-run mode is currently enabled.

    Not thread-safe/re-entrant; GHAADD runs one mutating instance at a time
    (see daemon_lock.py), so a single process-wide flag is sufficient.
    """
    return _dry_run_enabled
