import datetime
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

def generate_folder_name(repo, release_data, headers):
    """Generates the folder name matching the Tampermonkey script output."""
    # 1. Date (YYYY-MM-DD_HH-MM in UTC)
    pub_time_str = release_data.get("published_at", "")
    if pub_time_str:
        # The API returns UTC time with a 'Z' at the end
        pub_time_str = pub_time_str.replace("Z", "+00:00")
        pub_date = datetime.datetime.fromisoformat(pub_time_str)
        formatted_date = pub_date.strftime("%Y-%m-%d_%H-%M")
    else:
        formatted_date = "unknown-date"

    # 2. Name
    raw_name = release_data.get("name") or release_data.get("tag_name", "unknown-name")
    
    # 3. Tag
    raw_tag = release_data.get("tag_name", "unknown-tag")
    
    # 4. Commit Hash
    raw_commit = get_short_commit_hash(repo, raw_tag, headers)

    # Sanitize and combine
    safe_name = sanitize_folder_name(raw_name)
    safe_tag = sanitize_folder_name(raw_tag)
    safe_commit = sanitize_folder_name(raw_commit)

    return f"{formatted_date}, {safe_name}, {safe_tag}, {safe_commit}"

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
            "name": asset["name"],
            "url": asset["browser_download_url"]
        })
        
    # 2. Add the auto-generated Source Code (zip and tar.gz)
    if "zipball_url" in release_data:
        download_queue.append({
            "name": f"{repo_name}-{tag}-Source_code.zip",
            "url": release_data["zipball_url"]
        })
    if "tarball_url" in release_data:
        download_queue.append({
            "name": f"{repo_name}-{tag}-Source_code.tar.gz",
            "url": release_data["tarball_url"]
        })

    # 3. Add Release Attestations (if present)
    attestation_url = get_release_attestation_url(release_data, headers_api)
    if attestation_url:
        # Add to queue with a placeholder name - actual name will be extracted from Content-Disposition header
        download_queue.append({
            "name": None,  # Will be extracted from response header
            "url": attestation_url,
            "is_attestation": True
        })

    print(f"📦 Found {len(download_queue)} total items to download (including source code and attestations).")
    
    # 1. CREATE THE DIRECTORY ONCE (Outside the loop)
    # Generate the custom folder name and create it
    custom_folder = generate_folder_name(repo, release_data, headers_api)
    final_download_dir = os.path.join(download_dir, custom_folder)
    os.makedirs(final_download_dir, exist_ok=True)
    
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

    # 3. DOWNLOAD THE FILES
    # NEW: Create a session to reuse the underlying TCP connection
    with requests.Session() as session:
        # Apply your API headers to the entire session
        session.headers.update(headers_api)
        
        for i, item in enumerate(download_queue, 1):
            file_name = item["name"]
            download_url = item["url"]
            
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

    return True

if __name__ == "__main__":
    test_repo = "Genymobile/scrcpy"
    test_tag = "v4.1"
    
    # We pass the base directory directly. 
    # The script will append your custom syntax folder directly inside it.
    print(f"Base download directory set to: {BASE_DOWNLOAD_DIR}")
    download_all_assets(test_repo, test_tag, BASE_DOWNLOAD_DIR)