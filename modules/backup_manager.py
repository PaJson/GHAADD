"""Backups of state.db and the settings files: one zip per backup, made by hand or on the daemon's schedule.

A backup holds a consistent copy of state.db (SQLite online backup, safe while the daemon runs), config.json,
mapping.json and, only when asked for, .env (it contains passwords). Old backups beyond `backup.keep_files` are
deleted. Settings live in the `backup` section of config.json (`config_manager.get_backup_settings`). To restore,
stop the daemon and unzip the files over the originals. Toolkit-independent; the GUI and the CLI call this module.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from modules import config_manager, db_manager, env_manager, mapping_manager
from modules.app_info import __version__
from modules.dry_run_mode import is_dry_run

BACKUP_FILE_PATTERN = re.compile(r"^ghaadd_backup_(\d{8}_\d{6})(?:_(\d+))?\.zip$")
STATE_DB_ENTRY = "state.db"


@dataclass(frozen=True)
class BackupResult:
    """Outcome of one backup: success flag, a plain-language message and the zip file (when one was written)."""

    ok: bool
    message: str
    path: Optional[str] = None


@dataclass(frozen=True)
class BackupInfo:
    """One backup file found in the backup folder."""

    path: str
    name: str
    size: int
    created: float  # epoch seconds, from the file name (falls back to the modification time)


def _sort_key(name: str) -> tuple[str, int]:
    """Order backup file names oldest to newest: by timestamp, then collision counter."""
    match = BACKUP_FILE_PATTERN.match(name)
    return (match.group(1), int(match.group(2) or 1)) if match else ("", 0)


def list_backups(directory: str) -> list[BackupInfo]:
    """Return the backups in `directory`, newest first (an unreadable or missing folder gives an empty list)."""
    try:
        names = [name for name in os.listdir(directory) if BACKUP_FILE_PATTERN.match(name)]
    except OSError:
        return []
    found = []
    for name in sorted(names, key=_sort_key, reverse=True):
        path = os.path.join(directory, name)
        try:
            size, modified = os.path.getsize(path), os.path.getmtime(path)
        except OSError:
            continue
        stamp = BACKUP_FILE_PATTERN.match(name)
        try:
            created = datetime.strptime(stamp.group(1), "%Y%m%d_%H%M%S").timestamp() if stamp else modified
        except ValueError:
            created = modified
        found.append(BackupInfo(path, name, size, created))
    return found


def new_backup_path(directory: str, now: Optional[datetime] = None) -> str:
    """Return an unused backup file path named after `now`, with a counter on collisions."""
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(directory, f"ghaadd_backup_{stamp}.zip")
    counter = 1
    while os.path.exists(path):
        counter += 1
        path = os.path.join(directory, f"ghaadd_backup_{stamp}_{counter}.zip")
    return path


def prune_backups(directory: str, keep_files: int) -> int:
    """Delete the oldest backups beyond `keep_files` (0 keeps everything); return how many were removed."""
    if keep_files <= 0 or is_dry_run():
        return 0
    removed = 0
    for info in list_backups(directory)[keep_files:]:
        try:
            os.remove(info.path)
            removed += 1
        except OSError:
            pass  # in use or already gone: the next backup tries again
    return removed


def is_backup_due(settings: config_manager.BackupSettings, now: Optional[float] = None) -> bool:
    """True when backups are on and the newest one is older than `every_hours` (or there is none yet)."""
    if not settings["enabled"]:
        return False
    backups = list_backups(settings["directory"])
    if not backups:
        return True
    return (time.time() if now is None else now) - backups[0].created >= settings["every_hours"] * 3600


def _source_files(include_env: bool) -> list[tuple[str, str]]:
    """The (path on disk, name in the zip) pairs of the settings files that exist."""
    candidates = [
        (config_manager.get_config_path(), "config.json"),
        (mapping_manager._mapping_file_path(), "mapping.json"),
    ]
    if include_env:
        candidates.append((env_manager.get_env_path(), ".env"))
    return [(path, name) for path, name in candidates if os.path.isfile(path)]


def create_backup(
    settings: Optional[config_manager.BackupSettings] = None,
    now: Optional[datetime] = None,
    snapshot: Optional[Callable[[str], None]] = None,
) -> BackupResult:
    """Write one backup zip now (whether or not backups are switched on) and delete backups beyond the keep limit.

    The zip is written under a temporary name and renamed when complete, so a crash never leaves a half
    backup that looks real. `snapshot` writes the database copy (default: db_manager.backup_database; a parameter for tests).
    """
    settings = settings or config_manager.get_backup_settings()
    directory = settings["directory"]
    if is_dry_run():
        return BackupResult(True, f"Dry run: a backup would be written to {directory}.")

    temporary_db = None
    temporary_zip = None
    try:
        os.makedirs(directory, exist_ok=True)
        handle, temporary_db = tempfile.mkstemp(suffix=".db", prefix="ghaadd_state_")
        os.close(handle)
        (snapshot or db_manager.backup_database)(temporary_db)
        sources = _source_files(settings["include_env"])
        final_path = new_backup_path(directory, now)
        temporary_zip = final_path + ".tmp"
        manifest = {
            "app_version": __version__,
            "created": (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S"),
            "files": [STATE_DB_ENTRY] + [name for _path, name in sources],
        }
        with zipfile.ZipFile(temporary_zip, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(temporary_db, STATE_DB_ENTRY)
            for path, name in sources:
                archive.write(path, name)
            archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        os.replace(temporary_zip, final_path)
        temporary_zip = None
    except Exception as exc:  # OSError, sqlite3.Error...: report it, a backup must never stop the daemon
        return BackupResult(False, f"The backup failed: {exc}")
    finally:
        for leftover in (temporary_db, temporary_zip):
            if leftover:
                try:
                    os.remove(leftover)
                except OSError:
                    pass

    removed = prune_backups(directory, settings["keep_files"])
    note = f" ({removed} old backup(s) removed)" if removed else ""
    return BackupResult(True, f"Backup written: {final_path}{note}", final_path)


def run_scheduled_backup(now: Optional[float] = None) -> Optional[BackupResult]:
    """Make a backup if the schedule says one is due; None when nothing was due. Called by the daemon."""
    settings = config_manager.get_backup_settings()
    if not is_backup_due(settings, now):
        return None
    return create_backup(settings)
