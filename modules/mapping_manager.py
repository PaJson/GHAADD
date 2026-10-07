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

from modules.app_info import MAPPING_FORMAT
from modules.config_manager import get_default_repository_limit, get_default_subfolder, get_recheck_intervals_minutes
from modules.dry_run_mode import is_dry_run
from modules.lifecycle_logger import log_warning


DEFAULT_LIMIT_RELEASE_TYPE_FOLDERS = ["Release", "Pre-release"]
# How the "file count changed" sanity check picks the release to compare with (per repository):
# any_tag = the previous successful release of the repository whatever its tag (default),
# same_tag = only a previous release with the same tag (suits rolling tags such as "nightly"), off = no check.
SANITY_CHECK_MODES = ("any_tag", "same_tag", "off")
DEFAULT_SANITY_CHECK = "any_tag"
_MAPPING_FIELD_ORDER = (
    "repository",
    "folder",
    "subfolder",
    "destination",
    "skiplist",
    "recheck_intervals",
    "limit",
    "limit_folders",
    "sanity_check",
    "last_notification",
    "last_finalized",
    "active",
)

# Key names used by mapping.json before 2.0. A file written by an older version is understood as it is
# read (in memory) and rewritten, after a backup copy, by migrate_mapping_file() or the next write.
# `paused: true` became `active: false`, so that one is inverted rather than renamed (see _upgrade_legacy_entry).
_LEGACY_KEY_RENAMES = {
    "name": "repository",
    "foldername": "folder",
    "recheck_intervals_minutes": "recheck_intervals",
    "limit_release_type_folders": "limit_folders",
    "last_notification_seen": "last_notification",
}
_LEGACY_BACKUP_SUFFIX = ".v1.bak"


_MAPPING_LOCK_TIMEOUT_SECONDS = 10.0
_REPLACE_RETRY_ATTEMPTS = 10
_REPLACE_RETRY_DELAY_SECONDS = 0.05

# Fields the daemon maintains. Other writers (GUI/CLI) must not set them, and
# "repository" is the entry's identity.
_DAEMON_OWNED_FIELDS = frozenset({"last_notification", "last_finalized"})
_IDENTITY_FIELDS = frozenset({"repository"})

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


def _upgrade_legacy_entry(entry: dict[str, Any]) -> bool:
    """Rename pre-2.0 keys of one entry in place (`paused` becomes the inverted `active`); True if anything changed.

    When both the old and the new key exist the new one wins and the old one is dropped.
    """
    changed = False
    for old_key, new_key in _LEGACY_KEY_RENAMES.items():
        if old_key in entry:
            value = entry.pop(old_key)
            entry.setdefault(new_key, value)
            changed = True
    if "paused" in entry:
        paused = entry.pop("paused")
        # A value that is not a boolean is kept as it is, so validation reports it under its new name.
        entry.setdefault("active", (not paused) if isinstance(paused, bool) else paused)
        changed = True
    return changed


