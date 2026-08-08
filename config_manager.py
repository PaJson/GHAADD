import json
import os
from typing import Any, Dict, Optional, TypedDict

DEFAULT_ENABLE_POLLING = False
DEFAULT_POLL_INTERVAL_SECONDS = 300
DEFAULT_POLL_JITTER_MIN_SECONDS = 5
DEFAULT_POLL_JITTER_MAX_SECONDS = 30
DEFAULT_MAX_EMAILS_TO_PROCESS = 0
DEFAULT_RECHECK_INTERVALS_MINUTES = [5, 15, 60, 1440]
DEFAULT_DISABLE_STATE_PERSISTENCE = False
DEFAULT_GMAIL_FOLDER = "GitHubNotifications"
DEFAULT_ENABLE_LOGGING = False
DEFAULT_GHAADD_ROOT_FOLDER = "GHAADD"
DEFAULT_PROCESSING_FOLDER = "Processing"
DEFAULT_COMPLETE_FOLDER = "Complete"
DEFAULT_PARTIAL_FOLDER = "Partial"
DEFAULT_LOGS_FOLDER = "Logs"


class FolderSettings(TypedDict):
    ghaadd_root: str
    processing: str
    complete: str
    partial: str
    logs: str


class LoggingSettings(TypedDict):
    enabled: bool
    directory: str


def _as_bool(value, default=False):
    """Return a boolean parsed from value, or default when value is unset."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() == "true"


def _as_int(value, default):
    """Return value parsed as int, or default when parsing fails."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _get_nested(config, *keys):
    """Return a nested dictionary value by key path, or None when missing."""
    current = config
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _normalize_configured_path(path_value: str) -> str:
    """Normalize configured path values and fix Windows drive-root shorthand."""
    normalized = path_value.strip()
    if not normalized:
        return ""

    # On Windows, "D:" is drive-relative (not drive-rooted). Treat it as "D:\\".
    if os.name == "nt":
        drive, tail = os.path.splitdrive(normalized)
        if drive and tail == "":
            return drive + "\\"

    return normalized


def load_config() -> Dict[str, Any]:
    """Load and return config.json as a dictionary, or an empty dict on failure."""
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as config_file:
            data = json.load(config_file)
            if isinstance(data, dict):
                return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def get_max_emails_to_process(config=None):
    """Return the configured maximum number of emails to process."""
    config = config if config is not None else load_config()
    value = _get_nested(config, "processing", "max_emails_to_process")
    return max(0, _as_int(value, DEFAULT_MAX_EMAILS_TO_PROCESS))


def get_recheck_intervals_minutes(config=None):
    """Return queue re-check intervals in minutes, normalized and validated."""
    config = config if config is not None else load_config()

    value = _get_nested(config, "processing", "recheck_intervals_minutes")

    if not isinstance(value, list):
        return list(DEFAULT_RECHECK_INTERVALS_MINUTES)

    normalized = []
    for item in value:
        minutes = _as_int(item, None)
        if minutes is None or minutes <= 0:
            continue
        if minutes not in normalized:
            normalized.append(minutes)

    if not normalized:
        return list(DEFAULT_RECHECK_INTERVALS_MINUTES)

    return normalized


def is_state_persistence_disabled(config=None):
    """Return whether state persistence is disabled in configuration."""
    config = config if config is not None else load_config()
    value = _get_nested(config, "state", "disable_state_persistence")
    return _as_bool(value, DEFAULT_DISABLE_STATE_PERSISTENCE)


def get_default_download_dir(config: Optional[Dict[str, Any]] = None) -> str:
    """Return the default download directory from config or user Downloads."""
    config = config if config is not None else load_config()
    configured_dir = _get_nested(config, "paths", "default_download_dir")
    if isinstance(configured_dir, str) and configured_dir.strip():
        return _normalize_configured_path(configured_dir)

    return os.path.join(os.path.expanduser("~"), "Downloads")


def get_all_download_dirs(config: Optional[Dict[str, Any]] = None) -> list[str]:
    """Return all configured download roots used for staging/Complete folders."""
    config = config if config is not None else load_config()

    unique_dirs: list[str] = []

    def add_path(candidate: Any) -> None:
        if not isinstance(candidate, str):
            return
        normalized = _normalize_configured_path(candidate)
        if not normalized:
            return
        if normalized not in unique_dirs:
            unique_dirs.append(normalized)

    add_path(get_default_download_dir(config))
    return unique_dirs


def get_gmail_folder(config=None):
    """Return the Gmail folder used for GitHub notification processing."""
    config = config if config is not None else load_config()
    value = _get_nested(config, "mailbox", "folder")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return DEFAULT_GMAIL_FOLDER


def get_logging_settings(config: Optional[Dict[str, Any]] = None) -> LoggingSettings:
    """Return normalized logging settings with safe defaults."""
    config = config if config is not None else load_config()
    enabled = _get_nested(config, "logging", "enabled")
    folder_settings = get_folder_settings(config)
    log_dir = os.path.join(
        get_default_download_dir(config),
        folder_settings["ghaadd_root"],
        folder_settings["logs"],
    )
    normalized_log_dir = _normalize_configured_path(log_dir)

    return {
        "enabled": _as_bool(enabled, DEFAULT_ENABLE_LOGGING),
        "directory": normalized_log_dir,
    }


def get_folder_settings(config: Optional[Dict[str, Any]] = None) -> FolderSettings:
    """Return configured folder names used under the GHAADD working area."""
    config = config if config is not None else load_config()

    folders = _get_nested(config, "folders")
    folders = folders if isinstance(folders, dict) else {}

    def _folder_name(key: str, default_value: str) -> str:
        value = folders.get(key)
        if isinstance(value, str):
            candidate = value.strip().strip("\\/")
            candidate = candidate.replace("/", "_").replace("\\", "_")
            candidate = candidate.strip(".")
            if candidate:
                return candidate
        return default_value

    return {
        "ghaadd_root": _folder_name("ghaadd_root", DEFAULT_GHAADD_ROOT_FOLDER),
        "processing": _folder_name("processing", DEFAULT_PROCESSING_FOLDER),
        "complete": _folder_name("complete", DEFAULT_COMPLETE_FOLDER),
        "partial": _folder_name("partial", DEFAULT_PARTIAL_FOLDER),
        "logs": _folder_name("logs", DEFAULT_LOGS_FOLDER),
    }


def get_download_dir_for_release(repo, release_type=None, config=None):
    """Return the configured default download directory for a release."""
    return get_default_download_dir(config)


def get_polling_settings(config=None):
    """Return normalized polling settings with safe defaults."""
    config = config if config is not None else load_config()

    enabled = _get_nested(config, "polling", "enabled")

    interval_seconds = _get_nested(config, "polling", "interval_seconds")

    jitter_min_seconds = _get_nested(config, "polling", "jitter_min_seconds")

    jitter_max_seconds = _get_nested(config, "polling", "jitter_max_seconds")

    return {
        "enabled": _as_bool(enabled, DEFAULT_ENABLE_POLLING),
        "interval_seconds": max(0, _as_int(interval_seconds, DEFAULT_POLL_INTERVAL_SECONDS)),
        "jitter_min_seconds": max(0, _as_int(jitter_min_seconds, DEFAULT_POLL_JITTER_MIN_SECONDS)),
        "jitter_max_seconds": max(0, _as_int(jitter_max_seconds, DEFAULT_POLL_JITTER_MAX_SECONDS)),
    }
