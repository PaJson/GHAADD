"""Toolkit-independent parsing and validation for the GUI's edit forms.

The widgets hand over plain strings/bools; these functions turn them into the
values mapping_manager / config_manager store, or into readable error messages.
Nothing here touches Tk, files or the database (path checks are the one
exception and are reported as non-blocking warnings).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from modules import config_manager, mapping_manager

# config.json keys (dotted path) behind the Settings dialog.
SETTINGS_KEYS = {
    "recheck": "processing.recheck_intervals_minutes",
    "max_emails": "processing.max_emails_to_process",
    "dest_check": "processing.destination_check_every_n_polls",
    "default_limit": "processing.default_limit",
    "default_subfolder": "paths.default_subfolder",
    "polling_enabled": "polling.enabled",
    "interval": "polling.interval_seconds",
    "jitter_min": "polling.jitter_min_seconds",
    "jitter_max": "polling.jitter_max_seconds",
    "download_dir": "paths.default_download_dir",
    "log_enabled": "terminal_log.enabled",
    "log_max_mb": "terminal_log.max_file_mb",
    "log_keep": "terminal_log.keep_files",
    "start_minimized": "gui.start_minimized",
    "start_daemon": "gui.start_daemon",
    "tray": "gui.tray",
    "notifications": "gui.notifications",
    "minimize_to_tray": "gui.minimize_to_tray",
    "close_to_tray": "gui.close_to_tray",
}

MIN_POLL_INTERVAL_SECONDS = 10


@dataclass
class FormResult:
    """Outcome of validating a form: `changes` is only usable when `errors` is empty."""

    changes: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def format_list(values: Optional[list[Any]]) -> str:
    """Render a list for an entry box: [5, 15] -> "5, 15"."""
    return ", ".join(str(value) for value in (values or []))


def parse_str_list(text: Optional[str]) -> list[str]:
    """Split a comma-separated entry into trimmed, non-empty, case-insensitively unique items."""
    items: list[str] = []
    seen: set[str] = set()
    for part in str(text or "").split(","):
        item = part.strip()
        if item and item.lower() not in seen:
            seen.add(item.lower())
            items.append(item)
    return items


def parse_int_list(text: str, label: str, errors: list[str]) -> list[int]:
    """Parse "5, 15, 30" into [5, 15, 30]; positive whole numbers only, no duplicates."""
    values: list[int] = []
    for part in str(text or "").split(","):
        item = part.strip()
        if not item:
            continue
        if not item.isdigit() or int(item) <= 0:
            errors.append(f"{label}: '{item}' is not a positive whole number.")
            continue
        if int(item) not in values:
            values.append(int(item))
    return values


def parse_int(text: Any, label: str, errors: list[str], minimum: int = 0) -> Optional[int]:
    """Parse a whole number >= minimum, appending an error and returning None otherwise."""
    raw = str(text if text is not None else "").strip()
    if not raw.lstrip("-").isdigit():
        errors.append(f"{label} must be a whole number.")
        return None
    value = int(raw)
    if value < minimum:
        errors.append(f"{label} must be {minimum} or more.")
        return None
    return value


def _safe_part(text: str) -> str:
    """One folder name the way the downloader writes it (asset_downloader.sanitize_folder_name)."""
    return re.sub(r'[\\/<>"|?*]', "_", text.replace(":", "-")).strip()


def resolve_open_folder(repo: str, destination: str, folder: str, subfolder: str) -> Optional[str]:
    """The folder the downloader fills for this repository, or its nearest existing parent.

    Built like asset_downloader: <destination>/<folder name>[/<subfolder>], the folder name defaulting to
    "repo (owner)". Nothing downloaded yet (or a subfolder that was never created) falls back to the
    longest part that exists. None when there is no destination or none of it exists.
    """
    destination = destination.strip()
    if not destination:
        return None
    path = os.path.normpath(os.path.expanduser(os.path.expandvars(destination)))
    name = _safe_part(folder.strip() or mapping_manager.build_default_folder(repo))
    parts = [name or "unknown"]
    for part in re.split(r"[\\/]+", subfolder.strip()):
        part = _safe_part(part)
        if part and part not in (".", ".."):
            parts.append(part)
    candidates = [path]
    for part in parts:
        candidates.append(os.path.join(candidates[-1], part))
    for candidate in reversed(candidates):
        if os.path.isdir(candidate):
            return candidate
    return None


def gmail_filter_hint(folder: str) -> str:
    """How to make Gmail put the GitHub mails in `folder` (a label), for the Gmail & GitHub window."""
    return (
        "Gmail needs a filter that fills this folder (Gmail's Settings, Filters and blocked addresses):\n"
        "Matches: from:(notifications@github.com)\n"
        f"Do this: Skip Inbox, Apply label \"{folder}\", Never send it to Spam, Never mark it as important"
    )


def folder_display_name(repo: str, entry: Optional[Mapping[str, Any]]) -> str:
    """The "Name (folder)" of a mapped repository as the Mappings tab shows it ("" when it is not in mapping.json)."""
    if not isinstance(entry, Mapping):
        return ""
    return str(entry.get("folder") or "").strip() or mapping_manager.build_default_folder(repo)


def open_folder_for_entry(repo: str, entry: Optional[Mapping[str, Any]]) -> Optional[str]:
    """What the Mappings tab's "Open folder" button opens, from a mapping.json entry (None: nothing to open)."""
    if not isinstance(entry, Mapping):
        return None
    return resolve_open_folder(
        repo,
        str(entry.get("destination") or ""),
        str(entry.get("folder") or ""),
        str(entry.get("subfolder") or ""),
    )


