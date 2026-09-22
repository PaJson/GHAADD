import json
import os
import re
import shutil
from datetime import datetime
from typing import Any, Optional, TypedDict

from modules.config_manager import get_recheck_intervals_minutes
from modules.dry_run_mode import is_dry_run
from modules.lifecycle_logger import log_warning


DEFAULT_LIMIT_RELEASE_TYPE_FOLDERS = ["Release", "Pre-release"]
DEFAULT_SUBFOLDER = "@GitHub"
DEFAULT_LIMIT = 10
_MAPPING_FIELD_ORDER = (
    "name",
    "destination",
    "foldername",
    "subfolder",
    "limit",
    "limit_release_type_folders",
    "recheck_intervals_minutes",
    "skiplist",
    "last_notification_seen",
    "last_finalized",
    "paused",
)


_BACKED_UP_INVALID_MAPPING_SIGNATURES: set[tuple[str, int, int]] = set()


class MappingValidationResult(TypedDict):
    ok: bool
    errors: list[str]
    warnings: list[str]


def _mapping_file_path() -> str:
    """Return absolute path to mapping.json beside application files."""
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(app_dir, "mapping.json")


def _backup_invalid_mapping_file(file_path: str, reason: str) -> None:
    """Copy an invalid mapping file aside once per file version."""
    try:
        file_stat = os.stat(file_path)
    except OSError:
        return

    signature = (file_path, int(file_stat.st_mtime_ns), int(file_stat.st_size))
    if signature in _BACKED_UP_INVALID_MAPPING_SIGNATURES:
        return

    backup_path = os.path.join(
        os.path.dirname(file_path),
        f"mapping.json_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}",
    )

    try:
        shutil.copy2(file_path, backup_path)
    except OSError as exc:
        log_warning(
            "MAPPING",
            f"mapping.json could not be parsed ({reason}) and the backup copy failed: {exc}",
        )
        return

    _BACKED_UP_INVALID_MAPPING_SIGNATURES.add(signature)
    log_warning(
        "MAPPING",
        f"mapping.json could not be parsed ({reason}). Backed up the broken file to '{backup_path}'.",
    )


def _default_mapping_payload() -> dict[str, list[dict[str, Any]]]:
    """Return default mapping payload shape."""
    return {"repositories": []}


def _normalize_mapping_payload(payload: Any) -> dict[str, list[dict[str, Any]]]:
    """Normalize loaded mapping payload to expected structure."""
    if not isinstance(payload, dict):
        return _default_mapping_payload()

    repositories = payload.get("repositories")
    if not isinstance(repositories, list):
        repositories = []

    normalized_repositories = [
        item for item in repositories if isinstance(item, dict)
    ]
    return {"repositories": normalized_repositories}


def _normalize_recheck_intervals_minutes(value: Any) -> list[int]:
    """Normalize repository recheck intervals as an explicit override list."""
    if not isinstance(value, list):
        return []

    normalized: list[int] = []
    for item in value:
        if isinstance(item, bool):
            continue
        try:
            minutes = int(item)
        except (TypeError, ValueError):
            continue
        if minutes <= 0:
            continue
        if minutes not in normalized:
            normalized.append(minutes)

    return normalized


def _normalize_limit_release_type_folders(value: Any) -> list[str]:
    """Normalize release-type folder override list for limit counting."""
    if not isinstance(value, list):
        return []

    normalized: list[str] = []
    seen_lower: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue

        candidate = item.strip()
        if not candidate:
            continue

        candidate_lower = candidate.lower()
        if candidate_lower in seen_lower:
            continue

        seen_lower.add(candidate_lower)
        normalized.append(candidate)

    return normalized


def _default_limit_release_type_folders() -> list[str]:
    """Return the default release-type folder list for limit counting."""
    return list(DEFAULT_LIMIT_RELEASE_TYPE_FOLDERS)


def _normalize_skiplist(value: Any) -> list[str]:
    """Normalize repository release-type skiplist entries."""
    return _normalize_limit_release_type_folders(value)


