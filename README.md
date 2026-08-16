# GHAADD (GitHub Automatic Asset Downloader Daemon)

Automated Python utility that reads GitHub release notification emails from Gmail, extracts release metadata, and downloads release assets, source archives, and release attestations.

## Features

- Fetches unread GitHub release notifications from a dedicated Gmail folder.
- Parses both Release and Pre-release subject formats.
- Logs when a fallback subject parser pattern is used.
- Downloads all release assets plus zip/tar source archives.
- Renames all downloaded source archives to include " (source)" before the archive suffix to avoid naming conflicts with release assets.
- Attempts to discover and download GitHub release attestations.
- Preserves file timestamps using upstream metadata when available.
- Uses local SQLite state to skip files that are already up to date.
- Replaces older pending jobs with the newest notification for the same repo/tag/release type.
- Supersedes older queue jobs when a release tag commit changes.
- Supports repository-specific queue re-check intervals via mapping.json overrides.
- Supports configurable polling mode with jitter.
- Prints app version at startup and prints the next pending job after each ingest/process cycle.
- Supports optional per-run terminal logging to timestamped .log files.
- Writes lifecycle logs for completed moves, superseded partial moves, and typed warnings.
- Supports config-based download path routing by repo and release type.
- Retries marking processed notification emails as read/deleted once on transient IMAP failures before logging a warning; the job is still queued even if the email cleanup ultimately fails.

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
3. Optional: create or edit mapping.json for repository metadata and routing overrides.

If mapping.json contains invalid JSON, the app keeps the original file by copying it to a timestamped file named `mapping.json_YYYYMMDD_HHMMSS_ffffff` beside the application files. It then falls back to an empty mapping payload for the current operation and records the backup path in `Warning.log`. Review or restore the backup before saving a corrected mapping file.

### Repository mapping file (mapping.json)

The app auto-creates and updates repository entries in mapping.json as notifications are ingested.

Each repository entry supports these fields:

- active: timestamp of the last activity for that repository entry
- destination: optional destination base path for custom routing
- folder: optional nested folder path to append under the destination
- limit: optional integer warning threshold for the number of folders at the destination (`0` disables checks)
- limit_release_type_folders: optional array of folder names to include in destination limit counting for this repository (for example, `['Release', 'Pre-release']`)
- name: repository identity in owner/repo form
- nicename: optional display name used for human-readable labels
- paused: when `true`, matching notification emails move to Trash but remain unread, no new jobs are queued, and jobs already queued continue normally
- recheck_intervals_minutes: optional list of positive integers to override queue re-check cadence for this repository

When a new repository is seen, the app fills in a default skeleton entry with:

- the repository name
- a generated nicename (for example, repo (owner))
- empty destination/folder values
- a default `limit` value of `0` (disabled until you configure a threshold)
- `paused: false`
- an active timestamp

If you want to customize the display name or routing, you can edit these fields manually in mapping.json.

Example:

```json
{
	"active": "2026-08-08_14-59",
	"destination": "X:\\Path\\To\\Destination",
	"folder": "@GitHub/Nightly",
	"limit": 25,
	"limit_release_type_folders": ["Release", "Pre-release"],
	"name": "example-org/example-repo",
	"nicename": "My name for this repository",
	"paused": false,
	"recheck_intervals_minutes": [3, 10, 30, 120]
}
```

In this example, the repository is still identified by its GitHub name in the name field, but the visible label becomes "My name for this repository" and the files are routed under the destination path plus the folder.

The resulting folder path for this example would be:

```text
X:\Path\To\Destination\My name for this repository\@GitHub\Nightly
```

In other words, the app uses the combination of <destination> + <nicename> + <folder> as the effective base folder for that repository.

Folder behavior:

