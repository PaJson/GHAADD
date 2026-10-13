"""The working folders under ghaadd_root (Complete, Logs, Partial, Processing) for the GUI's "Folders" buttons.

Toolkit-independent: paths come from config.json, counts from one directory listing each.
"""

import os
from typing import Any, Dict, NamedTuple, Optional

from modules import asset_downloader, config_manager, mapping_manager
from modules.file_cache import StatCache


class WorkFolder(NamedTuple):
    """One working folder: its settings key, the button label and its path."""
    key: str
    label: str
    path: str


# Button order, left to right: (key in config_manager.FolderSettings, button label).
FOLDER_BUTTONS = (
    ("complete", "Complete"),
    ("logs", "Logs"),
    ("partial", "Partial"),
    ("processing", "Processing"),
)


def work_folders(config: Optional[Dict[str, Any]] = None) -> list[WorkFolder]:
    """Return the four working folders in button order, resolved against the configured download directory."""
    config = config if config is not None else config_manager.load_config()
    settings = config_manager.get_folder_settings(config)
    root = os.path.join(config_manager.get_default_download_dir(config), settings["ghaadd_root"])
    return [WorkFolder(key, label, os.path.join(root, settings[key])) for key, label in FOLDER_BUTTONS]  # type: ignore[literal-required]


def count_entries(path: str) -> int:
    """Return how many files and folders are directly inside `path` (0 when it is missing or unreadable)."""
    try:
        with os.scandir(path) as entries:
            return sum(1 for _ in entries)
    except OSError:
        return 0


def folder_count_cache() -> StatCache[dict[str, int]]:
    """Return a cache of {key: entry count} that re-lists a folder only when its modified time changed.

    Adding or removing a direct entry changes a folder's mtime, which is exactly what is counted; the cache's
    periodic safety refresh covers filesystems with coarse timestamps.
    """
    return StatCache(
        paths=lambda: [item.path for item in work_folders()],
        compute=lambda: {item.key: count_entries(item.path) for item in work_folders()},
    )


def movable_cache() -> StatCache[int]:
    """Return a cache of how many repositories wait in Complete with a destination that is ready now.

    Watches mapping.json and every Complete folder; a destination folder that appears later is caught by the
    cache's periodic safety refresh.
    """
    def watched() -> list[str]:
        """List mapping.json and the Complete folder of every download root."""
        config = config_manager.load_config()
        settings = config_manager.get_folder_settings(config)
        roots = [
            os.path.join(root, settings["ghaadd_root"], settings["complete"])
            for root in config_manager.get_all_download_dirs(config)
        ]
        return [mapping_manager._mapping_file_path(), *roots]

    return StatCache(paths=watched, compute=asset_downloader.count_movable_complete_repos)


def move_blocked_reason(busy_repo: Optional[str], job_running: bool) -> str:
    """Return why the move button must wait ("" when it may run): the daemon is in the middle of a job."""
    if not job_running:
        return ""
    where = f" with {busy_repo}" if busy_repo else ""
    return f"The daemon is busy{where}: try again when the job has finished."


def move_result_text(summary: Dict[str, int]) -> str:
    """Return the status line for a finished move: how many items moved for how many repositories."""
    items = int(summary.get("moved_release_folders", 0))
    repos = int(summary.get("moved_repositories", 0))
    problems = int(summary.get("missing_destination_warnings", 0))
    if items == 0:
        text = "Nothing was moved."
    else:
        text = f"Moved {items} item{'s' if items != 1 else ''} for {repos} repositor{'ies' if repos != 1 else 'y'}."
    if problems:
        text += f" {problems} destination{'s do' if problems != 1 else ' does'} not exist (see Warnings)."
    return text


def button_text(label: str, count: int) -> str:
    """Return the button text: "Complete (2)", or just "Complete" when the folder is empty."""
    return f"{label} ({count})" if count > 0 else label


def open_target(path: str) -> Optional[str]:
    """Return the folder to open for `path`: the folder itself, else its nearest existing parent, else None."""
    current = os.path.normpath(path)
    while current:
        if os.path.isdir(current):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent
    return None
