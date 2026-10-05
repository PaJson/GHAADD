import copy
import json
import os
import re
import shutil
import tempfile
import time
from datetime import datetime
from typing import Any, Callable, Optional, TypedDict, TypeVar

from filelock import FileLock, Timeout

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


_MAPPING_LOCK_TIMEOUT_SECONDS = 10.0
_REPLACE_RETRY_ATTEMPTS = 10
_REPLACE_RETRY_DELAY_SECONDS = 0.05

# Fields the daemon maintains. Other writers (GUI/CLI) must not set them, and
# "name" is the entry's identity.
_DAEMON_OWNED_FIELDS = frozenset({"last_notification_seen", "last_finalized"})
_IDENTITY_FIELDS = frozenset({"name"})

_REPOSITORY_NAME_PATTERN = re.compile(r"^[^/\s]+/[^/\s]+$")

_T = TypeVar("_T")

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


def ensure_mapping_file() -> bool:
    """Create an empty mapping.json on a fresh install; return True if it was created.

    An existing file (even an invalid one, which load_mapping backs up when it is
    read) is never touched. Does nothing in dry-run mode.
    """
    if is_dry_run():
        return False

    file_path = _mapping_file_path()
    if os.path.exists(file_path):
        return False

    lock = FileLock(_mapping_lock_path(), timeout=_MAPPING_LOCK_TIMEOUT_SECONDS)
    try:
        lock.acquire()
    except Timeout as exc:
        raise MappingLockTimeout(
            f"Timed out after {_MAPPING_LOCK_TIMEOUT_SECONDS:.0f}s waiting for the "
            "mapping.json lock; another GHAADD process may be stuck writing it."
        ) from exc

    try:
        # Re-check under the lock: another process may have created it meanwhile.
        if os.path.exists(file_path):
            return False
        _write_mapping_atomically(file_path, _serialize_mapping_payload(_default_mapping_payload()))
        return True
    finally:
        lock.release()


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


class MappingLockTimeout(RuntimeError):
    """Raised when mapping.json could not be locked for writing in time."""


