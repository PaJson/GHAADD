import json
import os

DEFAULT_ENABLE_POLLING = False
DEFAULT_POLL_INTERVAL_SECONDS = 300
DEFAULT_POLL_JITTER_MIN_SECONDS = 5
DEFAULT_POLL_JITTER_MAX_SECONDS = 30
DEFAULT_MAX_EMAILS_TO_PROCESS = 0
DEFAULT_DISABLE_STATE_PERSISTENCE = False


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


def _normalize_release_type(release_type):
    """Normalize a release-type label for config key lookups."""
    if release_type is None:
        return ""
    normalized = str(release_type).strip().lower()
    normalized = normalized.replace("_", "-")
    return normalized


def load_config():
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


def is_state_persistence_disabled(config=None):
    """Return whether state persistence is disabled in configuration."""
    config = config if config is not None else load_config()
    value = _get_nested(config, "state", "disable_state_persistence")
    return _as_bool(value, DEFAULT_DISABLE_STATE_PERSISTENCE)


def get_default_download_dir(config=None):
    """Return the default download directory from config or user Downloads."""
    config = config if config is not None else load_config()
    configured_dir = _get_nested(config, "paths", "default_download_dir")
    if configured_dir:
        return configured_dir

    return os.path.join(os.path.expanduser("~"), "Downloads")


def get_download_dir_for_release(repo, release_type=None, config=None):
    """Return the destination directory using repo and release-type overrides.

    Resolution order:
    1) paths.repo_release_type_paths[repo][release_type]
    2) paths.repo_paths[repo]
    3) paths.release_type_paths[release_type]
    4) paths.default_download_dir
    """
    config = config if config is not None else load_config()
    normalized_release_type = _normalize_release_type(release_type)

    repo_release_type_paths = _get_nested(config, "paths", "repo_release_type_paths")
    if isinstance(repo_release_type_paths, dict):
        repo_map = repo_release_type_paths.get(repo)
        if isinstance(repo_map, dict) and normalized_release_type:
            value = repo_map.get(normalized_release_type)
            if isinstance(value, str) and value.strip():
                return value

    repo_paths = _get_nested(config, "paths", "repo_paths")
    if isinstance(repo_paths, dict):
        value = repo_paths.get(repo)
        if isinstance(value, str) and value.strip():
            return value

    release_type_paths = _get_nested(config, "paths", "release_type_paths")
    if isinstance(release_type_paths, dict) and normalized_release_type:
        value = release_type_paths.get(normalized_release_type)
        if isinstance(value, str) and value.strip():
            return value

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
