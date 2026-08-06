# GHAADD (GitHub Automatic Asset Downloader Daemon)

Automated Python utility that reads GitHub release notification emails from Gmail, extracts release metadata, and downloads release assets, source archives, and release attestations.

## Features

- Fetches unread GitHub release notifications from a dedicated Gmail folder.
- Parses both Release and Pre-release subject formats.
- Logs when a fallback subject parser pattern is used.
- Downloads all release assets plus zip/tar source archives.
- Renames all downloaded source archives to include " (source)" before the archive suffix.
- Attempts to discover and download GitHub release attestations.
- Preserves file timestamps using upstream metadata when available.
- Uses local SQLite state to skip files that are already up to date.
- Replaces older pending jobs with the newest notification for the same repo/tag/release type.
- Supersedes older queue jobs when a release tag commit changes.
- Supports configurable polling mode with jitter.
- Supports config-based download path routing by repo and release type.

## Requirements

- Python 3.8+
- Gmail App Password for IMAP access
- GitHub PAT (recommended to avoid rate limits)

## Environment Variables

Create a .env file in the project root with:

```env
GMAIL_USER=your-email@gmail.com
GMAIL_APP_PASSWORD=your-app-password
GITHUB_PAT=your-github-token
```

Notes:

- GMAIL_USER and GMAIL_APP_PASSWORD are required.
- GITHUB_PAT is optional but strongly recommended.

## Gmail Folder

The folder is configured in config.json under mailbox.folder.

Default value:

- GitHubNotifications

