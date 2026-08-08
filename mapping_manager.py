import json
import os
from datetime import datetime
from typing import Any, Optional, TypedDict


class MappingValidationResult(TypedDict):
    ok: bool
    errors: list[str]
    warnings: list[str]


def _mapping_file_path() -> str:
    """Return absolute path to mapping.json beside application files."""
    app_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(app_dir, "mapping.json")


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


def load_mapping() -> dict[str, list[dict[str, Any]]]:
    """Load mapping.json with safe fallback payload."""
    file_path = _mapping_file_path()
    if not os.path.exists(file_path):
        return _default_mapping_payload()

    try:
        with open(file_path, "r", encoding="utf-8") as mapping_file:
            payload = json.load(mapping_file)
    except (OSError, json.JSONDecodeError):
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
    except (OSError, json.JSONDecodeError):
        return None


def save_mapping(mapping_payload: dict[str, list[dict[str, Any]]]) -> None:
    """Persist mapping payload to mapping.json."""
    file_path = _mapping_file_path()
    normalized_payload = _normalize_mapping_payload(mapping_payload)
    normalized_payload["repositories"] = sorted(
        normalized_payload["repositories"],
        key=lambda entry: str(
            entry.get("nicename") or entry.get("name") or ""
        ).strip().lower(),
    )
    with open(file_path, "w", encoding="utf-8") as mapping_file:
        json.dump(normalized_payload, mapping_file, indent=2)
        mapping_file.write("\n")


def build_default_nicename(repo: str) -> str:
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


def _current_active_stamp() -> str:
    """Return activity timestamp in YYYY-MM-DD_HH-MM format."""
    return datetime.now().strftime("%Y-%m-%d_%H-%M")


def upsert_repository_mapping(
    repo: str,
    active_stamp: Optional[str] = None,
) -> tuple[bool, bool]:
    """Upsert one repository mapping and return (created, updated)."""
    mapping_payload = load_mapping()
    repositories = mapping_payload["repositories"]
    effective_active_stamp = active_stamp or _current_active_stamp()

    for entry in repositories:
        if not _is_same_repository_identity(entry, repo):
            continue

        updated = False
        if entry.get("active") != effective_active_stamp:
            entry["active"] = effective_active_stamp
            updated = True

        if updated:
            save_mapping(mapping_payload)
        return (False, updated)

    skeleton_entry = {
        "name": repo,
        "nicename": build_default_nicename(repo),
        "destination": "",
        "subfolder": "",
        "limit": 0,
        "active": effective_active_stamp,
    }
    repositories.append(skeleton_entry)
    save_mapping(mapping_payload)
    return (True, False)


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
    for index, entry in enumerate(repositories, 1):
        location = f"repositories[{index}]"

        if not isinstance(entry, dict):
            errors.append(f"{location} must be an object.")
            continue

        unknown_keys = sorted(
            key
            for key in entry.keys()
            if key not in {"name", "nicename", "destination", "subfolder", "limit", "active"}
        )
        for key in unknown_keys:
            warnings.append(f"{location} has unknown key '{key}'.")

        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"{location}.name must be a non-empty string.")
        else:
            normalized_name = name.strip().lower()
            if normalized_name in seen_names:
                other_index = seen_names[normalized_name]
                errors.append(
                    f"{location}.name duplicates repositories[{other_index}].name ('{name}')."
                )
            else:
                seen_names[normalized_name] = index

        for field_name in ("nicename", "destination", "subfolder", "limit", "active"):
            field_value = entry.get(field_name)
            if field_name == "limit":
                if field_value is None:
                    continue
                if isinstance(field_value, int):
                    if field_value < 0:
                        errors.append(f"{location}.limit must be an integer greater than or equal to 0.")
                    continue
                errors.append(f"{location}.limit must be an integer greater than or equal to 0.")
                continue

            if field_value is not None and not isinstance(field_value, str):
                errors.append(f"{location}.{field_name} must be a string when provided.")

        destination = entry.get("destination")
        if isinstance(destination, str) and not destination.strip():
            warnings.append(
                f"{location}.destination is empty; files will keep default routing until configured."
            )

        nicename = entry.get("nicename")
        if isinstance(nicename, str) and not nicename.strip():
            warnings.append(
                f"{location}.nicename is empty; default display name will be used."
            )

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }