import datetime
import os
import re
import requests
import shutil
import time
from db_manager import open_database, load_release_state, save_state_entry, prune_release_state
from dotenv import load_dotenv
from config_manager import get_download_dir_for_release, is_state_persistence_disabled
from email.utils import parsedate_tz, mktime_tz
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
    """Return (processing_dir, done_dir) under the GHAADD staging root."""
    ghaadd_root = os.path.join(base_download_dir, "GHAADD")
    processing_dir = os.path.join(ghaadd_root, "Processing")
    done_dir = os.path.join(ghaadd_root, "Done")
    os.makedirs(processing_dir, exist_ok=True)
    os.makedirs(done_dir, exist_ok=True)
    return processing_dir, done_dir


def _build_repo_parent_folder(repo: str) -> str:
    """Return a safe parent folder name like 'repo (owner)' from 'owner/repo'."""
    if not repo:
        return "unknown (unknown)"

    owner, repo_name = (repo.split("/", 1) + [""])[:2]
    safe_owner = sanitize_folder_name(owner) or "unknown"
    safe_repo_name = sanitize_folder_name(repo_name or owner) or "unknown"
    return f"{safe_repo_name} ({safe_owner})"


def move_processing_folder_to_done(working_dir: str) -> Optional[str]:
    """Move a finished release folder from .../GHAADD/Processing to .../GHAADD/Done."""
    if not working_dir:
        return None

    normalized_working_dir = os.path.normpath(working_dir)
    if not os.path.isdir(normalized_working_dir):
        return None

    processing_root = normalized_working_dir
    while True:
        parent_dir = os.path.dirname(processing_root)
        if parent_dir == processing_root:
            processing_root = ""
            break
        if os.path.basename(parent_dir).lower() == "processing":
            processing_root = parent_dir
            break
        processing_root = parent_dir

    if not processing_root:
        return None

    ghaadd_root = os.path.dirname(processing_root)
    done_dir = os.path.join(ghaadd_root, "Done")
    os.makedirs(done_dir, exist_ok=True)

    relative_path = os.path.relpath(normalized_working_dir, processing_root)
    target_dir = os.path.normpath(os.path.join(done_dir, relative_path))
    os.makedirs(os.path.dirname(target_dir), exist_ok=True)
    target_dir = _resolve_directory_name_collision(target_dir)
    shutil.move(normalized_working_dir, target_dir)
    return target_dir


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
    download_dir = cast(str, get_download_dir_for_release(repo, release_type))
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
        prune_release_state(state_db, release_key, {item["key"] for item in download_queue})
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
                force_redownload_source = False
                if file_name is not None:
                    file_path = os.path.join(final_download_dir, file_name)
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
                                    file_path,
                                    expected_signature,
                                    remote_size,
                                    remote_last_modified,
                                    remote_etag,
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
                                        file_path,
                                        expected_signature,
                                        remote_size,
                                        remote_last_modified,
                                        remote_etag,
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
                                    file_path,
                                    expected_signature,
                                    remote_size,
                                    remote_last_modified,
                                    remote_etag,
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