## Installation

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install requests python-dotenv imapclient
```

## Local Configuration Files

This project keeps machine-specific settings out of git:

- config.json
- mapping.json

Both files are ignored via .gitignore.

First-time setup:

1. Copy config.example.json to config.json.
2. Update paths and other values in config.json for your machine.
3. Optional: create or edit mapping.json for repository destination mapping.

## Running

Default run:

```bash
python main.py
```

CLI options:

- --purge-state: Delete local state.db and exit.
- --smoke-test: Run internal smoke tests and exit.
- --mapping-validate: Validate mapping.json schema and print errors/warnings.
	- Add --json to output the validation result as JSON.
- --queue-status: Print queue counts, due-now count, next pending job, and recent jobs.
	- --queue-remove-pending-ids ID [ID ...]: Mark specific pending jobs as SUPERSEDED (removes them from pending queue).
		- Example: python main.py --queue-remove-pending-ids 23 27 31
	- Add --json to output machine-readable JSON (example: python main.py --queue-status --json).
	- Add --queue-all to show all matching jobs instead of the default capped history.
	- Add --queue-limit N to control history size (example: --queue-limit 50, --queue-limit 0 for all).
	- Add --queue-hours H to filter to jobs created in the last H hours (example: --queue-hours 24).
	- Add --queue-date YYYY-MM-DD to filter to jobs created on a specific date.
	- Add --queue-repo-filter TEXT to filter by repository substring (case-insensitive, example: --queue-repo-filter <repository>).
	- Add --queue-status-filter STATUS to filter by status (PENDING, COMPLETED, FAILED, SUPERSEDED).
	- Add --queue-report to print a compact summary report (rates, top repos, top skipped items, and skip reasons).
	- Add --queue-report-only to print only the summary report section.
	- Add --queue-report-csv [PATH] to export the report section to a CSV file.
		- When PATH is omitted, a timestamped filename is auto-generated in the current working directory.
	- Includes per-job file counters: downloaded, skipped, and total items.
	- JSON output includes previous successful baseline fields on recent successful jobs:
		- previous_success_tag
		- previous_success_total_items
		- file_count_delta_vs_previous_success
	- Queue status also includes skipped-item detail previews (when available) for listed jobs.
- --once: Force single-run mode even when polling is enabled.
- --poll: Force polling mode for this run.

Polling behavior:

- Polling runs when either:
	- --poll is provided, or
	- polling.enabled is true in config.json.
- --once always overrides polling and runs a single cycle.

## Configuration (config.json)

Example:

```json
{
	"processing": {
		"max_emails_to_process": 0,
		"recheck_intervals_minutes": [5, 15, 60, 1440]
	},
	"mailbox": {
		"folder": "GitHubNotifications"
	},
	"state": {
		"disable_state_persistence": false
	},
	"polling": {
		"enabled": true,
		"interval_seconds": 300,
		"jitter_min_seconds": 5,
		"jitter_max_seconds": 30
	},
	"paths": {
		"default_download_dir": "D:\\Users\\YourUser\\Downloads",
		"repo_paths": {},
		"release_type_paths": {},
		"repo_release_type_paths": {}
	}
}
```

Key behavior:

- processing.max_emails_to_process
	- 0 means process all unread notifications.
	- > 0 limits processing to that many emails per cycle.
- processing.recheck_intervals_minutes
	- Re-check cadence list used by the queue system.
	- Values are interpreted as minutes.
	- Every queued job is re-checked at each listed age from the original queue time.
	- Example: [5, 15, 60] means checks at queue time, then at +5, +15, and +60 minutes.
	- This allows late-added release assets to be discovered in later re-checks.
	- Invalid or non-positive values are ignored; defaults are used when the list is missing or fully invalid.
- mailbox.folder
	- Gmail folder used for reading and post-processing notification emails.
	- Defaults to GitHubNotifications when missing or empty.
- state.disable_state_persistence
	- false keeps and uses state.db for duplicate detection.
	- true disables persistent duplicate state for the run.
- polling.interval_seconds, polling.jitter_min_seconds, polling.jitter_max_seconds
	- Next run delay is interval_seconds + random jitter.

## Download Path Routing

Destination directory resolution order (most specific first):

1. paths.repo_release_type_paths[repo][release-type]
2. paths.repo_paths[repo]
3. paths.release_type_paths[release-type]
4. paths.default_download_dir

Release-type keys are normalized to lowercase with hyphens (for example: release, pre-release).

## Staging Folders

Downloads are staged under the resolved base path in a GHAADD working area:

- <resolved-base-path>/GHAADD/Processing
- <resolved-base-path>/GHAADD/Done

Each release is stored under a repository parent folder:

- <resolved-base-path>/GHAADD/Processing/<repo> (<owner>)/<type of release>/<release-folder>
- <resolved-base-path>/GHAADD/Done/<repo> (<owner>)/<type of release>/<release-folder>

Behavior:

- During retries/rechecks, files are updated in Processing.
- Repository folders include release type as an extra path segment (for example: Pre-release, Release).
- When a new notification is ingested for the same repo/tag/release type, existing pending jobs for that identity are marked SUPERSEDED and replaced by a fresh pending job.
- When a commit hash changes for the same repo/tag during rechecks, the old PENDING job is marked SUPERSEDED and a new PENDING job is created for the updated commit.
- When a release/tag is not found on GitHub (SKIP: release_not_found), the job is completed immediately and not rechecked.
- When a queue job reaches a terminal state (COMPLETED or FAILED with no retries left), its release folder is moved to Done.

## Source Archive Naming

Source archives are always saved with a " (source)" marker in the filename.

Suffix placement rules:

- .zip source files are renamed to: <name> (source).zip
- .tar.gz source files are renamed to: <name> (source).tar.gz

Examples:

- project-v1.2.3.zip becomes project-v1.2.3. (source).zip
- project-v1.2.3.tar.gz becomes project-v1.2.3 (source).tar.gz

Source archive naming is deterministic per item key, which allows rechecks to skip already-downloaded source files instead of creating numbered duplicates.

Re-check source refresh behavior:

- Source archives are processed after all normal assets.
- If no normal assets were newly downloaded in the current run, existing source archives are skipped.
- If any normal asset was newly downloaded in the current run, existing source archives are re-downloaded and overwritten.

## State Database

The app stores state in state.db beside the Python files.

Use:

```bash
python main.py --purge-state
```

to remove the database.

## Troubleshooting

- Missing Gmail credentials:
	- Ensure GMAIL_USER and GMAIL_APP_PASSWORD are set in .env.
- No notifications found:
	- Confirm messages are unread and in mailbox.folder.
- API rate limits or release lookup errors:
	- Set GITHUB_PAT.
- Unexpected duplicates:
	- Keep state.disable_state_persistence set to false.
- Fallback subject parser warnings:
	- If you see "Subject matched fallback parser pattern", save that subject line for parser-rule review.

## Notes About JSON Comments

config.json is parsed as strict JSON.

- Standard JSON comments are not allowed.
- Lines such as // comment or /* comment */ will break parsing.
- If you need inline notes, use extra keys (for example "_comment") that your code ignores.