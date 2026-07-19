# GHAADD (GitHub Automatic Asset Downloader Daemon)

A highly efficient, automated Python background utility designed to parse incoming Gmail update notifications for GitHub releases, extract crucial repository metadata, and systematically download all associated release assets, source code archives, and cryptographically signed release attestations while keeping original file timestamps intact.

## 🚀 Features

- **Batch Mail Processing**: Authenticates securely via IMAP to fetch and parse unread GitHub release notifications in a single operational cycle, preventing aggressive connection overhead.
- **Two-Pass Subject Parsing**: Leverages an intelligent regular expression system to dynamically separate repository owners, names, and version identifiers from complex email subject text variations.
- **High-Performance Downloader**: Implements persistent HTTP session connection pooling via `requests.Session` to maximize throughput across standard binary assets, source distributions, and remote attestation lookups.
- **Self-Healing Execution Flow**: Automatically detects and skips deleted, corrupted, or overwritten remote releases, and bypasses hanging loops on tags that solely contain auto-generated source archives.
- **Deterministic Pathing & Metadata Preservation**: Sanitizes local storage targets into an organized folder layout (`YYYY-MM-DD_HH-MM, Name, Tag, Short-SHA`) and updates local file system attributes using remote `Last-Modified` timestamps.

## 🛠️ Prerequisites

- Python 3.8 or higher
- A Gmail account with an authorized **App Password** configured
- A GitHub **Personal Access Token (PAT)** for API authentication

## 📦 Setup & Installation

1. ...