def _collapse_recheck_intervals_arrays(serialized_json: str) -> str:
    """Render recheck_intervals_minutes arrays on a single line for readability."""
    pattern = re.compile(
        r'("recheck_intervals_minutes"\s*:\s*)\[\n(?P<body>(?:\s*\d+\s*,?\n)*)\s*\]',
        re.MULTILINE,
    )

    def _replace(match: re.Match[str]) -> str:
        body = match.group("body") or ""
        values = [
            line.strip().rstrip(",")
            for line in body.splitlines()
            if line.strip()
        ]
        return f"{match.group(1)}[{', '.join(values)}]"

    return pattern.sub(_replace, serialized_json)


def _collapse_limit_release_type_folders_arrays(serialized_json: str) -> str:
    """Render limit_release_type_folders arrays on a single line for readability."""
    pattern = re.compile(
        r'("limit_release_type_folders"\s*:\s*)\[\n(?P<body>(?:\s*"[^"]+"\s*,?\n)*)\s*\]',
        re.MULTILINE,
    )

    def _replace(match: re.Match[str]) -> str:
        body = match.group("body") or ""
        values = [
            line.strip().rstrip(",")
            for line in body.splitlines()
            if line.strip()
        ]
        return f"{match.group(1)}[{', '.join(values)}]"

    return pattern.sub(_replace, serialized_json)


def _collapse_skiplist_arrays(serialized_json: str) -> str:
    """Render skiplist arrays on a single line for readability."""
    pattern = re.compile(
        r'("skiplist"\s*:\s*)\[\n(?P<body>(?:\s*"[^"]+"\s*,?\n)*)\s*\]',
        re.MULTILINE,
    )

    def _replace(match: re.Match[str]) -> str:
        body = match.group("body") or ""
        values = [
            line.strip().rstrip(",")
            for line in body.splitlines()
            if line.strip()
        ]
        return f"{match.group(1)}[{', '.join(values)}]"

    return pattern.sub(_replace, serialized_json)


def load_mapping() -> dict[str, list[dict[str, Any]]]:
    """Load mapping.json with safe fallback payload."""
    file_path = _mapping_file_path()
    if not os.path.exists(file_path):
        return _default_mapping_payload()

    try:
        with open(file_path, "r", encoding="utf-8") as mapping_file:
            payload = json.load(mapping_file)
    except OSError:
        return _default_mapping_payload()
    except json.JSONDecodeError as exc:
        _backup_invalid_mapping_file(file_path, str(exc))
        return _default_mapping_payload()

    return _normalize_mapping_payload(payload)


def load_mapping_raw() -> Any:
    """Load raw mapping JSON payload without normalization."""
    file_path = _mapping_file_path()
    if not os.path.exists(file_path):
        return None

    try:
        with open(file_path, "r", encoding="utf-8") as mapping_file:
            return json.load(mapping_file)
    except OSError:
        return None
    except json.JSONDecodeError as exc:
        _backup_invalid_mapping_file(file_path, str(exc))
        return None


def _repository_sort_key(entry: dict[str, Any]) -> tuple[str, str]:
    """Return a stable sort key for a repository mapping entry by repo name only."""
    name_value = str(entry.get("name") or entry.get("foldername") or "").strip()
    repo_name = name_value.split("/", 1)[1] if "/" in name_value else name_value
    return (repo_name.lower(), name_value.lower())


