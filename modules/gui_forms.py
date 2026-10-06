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

from modules import mapping_manager

# config.json keys (dotted path) behind the Settings dialog.
SETTINGS_KEYS = {
    "recheck": "processing.recheck_intervals_minutes",
    "max_emails": "processing.max_emails_to_process",
    "dest_check": "processing.destination_check_every_n_polls",
    "interval": "polling.interval_seconds",
    "jitter_min": "polling.jitter_min_seconds",
    "jitter_max": "polling.jitter_max_seconds",
    "download_dir": "paths.default_download_dir",
    "log_enabled": "terminal_log.enabled",
    "log_max_mb": "terminal_log.max_file_mb",
    "log_keep": "terminal_log.keep_files",
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


def parse_str_list(text: str) -> list[str]:
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


def resolve_open_folder(repo: str, destination: str, foldername: str, subfolder: str) -> Optional[str]:
    """The folder the downloader fills for this repository, or its nearest existing parent.

    Built like asset_downloader: <destination>/<folder name>[/<subfolder>], the folder name defaulting to
    "repo (owner)". Nothing downloaded yet (or a subfolder that was never created) falls back to the
    longest part that exists. None when there is no destination or none of it exists.
    """
    destination = destination.strip()
    if not destination:
        return None
    path = os.path.normpath(os.path.expanduser(os.path.expandvars(destination)))
    name = _safe_part(foldername.strip() or mapping_manager.build_default_foldername(repo))
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
    last_notification_seen / last_finalized are never included. Duplicate
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
        "foldername": str(form.get("foldername", "")).strip(),
        "subfolder": str(form.get("subfolder", "")).strip(),
        "limit": limit,
        "limit_release_type_folders": parse_str_list(str(form.get("release_folders", ""))),
        "recheck_intervals_minutes": recheck,
        "skiplist": parse_str_list(str(form.get("skiplist", ""))),
        "paused": bool(form.get("paused", False)),
    }
    return result


def build_new_repo(name: str, destination: str) -> FormResult:
    """Validate the Add-repository dialog; `changes` holds {"name", "destination"}."""
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
    result.changes = {"name": repo, "destination": dest}
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
    interval = parse_int(form.get("interval"), "Polling interval", errors, minimum=MIN_POLL_INTERVAL_SECONDS)
    jitter_min = parse_int(form.get("jitter_min"), "Jitter min", errors)
    jitter_max = parse_int(form.get("jitter_max"), "Jitter max", errors)
    if jitter_min is not None and jitter_max is not None and jitter_min > jitter_max:
        errors.append("Jitter min must not be larger than jitter max.")
    log_max_mb = parse_int(form.get("log_max_mb"), "Max log file size", errors)
    log_keep = parse_int(form.get("log_keep"), "Keep log files", errors)

    download_dir = str(form.get("download_dir", "")).strip()
    if not download_dir:
        errors.append("Default download dir must not be empty.")
    else:
        warning = _directory_warning("Default download dir", download_dir)
        if warning:
            result.warnings.append(warning)

    values = {
        "recheck": recheck,
        "max_emails": max_emails,
        "dest_check": dest_check,
        "interval": interval,
        "jitter_min": jitter_min,
        "jitter_max": jitter_max,
        "download_dir": download_dir,
        "log_enabled": bool(form.get("log_enabled", False)),
        "log_max_mb": log_max_mb,
        "log_keep": log_keep,
    }
    result.changes = {SETTINGS_KEYS[key]: value for key, value in values.items()}
    return result
