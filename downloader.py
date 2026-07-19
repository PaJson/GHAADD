import datetime
import json
import os
import re
import requests
import time
from dotenv import load_dotenv
from email.utils import parsedate_tz, mktime_tz
from urllib.parse import urljoin
from requests.exceptions import ChunkedEncodingError, ConnectionError, Timeout

# Load environment variables (.env)
load_dotenv()
GITHUB_TOKEN = os.getenv("GITHUB_PAT")

# Get the custom download directory, or fallback to the system's default user Downloads folder
BASE_DOWNLOAD_DIR = os.getenv("DEFAULT_DOWNLOAD_DIR")
if not BASE_DOWNLOAD_DIR:
    BASE_DOWNLOAD_DIR = os.path.join(os.path.expanduser('~'), 'Downloads')

STATE_FILE_NAME = ".ghaadd-state.json"

def sanitize_folder_name(text):
    """Replicates the JS Windows-safe sanitization."""
    if not text:
        return "unknown"
    # Replace colons with dashes
    text = text.replace(':', '-')
    # Replace invalid Windows chars with underscores
    text = re.sub(r'[\\/<>\"|?*]', '_', text)
    # Remove leading/trailing whitespace
    return text.strip()

def get_short_commit_hash(repo, tag, headers):
    """Fetches the 7-character commit hash for a specific tag."""
    url = f"https://api.github.com/repos/{repo}/commits/{tag}"
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json().get("sha", "unknown")[:7]
    return "unknown-commit"

def build_folder_name(repo, release_data, headers, raw_name):
    """Builds a release folder name from a specific display name."""
    # 1. Date (YYYY-MM-DD_HH-MM in UTC)
    pub_time_str = release_data.get("published_at", "")
    if pub_time_str:
        # The API returns UTC time with a 'Z' at the end
        pub_time_str = pub_time_str.replace("Z", "+00:00")
        pub_date = datetime.datetime.fromisoformat(pub_time_str)
        formatted_date = pub_date.strftime("%Y-%m-%d_%H-%M")
    else:
        formatted_date = "unknown-date"

    # 3. Tag
    raw_tag = release_data.get("tag_name", "unknown-tag")
    
    # 4. Commit Hash
    raw_commit = get_short_commit_hash(repo, raw_tag, headers)

    # Sanitize and combine
    safe_name = sanitize_folder_name(raw_name)
    safe_tag = sanitize_folder_name(raw_tag)
    safe_commit = sanitize_folder_name(raw_commit)

    return f"{formatted_date}, {safe_name}, {safe_tag}, {safe_commit}"


def generate_folder_name(repo, release_data, headers):
    """Generates a stable folder name for a release."""
    return build_folder_name(repo, release_data, headers, repo.replace("/", "-"))


def generate_legacy_folder_name(repo, release_data, headers):
    """Generates the pre-migration folder name based on the release title."""
    legacy_name = release_data.get("name") or release_data.get("tag_name", "unknown-name")
    return build_folder_name(repo, release_data, headers, legacy_name)


def load_download_state(download_dir):
    """Loads persisted per-release download metadata."""
    state_path = os.path.join(download_dir, STATE_FILE_NAME)
    if not os.path.exists(state_path):
        return state_path, {"release": {}, "assets": {}}

    try:
        with open(state_path, 'r', encoding='utf-8') as state_file:
            data = json.load(state_file)
    except (OSError, json.JSONDecodeError):
        return state_path, {"release": {}, "assets": {}}

    if not isinstance(data, dict):
        return state_path, {"release": {}, "assets": {}}

    data.setdefault("release", {})
    data.setdefault("assets", {})
    return state_path, data


def save_download_state(state_path, state):
    """Atomically saves per-release download metadata."""
    temp_state_path = f"{state_path}.tmp"
    with open(temp_state_path, 'w', encoding='utf-8') as state_file:
        json.dump(state, state_file, indent=2, sort_keys=True)
    os.replace(temp_state_path, state_path)


