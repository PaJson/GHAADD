import copy
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from typing import Any, Callable, Dict, Optional, TypedDict, TypeVar

from filelock import FileLock, Timeout

from modules import env_manager
from modules.dry_run_mode import is_dry_run
from modules.warning_types import DEFAULT_SILENCED as DEFAULT_SILENCED_WARNING_TYPES

DEFAULT_ENABLE_POLLING = False
DEFAULT_POLL_INTERVAL_SECONDS = 300
DEFAULT_POLL_JITTER_MIN_SECONDS = 5
DEFAULT_POLL_JITTER_MAX_SECONDS = 30
DEFAULT_MAX_EMAILS_TO_PROCESS = 0
DEFAULT_RECHECK_INTERVALS_MINUTES = [5, 15, 30, 60, 120, 360, 720, 1440]
DEFAULT_DESTINATION_CHECK_EVERY_N_POLLS = 10
DEFAULT_SUBFOLDER = ""  # no subfolder: Release / Pre-release folders sit straight in the repository folder
DEFAULT_GUI_REFRESH_SECONDS = 3.0  # how often the GUI re-reads the data it shows
DEFAULT_GUI_STATUS_MESSAGE_SECONDS = 6.0  # how long a status-bar message stays
GUI_REFRESH_SECONDS_RANGE = (1.0, 60.0)
GUI_STATUS_MESSAGE_SECONDS_RANGE = (2.0, 60.0)
DEFAULT_REPOSITORY_LIMIT = 10
DEFAULT_DISABLE_STATE_PERSISTENCE = False
DEFAULT_GMAIL_FOLDER = "GitHubNotifications"
DEFAULT_ENABLE_TERMINAL_LOG = False
DEFAULT_TERMINAL_LOG_MAX_FILE_MB = 10
DEFAULT_TERMINAL_LOG_KEEP_FILES = 30
DEFAULT_GHAADD_ROOT_FOLDER = "GHAADD"
DEFAULT_PROCESSING_FOLDER = "Processing"
DEFAULT_COMPLETE_FOLDER = "Complete"
DEFAULT_PARTIAL_FOLDER = "Partial"
DEFAULT_LOGS_FOLDER = "Logs"

_CONFIG_LOCK_TIMEOUT_SECONDS = 10.0
_REPLACE_RETRY_ATTEMPTS = 10
_REPLACE_RETRY_DELAY_SECONDS = 0.05

_T = TypeVar("_T")


class FolderSettings(TypedDict):
    ghaadd_root: str
    processing: str
    complete: str
    partial: str
    logs: str


class TerminalLogSettings(TypedDict):
    enabled: bool
    directory: str
    max_file_mb: int
    keep_files: int


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


def _config_file_path() -> str:
    """Return absolute path to config.json beside application files."""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")


def get_config_path() -> str:
    """Absolute path of config.json (for "open the file" buttons)."""
    return _config_file_path()


def load_config() -> Dict[str, Any]:
    """Load and return config.json as a dictionary, or an empty dict on failure."""
    config_path = _config_file_path()
    try:
        with open(config_path, "r", encoding="utf-8") as config_file:
            data = json.load(config_file)
            if isinstance(data, dict):
                return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


class ConfigLockTimeout(RuntimeError):
    """Raised when config.json could not be locked for writing in time."""


class ConfigUnreadableError(RuntimeError):
    """Raised when config.json exists but is not a valid JSON object; nothing was written."""


def _read_config_strict(file_path: str) -> Dict[str, Any]:
    """Return config.json as a dict; {} when missing, ConfigUnreadableError when broken.

    load_config() silently returns {} on a parse error, which is fine for
    readers but would make a writer overwrite a hand-edited file with defaults.
    """
    if not os.path.exists(file_path):
        return {}
    try:
        with open(file_path, "r", encoding="utf-8") as config_file:
            data = json.load(config_file)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigUnreadableError(f"config.json cannot be read ({exc}); fix it by hand first.") from exc
    if not isinstance(data, dict):
        raise ConfigUnreadableError("config.json must contain a JSON object; fix it by hand first.")
    return data