def _upgrade_legacy_payload(payload: dict[str, list[dict[str, Any]]]) -> bool:
    """Upgrade every entry of a normalized payload in place; True if any entry used old key names."""
    changed = False
    for entry in payload.get("repositories", []):
        if _upgrade_legacy_entry(entry):
            changed = True
    return changed


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
    """Render recheck_intervals arrays on a single line for readability."""
    pattern = re.compile(
        r'("recheck_intervals"\s*:\s*)\[\n(?P<body>(?:\s*\d+\s*,?\n)*)\s*\]',
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
    """Render limit_folders arrays on a single line for readability."""
    pattern = re.compile(
        r'("limit_folders"\s*:\s*)\[\n(?P<body>(?:\s*"[^"]+"\s*,?\n)*)\s*\]',
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
    """Load mapping.json with safe fallback payload (keys of an older version are understood, see _LEGACY_KEY_RENAMES)."""
    return _load_mapping(upgrade=True)


def _load_mapping(upgrade: bool) -> dict[str, list[dict[str, Any]]]:
    """load_mapping(), optionally leaving pre-2.0 key names as they are on disk (update_mapping needs that)."""
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

    normalized = _normalize_mapping_payload(payload)
    if upgrade:
        _upgrade_legacy_payload(normalized)
    return normalized


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
    read) is only touched to upgrade pre-2.0 key names. Does nothing in dry-run mode.
    """
    if is_dry_run():
        return False

    file_path = _mapping_file_path()
    if os.path.exists(file_path):
        try:
            migrate_mapping_file()  # a file with pre-2.0 key names is rewritten with the new ones (after a backup)
        except (MappingLockTimeout, MappingValidationError, OSError):
            pass  # not fatal: the old names are still understood when reading
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
    name_value = str(entry.get("repository") or entry.get("folder") or "").strip()
    repo_name = name_value.split("/", 1)[1] if "/" in name_value else name_value
    return (repo_name.lower(), name_value.lower())


def _order_mapping_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Return a consistently ordered mapping entry."""
    ordered_entry = dict(entry)
    ordered_entry.setdefault("active", True)

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
        mapping_payload = _load_mapping(upgrade=False)
        snapshot = copy.deepcopy(mapping_payload)
        had_legacy_keys = _upgrade_legacy_payload(mapping_payload)  # then the write below also upgrades the file
        if had_legacy_keys and _older_daemon_running():
            raise MappingValidationError([
                "mapping.json still uses the old key names and the running daemon is an older version that "
                "would not understand the upgraded file. Restart the daemon first (Stop, then Start), then try again."
            ])
        result = mutator(mapping_payload)
        if mapping_payload != snapshot:
            if had_legacy_keys:
                _backup_legacy_mapping_file(file_path)
            _write_mapping_atomically(file_path, _serialize_mapping_payload(mapping_payload))
        return result
    finally:
        lock.release()


def _backup_legacy_mapping_file(file_path: str) -> None:
    """Keep a copy of a mapping.json that still has pre-2.0 key names before it is rewritten."""
    if not os.path.exists(file_path):
        return
    backup_path = f"{file_path}{_LEGACY_BACKUP_SUFFIX}"
    if os.path.exists(backup_path):  # never overwrite an earlier backup
        backup_path = f"{backup_path}-{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(file_path, backup_path)
    message = f"mapping.json was upgraded to the 2.0 key names; the previous file is kept as {backup_path}."
    print(message)
    log_warning("MAPPING", message)


def _older_daemon_running() -> bool:
    """True while a daemon runs that does not understand the current mapping.json format (started before 2.0)."""
    from modules.daemon_lock import get_daemon_status

    status = get_daemon_status()
    return bool(status["running"] and status.get("mapping_format") != MAPPING_FORMAT)


def migrate_mapping_file() -> bool:
    """Rewrite mapping.json with the 2.0 key names when it still has the old ones; True if it was rewritten.

    A backup copy (mapping.json.v1.bak) is made first. While an older daemon is running the file is left alone
    (it would not understand the new names); the upgrade then happens once that daemon has been restarted.
    Does nothing in dry-run mode.
    """
    if is_dry_run():
        return False
    raw = load_mapping_raw()
    repositories = raw.get("repositories") if isinstance(raw, dict) else None
    if not isinstance(repositories, list) or not any(
        isinstance(entry, dict) and (set(_LEGACY_KEY_RENAMES) | {"paused"}) & set(entry) for entry in repositories
    ):
        return False
    if _older_daemon_running():
        return False
    update_mapping(lambda payload: None)  # reading upgrades the payload, so the write happens by itself
    return True


def build_default_folder(repo: str) -> str:
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
    entry_name = str(entry.get("repository") or "").strip()
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


def is_repository_active(repo: str) -> bool:
    """Return False only when a repository is explicitly switched off (`active: false`) in mapping.json."""
    mapping_entry = get_repository_mapping(repo)
    return not (isinstance(mapping_entry, dict) and mapping_entry.get("active") is False)


def get_repository_recheck_intervals_minutes(repo: str) -> list[int]:
    """Return per-repository recheck intervals when configured; else fall back to config."""
    mapping_entry = get_repository_mapping(repo)
    if not isinstance(mapping_entry, dict):
        return list(get_recheck_intervals_minutes())

    intervals_value = mapping_entry.get("recheck_intervals")
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
        mapping_entry.get("limit_folders")
    )


