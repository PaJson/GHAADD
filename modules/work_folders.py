"""The working folders under ghaadd_root (Complete, Logs, Partial, Processing) for the GUI's "Folders" buttons.

Toolkit-independent: paths come from config.json, counts from one directory listing each.
"""

import os
from typing import Any, Dict, NamedTuple, Optional

from modules import config_manager
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