def read_config_strict() -> Dict[str, Any]:
    """Return config.json as a dict ({} when missing); raises ConfigUnreadableError when it is broken.

    Unlike load_config(), a half-saved or invalid file is reported instead of looking like "all defaults".
    """
    return _read_config_strict(_config_file_path())


def _serialize_config(config: Dict[str, Any]) -> str:
    """Return human-formatted config.json text (number arrays kept on one line)."""
    text = json.dumps(config, indent=2, ensure_ascii=False)

    def _collapse(match: "re.Match[str]") -> str:
        return "[" + re.sub(r",\n\s*", ", ", match.group(1)) + "]"

    text = re.sub(r"\[\n\s*(-?\d+(?:,\n\s*-?\d+)*)\n\s*\]", _collapse, text)
    return text + "\n"


def _write_config_atomically(file_path: str, serialized: str) -> None:
    """Write to a temp file beside the target, then swap it in (readers never see a partial file)."""
    temp_handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        dir=os.path.dirname(file_path),
        prefix="config.json.",
        suffix=".tmp",
        delete=False,
    )
    temp_path = temp_handle.name
    try:
        with temp_handle:
            temp_handle.write(serialized)
            temp_handle.flush()
            os.fsync(temp_handle.fileno())

        # On Windows os.replace raises PermissionError while a reader briefly has the target open.
        for attempt in range(_REPLACE_RETRY_ATTEMPTS):
            try:
                os.replace(temp_path, file_path)
                return
            except PermissionError:
                if attempt == _REPLACE_RETRY_ATTEMPTS - 1:
                    raise
                time.sleep(_REPLACE_RETRY_DELAY_SECONDS)
    except BaseException:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise


def update_config(mutator: Callable[[Dict[str, Any]], _T]) -> _T:
    """Apply one change to config.json safely and return the mutator's result.

    The single write path for config.json (GUI/CLI), mirroring
    mapping_manager.update_mapping: under a cross-process lock it re-reads the
    file fresh, lets `mutator` change the dict in place, then writes it
    atomically. The file is not rewritten when nothing changed. The daemon
    loads config once at startup, so changes apply on its next start.

    Raises ConfigLockTimeout when the lock cannot be taken in time and
    ConfigUnreadableError when the existing file is not valid JSON. In dry-run
    mode the mutator runs on the loaded config but nothing is locked or written.
    """
    file_path = _config_file_path()
    if is_dry_run():
        return mutator(load_config())

    lock = FileLock(f"{file_path}.lock", timeout=_CONFIG_LOCK_TIMEOUT_SECONDS)
    try:
        lock.acquire()
    except Timeout as exc:
        raise ConfigLockTimeout(
            f"Timed out after {_CONFIG_LOCK_TIMEOUT_SECONDS:.0f}s waiting for the config.json lock."
        ) from exc

    try:
        config = _read_config_strict(file_path)
        snapshot = copy.deepcopy(config)
        result = mutator(config)
        if config != snapshot:
            _write_config_atomically(file_path, _serialize_config(config))
        return result
    finally:
        lock.release()


def set_config_values(changes: Dict[str, Any]) -> bool:
    """Set several values by dotted path (e.g. "polling.interval_seconds"); True when the file changed.

    Missing sections are created. Callers validate values first (see
    modules/gui_forms.py); this only writes.
    """

    def _apply(config: Dict[str, Any]) -> bool:
        before = copy.deepcopy(config)
        for dotted_key, value in changes.items():
            *parents, leaf = dotted_key.split(".")
            node = config
            for part in parents:
                child = node.get(part)
                if not isinstance(child, dict):
                    child = {}
                    node[part] = child
                node = child
            node[leaf] = value
        return config != before

    return update_config(_apply)