def build_expected_signature(item):
    """Builds a stable signature for queue items with trusted upstream metadata."""
    signature_parts = [item.get("key")]
    for field in ("name", "expected_size", "expected_updated_at"):
        value = item.get(field)
        if value is not None:
            signature_parts.append(str(value))

    if len(signature_parts) == 1:
        return None

    return "|".join(signature_parts)


def update_state_entry(state, item_key, file_name, file_path, expected_signature, remote_size, remote_last_modified, remote_etag):
    """Updates the persisted metadata for a downloaded or verified asset."""
    entry = {
        "file_name": file_name,
        "file_path": file_path,
        "size": remote_size,
        "last_modified": remote_last_modified,
        "etag": remote_etag,
        "expected_signature": expected_signature,
    }

    try:
        entry["local_size"] = os.path.getsize(file_path)
    except OSError:
        entry["local_size"] = None

    try:
        entry["local_mtime"] = os.path.getmtime(file_path)
    except OSError:
        entry["local_mtime"] = None

    state["assets"][item_key] = entry

def get_release_data(repo, tag):
    """
    Queries the GitHub REST API for a specific repository release tag.
    Returns the full release JSON if found, or None if it doesn't exist yet.
    """
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
    elif response.status_code == 404:
        print(f"   ⚠️ Release tag {tag} not found yet on GitHub.")
        return None
    else:
        print(f"   ❌ API Error ({response.status_code}): {response.text}")
        return None


