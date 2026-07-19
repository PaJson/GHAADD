import json
import os

DEFAULT_ENABLE_POLLING = False
DEFAULT_POLL_INTERVAL_SECONDS = 300
DEFAULT_POLL_JITTER_MIN_SECONDS = 5
DEFAULT_POLL_JITTER_MAX_SECONDS = 30
DEFAULT_MAX_EMAILS_TO_PROCESS = 0
DEFAULT_DISABLE_STATE_PERSISTENCE = False

def _as_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() == "true"

def _as_int(value, default):
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

def _get_nested(config, *keys):
    current = config
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current

def load_config():
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
    config = config if config is not None else load_config()
    value = _get_nested(config, "processing", "max_emails_to_process")
    return max(0, _as_int(value, DEFAULT_MAX_EMAILS_TO_PROCESS))

def is_state_persistence_disabled(config=None):
    config = config if config is not None else load_config()
    value = _get_nested(config, "state", "disable_state_persistence")
    return _as_bool(value, DEFAULT_DISABLE_STATE_PERSISTENCE)

def get_default_download_dir(config=None):
    config = config if config is not None else load_config()
    configured_dir = _get_nested(config, "paths", "default_download_dir")
    if configured_dir:
        return configured_dir

    return os.path.join(os.path.expanduser("~"), "Downloads")

def get_polling_settings(config=None):
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