# The sanity-check choices of the editor: (mapping.json value, text shown in the drop-down).
SANITY_CHOICES = (
    ("any_tag", "Compare with previous release"),
    ("same_tag", "Compare same tag only"),
    ("off", "Off"),
)


def sanity_label(value: Any) -> str:
    """Drop-down text for a stored sanity_check value (missing or unknown = the default, any_tag)."""
    stored = str(value or "").strip().lower()
    for key, label in SANITY_CHOICES:
        if key == stored:
            return label
    return SANITY_CHOICES[0][1]


def sanity_value(label: Any) -> str:
    """The sanity_check value for a drop-down text (anything unknown = the default, any_tag)."""
    for key, text in SANITY_CHOICES:
        if text == str(label or "").strip():
            return key
    return SANITY_CHOICES[0][0]


_REPO_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def github_url(repo: str) -> Optional[str]:
    """The repository's GitHub page ("owner/repo" -> https://github.com/owner/repo), None if the name is odd."""
    repo = repo.strip()
    return f"https://github.com/{repo}" if _REPO_NAME.match(repo) else None


def _directory_warning(label: str, path: str) -> Optional[str]:
    if path and not os.path.isdir(path):
        return f"{label} '{path}' does not exist (yet)."
    return None


def build_repo_changes(form: Mapping[str, Any]) -> FormResult:
    """Validate the Mappings editor and return the fields for update_repository_fields.

    Only editable fields are produced; `name` and the daemon-owned
    last_notification / last_finalized are never included. Duplicate
    detection and other cross-entry rules are left to mapping_manager, which
    raises MappingValidationError on save.
    """
    result = FormResult()
    destination = str(form.get("destination", "")).strip()
    if not destination:
        result.errors.append("Destination must not be empty.")
    else:
        warning = _directory_warning("Destination", destination)
        if warning:
            result.warnings.append(warning)

    limit = parse_int(form.get("limit", ""), "Limit", result.errors, minimum=0)
    recheck = parse_int_list(str(form.get("recheck", "")), "Recheck", result.errors)

    result.changes = {
        "destination": destination,
        "folder": str(form.get("folder", "")).strip(),
        "subfolder": str(form.get("subfolder", "")).strip(),
        "limit": limit,
        "limit_folders": parse_str_list(str(form.get("release_folders", ""))),
        "recheck_intervals": recheck,
        "skiplist": parse_str_list(str(form.get("skiplist", ""))),
        "sanity_check": sanity_value(form.get("sanity_check")),
        "active": bool(form.get("active", True)),
        "shared_destination": bool(form.get("shared_destination", False)),
    }
    return result