def get_release_attestation_url(release_data, headers):
    """
    Extracts the release attestation download URL from the expanded assets page.

    GitHub exposes the attestation link in the expanded assets HTML, but it is not
    currently included in the REST release JSON payload.
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

def download_release(repo, tag):
    """
    Downloads a GitHub release and all its assets.
    Wrapper around download_all_assets() using the configured BASE_DOWNLOAD_DIR.
    
    Args:
        repo: Repository in format 'owner/repo' (e.g., 'Genymobile/scrcpy')
        tag: Release tag (e.g., 'v4.1')
    
    Returns:
        bool: True if download successful, False otherwise
    """
    return download_all_assets(repo, tag, BASE_DOWNLOAD_DIR)


def download_all_assets(repo, tag, download_dir):
    """
    Checks for assets (and source code), handles retry logic, 
    and downloads every file while preserving timestamps.
    """
    max_retries = 1
    retry_delay = 10 
    release_data = None
    
    for attempt in range(max_retries):
        print(f"🔍 Checking GitHub API for {repo} ({tag})...")
        release_data = get_release_data(repo, tag)
        
        if release_data:
            assets = release_data.get("assets", [])
            # NEW: Check if there are assets OR if source code URLs exist
            has_source = "zipball_url" in release_data or "tarball_url" in release_data
            
            if len(assets) == 0 and not has_source:
                print(f"   ⏳ No assets or source code found. Attempt {attempt + 1}/{max_retries}. Waiting {retry_delay}s...")
                time.sleep(retry_delay)
                continue
            break
        else:
            print(f"   ⏭️ Skipping {repo} ({tag}): Release no longer exists.")
            return "SKIP"
    else:
        print(f"   ❌ Timed out waiting for assets to populate for {repo}.")
        return False

    # API headers are needed for attestation lookup, folder naming, and downloads.
    headers_api = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers_api["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    # Extract the base repository name (e.g., 'scrcpy' from 'Genymobile/scrcpy')
    repo_name = repo.split('/')[-1]
    
    # 1. Start with the standard uploaded assets
    download_queue = []
    for asset in release_data.get("assets", []):
        download_queue.append({
            "key": f"asset:{asset['id']}",
            "name": asset["name"],
            "url": asset["browser_download_url"],
            "expected_size": asset.get("size"),
            "expected_updated_at": asset.get("updated_at"),
        })
        
    # 2. Add the auto-generated Source Code (zip and tar.gz)
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

    # 3. Add Release Attestations (if present)
    attestation_url = get_release_attestation_url(release_data, headers_api)
    if attestation_url:
        # Add to queue with a placeholder name - actual name will be extracted from Content-Disposition header
        download_queue.append({
            "key": f"attestation:{attestation_url}",
            "name": None,  # Will be extracted from response header
            "url": attestation_url,
            "is_attestation": True,
            "expected_size": None,
            "expected_updated_at": release_data.get("published_at"),
        })

    print(f"📦 Found {len(download_queue)} total items to download (including source code and attestations).")
    
    # 1. CREATE THE DIRECTORY ONCE (Outside the loop)
    # Generate the custom folder name and create it
    custom_folder = generate_folder_name(repo, release_data, headers_api)
    final_download_dir = os.path.join(download_dir, custom_folder)
    legacy_folder = generate_legacy_folder_name(repo, release_data, headers_api)
    legacy_download_dir = os.path.join(download_dir, legacy_folder)
    if not os.path.exists(final_download_dir) and os.path.exists(legacy_download_dir):
        custom_folder = legacy_folder
        final_download_dir = legacy_download_dir
    os.makedirs(final_download_dir, exist_ok=True)
    state_path, download_state = load_download_state(final_download_dir)
    download_state["release"] = {
        "repo": repo,
        "tag": tag,
        "release_id": release_data.get("id"),
        "published_at": release_data.get("published_at"),
        "target_commitish": release_data.get("target_commitish"),
    }
    
    print(f"📁 Target Folder: {custom_folder}")

    # 2. PARSE FALLBACK TIME
    # The API returns ISO 8601 format like "2024-05-20T14:32:00Z"
    fallback_time = release_data.get("published_at")
    if fallback_time:
        fallback_time = fallback_time.replace("Z", "+00:00")
        fallback_timestamp = datetime.datetime.fromisoformat(fallback_time).timestamp()
    else:
        fallback_timestamp = time.time()

    # Calculate the total count once before the loop
    total_files = len(download_queue)
    per_file_retries = 3
    per_file_retry_delay = 2

    def should_skip_existing_file(file_path, item_key, expected_signature, remote_size, remote_last_modified, remote_etag):
        """
        Returns True when the local file can be considered up-to-date.
        Prefers a persisted signature match; otherwise requires corroborating remote metadata.
        """
        if not os.path.exists(file_path):
            return False

        state_entry = download_state["assets"].get(item_key, {})

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

    # 3. DOWNLOAD THE FILES
    # NEW: Create a session to reuse the underlying TCP connection
    with requests.Session() as session:
        # Apply your API headers to the entire session
        session.headers.update(headers_api)
        
        for i, item in enumerate(download_queue, 1):
            item_key = item["key"]
            expected_signature = build_expected_signature(item)
            file_name = item["name"]
            download_url = item["url"]

            # For known asset names, use HEAD to skip files we already have.
            if file_name is not None:
                file_path = os.path.join(final_download_dir, file_name)
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

                    if should_skip_existing_file(
                        file_path,
                        item_key,
                        expected_signature,
                        remote_size,
                        remote_last_modified,
                        remote_etag,
                    ):
                        update_state_entry(
                            download_state,
                            item_key,
                            file_name,
                            file_path,
                            expected_signature,
                            remote_size,
                            remote_last_modified,
                            remote_etag,
                        )
                        print(f"   ⏭️ Skipping ({i}/{total_files}): {file_name} already exists and matches remote metadata.")
                        continue
                except requests.RequestException:
                    # If HEAD is not supported or fails, continue with normal GET download.
                    pass
            
            print(f"   📥 Downloading ({i}/{total_files}): {file_name or 'attestation'}")

            file_saved = False
            for download_attempt in range(1, per_file_retries + 1):
                temp_file_path = None
                try:
                    # Keep connect/read timeouts bounded so retries can trigger on bad links.
                    with session.get(download_url, stream=True, timeout=(10, 60)) as r:
                        r.raise_for_status()

                        # If filename is None (attestation), extract from Content-Disposition header
                        if file_name is None:
                            content_disposition = r.headers.get('Content-Disposition', '')
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
                        content_length = r.headers.get('Content-Length')
                        if content_length and content_length.isdigit():
                            remote_size = int(content_length)

                        remote_last_modified = None
                        stream_last_modified = r.headers.get('Last-Modified')
                        if stream_last_modified:
                            parsed_date = parsedate_tz(stream_last_modified)
                            if parsed_date:
                                remote_last_modified = mktime_tz(parsed_date)

                        remote_etag = r.headers.get('ETag')

                        if should_skip_existing_file(
                            file_path,
                            item_key,
                            expected_signature,
                            remote_size,
                            remote_last_modified,
                            remote_etag,
                        ):
                            update_state_entry(
                                download_state,
                                item_key,
                                file_name,
                                file_path,
                                expected_signature,
                                remote_size,
                                remote_last_modified,
                                remote_etag,
                            )
                            print(
                                f"   ⏭️ Skipping ({i}/{total_files}): {file_name} already exists and matches remote metadata."
                            )
                            file_saved = True
                            break

                        with open(temp_file_path, 'wb') as f:
                            for chunk in r.iter_content(chunk_size=8192):
                                if chunk:
                                    f.write(chunk)

                        os.replace(temp_file_path, file_path)

                        # Attempt to use the server's Last-Modified header
                        if 'Last-Modified' in r.headers:
                            last_modified_str = r.headers['Last-Modified']
                            parsed_date = parsedate_tz(last_modified_str)
                            if parsed_date:
                                timestamp = mktime_tz(parsed_date)
                                os.utime(file_path, (timestamp, timestamp))
                            else:
                                os.utime(file_path, (fallback_timestamp, fallback_timestamp))
                        else:
                            os.utime(file_path, (fallback_timestamp, fallback_timestamp))

                        update_state_entry(
                            download_state,
                            item_key,
                            file_name,
                            file_path,
                            expected_signature,
                            remote_size,
                            remote_last_modified,
                            remote_etag,
                        )

                        print(f"   ✅ Saved & timestamp preserved.")
                        file_saved = True
                        break
                except (ChunkedEncodingError, ConnectionError, Timeout) as e:
                    if temp_file_path and os.path.exists(temp_file_path):
                        os.remove(temp_file_path)

                    if download_attempt < per_file_retries:
                        sleep_seconds = per_file_retry_delay * download_attempt
                        print(
                            f"   ⚠️ Network error while downloading {file_name or 'attestation'} "
                            f"(attempt {download_attempt}/{per_file_retries}): {e}. "
                            f"Retrying in {sleep_seconds}s..."
                        )
                        time.sleep(sleep_seconds)
                    else:
                        print(
                            f"   ❌ Failed to download {file_name or 'attestation'} after "
                            f"{per_file_retries} attempts: {e}"
                        )
                except requests.RequestException as e:
                    if temp_file_path and os.path.exists(temp_file_path):
                        os.remove(temp_file_path)
                    print(f"   ❌ Request error while downloading {file_name or 'attestation'}: {e}")
                    break
                except OSError as e:
                    if temp_file_path and os.path.exists(temp_file_path):
                        os.remove(temp_file_path)
                    print(f"   ❌ File write error for {file_name or 'attestation'}: {e}")
                    break

            if not file_saved:
                return False

    save_download_state(state_path, download_state)

    return True

if __name__ == "__main__":
    test_repo = "Genymobile/scrcpy"
    test_tag = "v4.1"
    
    # We pass the base directory directly. 
    # The script will append your custom syntax folder directly inside it.
    print(f"Base download directory set to: {BASE_DOWNLOAD_DIR}")
    download_all_assets(test_repo, test_tag, BASE_DOWNLOAD_DIR)