def _hand_edited_defaults():
    """Optional settings that have no field in the Settings window, with the value used when they are missing.

    The getters fall back to these anyway; writing them into config.json only makes them visible and easy to edit.
    """
    return [
        ("gui.refresh_seconds", 3),
        ("gui.status_message_seconds", 6),
        ("gui.tray", True),
        ("gui.notifications", True),
        ("gui.minimize_to_tray", sys.platform != "linux"),
        ("gui.close_to_tray", False),
        ("gui.start_minimized", False),
        ("gui.start_daemon", False),
        ("gui.silenced_warning_types", list(DEFAULT_SILENCED_WARNING_TYPES)),
    ]


def add_missing_defaults() -> bool:
    """Add the optional hand-edited settings that config.json lacks; True when the file changed.

    Existing values (even unusual ones) are never touched, and a missing section is created.
    """

    def _apply(config: Dict[str, Any]) -> bool:
        changed = False
        for dotted_key, value in _hand_edited_defaults():
            *parents, leaf = dotted_key.split(".")
            node = config
            for part in parents:
                child = node.get(part)
                if not isinstance(child, dict):
                    child = {}
                    node[part] = child
                node = child
            if leaf not in node:
                node[leaf] = value
                changed = True
        return changed

    return update_config(_apply)


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


def get_destination_check_every_n_polls(config=None):
    """Return how often (in poll cycles) to verify mapped destinations still exist.

    A value of 0 disables the periodic check.
    """
    config = config if config is not None else load_config()
    value = _get_nested(config, "processing", "destination_check_every_n_polls")
    return max(0, _as_int(value, DEFAULT_DESTINATION_CHECK_EVERY_N_POLLS))


def is_valid_subfolder(value: Any) -> bool:
    """True for a relative sub-path (empty = none) that stays below the repository folder."""
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return True
    if re.match(r"^([A-Za-z]:|[\\/])", text):
        return False
    return ".." not in re.split(r"[\\/]+", text)


def get_default_subfolder(config=None):
    """Return the subfolder given to newly created mapping entries (paths.default_subfolder).

    An empty string (also the default) means no subfolder; a missing or unusable value gives the default.
    """
    config = config if config is not None else load_config()
    value = _get_nested(config, "paths", "default_subfolder")
    if isinstance(value, str) and is_valid_subfolder(value):
        return value.strip()
    return DEFAULT_SUBFOLDER


def get_default_repository_limit(config=None):
    """Return the folder limit given to newly created mapping entries (processing.default_limit).

    0 switches the limit check off for new entries; a missing or negative value gives 10.
    """
    config = config if config is not None else load_config()
    value = _as_int(_get_nested(config, "processing", "default_limit"), DEFAULT_REPOSITORY_LIMIT)
    return value if value >= 0 else DEFAULT_REPOSITORY_LIMIT


def _gui_seconds(config, key, default, bounds):
    """A number of seconds from the "gui" section, kept inside bounds; anything unusable gives the default."""
    value = _get_nested(config, "gui", key)
    if isinstance(value, bool) or value is None:
        return default
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return default
    if seconds != seconds:  # NaN
        return default
    return min(max(seconds, bounds[0]), bounds[1])


def get_gui_refresh_seconds(config=None):
    """Seconds between the GUI's data refreshes (gui.refresh_seconds, default 3, 1 to 60)."""
    config = config if config is not None else load_config()
    return _gui_seconds(config, "refresh_seconds", DEFAULT_GUI_REFRESH_SECONDS, GUI_REFRESH_SECONDS_RANGE)


def get_gui_status_message_seconds(config=None):
    """Seconds a GUI status-bar message stays (gui.status_message_seconds, default 6, 2 to 60)."""
    config = config if config is not None else load_config()
    return _gui_seconds(
        config, "status_message_seconds", DEFAULT_GUI_STATUS_MESSAGE_SECONDS, GUI_STATUS_MESSAGE_SECONDS_RANGE
    )


def _gui_flag(config, key, default):
    """A true/false setting from the "gui" section; anything that is not a real boolean gives the default."""
    value = _get_nested(config, "gui", key)
    return value if isinstance(value, bool) else default


def get_gui_tray_enabled(config=None):
    """Use a system tray icon (gui.tray, default true; it also needs the optional pystray + Pillow packages)."""
    config = config if config is not None else load_config()
    return _gui_flag(config, "tray", True)


