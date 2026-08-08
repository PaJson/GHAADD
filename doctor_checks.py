import os
import re
import sys
from typing import TypedDict

from mapping_manager import validate_mapping_schema


class DoctorReport(TypedDict):
    ok: bool
    platform: str
    errors: list[str]
    warnings: list[str]
    checks: list[str]


_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:\\")


def _config_file_path() -> str:
    app_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(app_dir, "config.json")


def _mapping_file_path() -> str:
    app_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(app_dir, "mapping.json")


def _looks_windows_style(path_value: str) -> bool:
    return bool(_WINDOWS_DRIVE_RE.match(path_value)) or path_value.startswith("\\\\")


def _looks_posix_style(path_value: str) -> bool:
    return path_value.startswith("/")


def _warn_path_style_mismatch(path_value: str, platform_name: str) -> str | None:
    if not isinstance(path_value, str) or not path_value.strip():
        return None

    if platform_name == "windows" and _looks_posix_style(path_value):
        return f"Path looks POSIX-style on Windows: {path_value}"

    if platform_name in {"linux", "darwin"} and _looks_windows_style(path_value):
        return f"Path looks Windows-style on {platform_name}: {path_value}"

    return None


def _iter_config_paths(config_payload: dict) -> list[tuple[str, str]]:
    paths = []
    paths_section = config_payload.get("paths")
    if not isinstance(paths_section, dict):
        return paths

    default_download_dir = paths_section.get("default_download_dir")
    if isinstance(default_download_dir, str):
        paths.append(("paths.default_download_dir", default_download_dir))

    repo_paths = paths_section.get("repo_paths")
    if isinstance(repo_paths, dict):
        for repo, target in repo_paths.items():
            if isinstance(target, str):
                paths.append((f"paths.repo_paths[{repo}]", target))

    release_type_paths = paths_section.get("release_type_paths")
    if isinstance(release_type_paths, dict):
        for release_type, target in release_type_paths.items():
            if isinstance(target, str):
                paths.append((f"paths.release_type_paths[{release_type}]", target))

    combo_paths = paths_section.get("repo_release_type_paths")
    if isinstance(combo_paths, dict):
        for repo, repo_map in combo_paths.items():
            if not isinstance(repo_map, dict):
                continue
            for release_type, target in repo_map.items():
                if isinstance(target, str):
                    paths.append((f"paths.repo_release_type_paths[{repo}][{release_type}]", target))

    return paths


def _iter_mapping_paths(mapping_payload: dict) -> list[tuple[str, str]]:
    paths = []
    repositories = mapping_payload.get("repositories")
    if not isinstance(repositories, list):
        return paths

    for index, entry in enumerate(repositories, 1):
        if not isinstance(entry, dict):
            continue
        destination = entry.get("destination")
        if isinstance(destination, str):
            repo_name = str(entry.get("name") or "unknown")
            paths.append((f"mapping.repositories[{index}] ({repo_name}).destination", destination))

    return paths


def run_doctor() -> DoctorReport:
    errors: list[str] = []
    warnings: list[str] = []
    checks: list[str] = []

    platform_name = sys.platform
    checks.append(f"Detected platform: {platform_name}")

    gmail_user = os.getenv("GMAIL_USER")
    gmail_app_password = os.getenv("GMAIL_APP_PASSWORD")
    github_pat = os.getenv("GITHUB_PAT")

    if not gmail_user:
        errors.append("Missing required environment variable: GMAIL_USER")
    if not gmail_app_password:
        errors.append("Missing required environment variable: GMAIL_APP_PASSWORD")
    if not github_pat:
        warnings.append("GITHUB_PAT is not set; GitHub API calls may hit strict rate limits.")

    config_path = _config_file_path()
    if not os.path.exists(config_path):
        warnings.append("config.json is missing; defaults will be used.")
        config_payload = {}
    else:
        try:
            import json
            with open(config_path, "r", encoding="utf-8") as f:
                config_payload = json.load(f)
            if not isinstance(config_payload, dict):
                errors.append("config.json root must be an object.")
                config_payload = {}
        except Exception as exc:
            errors.append(f"config.json could not be parsed: {exc}")
            config_payload = {}

    mapping_path = _mapping_file_path()
    if not os.path.exists(mapping_path):
        warnings.append("mapping.json is missing; it will be created when notifications are ingested.")
        mapping_payload = {"repositories": []}
    else:
        try:
            import json
            with open(mapping_path, "r", encoding="utf-8") as f:
                mapping_payload = json.load(f)
            if not isinstance(mapping_payload, dict):
                errors.append("mapping.json root must be an object.")
                mapping_payload = {"repositories": []}
        except Exception as exc:
            errors.append(f"mapping.json could not be parsed: {exc}")
            mapping_payload = {"repositories": []}

    mapping_validation = validate_mapping_schema()
    for error in mapping_validation["errors"]:
        errors.append(f"mapping schema: {error}")
    for warning in mapping_validation["warnings"]:
        warnings.append(f"mapping schema: {warning}")

    for location, path_value in _iter_config_paths(config_payload):
        mismatch_warning = _warn_path_style_mismatch(path_value, platform_name)
        if mismatch_warning:
            warnings.append(f"{location}: {mismatch_warning}")

    for location, path_value in _iter_mapping_paths(mapping_payload):
        mismatch_warning = _warn_path_style_mismatch(path_value, platform_name)
        if mismatch_warning:
            warnings.append(f"{location}: {mismatch_warning}")

    checks.append("Validated environment variables and local JSON config files.")
    checks.append("Checked path style compatibility for configured destination paths.")

    return {
        "ok": len(errors) == 0,
        "platform": platform_name,
        "errors": errors,
        "warnings": warnings,
        "checks": checks,
    }