def _order_mapping_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Return a consistently ordered mapping entry."""
    ordered_entry = dict(entry)
    ordered_entry.setdefault("paused", False)

    return {
        key: ordered_entry[key]
        for key in _MAPPING_FIELD_ORDER
        if key in ordered_entry
    } | {
        key: ordered_entry[key]
        for key in sorted(ordered_entry.keys())
        if key not in _MAPPING_FIELD_ORDER
    }


def save_mapping(mapping_payload: dict[str, list[dict[str, Any]]]) -> None:
    """Persist mapping payload to mapping.json."""
    if is_dry_run():
        return

    file_path = _mapping_file_path()
    normalized_payload = _normalize_mapping_payload(mapping_payload)
    normalized_payload["repositories"] = sorted(
        [_order_mapping_entry(entry) for entry in normalized_payload["repositories"]],
        key=_repository_sort_key,
    )

    serialized_payload = json.dumps(normalized_payload, indent=2, ensure_ascii=True)
    serialized_payload = _collapse_recheck_intervals_arrays(serialized_payload)
    serialized_payload = _collapse_limit_release_type_folders_arrays(serialized_payload)
    serialized_payload = _collapse_skiplist_arrays(serialized_payload)
    with open(file_path, "w", encoding="utf-8") as mapping_file:
        mapping_file.write(serialized_payload)
        mapping_file.write("\n")


def build_default_foldername(repo: str) -> str:
    """Build default display name like 'duckstation (stenzek)' from owner/repo."""
    if not repo:
        return "unknown (unknown)"

    owner, repo_name = (repo.split("/", 1) + [""])[:2]
    owner = owner.strip() or "unknown"
    repo_name = (repo_name.strip() or owner)
    return f"{repo_name} ({owner})"


def _is_same_repository_identity(
    entry: dict[str, Any],
    repo: str,
) -> bool:
    """Return True when entry matches repository identity."""
    entry_name = str(entry.get("name") or "").strip()
    return entry_name.lower() == repo.lower()


def get_repository_mapping(repo: str) -> Optional[dict[str, Any]]:
    """Return the matching repository mapping entry, or None when missing."""
    normalized_repo = str(repo or "").strip()
    if not normalized_repo:
        return None

    mapping_payload = load_mapping()
    repositories = mapping_payload.get("repositories", [])
    for entry in repositories:
        if not isinstance(entry, dict):
            continue
        if _is_same_repository_identity(entry, normalized_repo):
            return entry

    return None


def is_repository_paused(repo: str) -> bool:
    """Return whether a repository is explicitly paused in mapping.json."""
    mapping_entry = get_repository_mapping(repo)
    return bool(isinstance(mapping_entry, dict) and mapping_entry.get("paused") is True)


def get_repository_recheck_intervals_minutes(repo: str) -> list[int]:
    """Return per-repository recheck intervals when configured; else fall back to config."""
    mapping_entry = get_repository_mapping(repo)
    if not isinstance(mapping_entry, dict):
        return list(get_recheck_intervals_minutes())

    intervals_value = mapping_entry.get("recheck_intervals_minutes")
    if not isinstance(intervals_value, list):
        return list(get_recheck_intervals_minutes())

    normalized: list[int] = []
    for item in intervals_value:
        if isinstance(item, bool):
            continue
        try:
            minutes = int(item)
        except (TypeError, ValueError):
            continue
        if minutes <= 0:
            continue
        if minutes not in normalized:
            normalized.append(minutes)

    if normalized:
        return normalized

    return list(get_recheck_intervals_minutes())


def get_repository_limit_release_type_folders(repo: str) -> list[str]:
    """Return repository folder names used for destination limit counting overrides."""
    mapping_entry = get_repository_mapping(repo)
    if not isinstance(mapping_entry, dict):
        return []

    return _normalize_limit_release_type_folders(
        mapping_entry.get("limit_release_type_folders")
    )


def get_repository_skiplist(repo: str) -> list[str]:
    """Return repository release types (e.g. 'Release', 'Pre-release') that should be skipped."""
    mapping_entry = get_repository_mapping(repo)
    if not isinstance(mapping_entry, dict):
        return []

    return _normalize_skiplist(mapping_entry.get("skiplist"))


def is_release_type_skipped(repo: str, release_type: Optional[str]) -> bool:
    """Return True when the given release type is in the repository's skiplist."""
    normalized_release_type = str(release_type or "Release").strip().lower()
    skiplist = get_repository_skiplist(repo)
    return any(entry.lower() == normalized_release_type for entry in skiplist)


def _current_mapping_stamp() -> str:
    """Return a mapping timestamp in YYYY-MM-DD_HH-MM format."""
    return datetime.now().strftime("%Y-%m-%d_%H-%M")