def get_gui_minimize_to_tray(config=None):
    """Minimizing hides the window in the tray (gui.minimize_to_tray; default true on Windows/macOS, false on Linux
    where many desktops show no tray icon and the window would be lost)."""
    config = config if config is not None else load_config()
    return _gui_flag(config, "minimize_to_tray", sys.platform != "linux")


def get_gui_close_to_tray(config=None):
    """Closing the window hides it in the tray instead of ending the GUI (gui.close_to_tray, default false)."""
    config = config if config is not None else load_config()
    return _gui_flag(config, "close_to_tray", False)


def get_gui_notifications_enabled(config=None):
    """Show tray notifications for new warnings, failed jobs, unmapped repositories and a stopped daemon (gui.notifications, default true)."""
    config = config if config is not None else load_config()
    return _gui_flag(config, "notifications", True)


def get_gui_silenced_warning_types(config=None) -> list[str]:
    """Warning types that never pop up a notification (gui.silenced_warning_types; default API and LIMIT).

    The Warnings tab still shows them. A value that is not a list of strings gives the default.
    """
    config = config if config is not None else load_config()
    value = _get_nested(config, "gui", "silenced_warning_types")
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return sorted({item.strip().upper() for item in value if item.strip()})
    return list(DEFAULT_SILENCED_WARNING_TYPES)


def get_gui_start_minimized(config=None):
    """Open the GUI minimized (as the minimize button does: in the tray, or on the taskbar) instead of showing the window (gui.start_minimized, default false)."""
    config = config if config is not None else load_config()
    return _gui_flag(config, "start_minimized", False)


def get_gui_start_daemon(config=None):
    """Start the polling daemon when the GUI starts and none is running (gui.start_daemon, default false)."""
    config = config if config is not None else load_config()
    return _gui_flag(config, "start_daemon", False)


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


def get_terminal_log_settings(config: Optional[Dict[str, Any]] = None) -> TerminalLogSettings:
    """Return normalized terminal transcript mirroring settings with safe defaults."""
    config = config if config is not None else load_config()
    enabled = _get_nested(config, "terminal_log", "enabled")
    folder_settings = get_folder_settings(config)
    log_dir = os.path.join(
        get_default_download_dir(config),
        folder_settings["ghaadd_root"],
        folder_settings["logs"],
    )
    normalized_log_dir = _normalize_configured_path(log_dir)

    return {
        "enabled": _as_bool(enabled, DEFAULT_ENABLE_TERMINAL_LOG),
        "directory": normalized_log_dir,
        "max_file_mb": max(
            0, _as_int(_get_nested(config, "terminal_log", "max_file_mb"), DEFAULT_TERMINAL_LOG_MAX_FILE_MB)
        ),
        "keep_files": max(
            0, _as_int(_get_nested(config, "terminal_log", "keep_files"), DEFAULT_TERMINAL_LOG_KEEP_FILES)
        ),
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


def get_config_fingerprint(config: Optional[Dict[str, Any]] = None) -> str:
    """Return a short hash of the *effective* settings the daemon uses.

    The daemon publishes it at startup and the GUI compares it with the current
    config.json to tell whether a restart is needed. It is built from the
    normalized getters, so unrelated edits (the GUI's window size, formatting,
    a default written out explicitly) do not change it.
    """
    config = config if config is not None else load_config()
    effective = {
        "polling": get_polling_settings(config),
        "recheck": get_recheck_intervals_minutes(config),
        "max_emails": get_max_emails_to_process(config),
        "destination_check": get_destination_check_every_n_polls(config),
        "download_dirs": get_all_download_dirs(config),
        "terminal_log": get_terminal_log_settings(config),
        "folders": get_folder_settings(config),
        "gmail_folder": get_gmail_folder(config),
        "credentials": env_manager.credentials_fingerprint(),  # a hash of the .env login: a changed login needs a restart too
        "state_persistence_disabled": is_state_persistence_disabled(config),
    }
    payload = json.dumps(effective, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


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
