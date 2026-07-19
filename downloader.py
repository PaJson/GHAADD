import datetime
import os
import re
import requests
import sqlite3
import time
from dotenv import load_dotenv
from config_manager import get_default_download_dir, is_state_persistence_disabled
from email.utils import parsedate_tz, mktime_tz
from requests.exceptions import ChunkedEncodingError, ConnectionError, Timeout
from urllib.parse import urljoin

# Load environment variables (.env)
load_dotenv()
GITHUB_TOKEN = os.getenv("GITHUB_PAT")

# Get the custom download directory from config.json
BASE_DOWNLOAD_DIR = get_default_download_dir()

STATE_DB_NAME = "state.db"
STATE_PERSISTENCE_ENV_VAR = "DISABLE_STATE_PERSISTENCE"

def sanitize_folder_name(text):
    """Replicates the JS Windows-safe sanitization."""
    if not text:
        return "unknown"
    text = text.replace(':', '-')
    text = re.sub(r'[\\/<>\"|?*]', '_', text)
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
    """Generates a stable folder name for a release."""
    return build_folder_name(repo, release_data, headers, repo.replace("/", "-"))

def generate_legacy_folder_name(repo, release_data, headers):
    """Generates the pre-migration folder name based on the release title."""
    legacy_name = release_data.get("name") or release_data.get("tag_name", "unknown-name")
    return build_folder_name(repo, release_data, headers, legacy_name)

def get_state_db_path():
    """Returns the SQLite state database path beside the app files."""
    app_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(app_dir, STATE_DB_NAME)

def is_state_persistence_enabled():
    """Returns whether persistent duplicate-detection state is enabled."""
    return not is_state_persistence_disabled()

def purge_state_database():
    """Deletes the local state database if it exists."""
    state_db_path = get_state_db_path()
    if not os.path.exists(state_db_path):
        return False

    os.remove(state_db_path)
    return True

def open_state_database():
    """Opens the central SQLite state database and ensures the schema exists."""
    connection = sqlite3.connect(get_state_db_path())
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS asset_state (
            release_key TEXT NOT NULL,
            item_key TEXT NOT NULL,
            file_name TEXT,
            file_path TEXT NOT NULL,
            size INTEGER,
            last_modified REAL,
            etag TEXT,
            expected_signature TEXT,
            local_size INTEGER,
            local_mtime REAL,
            PRIMARY KEY (release_key, item_key)
        )
        """
    )
    connection.commit()
    return connection

def load_release_state(connection, release_key):
    """Loads persisted per-release download metadata from the state database."""
    rows = connection.execute(
        """
        SELECT item_key, file_name, file_path, size, last_modified, etag,
               expected_signature, local_size, local_mtime
        FROM asset_state
        WHERE release_key = ?
        """,
        (release_key,),
    ).fetchall()

    state = {}
    for row in rows:
        state[row["item_key"]] = {
            "file_name": row["file_name"],
            "file_path": row["file_path"],
            "size": row["size"],
            "last_modified": row["last_modified"],
            "etag": row["etag"],
            "expected_signature": row["expected_signature"],
            "local_size": row["local_size"],
            "local_mtime": row["local_mtime"],
        }
    return state

def save_state_entry(connection, release_key, item_key, file_name, file_path, expected_signature, remote_size, remote_last_modified, remote_etag):
    """Upserts metadata for a downloaded or verified asset."""
    try:
        local_size = os.path.getsize(file_path)
    except OSError:
        local_size = None

    try:
        local_mtime = os.path.getmtime(file_path)
    except OSError:
        local_mtime = None

    connection.execute(
        """
        INSERT INTO asset_state (
            release_key, item_key, file_name, file_path, size, last_modified,
            etag, expected_signature, local_size, local_mtime
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(release_key, item_key) DO UPDATE SET
            file_name = excluded.file_name,
            file_path = excluded.file_path,
            size = excluded.size,
            last_modified = excluded.last_modified,
            etag = excluded.etag,
            expected_signature = excluded.expected_signature,
            local_size = excluded.local_size,
            local_mtime = excluded.local_mtime
        """,
        (
            release_key,
            item_key,
            file_name,
            file_path,
            remote_size,
            remote_last_modified,
            remote_etag,
            expected_signature,
            local_size,
            local_mtime,
        ),
    )
    connection.commit()

def prune_release_state(connection, release_key, valid_item_keys):
    """Removes stale database rows for missing files or no-longer-expected assets."""
    rows = connection.execute(
        "SELECT item_key, file_path FROM asset_state WHERE release_key = ?",
        (release_key,),
    ).fetchall()

    stale_keys = []
    for row in rows:
        file_path = row["file_path"]
        if row["item_key"] not in valid_item_keys or not file_path or not os.path.exists(file_path):
            stale_keys.append(row["item_key"])

    if not stale_keys:
        return

    connection.executemany(
        "DELETE FROM asset_state WHERE release_key = ? AND item_key = ?",
        [(release_key, item_key) for item_key in stale_keys],
    )
    connection.commit()

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
    if response.status_code == 404:
        print(f"   ⚠️ Release tag {tag} not found yet on GitHub.")
        return None

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
            has_source = "zipball_url" in release_data or "tarball_url" in release_data

            if len(assets) == 0 and not has_source:
                print(f"   ⏳ No assets or source code found. Attempt {attempt + 1}/{max_retries}. Waiting {retry_delay}s...")
                time.sleep(retry_delay)
                continue
            break

        print(f"   ⏭️ Skipping {repo} ({tag}): Release no longer exists.")
        return "SKIP"
    else:
        print(f"   ❌ Timed out waiting for assets to populate for {repo}.")
        return False

    headers_api = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers_api["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    repo_name = repo.split('/')[-1]

    download_queue = []
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

    custom_folder = generate_folder_name(repo, release_data, headers_api)
    final_download_dir = os.path.join(download_dir, custom_folder)
    legacy_folder = generate_legacy_folder_name(repo, release_data, headers_api)
    legacy_download_dir = os.path.join(download_dir, legacy_folder)
    if not os.path.exists(final_download_dir) and os.path.exists(legacy_download_dir):
        custom_folder = legacy_folder
        final_download_dir = legacy_download_dir
    os.makedirs(final_download_dir, exist_ok=True)

    release_key = f"{repo}|{tag}"
    state_enabled = is_state_persistence_enabled()
    state_db = open_state_database() if state_enabled else None
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

    total_files = len(download_queue)
    per_file_retries = 3
    per_file_retry_delay = 2

    def refresh_release_state():
        nonlocal release_state
        if state_db is not None:
            release_state = load_release_state(state_db, release_key)

    def should_skip_existing_file(file_path, item_key, expected_signature, remote_size, remote_last_modified, remote_etag):
        """Returns True when the local file can be considered up-to-date."""
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

            for i, item in enumerate(download_queue, 1):
                item_key = item["key"]
                expected_signature = build_expected_signature(item)
                file_name = item["name"]
                download_url = item["url"]

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

                            if should_skip_existing_file(
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
                    return False
    finally:
        if state_db is not None:
            state_db.close()

    return True

# For manual testing: run this file directly to download a specific release
if __name__ == "__main__":
    test_repo = "cli/cli"
    test_tag = "v2.30.0"
    print(f"Base download directory set to: {BASE_DOWNLOAD_DIR}")
    download_all_assets(test_repo, test_tag, BASE_DOWNLOAD_DIR)