- `folder` supports nested folders using either `/` or `\` as separators.
- The app sanitizes each path segment separately, then joins them using the current OS path separator.
- This means `@GitHub/Nightly` works on Windows, Linux, and macOS.
- Empty segments and unsafe relative segments like `.` and `..` are ignored.

Destination behavior:

- If `destination` is empty, the app falls back to the default `GHAADD/Complete` location.
- If `destination` is set but the destination root folder does not exist, the app prints a warning and falls back to `GHAADD/Complete`.
- When destination exists, `nicename` and `folder` folders are created automatically when needed.

If `limit` is `0`, no folder-count warning is applied for that repository.
If `limit` is greater than `0`, the app warns when the repository destination appears to contain too many folders.

Limit-count folder scope behavior:

- If `limit_release_type_folders` is configured with one or more values, only those top-level folders are counted for the repository limit warning.
- If `limit_release_type_folders` is missing or empty, the app auto-detects managed release-type folders using defaults (`Release`, `Pre-release`) plus known release_type values from the queue state.
- Folder names are matched case-insensitively after normal path-name sanitization.

If `recheck_intervals_minutes` is set for a repository, those values are used for that repository's re-check schedule. If the list is missing or invalid, the global `processing.recheck_intervals_minutes` values are used.

Set `paused` to `true` to temporarily stop new jobs for a repository. Matching notification emails move to Trash while remaining unread, so they are visible there but excluded from later polls of the configured mailbox. Jobs already in the queue continue to completion.

The doctor check validates the mapping schema and warns when destination values are empty or when path styles do not match the current OS.

Path note:

- Use local paths (including in the config.json example below) that match your operating system.
- Do not copy Windows-style paths on Linux/macOS, or POSIX paths on Windows.
- On Windows, a bare drive like `D:` is drive-relative. Use `D:\\` (or a full path like `D:\\Downloads`) for a drive root.
- Examples:
	- Windows: D:\\Users\\YourUser\\Downloads
	- Linux/macOS: /home/youruser/Downloads

## Running

Default run:

```bash
python main.py
```

CLI options:

- --purge-state: Delete local state.db and exit.
- --smoke-test: Run internal smoke tests and exit.
- --doctor: Run environment and cross-platform diagnostics.
	- Add --json to output the diagnostics report as JSON.
- --mapping-validate: Validate mapping.json schema and print errors/warnings.
	- Add --json to output the validation result as JSON.
- --move-complete-to-destination: Retry moving repository folders from `Complete` to configured mapping destinations.
	- Useful when destination storage was unavailable earlier (for example NAS/network issues).
	- Add --json to output a machine-readable summary.
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
- Polling output includes a computed next poll timestamp.
- After each ingest/process cycle, the app prints the next scheduled pending job (or `NONE` when the queue is empty).

## Configuration (config.json)

Example:

```json
{
	"processing": {
		"max_emails_to_process": 0,
		"recheck_intervals_minutes": [5, 15, 60, 360, 720, 1440]
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
	"folders": {
		"ghaadd_root": "GHAADD",
		"processing": "Processing",
		"complete": "Complete",
		"partial": "Partial",
		"logs": "Logs"
	},
	"logging": {
		"enabled": false
	},
	"paths": {
		"default_download_dir": "D:"
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
	- Per-repository mapping overrides can replace this cadence for specific repositories.
- mailbox.folder
	- Gmail folder used for reading and post-processing notification emails.
	- Defaults to GitHubNotifications when missing or empty.
- state.disable_state_persistence
	- false keeps and uses state.db for duplicate detection.
	- true disables persistent duplicate state for the run.
- polling.interval_seconds, polling.jitter_min_seconds, polling.jitter_max_seconds
	- Next run delay is interval_seconds + random jitter.
- folders.ghaadd_root, folders.processing, folders.complete, folders.partial, folders.logs
	- Folder names used under each resolved base download path.
	- Defaults are GHAADD, Processing, Complete, Partial, and Logs.
- logging.enabled
	- false disables terminal output logging.
	- true writes all terminal output (stdout and stderr) to a .log file for this run.
	- Log files are written under: paths.default_download_dir/folders.ghaadd_root/folders.logs.
	- Each run creates a new log file named with app start time (format: YYYYMMDD_HHMMSS.log).
	- The app also writes lifecycle files in the same logs directory:
		- Complete.log: completed staging-folder finalizations.
		- Partial.log: superseded incomplete staging folders moved to Partial.
		- Warning.log: typed warning entries (for example API, destination, move, sanity-check, supersede, and premature-finalize warnings).

## Download Path Routing

Destination directory resolution order (most specific first):

1. paths.default_download_dir

Release-type keys are normalized to lowercase with hyphens (for example: release, pre-release).

## Staging Folders

Downloads are staged under the resolved base path in a working area folder (default: GHAADD):

- <resolved-base-path>/GHAADD/Processing
- <resolved-base-path>/GHAADD/Complete
- <resolved-base-path>/GHAADD/Partial

These names are configurable via folders.ghaadd_root, folders.processing, folders.complete, and folders.partial.

Each release is stored under a repository parent folder:

- <resolved-base-path>/GHAADD/Processing/<repo> (<owner>)/<type of release>/<release-folder>
- <resolved-base-path>/GHAADD/Complete/<repo> (<owner>)/<type of release>/<release-folder>

Behavior:

- During retries/rechecks, files are updated in Processing.
- Repository folders include release type as an extra path segment (for example: Pre-release, Release).
- When a new notification is ingested for the same repo/tag/release type, existing pending jobs for that identity are marked SUPERSEDED and replaced by a fresh pending job.
- When a commit hash changes for the same repo/tag during rechecks, the old PENDING job is marked SUPERSEDED and a new PENDING job is created for the updated commit.
- When a release/tag is not found on GitHub during an automatic poll (SKIP: release_not_found), the job stays PENDING and is rechecked on the repository's normal re-check schedule, instead of completing immediately. This covers releases published before their assets/commit are attached. Once the re-check schedule is exhausted, the job is completed and its staged folder (if any) is finalized using the counters/working_dir recorded from earlier attempts: moved to Complete if fully accounted for, or to Partial if incomplete. A zero-result attempt (transient error, or a non-terminal SKIP/FAILED before the asset list is reached) never overwrites previously recorded non-zero counters, so a release that was already fully downloaded is not mistaken for incomplete just because it later disappeared upstream. When such a job is finalized to Complete this way, a PREMATURE_FINALIZE entry is written to Warning.log noting the release likely was superseded/replaced before the re-check schedule finished. Manual pending-job runs (--run-pending) still complete release_not_found immediately since they run on demand.
- When a queue job reaches a terminal state (COMPLETED or FAILED with no retries left), its release folder is moved to Complete.
- When a pending job is superseded and its file counters indicate completion, its staged folder is finalized to Complete or mapped destination.
- When a pending job is superseded and appears incomplete, its staged folder is moved to Partial for quarantine/inspection.

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