def build_new_repo(name: str, destination: str) -> FormResult:
    """Validate the Add-repository dialog; `changes` holds {"repository", "destination"}."""
    result = FormResult()
    repo = str(name or "").strip()
    parts = repo.split("/")
    if len(parts) != 2 or not all(part and not any(c.isspace() for c in part) for part in parts):
        result.errors.append("Repository must look like 'owner/repo'.")
    dest = str(destination or "").strip()
    if not dest:
        result.errors.append("Destination must not be empty.")
    else:
        warning = _directory_warning("Destination", dest)
        if warning:
            result.warnings.append(warning)
    result.changes = {"repository": repo, "destination": dest}
    return result


def build_settings_changes(form: Mapping[str, Any]) -> FormResult:
    """Validate the Settings dialog; `changes` maps dotted config.json keys to values."""
    result = FormResult()
    errors = result.errors

    recheck = parse_int_list(str(form.get("recheck", "")), "Default recheck", errors)
    if not recheck and not errors:
        errors.append("Default recheck needs at least one interval (minutes).")

    max_emails = parse_int(form.get("max_emails"), "Max emails per poll", errors)
    dest_check = parse_int(form.get("dest_check"), "Destination check every N polls", errors)
    default_limit = parse_int(form.get("default_limit"), "Default limit", errors, minimum=0)
    default_subfolder = str(form.get("default_subfolder", "")).strip()
    if not config_manager.is_valid_subfolder(default_subfolder):
        errors.append("Default subfolder must be a relative path below the repository folder (no drive, no '..').")
    interval = parse_int(form.get("interval"), "Polling interval", errors, minimum=MIN_POLL_INTERVAL_SECONDS)
    jitter_min = parse_int(form.get("jitter_min"), "Jitter min", errors)
    jitter_max = parse_int(form.get("jitter_max"), "Jitter max", errors)
    if jitter_min is not None and jitter_max is not None and jitter_min > jitter_max:
        errors.append("Jitter min must not be larger than jitter max.")
    log_max_mb = parse_int(form.get("log_max_mb"), "Max log file size", errors)
    log_keep = parse_int(form.get("log_keep"), "Keep log files", errors)

    download_dir = str(form.get("download_dir", "")).strip()
    if not download_dir:
        errors.append("Default download folder must not be empty.")
    else:
        warning = _directory_warning("Default download folder", download_dir)
        if warning:
            result.warnings.append(warning)

    values = {
        "recheck": recheck,
        "max_emails": max_emails,
        "dest_check": dest_check,
        "default_limit": default_limit,
        "default_subfolder": default_subfolder,
        "polling_enabled": bool(form.get("polling_enabled", config_manager.get_polling_settings({})["enabled"])),
        "interval": interval,
        "jitter_min": jitter_min,
        "jitter_max": jitter_max,
        "download_dir": download_dir,
        "log_enabled": bool(form.get("log_enabled", False)),
        "log_max_mb": log_max_mb,
        "log_keep": log_keep,
        "start_minimized": bool(form.get("start_minimized", False)),
        "start_daemon": bool(form.get("start_daemon", False)),
        "tray": bool(form.get("tray", True)),
        "notifications": bool(form.get("notifications", True)),
        "minimize_to_tray": bool(form.get("minimize_to_tray", config_manager.get_gui_minimize_to_tray({}))),
        "close_to_tray": bool(form.get("close_to_tray", False)),
    }
    result.changes = {SETTINGS_KEYS[key]: value for key, value in values.items()}
    return result