class MappingValidationError(ValueError):
    """Raised when a requested mapping change would be invalid; nothing was written."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = list(errors)


def _mapping_lock_path() -> str:
    """Return the lock file path guarding mapping.json read-modify-write cycles."""
    return f"{_mapping_file_path()}.lock"


def _serialize_mapping_payload(mapping_payload: dict[str, list[dict[str, Any]]]) -> str:
    """Return the sorted, normalized, human-formatted mapping.json text."""
    normalized_payload = _normalize_mapping_payload(mapping_payload)
    normalized_payload["repositories"] = sorted(
        [_order_mapping_entry(entry) for entry in normalized_payload["repositories"]],
        key=_repository_sort_key,
    )

    serialized_payload = json.dumps(normalized_payload, indent=2, ensure_ascii=True)
    serialized_payload = _collapse_recheck_intervals_arrays(serialized_payload)
    serialized_payload = _collapse_limit_release_type_folders_arrays(serialized_payload)
    serialized_payload = _collapse_skiplist_arrays(serialized_payload)
    return serialized_payload + "\n"


def _write_mapping_atomically(file_path: str, serialized_payload: str) -> None:
    """Write mapping text to a temp file beside the target, then swap it in.

    Readers therefore see either the old or the new file, never a partial one.
    """
    temp_handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=os.path.dirname(file_path),
        prefix="mapping.json.",
        suffix=".tmp",
        delete=False,
    )
    temp_path = temp_handle.name
    try:
        with temp_handle:
            temp_handle.write(serialized_payload)
            temp_handle.flush()
            os.fsync(temp_handle.fileno())

        # On Windows os.replace raises PermissionError while another process
        # briefly has the target open (e.g. a reader), so retry a few times.
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


def update_mapping(mutator: Callable[[dict[str, list[dict[str, Any]]]], _T]) -> _T:
    """Apply one change to mapping.json safely and return the mutator's result.

    The single write path for every writer (daemon, CLI, GUI). Under a
    cross-process lock it re-reads the file fresh, lets `mutator` change the
    payload in place, then writes the sorted result atomically. Because the
    payload is always re-read inside the lock, a writer only ever changes what
    its mutator touches and cannot overwrite another process's updates. The
    file is not rewritten when the mutator changes nothing.

    The mutator must be quick and must not call update_mapping itself (the
    lock is not re-entrant across calls). In dry-run mode the mutator still
    runs on the loaded payload so results are accurate, but nothing is locked
    or written.

    Raises MappingLockTimeout when the lock cannot be taken in time.
    """
    if is_dry_run():
        return mutator(load_mapping())

    file_path = _mapping_file_path()
    lock = FileLock(_mapping_lock_path(), timeout=_MAPPING_LOCK_TIMEOUT_SECONDS)
    try:
        lock.acquire()
    except Timeout as exc:
        raise MappingLockTimeout(
            f"Timed out after {_MAPPING_LOCK_TIMEOUT_SECONDS:.0f}s waiting for the "
            "mapping.json lock; another GHAADD process may be stuck writing it."
        ) from exc

    try:
        mapping_payload = load_mapping()
        snapshot = copy.deepcopy(mapping_payload)
        result = mutator(mapping_payload)
        if mapping_payload != snapshot:
            _write_mapping_atomically(file_path, _serialize_mapping_payload(mapping_payload))
        return result
    finally:
        lock.release()


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


def _build_skeleton_entry(repo: str, notification_seen_stamp: str) -> dict[str, Any]:
    """Return a new repository entry with default values."""
    return {
        "name": repo,
        "destination": "",
        "foldername": build_default_foldername(repo),
        "subfolder": DEFAULT_SUBFOLDER,
        "limit": DEFAULT_LIMIT,
        "limit_release_type_folders": _default_limit_release_type_folders(),
        "recheck_intervals_minutes": [],
        "skiplist": [],
        "last_notification_seen": notification_seen_stamp,
        "last_finalized": "",
        "paused": False,
    }


def upsert_repository_mapping(
    repo: str,
    notification_seen_stamp: Optional[str] = None,
) -> tuple[bool, bool]:
    """Upsert one repository mapping and return (created, updated)."""
    effective_notification_seen_stamp = (
        notification_seen_stamp or _current_mapping_stamp()
    )

    def _apply(mapping_payload: dict[str, list[dict[str, Any]]]) -> tuple[bool, bool]:
        repositories = mapping_payload["repositories"]
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

            return (False, updated)

        repositories.append(
            _build_skeleton_entry(repo, effective_notification_seen_stamp)
        )
        return (True, False)

    created, updated = update_mapping(_apply)
    if created:
        log_warning(
            "MAPPING",
            f"Repository '{repo}' was added without a configured destination; it will use default routing until mapped.",
        )
    return (created, updated)


def mark_repository_finalized(
    repo: str,
    finalized_stamp: Optional[str] = None,
) -> bool:
    """Record when a repository release was moved to its complete destination."""
    normalized_repo = str(repo or "").strip()
    if not normalized_repo:
        return False

    finalized_value = finalized_stamp or _current_mapping_stamp()

    def _apply(mapping_payload: dict[str, list[dict[str, Any]]]) -> bool:
        for entry in mapping_payload["repositories"]:
            if not _is_same_repository_identity(entry, normalized_repo):
                continue
            if entry.get("last_finalized") == finalized_value:
                return False
            entry["last_finalized"] = finalized_value
            return True

        return False

    return update_mapping(_apply)


def _reject_non_editable_fields(fields: dict[str, Any]) -> None:
    """Raise ValueError when fields include daemon-owned fields or the entry name."""
    forbidden = (_DAEMON_OWNED_FIELDS | _IDENTITY_FIELDS) & set(fields)
    if forbidden:
        raise ValueError(f"Fields cannot be set by GUI/CLI edits: {sorted(forbidden)}")


def _apply_validated(
    mapping_payload: dict[str, list[dict[str, Any]]],
    change: Callable[[], _T],
) -> _T:
    """Run `change` on the payload; raise MappingValidationError if it adds errors.

    Only errors the change introduces count, so a pre-existing problem in an
    unrelated entry never blocks a valid edit. Raising inside an update_mapping
    mutator leaves the file untouched.
    """
    errors_before = set(validate_mapping_payload(mapping_payload)["errors"])
    result = change()
    new_errors = [
        error
        for error in validate_mapping_payload(mapping_payload)["errors"]
        if error not in errors_before
    ]
    if new_errors:
        raise MappingValidationError(new_errors)
    return result


def update_repository_fields(repo: str, changes: dict[str, Any]) -> bool:
    """Change only the given fields of one repository entry (GUI/CLI edit path).

    Re-reads mapping.json under the lock, so the daemon's own updates to other
    fields are preserved. Returns True when the file changed. Raises ValueError
    for daemon-owned fields or the entry name, KeyError when the repository is
    not mapped, and MappingValidationError (nothing written) when the new
    values would make the entry invalid.
    """
    _reject_non_editable_fields(changes)
    normalized_repo = str(repo or "").strip()

    def _apply(mapping_payload: dict[str, list[dict[str, Any]]]) -> bool:
        for entry in mapping_payload["repositories"]:
            if not _is_same_repository_identity(entry, normalized_repo):
                continue

            def _change() -> bool:
                changed = any(entry.get(key) != value for key, value in changes.items())
                entry.update(changes)
                return changed

            return _apply_validated(mapping_payload, _change)

        raise KeyError(f"Repository '{normalized_repo}' is not in mapping.json")

    return update_mapping(_apply)


def add_repository(repo: str, fields: Optional[dict[str, Any]] = None) -> None:
    """Add a new repository entry with default values plus optional `fields`.

    `repo` must look like 'owner/repo'. Raises MappingValidationError when the
    name is malformed, already mapped (case-insensitive), or the resulting entry
    is invalid, and ValueError for daemon-owned fields in `fields`. The entry's
    last_notification_seen starts empty because no notification was seen yet.
    """
    extra_fields = dict(fields or {})
    _reject_non_editable_fields(extra_fields)

    normalized_repo = str(repo or "").strip()
    if not _REPOSITORY_NAME_PATTERN.match(normalized_repo):
        raise MappingValidationError(
            [f"Repository name '{normalized_repo}' must look like 'owner/repo'."]
        )

    def _apply(mapping_payload: dict[str, list[dict[str, Any]]]) -> None:
        if any(
            _is_same_repository_identity(entry, normalized_repo)
            for entry in mapping_payload["repositories"]
        ):
            raise MappingValidationError(
                [f"Repository '{normalized_repo}' is already in mapping.json."]
            )

        def _change() -> None:
            entry = _build_skeleton_entry(normalized_repo, "")
            entry.update(extra_fields)
            mapping_payload["repositories"].append(entry)

        _apply_validated(mapping_payload, _change)

    update_mapping(_apply)


def remove_repository(repo: str) -> bool:
    """Remove a repository entry. Returns False when it was not mapped.

    Only the mapping entry is removed; queued jobs in state.db are untouched,
    and a new notification for the repository re-creates a skeleton entry.
    """
    normalized_repo = str(repo or "").strip()

    def _apply(mapping_payload: dict[str, list[dict[str, Any]]]) -> bool:
        repositories = mapping_payload["repositories"]
        remaining = [
            entry for entry in repositories
            if not _is_same_repository_identity(entry, normalized_repo)
        ]
        if len(remaining) == len(repositories):
            return False
        mapping_payload["repositories"] = remaining
        return True

    return update_mapping(_apply)


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
    raw_payload = load_mapping_raw()
    if raw_payload is None:
        return {
            "ok": False,
            "errors": ["mapping.json is missing or contains invalid JSON."],
            "warnings": [],
        }

    return validate_mapping_payload(raw_payload)


def validate_mapping_payload(raw_payload: Any) -> MappingValidationResult:
    """Validate an in-memory mapping payload and return errors/warnings."""
    errors: list[str] = []
    warnings: list[str] = []

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