def upsert_repository_mapping(
    repo: str,
    notification_seen_stamp: Optional[str] = None,
) -> tuple[bool, bool]:
    """Upsert one repository mapping and return (created, updated)."""
    mapping_payload = load_mapping()
    repositories = mapping_payload["repositories"]
    effective_notification_seen_stamp = (
        notification_seen_stamp or _current_mapping_stamp()
    )

    for entry in repositories:
        if not _is_same_repository_identity(entry, repo):
            continue

        updated = False
        if entry.get("last_notification_seen") != effective_notification_seen_stamp:
            entry["last_notification_seen"] = effective_notification_seen_stamp
            updated = True

        if "last_finalized" not in entry:
            entry["last_finalized"] = ""
            updated = True

        if "recheck_intervals_minutes" not in entry:
            entry["recheck_intervals_minutes"] = []
            updated = True

        if "limit_release_type_folders" not in entry:
            entry["limit_release_type_folders"] = _default_limit_release_type_folders()
            updated = True

        if "skiplist" not in entry:
            entry["skiplist"] = []
            updated = True

        if "paused" not in entry:
            entry["paused"] = False
            updated = True

        if updated:
            save_mapping(mapping_payload)
        return (False, updated)

    skeleton_entry = {
        "name": repo,
        "destination": "",
        "foldername": build_default_foldername(repo),
        "subfolder": DEFAULT_SUBFOLDER,
        "limit": DEFAULT_LIMIT,
        "limit_release_type_folders": _default_limit_release_type_folders(),
        "recheck_intervals_minutes": [],
        "skiplist": [],
        "last_notification_seen": effective_notification_seen_stamp,
        "last_finalized": "",
        "paused": False,
    }
    repositories.append(skeleton_entry)
    save_mapping(mapping_payload)
    log_warning(
        "MAPPING",
        f"Repository '{repo}' was added without a configured destination; it will use default routing until mapped.",
    )
    return (True, False)


def mark_repository_finalized(
    repo: str,
    finalized_stamp: Optional[str] = None,
) -> bool:
    """Record when a repository release was moved to its complete destination."""
    normalized_repo = str(repo or "").strip()
    if not normalized_repo:
        return False

    mapping_payload = load_mapping()
    finalized_value = finalized_stamp or _current_mapping_stamp()
    for entry in mapping_payload["repositories"]:
        if not _is_same_repository_identity(entry, normalized_repo):
            continue
        if entry.get("last_finalized") == finalized_value:
            return False
        entry["last_finalized"] = finalized_value
        save_mapping(mapping_payload)
        return True

    return False


def _normalize_destination_root_for_comparison(destination: str) -> str:
    """Return a comparison-safe form of a destination root path."""
    normalized = os.path.normpath(os.path.expanduser(os.path.expandvars(destination.strip())))
    return normalized.lower() if os.name == "nt" else normalized


def _normalize_folder_segment_for_comparison(value: str) -> str:
    """Return a comparison-safe form of a foldername/subfolder segment."""
    segments = [segment for segment in re.split(r"[\\/]+", value.strip()) if segment not in ("", ".", "..")]
    joined = "/".join(segments)
    return joined.lower() if os.name == "nt" else joined


def _build_destination_comparison_key(
    destination: str,
    foldername: Any,
    subfolder: Any,
    repo_name: str,
) -> tuple[str, str, str]:
    """Return a normalized (destination, foldername, subfolder) key for duplicate checks."""
    destination_norm = _normalize_destination_root_for_comparison(destination)

    foldername_value = foldername if isinstance(foldername, str) and foldername.strip() else build_default_foldername(repo_name)
    foldername_norm = _normalize_folder_segment_for_comparison(foldername_value)

    subfolder_value = subfolder if isinstance(subfolder, str) else ""
    subfolder_norm = _normalize_folder_segment_for_comparison(subfolder_value)

    return (destination_norm, foldername_norm, subfolder_norm)


def find_missing_mapped_destinations() -> list[dict[str, str]]:
    """Return mapped repository entries whose destination folder no longer exists."""
    mapping_payload = load_mapping()
    missing_entries: list[dict[str, str]] = []

    for entry in mapping_payload.get("repositories", []):
        if not isinstance(entry, dict):
            continue

        destination = entry.get("destination")
        if not isinstance(destination, str) or not destination.strip():
            continue

        resolved_destination = os.path.normpath(
            os.path.expanduser(os.path.expandvars(destination.strip()))
        )
        if os.path.isdir(resolved_destination):
            continue

        missing_entries.append(
            {
                "name": str(entry.get("name") or "unknown"),
                "destination": destination.strip(),
                "resolved_destination": resolved_destination,
            }
        )

    return missing_entries