def get_repository_skiplist(repo: str) -> list[str]:
    """Return repository release types (e.g. 'Release', 'Pre-release') that should be skipped."""
    mapping_entry = get_repository_mapping(repo)
    if not isinstance(mapping_entry, dict):
        return []

    return _normalize_skiplist(mapping_entry.get("skiplist"))


def normalize_sanity_check_mode(value: Any) -> str:
    """A stored sanity_check value as one of SANITY_CHECK_MODES (missing or unknown = the default)."""
    normalized = str(value or "").strip().lower()
    return normalized if normalized in SANITY_CHECK_MODES else DEFAULT_SANITY_CHECK


def get_repository_sanity_check_mode(repo: str) -> str:
    """Return how the file-count sanity check works for a repository (see SANITY_CHECK_MODES)."""
    mapping_entry = get_repository_mapping(repo)
    return normalize_sanity_check_mode(mapping_entry.get("sanity_check") if isinstance(mapping_entry, dict) else None)


def is_release_type_skipped(repo: str, release_type: Optional[str]) -> bool:
    """Return True when the given release type is in the repository's skiplist."""
    normalized_release_type = str(release_type or "Release").strip().lower()
    skiplist = get_repository_skiplist(repo)
    return any(entry.lower() == normalized_release_type for entry in skiplist)


def _current_mapping_stamp() -> str:
    """Return a mapping timestamp in YYYY-MM-DD_HH-MM format."""
    return datetime.now().strftime("%Y-%m-%d_%H-%M")


def _build_skeleton_entry(repo: str, notification_seen_stamp: str) -> dict[str, Any]:
    """Return a new repository entry with default values (subfolder and limit come from config.json)."""
    return {
        "repository": repo,
        "folder": build_default_folder(repo),
        "subfolder": get_default_subfolder(),
        "destination": "",
        "skiplist": [],
        "recheck_intervals": [],
        "limit": get_default_repository_limit(),
        "limit_folders": _default_limit_release_type_folders(),
        "sanity_check": DEFAULT_SANITY_CHECK,
        "last_notification": notification_seen_stamp,
        "last_finalized": "",
        "active": True,
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
            if entry.get("last_notification") != effective_notification_seen_stamp:
                entry["last_notification"] = effective_notification_seen_stamp
                updated = True

            if "last_finalized" not in entry:
                entry["last_finalized"] = ""
                updated = True

            if "recheck_intervals" not in entry:
                entry["recheck_intervals"] = []
                updated = True

            if "limit_folders" not in entry:
                entry["limit_folders"] = _default_limit_release_type_folders()
                updated = True

            if "skiplist" not in entry:
                entry["skiplist"] = []
                updated = True

            if "sanity_check" not in entry:
                entry["sanity_check"] = DEFAULT_SANITY_CHECK
                updated = True

            if "active" not in entry:
                entry["active"] = True
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
    last_notification starts empty because no notification was seen yet.
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
    """Return a comparison-safe form of a folder/subfolder segment."""
    segments = [segment for segment in re.split(r"[\\/]+", value.strip()) if segment not in ("", ".", "..")]
    joined = "/".join(segments)
    return joined.lower() if os.name == "nt" else joined


def _build_destination_comparison_key(
    destination: str,
    folder: Any,
    subfolder: Any,
    repo_name: str,
) -> tuple[str, str, str]:
    """Return a normalized (destination, folder, subfolder) key for duplicate checks."""
    destination_norm = _normalize_destination_root_for_comparison(destination)

    folder_value = folder if isinstance(folder, str) and folder.strip() else build_default_folder(repo_name)
    folder_norm = _normalize_folder_segment_for_comparison(folder_value)

    subfolder_value = subfolder if isinstance(subfolder, str) else ""
    subfolder_norm = _normalize_folder_segment_for_comparison(subfolder_value)

    return (destination_norm, folder_norm, subfolder_norm)


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
                "name": str(entry.get("repository") or "unknown"),
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
    raw_payload = load_mapping_raw()  # validate_mapping_payload understands pre-2.0 key names
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
    repositories = copy.deepcopy(repositories)  # validate the 2.0 view of an old file without touching the caller's data
    for legacy_entry in repositories:
        if isinstance(legacy_entry, dict):
            _upgrade_legacy_entry(legacy_entry)

    seen_names: dict[str, int] = {}
    seen_destinations: dict[tuple[str, str, str], dict[str, Any]] = {}
    for index, entry in enumerate(repositories, 1):
        location = f"repositories[{index}]"

        if not isinstance(entry, dict):
            errors.append(f"{location} must be an object.")
            continue

        display_location = location
        display_suffix = ""
        name = entry.get("repository")
        if isinstance(name, str) and name.strip():
            display_suffix = f" ({name.strip()})"

        destination_value = entry.get("destination")
        if isinstance(destination_value, str) and destination_value.strip():
            repo_name_for_key = str(name) if isinstance(name, str) else ""
            destination_key = _build_destination_comparison_key(
                destination_value,
                entry.get("folder"),
                entry.get("subfolder"),
                repo_name_for_key,
            )
            folder_value = entry.get("folder")
            effective_folder = (
                folder_value.strip()
                if isinstance(folder_value, str) and folder_value.strip()
                else build_default_folder(repo_name_for_key)
            )
            subfolder_value = entry.get("subfolder")
            display_path_parts = [destination_value.strip(), effective_folder]
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
            not in set(_MAPPING_FIELD_ORDER)
        )
        for key in unknown_keys:
            warnings.append(f"{display_location} has unknown key '{key}'.{display_suffix}")

        if not isinstance(name, str) or not name.strip():
            errors.append(f"{location}.repository must be a non-empty string.")
        else:
            normalized_name = name.strip().lower()
            if normalized_name in seen_names:
                other_index = seen_names[normalized_name]
                errors.append(
                    f"{display_location}.repository duplicates repositories[{other_index}].repository ('{name}').{display_suffix}"
                )
            else:
                seen_names[normalized_name] = index

        for field_name in (
            "folder",
            "destination",
            "subfolder",
            "limit",
            "last_notification",
            "last_finalized",
        ):
            field_value = entry.get(field_name)
            if field_name == "limit":
                if field_value is None:
                    continue
                if isinstance(field_value, int) and not isinstance(field_value, bool):
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

        active_value = entry.get("active")
        if active_value is not None and not isinstance(active_value, bool):
            errors.append(
                f"{display_location}.active must be a boolean when provided.{display_suffix}"
            )

        destination = entry.get("destination")
        if isinstance(destination, str) and not destination.strip():
            warnings.append(
                f"{display_location}.destination is empty; files will keep default routing until configured.{display_suffix}"
            )

        folder = entry.get("folder")
        if isinstance(folder, str) and not folder.strip():
            warnings.append(
                f"{display_location}.folder is empty; default folder name will be used.{display_suffix}"
            )

        intervals_value = entry.get("recheck_intervals")
        if intervals_value is not None:
            if not isinstance(intervals_value, list):
                errors.append(
                    f"{display_location}.recheck_intervals must be an array when provided.{display_suffix}"
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
                        f"{display_location}.recheck_intervals contains invalid values; global processing.recheck_intervals_minutes may be used.{display_suffix}"
                    )

        limit_release_type_folders_value = entry.get("limit_folders")
        if limit_release_type_folders_value is not None:
            if not isinstance(limit_release_type_folders_value, list):
                errors.append(
                    f"{display_location}.limit_folders must be an array when provided.{display_suffix}"
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
                        f"{display_location}.limit_folders must contain only strings.{display_suffix}"
                    )
                if has_empty_item:
                    warnings.append(
                        f"{display_location}.limit_folders contains empty values; they will be ignored.{display_suffix}"
                    )

        sanity_check_value = entry.get("sanity_check")
        if sanity_check_value is not None and (
            not isinstance(sanity_check_value, str) or sanity_check_value.strip().lower() not in SANITY_CHECK_MODES
        ):
            errors.append(
                f"{display_location}.sanity_check must be one of {', '.join(SANITY_CHECK_MODES)}.{display_suffix}"
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