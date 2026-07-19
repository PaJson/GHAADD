# GHAADD (GitHub Automatic Asset Downloader Daemon)

A highly efficient, automated Python background utility designed to parse incoming Gmail update notifications for GitHub releases, extract crucial repository metadata, and systematically download all associated release assets, source code archives, and cryptographically signed release attestations while keeping original file timestamps intact.

## 🚀 Features

- **Batch Mail Processing**: Authenticates securely via IMAP to fetch and parse unread GitHub release notifications in a single operational cycle, preventing aggressive connection overhead.
- **Two-Pass Subject Parsing**: Leverages an intelligent regular expression system to dynamically separate repository owners, names, and version identifiers from complex email subject text variations.
- **High-Performance Downloader**: Implements persistent HTTP session connection pooling via `requests.Session` to maximize throughput across standard binary assets, source distributions, and remote attestation lookups.
- **Self-Healing Execution Flow**: Automatically detects and skips deleted, corrupted, or overwritten remote releases, and bypasses hanging loops on tags that solely contain auto-generated source archives.
- **Deterministic Pathing & Metadata Preservation**: Stores releases in a stable folder layout (`YYYY-MM-DD_HH-MM, owner-repo, Tag, Short-SHA`), keeps duplicate-detection state in `state.db` beside the app files, and updates local file system attributes using remote `Last-Modified` timestamps.

## Runtime Options

- Run `python main.py --purge-state` to delete the local `state.db` file without downloading anything.
- Runtime behavior is configured through `config.json` (polling, max emails, default download directory, state persistence toggle).
- To disable duplicate-detection state, set `state.disable_state_persistence` to `true` in `config.json`.

### Download Path Routing

Destination directory selection supports repo and release-type overrides in `config.json`:

- `paths.repo_release_type_paths[repo][release-type]`
- `paths.repo_paths[repo]`
- `paths.release_type_paths[release-type]`
- `paths.default_download_dir`

Resolution uses the order above (most specific to least specific).

Release-type keys should be lowercase and hyphenated, for example `release` and `pre-release`.

## 🛠️ Prerequisites

- Python 3.8 or higher
- A Gmail account with an authorized **App Password** configured
- A GitHub **Personal Access Token (PAT)** for API authentication

## 📦 Setup & Installation

1. ...