def warn_about_missing_mapped_destinations() -> int:
    """Log a warning for each mapped destination folder that no longer exists on disk."""
    missing_entries = find_missing_mapped_destinations()
    for missing_entry in missing_entries:
        warning_message = (
            f"Mapped destination for '{missing_entry['name']}' no longer exists: "
            f"{missing_entry['resolved_destination']} "
            "(folder may have been moved, renamed, or deleted; update mapping.json or recreate it)."
        )
        print(f"⚠️ {warning_message}")
        log_warning("MAPPING", warning_message)

    return len(missing_entries)


def validate_mapping_schema() -> MappingValidationResult:
    """Validate mapping.json structure and return errors/warnings."""
    errors: list[str] = []
    warnings: list[str] = []

    raw_payload = load_mapping_raw()
    if raw_payload is None:
        errors.append("mapping.json is missing or contains invalid JSON.")
        return {
            "ok": False,
            "errors": errors,
            "warnings": warnings,
        }

    if not isinstance(raw_payload, dict):
        errors.append("Root JSON value must be an object.")
        return {
            "ok": False,
            "errors": errors,
            "warnings": warnings,
        }

    repositories = raw_payload.get("repositories")
    if not isinstance(repositories, list):
        errors.append("'repositories' must be an array.")
        return {
            "ok": False,
            "errors": errors,
            "warnings": warnings,
        }

    seen_names: dict[str, int] = {}
    seen_destinations: dict[tuple[str, str, str], dict[str, Any]] = {}
    for index, entry in enumerate(repositories, 1):
        location = f"repositories[{index}]"

        if not isinstance(entry, dict):
            errors.append(f"{location} must be an object.")
            continue

        display_location = location
        display_suffix = ""
        name = entry.get("name")
        if isinstance(name, str) and name.strip():
            display_suffix = f" ({name.strip()})"

        destination_value = entry.get("destination")
        if isinstance(destination_value, str) and destination_value.strip():
            repo_name_for_key = str(name) if isinstance(name, str) else ""
            destination_key = _build_destination_comparison_key(
                destination_value,
                entry.get("foldername"),
                entry.get("subfolder"),
                repo_name_for_key,
            )
            foldername_value = entry.get("foldername")
            effective_foldername = (
                foldername_value.strip()
                if isinstance(foldername_value, str) and foldername_value.strip()
                else build_default_foldername(repo_name_for_key)
            )
            subfolder_value = entry.get("subfolder")
            display_path_parts = [destination_value.strip(), effective_foldername]
            if isinstance(subfolder_value, str) and subfolder_value.strip():
                display_path_parts.append(subfolder_value.strip())

            destination_group = seen_destinations.setdefault(
                destination_key,
                {"repo_names": [], "display_path": os.path.join(*display_path_parts)},
            )
            destination_group["repo_names"].append(
                str(name).strip() if isinstance(name, str) and name.strip() else f"repositories[{index}]"
            )

        unknown_keys = sorted(
            key
            for key in entry.keys()
            if key
            not in {
                "name",
                "destination",
                "foldername",
                "subfolder",
                "limit",
                "recheck_intervals_minutes",
                "limit_release_type_folders",
                "skiplist",
                "last_notification_seen",
                "last_finalized",
                "paused",
            }
        )
        for key in unknown_keys:
            warnings.append(f"{display_location} has unknown key '{key}'.{display_suffix}")

        if not isinstance(name, str) or not name.strip():
            errors.append(f"{location}.name must be a non-empty string.")
        else:
            normalized_name = name.strip().lower()
            if normalized_name in seen_names:
                other_index = seen_names[normalized_name]
                errors.append(
                    f"{display_location}.name duplicates repositories[{other_index}].name ('{name}').{display_suffix}"
                )
            else:
                seen_names[normalized_name] = index

        for field_name in (
            "foldername",
            "destination",
            "subfolder",
            "limit",
            "last_notification_seen",
            "last_finalized",
        ):
            field_value = entry.get(field_name)
            if field_name == "limit":
                if field_value is None:
                    continue
                if isinstance(field_value, int):
                    if field_value < 0:
                        errors.append(
                            f"{display_location}.limit must be an integer greater than or equal to 0.{display_suffix}"
                        )
                    continue
                errors.append(
                    f"{display_location}.limit must be an integer greater than or equal to 0.{display_suffix}"
                )
                continue

            if field_value is not None and not isinstance(field_value, str):
                errors.append(
                    f"{display_location}.{field_name} must be a string when provided.{display_suffix}"
                )

        paused_value = entry.get("paused")
        if paused_value is not None and not isinstance(paused_value, bool):
            errors.append(
                f"{display_location}.paused must be a boolean when provided.{display_suffix}"
            )

        destination = entry.get("destination")
        if isinstance(destination, str) and not destination.strip():
            warnings.append(
                f"{display_location}.destination is empty; files will keep default routing until configured.{display_suffix}"
            )

        foldername = entry.get("foldername")
        if isinstance(foldername, str) and not foldername.strip():
            warnings.append(
                f"{display_location}.foldername is empty; default folder name will be used.{display_suffix}"
            )

        intervals_value = entry.get("recheck_intervals_minutes")
        if intervals_value is not None:
            if not isinstance(intervals_value, list):
                errors.append(
                    f"{display_location}.recheck_intervals_minutes must be an array when provided.{display_suffix}"
                )
            else:
                normalized_intervals: list[int] = []
                invalid_item_detected = False
                for interval_item in intervals_value:
                    if isinstance(interval_item, bool):
                        invalid_item_detected = True
                        continue
                    try:
                        minutes = int(interval_item)
                    except (TypeError, ValueError):
                        invalid_item_detected = True
                        continue
                    if minutes <= 0:
                        invalid_item_detected = True
                        continue
                    if minutes not in normalized_intervals:
                        normalized_intervals.append(minutes)

                if invalid_item_detected:
                    warnings.append(
                        f"{display_location}.recheck_intervals_minutes contains invalid values; global processing.recheck_intervals_minutes may be used.{display_suffix}"
                    )

        limit_release_type_folders_value = entry.get("limit_release_type_folders")
        if limit_release_type_folders_value is not None:
            if not isinstance(limit_release_type_folders_value, list):
                errors.append(
                    f"{display_location}.limit_release_type_folders must be an array when provided.{display_suffix}"
                )
            else:
                has_invalid_item = False
                has_empty_item = False
                for folder_item in limit_release_type_folders_value:
                    if not isinstance(folder_item, str):
                        has_invalid_item = True
                        continue
                    if not folder_item.strip():
                        has_empty_item = True

                if has_invalid_item:
                    errors.append(
                        f"{display_location}.limit_release_type_folders must contain only strings.{display_suffix}"
                    )
                if has_empty_item:
                    warnings.append(
                        f"{display_location}.limit_release_type_folders contains empty values; they will be ignored.{display_suffix}"
                    )

        skiplist_value = entry.get("skiplist")
        if skiplist_value is not None:
            if not isinstance(skiplist_value, list):
                errors.append(
                    f"{display_location}.skiplist must be an array when provided.{display_suffix}"
                )
            else:
                has_invalid_item = False
                has_empty_item = False
                for skiplist_item in skiplist_value:
                    if not isinstance(skiplist_item, str):
                        has_invalid_item = True
                        continue
                    if not skiplist_item.strip():
                        has_empty_item = True

                if has_invalid_item:
                    errors.append(
                        f"{display_location}.skiplist must contain only strings.{display_suffix}"
                    )
                if has_empty_item:
                    warnings.append(
                        f"{display_location}.skiplist contains empty values; they will be ignored.{display_suffix}"
                    )

    for destination_group in seen_destinations.values():
        repo_names = destination_group["repo_names"]
        if len(repo_names) < 2:
            continue
        destination_display = destination_group["display_path"]
        warnings.append(
            "Multiple repositories resolve to the same destination folder "
            f"('{destination_display}'): {', '.join(repo_names)}. "
            "This is expected if intentionally shared, but often happens after a "
            "repository rename left an old entry pointing at the same place."
        )

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }