import datetime
import os
import re
import requests
import shutil
import time
from db_manager import open_database, load_release_state, save_state_entry, prune_release_state
from dotenv import load_dotenv
from config_manager import (
    get_all_download_dirs,
    get_default_download_dir,
    get_download_dir_for_release,
    get_folder_settings,
    is_state_persistence_disabled,
    load_config,
)
from email.utils import parsedate_tz, mktime_tz
from lifecycle_logger import log_warning
from mapping_manager import (
    build_default_foldername,
    get_repository_limit_release_type_folders,
    get_repository_mapping,
    load_mapping,
)
from payload_types import DownloadReleaseResult, DownloadResultPayload, ReleaseAssetQueueItem, SkippedItemPayload
from requests.exceptions import ChunkedEncodingError, ConnectionError, Timeout
from typing import Literal, Optional, cast
from urllib.parse import urljoin

# Load environment variables from .env.
load_dotenv()
GITHUB_TOKEN = os.getenv("GITHUB_PAT")


def _build_skipped_item_payload(
    item_key: Optional[str],
    file_name: Optional[str],
    reason: str,
) -> SkippedItemPayload:
    """Build a typed skipped-item payload."""
    return {
        "item_key": item_key,
        "file_name": file_name,
        "reason": reason,
    }


def _resolve_directory_name_collision(target_dir: str) -> str:
    """Return a unique directory path by appending a numeric suffix when needed."""
    if not os.path.exists(target_dir):
        return target_dir

    counter = 2
    while True:
        candidate = f"{target_dir} ({counter})"
        if not os.path.exists(candidate):
            return candidate
        counter += 1


def _build_staging_directories(base_download_dir: str) -> tuple[str, str]:
    """Return (processing_dir, complete_dir) under the configured staging root."""
    folder_settings = get_folder_settings()
    ghaadd_root = os.path.join(base_download_dir, folder_settings["ghaadd_root"])
    processing_dir = os.path.join(ghaadd_root, folder_settings["processing"])
    complete_dir = os.path.join(ghaadd_root, folder_settings["complete"])
    partial_dir = os.path.join(ghaadd_root, folder_settings["partial"])
    os.makedirs(processing_dir, exist_ok=True)
    os.makedirs(complete_dir, exist_ok=True)
    os.makedirs(partial_dir, exist_ok=True)
    return processing_dir, complete_dir


def _build_repo_parent_folder(repo: str) -> str:
    """Return a safe parent folder name like 'repo (owner)' from 'owner/repo'."""
    if not repo:
        return "unknown (unknown)"

    owner, repo_name = (repo.split("/", 1) + [""])[:2]
    safe_owner = sanitize_folder_name(owner) or "unknown"
    safe_repo_name = sanitize_folder_name(repo_name or owner) or "unknown"
    return f"{safe_repo_name} ({safe_owner})"


def _sanitize_folder_path(folder: str) -> str:
    """Return a safe nested folder path from slash- or backslash-delimited input."""
    parts = re.split(r"[\\/]+", str(folder or "").strip())
    sanitized_parts = []

    for part in parts:
        sanitized_part = sanitize_folder_name(part)
        if not sanitized_part or sanitized_part in {".", ".."}:
            continue
        sanitized_parts.append(sanitized_part)

    if not sanitized_parts:
        return ""

    return os.path.join(*sanitized_parts)


def _resolve_finalized_base_directory(
    repo: Optional[str],
    default_complete_dir: str,
) -> tuple[str, bool, Optional[str]]:
    """Return finalization base directory, mapping usage, and optional fallback warning."""
    normalized_repo = str(repo or "").strip()
    if not normalized_repo:
        return default_complete_dir, False, None

    mapping_entry = get_repository_mapping(normalized_repo)
    if not isinstance(mapping_entry, dict):
        return default_complete_dir, False, None

    destination = str(mapping_entry.get("destination") or "").strip()
    if not destination:
        return default_complete_dir, False, None

    destination_root = os.path.normpath(
        os.path.expanduser(os.path.expandvars(destination))
    )
    if not os.path.isdir(destination_root):
        warning_text = (
            "Mapped destination root does not exist; "
            f"falling back to Complete for {normalized_repo}: {destination_root}"
        )
        return default_complete_dir, False, warning_text

    foldername = str(mapping_entry.get("foldername") or "").strip()
    if not foldername:
        foldername = build_default_foldername(normalized_repo)
    foldername = sanitize_folder_name(foldername) or _build_repo_parent_folder(normalized_repo)

    base_dir = os.path.join(destination_root, foldername)

    subfolder = str(mapping_entry.get("subfolder") or "").strip()
    if subfolder:
        sanitized_folder_path = _sanitize_folder_path(subfolder)
        if sanitized_folder_path:
            base_dir = os.path.join(base_dir, sanitized_folder_path)

    return base_dir, True, None


def _resolve_repository_limit(repo: Optional[str]) -> int:
    """Return repository folder-limit threshold (0 disables warning checks)."""
    normalized_repo = str(repo or "").strip()
    if not normalized_repo:
        return 0

    mapping_entry = get_repository_mapping(normalized_repo)
    if not isinstance(mapping_entry, dict):
        return 0

    limit_value = mapping_entry.get("limit")
    if isinstance(limit_value, int) and limit_value > 0:
        return limit_value

    return 0


def _count_direct_subdirectories(path: str) -> int:
    """Return count of direct child directories for path."""
    try:
        with os.scandir(path) as entries:
            return sum(1 for entry in entries if entry.is_dir())
    except OSError:
        return 0


def _resolve_tracked_release_type_folders(repo: Optional[str]) -> set[str]:
    """Return normalized release-type folder names managed by GHAADD for one repository."""
    default_tracked_folders = {
        sanitize_folder_name("Release").lower(),
        sanitize_folder_name("Pre-release").lower(),
    }

    normalized_repo = str(repo or "").strip()
    if not normalized_repo:
        return default_tracked_folders

    configured_release_type_folders = get_repository_limit_release_type_folders(normalized_repo)
    configured_tracked_folders = {
        sanitize_folder_name(folder_name).lower()
        for folder_name in configured_release_type_folders
        if isinstance(folder_name, str) and folder_name.strip()
    }
    if configured_tracked_folders:
        return configured_tracked_folders

    tracked_folders = set(default_tracked_folders)

    state_db = None
    try:
        state_db = open_database()
        rows = state_db.execute(
            """
            SELECT DISTINCT release_type
            FROM job_queue
            WHERE repo = ?
              AND release_type IS NOT NULL
              AND TRIM(release_type) <> ''
            """,
            (normalized_repo,),
        ).fetchall()
        for row in rows:
            folder_name = sanitize_folder_name(str(row["release_type"]))
            if folder_name:
                tracked_folders.add(folder_name.lower())
    except OSError:
        pass
    except Exception:
        pass
    finally:
        if state_db is not None:
            state_db.close()

    return tracked_folders


def _count_repository_release_folders(
    repo_destination_root: str,
    tracked_release_type_folders: Optional[set[str]] = None,
) -> int:
    """Return count of release folders kept under managed release-type directories only."""
    if not os.path.isdir(repo_destination_root):
        return 0

    normalized_tracked = {
        name.strip().lower()
        for name in (tracked_release_type_folders or set())
        if isinstance(name, str) and name.strip()
    }

    release_type_dirs: list[str] = []
    try:
        with os.scandir(repo_destination_root) as entries:
            for entry in entries:
                if not entry.is_dir():
                    continue

                if normalized_tracked and entry.name.strip().lower() not in normalized_tracked:
                    continue

                release_type_dirs.append(entry.path)
    except OSError:
        return 0

    if not release_type_dirs:
        return 0

    total = 0
    for release_type_dir in release_type_dirs:
        release_count = _count_direct_subdirectories(release_type_dir)
        total += release_count if release_count > 0 else 1

    return total


def _warn_if_destination_limit_exceeded(repo: Optional[str], repo_destination_root: str) -> None:
    """Emit warning when repository destination exceeds configured folder limit."""
    folder_limit = _resolve_repository_limit(repo)
    if folder_limit <= 0:
        return

    tracked_release_type_folders = _resolve_tracked_release_type_folders(repo)
    release_folder_count = _count_repository_release_folders(
        repo_destination_root,
        tracked_release_type_folders,
    )
    if release_folder_count <= folder_limit:
        return

    repo_label = str(repo or "unknown")
    warning_message = (
        "Folder limit warning: "
        f"{repo_label} currently has {release_folder_count} folder(s) "
        f"in '{repo_destination_root}' (limit={folder_limit})."
    )
    print(f"   ⚠️ {warning_message}")
    log_warning("LIMIT", warning_message)


def _move_complete_repo_tree(source_repo_dir: str, target_repo_root: str) -> int:
    """Move one repository subtree from Complete into mapped destination root."""
    moved_release_folders = 0
    os.makedirs(target_repo_root, exist_ok=True)

    try:
        with os.scandir(source_repo_dir) as entries:
            top_level_entries = list(entries)
    except OSError:
        return 0

    for entry in top_level_entries:
        source_path = entry.path

        if entry.is_dir():
            target_release_type_dir = os.path.join(target_repo_root, entry.name)
            os.makedirs(target_release_type_dir, exist_ok=True)

            try:
                with os.scandir(source_path) as release_entries:
                    for release_entry in release_entries:
                        target_path = os.path.join(target_release_type_dir, release_entry.name)
                        target_path = _resolve_directory_name_collision(target_path)
                        shutil.move(release_entry.path, target_path)
                        moved_release_folders += 1
            except OSError:
                continue

            try:
                os.rmdir(source_path)
            except OSError:
                pass
            continue

        target_path = os.path.join(target_repo_root, entry.name)
        target_path = _resolve_directory_name_collision(target_path)
        shutil.move(source_path, target_path)
        moved_release_folders += 1

    try:
        os.rmdir(source_repo_dir)
    except OSError:
        pass

    return moved_release_folders


def move_complete_folders_to_mapped_destinations() -> dict[str, int]:
    """Move eligible repository folders from Complete to configured mapping destinations."""
    config = load_config()
    download_roots = get_all_download_dirs(config)
    folder_settings = get_folder_settings(config)
    mapping_payload = load_mapping()
    repositories = mapping_payload.get("repositories", [])

    scanned_repo_roots = 0
    moved_release_folders = 0
    missing_destination_warnings = 0
    skipped_without_destination = 0

    if not download_roots:
        print("No configured download roots found; nothing to migrate.")
        return {
            "scanned_repo_roots": scanned_repo_roots,
            "moved_release_folders": moved_release_folders,
            "missing_destination_warnings": missing_destination_warnings,
            "skipped_without_destination": skipped_without_destination,
        }

    for download_root in download_roots:
        complete_root = os.path.join(
            download_root,
            folder_settings["ghaadd_root"],
            folder_settings["complete"],
        )
        if not os.path.isdir(complete_root):
            continue

        for entry in repositories:
            if not isinstance(entry, dict):
                continue

            repo_name = str(entry.get("name") or "").strip()
            if not repo_name:
                continue

            repo_parent_folder = _build_repo_parent_folder(repo_name)
            source_repo_dir = os.path.join(complete_root, repo_parent_folder)
            if not os.path.isdir(source_repo_dir):
                continue

            scanned_repo_roots += 1

            destination_value = str(entry.get("destination") or "").strip()
            if not destination_value:
                skipped_without_destination += 1
                continue

            target_repo_root, uses_mapping_destination, fallback_warning = _resolve_finalized_base_directory(
                repo_name,
                complete_root,
            )
            if fallback_warning:
                missing_destination_warnings += 1
                print(f"⚠️ {fallback_warning}")
                log_warning("DESTINATION", fallback_warning)
                continue

            if not uses_mapping_destination:
                skipped_without_destination += 1
                continue

            moved_now = _move_complete_repo_tree(source_repo_dir, target_repo_root)
            if moved_now > 0:
                moved_release_folders += moved_now
                print(
                    "✅ Deferred move completed for "
                    f"{repo_name}: moved {moved_now} item(s) to '{target_repo_root}'."
                )
                _warn_if_destination_limit_exceeded(repo_name, target_repo_root)

    print(
        "Deferred Complete->Destination move summary: "
        f"scanned_repo_roots={scanned_repo_roots}, "
        f"moved_release_folders={moved_release_folders}, "
        f"missing_destination_warnings={missing_destination_warnings}, "
        f"skipped_without_destination={skipped_without_destination}"
    )

    return {
        "scanned_repo_roots": scanned_repo_roots,
        "moved_release_folders": moved_release_folders,
        "missing_destination_warnings": missing_destination_warnings,
        "skipped_without_destination": skipped_without_destination,
    }


def move_processing_folder_to_complete(working_dir: str, repo: Optional[str] = None) -> Optional[str]:
    """Move a finished release folder from Processing to final destination."""
    if not working_dir:
        return None

    normalized_working_dir = os.path.normpath(working_dir)
    if not os.path.isdir(normalized_working_dir):
        return None

    folder_settings = get_folder_settings()
    processing_folder_lower = folder_settings["processing"].lower()

    processing_root = normalized_working_dir
    while True:
        parent_dir = os.path.dirname(processing_root)
        if parent_dir == processing_root:
            processing_root = ""
            break
        if os.path.basename(parent_dir).lower() == processing_folder_lower:
            processing_root = parent_dir
            break
        processing_root = parent_dir

    if not processing_root:
        return None

    ghaadd_root = os.path.dirname(processing_root)
    complete_dir = os.path.join(ghaadd_root, folder_settings["complete"])
    relative_path = os.path.relpath(normalized_working_dir, processing_root)

    base_destination, uses_mapping_destination, fallback_warning = _resolve_finalized_base_directory(
        repo,
        complete_dir,
    )
    if fallback_warning:
        print(f"   ⚠️ {fallback_warning}")
        log_warning("DESTINATION", fallback_warning)
    os.makedirs(base_destination, exist_ok=True)

    target_relative_path = relative_path
    relative_parts = [part for part in os.path.normpath(relative_path).split(os.sep) if part and part != "."]
    if uses_mapping_destination:
        if len(relative_parts) > 1:
            target_relative_path = os.path.join(*relative_parts[1:])

    target_dir = os.path.normpath(os.path.join(base_destination, target_relative_path))
    os.makedirs(os.path.dirname(target_dir), exist_ok=True)
    target_dir = _resolve_directory_name_collision(target_dir)
    shutil.move(normalized_working_dir, target_dir)

    repo_destination_root = base_destination
    if not uses_mapping_destination and relative_parts:
        repo_destination_root = os.path.join(base_destination, relative_parts[0])

    _warn_if_destination_limit_exceeded(repo, repo_destination_root)

    _remove_empty_processing_parents(
        start_dir=os.path.dirname(normalized_working_dir),
        processing_root=processing_root,
    )
    return target_dir


def move_processing_folder_to_partial(working_dir: str) -> Optional[str]:
    """Move an incomplete superseded release folder from Processing to Partial."""
    if not working_dir:
        return None

    normalized_working_dir = os.path.normpath(working_dir)
    if not os.path.isdir(normalized_working_dir):
        return None

    folder_settings = get_folder_settings()
    processing_folder_lower = folder_settings["processing"].lower()

    processing_root = normalized_working_dir
    while True:
        parent_dir = os.path.dirname(processing_root)
        if parent_dir == processing_root:
            processing_root = ""
            break
        if os.path.basename(parent_dir).lower() == processing_folder_lower:
            processing_root = parent_dir
            break
        processing_root = parent_dir

    if not processing_root:
        return None

    ghaadd_root = os.path.dirname(processing_root)
    partial_dir = os.path.join(ghaadd_root, folder_settings["partial"])
    os.makedirs(partial_dir, exist_ok=True)

    relative_path = os.path.relpath(normalized_working_dir, processing_root)
    target_dir = os.path.normpath(os.path.join(partial_dir, relative_path))
    os.makedirs(os.path.dirname(target_dir), exist_ok=True)
    target_dir = _resolve_directory_name_collision(target_dir)
    shutil.move(normalized_working_dir, target_dir)

    _remove_empty_processing_parents(
        start_dir=os.path.dirname(normalized_working_dir),
        processing_root=processing_root,
    )
    return target_dir


def _remove_empty_processing_parents(start_dir: str, processing_root: str) -> None:
    """Remove empty parent directories under Processing after a move.

    This prunes only the branch that contained the moved folder and stops
    before removing the Processing root itself.
    """
    current_dir = os.path.normpath(start_dir)
    stop_dir = os.path.normpath(processing_root)

    while os.path.normcase(current_dir) != os.path.normcase(stop_dir):
        if not os.path.isdir(current_dir):
            parent_dir = os.path.dirname(current_dir)
            if parent_dir == current_dir:
                break
            current_dir = parent_dir
            continue

        try:
            os.rmdir(current_dir)
        except OSError:
            # Stop as soon as we hit a non-empty or non-removable directory.
            break

        parent_dir = os.path.dirname(current_dir)
        if parent_dir == current_dir:
            break
        current_dir = parent_dir


def _is_source_item(item_key: str) -> bool:
    """Return True when a queue item represents GitHub source archives."""
    return item_key.startswith("source:")


def _insert_source_marker(file_name: str) -> str:
    """Insert ' (source)' before known source suffixes (.zip/.tar.gz)."""
    lower_name = file_name.lower()

    if lower_name.endswith(".tar.gz"):
        split_index = len(file_name) - len(".tar.gz")
        return f"{file_name[:split_index]} (source){file_name[split_index:]}"

    if lower_name.endswith(".zip"):
        split_index = len(file_name) - len(".zip")
        return f"{file_name[:split_index]} (source){file_name[split_index:]}"

    return f"{file_name} (source)"


def _resolve_source_file_name(file_name: str) -> str:
    """Return the deterministic source filename with marker."""
    return _insert_source_marker(file_name)


def sanitize_folder_name(text):
    """Return a Windows-safe folder name using JS-compatible rules."""
    if not text:
        return "unknown"
    text = text.replace(':', '-')
    text = re.sub(r'[\\/<>\"|?*]', '_', text)
    return text.strip()


def get_short_commit_hash(repo, tag, headers):
    """Return the 7-character commit hash for a specific tag."""
    url = f"https://api.github.com/repos/{repo}/commits/{tag}"
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json().get("sha", "unknown")[:7]
    return "unknown-commit"


def build_folder_name(repo, release_data, headers, raw_name):
    """Build a release folder name from a specific display name."""
    pub_time_str = release_data.get("published_at", "")
    if pub_time_str:
        pub_time_str = pub_time_str.replace("Z", "+00:00")
        pub_date = datetime.datetime.fromisoformat(pub_time_str)
        formatted_date = pub_date.strftime("%Y-%m-%d_%H-%M")
    else:
        formatted_date = "unknown-date"

    raw_tag = release_data.get("tag_name", "unknown-tag")
    raw_commit = get_short_commit_hash(repo, raw_tag, headers)

    safe_name = sanitize_folder_name(raw_name)
    safe_tag = sanitize_folder_name(raw_tag)
    safe_commit = sanitize_folder_name(raw_commit)

    return f"{formatted_date}, {safe_name}, {safe_tag}, {safe_commit}"


def generate_folder_name(repo, release_data, headers):
    """Generate the default folder name based on the release title."""
    release_name = release_data.get("name") or release_data.get("tag_name", "unknown-name")
    return build_folder_name(repo, release_data, headers, release_name)


def is_state_persistence_enabled():
    """Return whether persistent duplicate-detection state is enabled."""
    return not is_state_persistence_disabled()


def build_expected_signature(item: ReleaseAssetQueueItem) -> Optional[str]:
    """Build a stable signature for queue items with trusted upstream metadata."""
    signature_parts = [item.get("key")]
    for field in ("name", "expected_size", "expected_updated_at"):
        value = item.get(field)
        if value is not None:
            signature_parts.append(str(value))

    if len(signature_parts) == 1:
        return None

    return "|".join(signature_parts)


def get_release_data(repo, tag):
    """Return release JSON for a repository tag, or None when unavailable."""
    url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"

    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    response = requests.get(url, headers=headers)

    if response.status_code == 200:
        return response.json()
    if response.status_code == 404:
        print(f"   ⚠️ Release tag {tag} not found yet on GitHub.")
        return None

    print(f"   ❌ API Error ({response.status_code}): {response.text}")
    return None


def get_release_attestation_url(release_data, headers):
    """Return the release attestation download URL from the expanded assets page.

    GitHub exposes this link in expanded-assets HTML, not in release JSON.
    """
    release_page_url = release_data.get("html_url")
    if not release_page_url:
        return None

    expanded_assets_url = release_page_url.replace("/releases/tag/", "/releases/expanded_assets/")
    response = requests.get(expanded_assets_url, headers=headers, timeout=30)
    response.raise_for_status()

    match = re.search(r'href="([^"]+/attestations/[^"]+/download)"', response.text)
    if match:
        return urljoin("https://github.com", match.group(1))

    return None


def _build_download_result(
    status: Literal["SUCCESS", "SKIP", "FAILED"],
    downloaded_count: int,
    skipped_count: int,
    total_items: int,
    include_stats: bool,
    skipped_items: Optional[list[SkippedItemPayload]] = None,
    working_dir: Optional[str] = None,
    skip_reason: Optional[str] = None,
) -> DownloadReleaseResult:
    """Return either legacy status values or a detailed result payload."""
    if skipped_items is None:
        skipped_items = []

    if include_stats:
        return {
            "status": status,
            "downloaded_count": int(downloaded_count),
            "skipped_count": int(skipped_count),
            "total_items": int(total_items),
            "skipped_items": skipped_items,
            "working_dir": working_dir,
            "skip_reason": skip_reason,
        }

    if status == "SUCCESS":
        return True
    if status == "SKIP":
        return "SKIP"
    return False


def download_release(
    repo: str,
    tag: str,
    release_type: Optional[str] = None,
    include_stats: bool = False,
) -> DownloadReleaseResult:
    """Download a GitHub release and all assets using config-driven routing."""
    config = load_config()

    # Always ensure default staging roots exist at download start.
    # This recovers from accidental folder deletion.
    default_download_dir = get_default_download_dir(config)
    _build_staging_directories(default_download_dir)

    download_dir = cast(str, get_download_dir_for_release(repo, release_type, config=config))
    return download_all_assets(repo, tag, download_dir, include_stats=include_stats, release_type=release_type)


def download_all_assets(
    repo: str,
    tag: str,
    download_dir: str,
    include_stats: bool = False,
    release_type: Optional[str] = None,
) -> DownloadReleaseResult:
    """Download all release items with retries and preserved timestamps."""
    max_retries = 1
    retry_delay = 10
    release_data = None
    downloaded_count = 0
    skipped_count = 0
    skipped_items: list[SkippedItemPayload] = []
    final_download_dir: Optional[str] = None

    for attempt in range(max_retries):
        print(f"🔍 Checking GitHub API for {repo} ({tag})...")
        release_data = get_release_data(repo, tag)

        if release_data:
            assets = release_data.get("assets", [])
            has_source = "zipball_url" in release_data or "tarball_url" in release_data

            if len(assets) == 0 and not has_source:
                print(f"   ⏳ No assets or source code found. Attempt {attempt + 1}/{max_retries}. Waiting {retry_delay}s...")
                time.sleep(retry_delay)
                continue
            break

        print(f"   ⏭️ Skipping {repo} ({tag}): Release no longer exists.")
        return _build_download_result(
            "SKIP",
            downloaded_count,
            skipped_count,
            0,
            include_stats,
            skipped_items,
            skip_reason="release_not_found",
        )
    else:
        print(f"   ❌ Timed out waiting for assets to populate for {repo}.")
        return _build_download_result("FAILED", downloaded_count, skipped_count, 0, include_stats, skipped_items)

    headers_api = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers_api["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    repo_name = repo.split('/')[-1]

    download_queue: list[ReleaseAssetQueueItem] = []
    for asset in release_data.get("assets", []):
        download_queue.append({
            "key": f"asset:{asset['id']}",
            "name": asset["name"],
            "url": asset["browser_download_url"],
            "expected_size": asset.get("size"),
            "expected_updated_at": asset.get("updated_at"),
        })

    if "zipball_url" in release_data:
        download_queue.append({
            "key": "source:zipball",
            "name": f"{repo_name}-{tag}-Source_code.zip",
            "url": release_data["zipball_url"],
            "expected_size": None,
            "expected_updated_at": release_data.get("target_commitish"),
        })
    if "tarball_url" in release_data:
        download_queue.append({
            "key": "source:tarball",
            "name": f"{repo_name}-{tag}-Source_code.tar.gz",
            "url": release_data["tarball_url"],
            "expected_size": None,
            "expected_updated_at": release_data.get("target_commitish"),
        })

    attestation_url = get_release_attestation_url(release_data, headers_api)
    if attestation_url:
        download_queue.append({
            "key": f"attestation:{attestation_url}",
            "name": None,
            "url": attestation_url,
            "expected_size": None,
            "expected_updated_at": release_data.get("published_at"),
        })

    print(f"📦 Found {len(download_queue)} total items to download (including source code and attestations).")

    non_source_items = [item for item in download_queue if not _is_source_item(item["key"])]
    source_items = [item for item in download_queue if _is_source_item(item["key"])]
    ordered_download_queue = non_source_items + source_items

    custom_folder = generate_folder_name(repo, release_data, headers_api)
    repo_parent_folder = _build_repo_parent_folder(repo)
    effective_release_type = release_type
    if not effective_release_type:
        effective_release_type = "Pre-release" if release_data.get("prerelease") else "Release"
    release_type_folder = sanitize_folder_name(effective_release_type) or "Release"
    processing_download_dir, _ = _build_staging_directories(download_dir)
    final_download_dir = os.path.join(
        processing_download_dir,
        repo_parent_folder,
        release_type_folder,
        custom_folder,
    )
    os.makedirs(final_download_dir, exist_ok=True)

    release_key = f"{repo}|{tag}"
    state_enabled = is_state_persistence_enabled()
    state_db = open_database() if state_enabled else None
    if state_db is not None:
        prune_release_state(state_db, release_key, {item["key"] for item in download_queue}, download_dir)
        release_state = load_release_state(state_db, release_key)
    else:
        release_state = {}

    print(f"📁 Target Folder: {custom_folder}")

    fallback_time = release_data.get("published_at")
    if fallback_time:
        fallback_time = fallback_time.replace("Z", "+00:00")
        fallback_timestamp = datetime.datetime.fromisoformat(fallback_time).timestamp()
    else:
        fallback_timestamp = time.time()

    total_files = len(ordered_download_queue)
    per_file_retries = 3
    per_file_retry_delay = 2
    normal_downloaded_count = 0

    def refresh_release_state():
        nonlocal release_state
        if state_db is not None:
            release_state = load_release_state(state_db, release_key)

    def should_skip_existing_file(file_path, item_key, expected_signature, remote_size, remote_last_modified, remote_etag):
        """Return True when the local file can be treated as up to date."""
        if not os.path.exists(file_path):
            return False

        state_entry = release_state.get(item_key, {})

        try:
            local_size = os.path.getsize(file_path)
        except OSError:
            return False

        if expected_signature and state_entry.get("expected_signature") == expected_signature:
            recorded_size = state_entry.get("local_size")
            if recorded_size is None or recorded_size == local_size:
                return True

        if remote_etag and state_entry.get("etag") == remote_etag:
            recorded_size = state_entry.get("size")
            if remote_size is None or recorded_size == remote_size == local_size:
                return True

        if remote_size is not None and remote_last_modified is not None:
            try:
                local_mtime = os.path.getmtime(file_path)
                if local_size == remote_size and local_mtime >= remote_last_modified:
                    return True
            except OSError:
                return False

        return False

    try:
        with requests.Session() as session:
            session.headers.update(headers_api)

            for i, item in enumerate(ordered_download_queue, 1):
                item_key = item["key"]
                is_source_item = _is_source_item(item_key)
                expected_signature = build_expected_signature(item)
                file_name = item["name"]
                download_url = item["url"]

                if file_name is not None and is_source_item:
                    resolved_source_name = _resolve_source_file_name(file_name)
                    if resolved_source_name != file_name:
                        print(
                            f"   ℹ️ Source filename adjusted. "
                            f"Using '{resolved_source_name}' instead of '{file_name}'."
                        )
                    file_name = resolved_source_name

                file_path: Optional[str] = None
                rel_file_path: Optional[str] = None
                force_redownload_source = False
                if file_name is not None:
                    file_path = os.path.join(final_download_dir, file_name)
                    rel_file_path = os.path.relpath(file_path, download_dir)
                    if is_source_item and os.path.exists(file_path):
                        if normal_downloaded_count == 0:
                            print(
                                f"   ⏭️ Skipping ({i}/{total_files}): {file_name} "
                                "because no normal assets changed in this run."
                            )
                            skipped_count += 1
                            skipped_items.append(_build_skipped_item_payload(item_key, file_name, "source_unchanged_no_asset_changes"))
                            continue

                        force_redownload_source = True
                        print(
                            f"   🔁 Refreshing ({i}/{total_files}): {file_name} "
                            "because normal assets changed in this run."
                        )

                if file_name is not None:
                    try:
                        head_response = session.head(download_url, allow_redirects=True, timeout=(10, 30))
                        head_response.raise_for_status()

                        remote_size = None
                        content_length = head_response.headers.get('Content-Length')
                        if content_length and content_length.isdigit():
                            remote_size = int(content_length)

                        remote_last_modified = None
                        head_last_modified = head_response.headers.get('Last-Modified')
                        if head_last_modified:
                            parsed_date = parsedate_tz(head_last_modified)
                            if parsed_date:
                                remote_last_modified = mktime_tz(parsed_date)

                        remote_etag = head_response.headers.get('ETag')

                        if not force_redownload_source and should_skip_existing_file(
                            file_path,
                            item_key,
                            expected_signature,
                            remote_size,
                            remote_last_modified,
                            remote_etag,
                        ):
                            if state_db is not None:
                                save_state_entry(
                                    state_db,
                                    release_key,
                                    item_key,
                                    file_name,
                                    rel_file_path,
                                    expected_signature,
                                    remote_size,
                                    remote_last_modified,
                                    remote_etag,
                                    download_dir,
                                )
                                refresh_release_state()
                            print(f"   ⏭️ Skipping ({i}/{total_files}): {file_name} already exists and matches remote metadata.")
                            skipped_count += 1
                            skipped_items.append(_build_skipped_item_payload(item_key, file_name, "metadata_match"))
                            continue
                    except requests.RequestException:
                        pass

                print(f"   📥 Downloading ({i}/{total_files}): {file_name or 'attestation'}")

                file_saved = False
                for download_attempt in range(1, per_file_retries + 1):
                    temp_file_path = None
                    try:
                        with session.get(download_url, stream=True, timeout=(10, 60)) as response:
                            response.raise_for_status()

                            if file_name is None:
                                content_disposition = response.headers.get('Content-Disposition', '')
                                if 'filename=' in content_disposition:
                                    extracted_name = re.findall(r'filename="?([^"]+)"?', content_disposition)
                                    if extracted_name:
                                        file_name = extracted_name[0]
                                    else:
                                        file_name = f"attestation-{i}.json"
                                else:
                                    file_name = f"attestation-{i}.json"

                            file_path = os.path.join(final_download_dir, file_name)
                            rel_file_path = os.path.relpath(file_path, download_dir)
                            temp_file_path = f"{file_path}.part"

                            remote_size = None
                            content_length = response.headers.get('Content-Length')
                            if content_length and content_length.isdigit():
                                remote_size = int(content_length)

                            remote_last_modified = None
                            stream_last_modified = response.headers.get('Last-Modified')
                            if stream_last_modified:
                                parsed_date = parsedate_tz(stream_last_modified)
                                if parsed_date:
                                    remote_last_modified = mktime_tz(parsed_date)

                            remote_etag = response.headers.get('ETag')

                            if not force_redownload_source and should_skip_existing_file(
                                file_path,
                                item_key,
                                expected_signature,
                                remote_size,
                                remote_last_modified,
                                remote_etag,
                            ):
                                if state_db is not None:
                                    save_state_entry(
                                        state_db,
                                        release_key,
                                        item_key,
                                        file_name,
                                        rel_file_path,
                                        expected_signature,
                                        remote_size,
                                        remote_last_modified,
                                        remote_etag,
                                        download_dir,
                                    )
                                    refresh_release_state()
                                print(f"   ⏭️ Skipping ({i}/{total_files}): {file_name} already exists and matches remote metadata.")
                                skipped_count += 1
                                skipped_items.append(_build_skipped_item_payload(item_key, file_name, "metadata_match"))
                                file_saved = True
                                break

                            with open(temp_file_path, 'wb') as file_handle:
                                for chunk in response.iter_content(chunk_size=8192):
                                    if chunk:
                                        file_handle.write(chunk)

                            os.replace(temp_file_path, file_path)

                            if 'Last-Modified' in response.headers:
                                last_modified_str = response.headers['Last-Modified']
                                parsed_date = parsedate_tz(last_modified_str)
                                if parsed_date:
                                    timestamp = mktime_tz(parsed_date)
                                    os.utime(file_path, (timestamp, timestamp))
                                else:
                                    os.utime(file_path, (fallback_timestamp, fallback_timestamp))
                            else:
                                os.utime(file_path, (fallback_timestamp, fallback_timestamp))

                            if state_db is not None:
                                save_state_entry(
                                    state_db,
                                    release_key,
                                    item_key,
                                    file_name,
                                    rel_file_path,
                                    expected_signature,
                                    remote_size,
                                    remote_last_modified,
                                    remote_etag,
                                    download_dir,
                                )
                                refresh_release_state()

                            print("   ✅ Saved & timestamp preserved.")
                            downloaded_count += 1
                            if not is_source_item:
                                normal_downloaded_count += 1
                            file_saved = True
                            break
                    except (ChunkedEncodingError, ConnectionError, Timeout) as error:
                        if temp_file_path and os.path.exists(temp_file_path):
                            os.remove(temp_file_path)

                        if download_attempt < per_file_retries:
                            sleep_seconds = per_file_retry_delay * download_attempt
                            print(
                                f"   ⚠️ Network error while downloading {file_name or 'attestation'} "
                                f"(attempt {download_attempt}/{per_file_retries}): {error}. "
                                f"Retrying in {sleep_seconds}s..."
                            )
                            time.sleep(sleep_seconds)
                        else:
                            print(
                                f"   ❌ Failed to download {file_name or 'attestation'} after "
                                f"{per_file_retries} attempts: {error}"
                            )
                    except requests.RequestException as error:
                        if temp_file_path and os.path.exists(temp_file_path):
                            os.remove(temp_file_path)
                        print(f"   ❌ Request error while downloading {file_name or 'attestation'}: {error}")
                        break
                    except OSError as error:
                        if temp_file_path and os.path.exists(temp_file_path):
                            os.remove(temp_file_path)
                        print(f"   ❌ File write error for {file_name or 'attestation'}: {error}")
                        break

                if not file_saved:
                    return _build_download_result(
                        "FAILED",
                        downloaded_count,
                        skipped_count,
                        total_files,
                        include_stats,
                        skipped_items,
                        working_dir=final_download_dir,
                    )
    finally:
        if state_db is not None:
            state_db.close()

    return _build_download_result(
        "SUCCESS",
        downloaded_count,
        skipped_count,
        total_files,
        include_stats,
        skipped_items,
        working_dir=final_download_dir,
    )

# Run this file directly for manual release-download testing.
if __name__ == "__main__":
    test_repo = "cli/cli"
    test_tag = "v2.30.0"
    test_download_dir = cast(str, get_download_dir_for_release(test_repo, "release"))
    print(f"Base download directory set to: {test_download_dir}")
    download_all_assets(test_repo, test_tag, test_download_dir)
