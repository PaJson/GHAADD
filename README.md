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
- Records lifecycle events (completed moves, superseded partial moves, typed warnings) in state.db, viewable with --lifecycle-log.
- Supports config-based download path routing by repo and release type.
- Retries marking processed notification emails as read/deleted once on transient IMAP failures before logging a warning; the job is still queued even if the email cleanup ultimately fails.
- Prevents accidentally running two mutating instances at once (default run, --once, --poll, --drain-queue) using a singleton file lock; read-only commands (--doctor, --queue-status, --mapping-validate, etc.) are unaffected and can run anytime.
- Has a Tkinter GUI (`python main_gui.py`) to monitor the daemon, edit repository mappings and read warnings (see Graphical interface below).
- Supports a tightly-scoped --single mode (at most one notification/queue item per run) and an orthogonal --dry-run modifier (no writes/deletes, full preview) for safe, controlled testing against real data.

## Graphical interface (GUI)

Start it with `python main_gui.py` (add `--theme clam` for coloured table headings). It needs Tkinter, which ships with Python on Windows and macOS (on Linux install your distribution's `python3-tk`). The GUI never owns the daemon: it reads `state.db`, `mapping.json` and `config.json` directly, can start a detached daemon, and only asks a running daemon to stop, pause, poll or check folders; closing the GUI leaves the daemon running.

- **One window:** only one GUI window runs at a time. Starting it again (the shortcut, `python main_gui.py`) brings the open window to the front, also from the tray or the taskbar, instead of opening a second one (a start that is meant to stay minimized, `--minimized` or `gui.start_minimized`, just exits quietly). It uses a lock file, `ghaadd.gui.lock`, that the system releases when the GUI ends however it ends. The daemon is separate and has its own single-instance guard.
- **Control bar:** three groups at the top: Daemon (Start, Stop (graceful), Pause/Resume, and a "Restart" button that appears when the daemon runs with older settings than config.json), Polling (Poll now = a full cycle; Poll one = at most one notification and one queue item; Check folders) and Tools (the Terminal log switch, Doctor, Stats, Settings (the global `config.json` values)). The footer shows the status messages on the left, the queue in the middle ("Queue: 36 due now, 17 due later": jobs waiting in the queue, and how many of them are due, which is the number the daemon prints as "Processing N due queue job(s)"; hover it for the explanation) and the daemon's status and countdown on the right. When jobs have been neglected (the daemon was stopped or idle), it reads "Queue: 27 due now (26 behind schedule), 0 due later": a job is *behind* when its next re-check time has passed as well, so after this poll's step it is due again straight away. Each poll gives every due job one step, so such jobs catch up one step per poll (the hover text says how many polls the slowest needs). While the daemon works through the due jobs, the middle shows which one it is on instead: "Processing 3 of 36: owner/repo (Release) tag @ commit" (hover for the complete repository, release type, tag, commit and how long it has been on that job). All the jobs due when the poll started are processed in that poll; the "N" is that number. Saving the Settings also adds the optional settings that have no field there (the `gui` section: refresh and message times, tray, notifications, minimize/close to tray) to `config.json` with their default values, without changing any value that is already there, so they are easy to find and edit. The "Open config.json…" button in the Settings window does the same and then opens the file in your default editor, for everything the window does not cover.
- **Mappings:** one row per repository (most recently worked-on first) with a status icon, name (folder), repository, destination, latest tag ((R) Release / (P) Pre-release), last check, re-check step, next check, file count and folder limit ("12 / 15", with a warning sign and amber text when over). Hover a column heading for an explanation. A repository can have several jobs waiting at once (every new build or release is its own job with its own re-check schedule): the Tag, Last check and Files cells describe the newest release, while the Recheck and Next check cells describe the oldest waiting job, and "3 / 5 (+2)" says that two more jobs are waiting besides it. Hover that cell for the list of all of them (tag, step and next check of each). Select a row to edit its settings below the table (name (folder), subfolder, destination, re-check intervals, limit, release types, sanity check, skiplist, active); double-click a row to open its folder. Add and remove repositories, filter the list, open the GitHub page with the globe button.
- **Stats button:** Tools -> Stats opens the statistics of `--stats` in four tabs: Overview (repositories mapped/active, the history's age and reliability, activity per period), Busiest (top 10 repositories by number of jobs), Biggest (top 10 repositories and top 10 single releases by size) and Per day (a bar chart of the jobs of the last 30 days). Refresh counts again, "Copy as text" puts the same report as `--stats` on the clipboard.
- **Doctor button:** runs the `--doctor` checks and shows the result (problems, warnings, what was checked). The button is highlighted (⚠) on a first run: when config.json or mapping.json is missing or the Gmail login (`.env`) is not set, and after a report with problems. Dialogs open centred over the main window.
- **Terminal log:** follows the newest terminal `.log` file (it is empty while the Terminal log switch is off). Filter, copy, open the log or its folder.
- **Warnings, Completed, Folder limits, Unmapped:** structured events from `state.db` (never parsed from log text), with unread counters in the tab titles, a filter on Warnings and Completed, a detail pane with the full text, and Clear buttons that delete what a tab lists. Double-click a row to jump to its repository.
- **System tray (optional):** with the extras installed (`pip install -r requirements-optional.txt`) the GUI shows a tray icon: green = daemon running, amber = paused, grey = not running, with a red dot while there is something unread (warnings of a type that is set to notify, unmapped repositories, failed downloads; the tab's own counter still counts every warning). Minimizing hides the window in the tray (on Windows and macOS; on Linux many desktops show no tray icon, so the window stays on the taskbar unless `gui.minimize_to_tray` is true). Double-click the icon, or use its menu, to show the window; the menu also has Poll now, Pause/Resume and Quit. While the window is hidden or minimized the tray shows a notification for new warnings, a folder over its limit, a new unmapped repository, a failed download and a daemon that stopped without being asked to. Nothing is announced that was already there when the GUI started, and nothing while you are looking at the window. On Windows the notifications are real toasts shown under the "GHAADD" identity (started from the GHAADD shortcut, see Shortcuts; if PowerShell cannot show one, the tray balloon is used). Windows' own list of tray icons still names the program that runs the GUI ("Python"), because that list uses the program file's name and not the app's identity. The tray is only the GUI's: the daemon runs the same with or without it.
- **Where things are kept:** window size and the "read" marks of the tabs live in `config.json` under `gui` (delete that section to reset; the daemon ignores it). All hover texts are in `modules/gui_tooltips.py`.

## Requirements

- Python 3.10+ (code minimum per `vermin` is 3.9; 3.9 is end-of-life, developed and tested on 3.14)
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
pip install -r requirements.txt
```

Optional, for the GUI's system tray icon and notifications: `pip install -r requirements-optional.txt` (pystray and Pillow).

## Local Configuration Files

This project keeps machine-specific settings out of git:

- config.json
- mapping.json

Both files are ignored via .gitignore.

First-time setup:

1. Copy config.example.json to config.json.
2. Update paths and other values in config.json for your machine.
3. Optional: create or edit mapping.json for repository metadata and routing overrides.

If mapping.json contains invalid JSON, the app keeps the original file by copying it to a timestamped file named `mapping.json_YYYYMMDD_HHMMSS_ffffff` beside the application files. It then falls back to an empty mapping payload for the current operation and records the backup path as a WARNING lifecycle event (see --lifecycle-log). Review or restore the backup before saving a corrected mapping file.

### Repository mapping file (mapping.json)

The app auto-creates and updates repository entries in mapping.json as notifications are ingested. `mapping.example.json` shows the format.

Each repository entry supports these fields (in this order):

- repository: repository identity in owner/repo form
- folder: optional display and repository folder name (default: `repo (owner)`)
- subfolder: optional nested folder path to append under the repository folder. Every entry keeps its own value (an empty value means no subfolder); `paths.default_subfolder` in config.json only supplies the value for entries created from now on. `mapping.example.json` is only an example (the app never reads it): the first entry shows what a new entry looks like with the defaults, the second one shows every field with a non-default value
- destination: optional destination base path for custom routing (can be any path on disk, not limited to paths.default_download_dir - see Deletion Safety below for what this does and does not do)
- skiplist: optional array of release types to skip for this repository (for example, `['Pre-release']`); matching notifications are logged as a SKIPPED WARNING lifecycle event (see --lifecycle-log), the email is marked as read and deleted, and no job is queued. Leave empty (`[]`) to download both `Release` and `Pre-release`
- recheck_intervals: optional list of positive integers (minutes) to override queue re-check cadence for this repository
- limit: optional integer warning threshold for the number of folders at the destination (`0` disables checks)
- limit_folders: optional array of release-type folder names to include in destination limit counting for this repository (for example, `['Release', 'Pre-release']`)
- sanity_check: how the "file count changed" warning (SANITY_CHECK) picks the release to compare a finished release with: `any_tag` (default; the previous successful release of the repository whatever its tag), `same_tag` (only a previous release with the same tag, for rolling tags such as `nightly`), or `off`. `same_tag` only helps for a repository that re-uses tags (a rolling `nightly` next to versioned releases, or one re-published tag per platform); when every release has a new tag it never finds a release to compare with and so never warns. Each repository is compared only with its own earlier releases, so repositories that share a tag do not affect each other. A missing field means `any_tag`
- shared_destination: `true` when this repository shares its destination folder (destination + folder + subfolder) with other repositories on purpose, for example one repository per platform. `--mapping-validate` and the Doctor then do not warn about the shared folder; the notice is hidden once every repository of the group is marked. Default `false`. In the GUI: the "Shared folder" checkbox
- last_notification: timestamp when the last GitHub release notification was seen
- last_finalized: timestamp when a release was last moved to its complete destination (empty when none has been finalized)
- active: `true` (the default) downloads new releases; when `false`, matching notification emails move to Trash but remain unread, no new jobs are queued, and jobs already queued continue normally

Files written by version 1.x used other names (`name`, `foldername`, `recheck_intervals_minutes`, `limit_release_type_folders`, `last_notification_seen`, and `paused`, where `paused: true` is now `active: false`). They are upgraded automatically: the first GUI start, daemon start or write rewrites mapping.json with the new names and keeps a copy of the old file as `mapping.json.v1.bak`. Until an older daemon (started before the upgrade) has been restarted, the upgrade waits and writes are refused with a message saying so. `state.db` is not affected: it refers to repositories by their owner/repo text only.

When a new repository is seen, the app fills in a default skeleton entry with:

- the repository name
- a generated folder name (for example, repo (owner))
- the default subfolder (`paths.default_subfolder` in config.json; empty unless you set one, so the Release / Pre-release folders sit straight in the repository folder) and an empty destination
- the default `limit` (`processing.default_limit` in config.json, `10` unless you changed it)
- an empty `skiplist` (`[]`, nothing is skipped)
- `active: true`
- a last_notification timestamp
- an empty last_finalized value

If you want to customize the display name or routing, you can edit these fields manually in mapping.json.

Example:

```json
{
	"repository": "example-org/example-repo",
	"folder": "My name for this repository",
	"subfolder": "@GitHub/Nightly",
	"destination": "X:\\Path\\To\\Destination",
	"skiplist": [],
	"recheck_intervals": [3, 10, 30, 120],
	"limit": 25,
	"limit_folders": ["Release", "Pre-release"],
	"sanity_check": "any_tag",
	"shared_destination": false,
	"last_notification": "2026-08-08_14-59",
	"last_finalized": "2026-08-16_15-20",
	"active": true
}
```

In this example, the repository is identified by its GitHub name in the repository field, the repository folder becomes "My name for this repository", and the files are routed under the destination path plus the subfolder.

The resulting folder path for this example would be:

```text
X:\Path\To\Destination\My name for this repository\@GitHub\Nightly
```

In other words, the app uses the combination of <destination> + <folder> + <subfolder> as the effective base folder for that repository.

Limit-count folder scope behavior:

- If `limit_folders` is configured with one or more values, only those top-level folders are counted for the repository limit warning.
- If `limit_folders` is missing or empty, the app auto-detects managed release-type folders using defaults (`Release`, `Pre-release`) plus known release_type values from the queue state.
- Folder names are matched case-insensitively after normal path-name sanitization.

If `recheck_intervals` is set for a repository, those values are used for that repository's re-check schedule. If the list is missing or invalid, the global `processing.recheck_intervals_minutes` values (in config.json) are used.

Skiplist behavior:

- `skiplist` lets you skip one or both release types (`Release`, `Pre-release`) per repository instead of downloading everything.
- When an incoming notification's release type matches an entry in `skiplist` (case-insensitive), the app does not queue a job for it, records a SKIPPED WARNING lifecycle event, and still marks the email as read and deletes it (unlike `active: false`, which leaves the email unread).
- Leave `skiplist` empty (`[]`) to keep downloading both release types (the default).
- Adding both `"Release"` and `"Pre-release"` to `skiplist` is valid and effectively switches off new downloads for that repository while still cleaning up matching emails.

Set `active` to `false` (the GUI's Active checkbox) to temporarily stop new jobs for a repository. Matching notification emails move to Trash while remaining unread, so they are visible there but excluded from later polls of the configured mailbox. Jobs already in the queue continue to completion. The GUI shows such a repository with the status "Inactive".

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

Only one mutating instance (default run, --once, --single, --poll, or --drain-queue) can run at a time. A second attempt exits immediately with "Another GHAADD instance is already running" instead of racing the first instance. This is enforced with a `ghaadd.lock` file created beside the app files; read-only commands below (--doctor, --queue-status, --mapping-validate, --run-pending, etc.) are never blocked by it. --dry-run is also exempt from the lock, since it performs no writes/deletes and is safe to run alongside another instance.

CLI options:

- --drain-queue: Skip email ingestion entirely and repeatedly process only due queue jobs (sleeping until the next pending job's scheduled re-check time between cycles) until the pending queue is fully empty, then exit. Takes precedence over --once and --poll when provided.
- --run-pending JOB [JOB ...]: Immediately run one or more specific pending jobs by ID, without changing their retry schedule (unlike automatic rechecks, this completes `release_not_found`/SKIP results right away). Example: python main.py --run-pending 23 27.
- --poll-one: Make the running daemon poll **one item**: at most one new notification is ingested and at most one queue item is processed (the same as `--single`, which cannot run next to a running daemon because both need its lock), then the countdown restarts. It is the GUI's "Poll one" button and the tray menu's "Poll one item", and like Poll now it works while paused (that one item runs, then it stays paused) and while polling is switched off.
- --check-folders: Make the running daemon check that every mapped destination exists, count the folders of each repository with a limit and raise missing limit warnings, right away (the GUI's "Check folders" button). The daemon also does this at start and every `destination_check_every_n_polls` polls (0 turns the automatic checks off; the button and flag still work). It runs even while polling is paused.
- --pause / --resume / --poll-now: Control a running polling daemon from a second terminal (or the GUI). --pause freezes the countdown to the next poll and no poll runs until --resume (a poll cycle already in progress stops at the next safe boundary: the running job or email finishes, the rest wait, and polling restarts immediately on resume); --poll-now makes the daemon poll right away and then restart its countdown (it also works while paused: that one poll runs to the end and the daemon stays paused afterwards). They write a single-row `daemon_control` table in `state.db`; a daemon that is not running reports "nothing to control". Pause is always cleared when a daemon starts.
- --stop: Stop the running polling daemon gracefully from a second terminal (or the GUI's Stop button). The job in progress finishes first, then the daemon exits and releases its lock; a stop request left behind by an earlier run never stops a new daemon. Same control channel and "nothing to control" behaviour as --pause. Only the polling mode (--poll / polling.enabled) listens for it; use Ctrl+C for the other run modes.
- --log-on / --log-off: Switch terminal logging on or off in a running polling daemon without restarting it (same control channel and "nothing to control" behaviour as --pause). --log-on starts a new .log file from that moment (it does not contain earlier output), --log-off closes the file; console output is unaffected. Each --log-on gets its own file, and retention (terminal_log.keep_files) is applied when it starts. The switch overrides terminal_log.enabled for the running session only and is cleared when a daemon starts, so the config value decides again after a restart.
- --purge-state: Delete local state.db and exit. Add --dry-run to preview whether it would delete anything without doing so.
- --smoke-test: Run internal smoke tests and exit.
- --doctor: Run environment and cross-platform diagnostics.
- --perf-report: Print a read-only performance baseline: data sizes and growth (state.db, tables, logs) and how long the routine queries and probes take on your data (config/mapping loads, the queue summary, the GUI's status and table queries, the terminal-log read). Safe to run while the daemon works; add --json for machine-readable output. Run it now and again after weeks of use to see what grows.
	- Add --json to output the diagnostics report as JSON.
- --stats: Print statistics (read-only, safe while the daemon runs; add --json for machine-readable output): how many repositories are mapped, active and inactive; how many jobs, polls, files and how much data per period (last 24 hours, 7, 30 and 365 days, lifetime, with jobs per day); jobs per day for the last 30 days; the 10 busiest repositories (most jobs), the 10 biggest repositories and the 10 biggest single releases; the reliability (completed / failed / replaced) and how old the history in state.db is. Sizes are those of the files kept per release (what GHAADD recorded when it fetched them), so a release counts once however often it was re-checked, and files you deleted later are still counted. The GUI shows the same in Tools -> Stats.
- --mapping-validate: Validate mapping.json schema and print errors/warnings.
	- Add --json to output the validation result as JSON.
	- Includes an advisory warning when two or more repositories resolve to the same destination+folder+subfolder path (often left over after a repository rename).
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
	- Add --queue-report to print a compact summary report (rates, top repos, top skipped items, skip reasons, and a purge-age preview - see below).
		- The purge-age preview shows the oldest/newest purgeable (COMPLETED/FAILED/SUPERSEDED) job's age plus, for common day thresholds (7/30/90/180/365), how many jobs a matching `--purge-jobs --purge-age N` would remove - use this to pick a `--purge-age`/`--purge-oldest` value for `--purge-jobs` before running it for real. Any `--queue-hours`/`--queue-date`/`--queue-repo-filter`/`--queue-status-filter` scoping applies to this preview too.
	- Add --queue-report-only to print only the summary report section.
	- Add --queue-report-csv [PATH] to export the report section to a CSV file.
		- When PATH is omitted, a timestamped filename is auto-generated in the current working directory.
	- Includes per-job file counters: downloaded, skipped, and total items.
	- JSON output includes previous successful baseline fields on recent successful jobs:
		- previous_success_tag
		- previous_success_total_items
		- file_count_delta_vs_previous_success
		- The baseline follows each repository's `sanity_check` setting in mapping.json, like the SANITY_CHECK warning: `any_tag` (default) compares with the previous successful release of the repository whatever its tag, `same_tag` only with an earlier run of the same tag, `off` shows no baseline. A job is only compared with releases that finished before it.
	- Queue status also includes skipped-item detail previews (when available) for listed jobs.
- --lifecycle-log: Print recent lifecycle events (completed moves, superseded partial moves, typed warnings) recorded in state.db.
	- Add --json to output the events as JSON.
	- Add --lifecycle-limit N to control how many events are printed (default 20, 0 means all).
	- Add --lifecycle-type TYPE to filter by event type (COMPLETED_MOVE, PARTIAL_MOVE, WARNING, CYCLE_SUMMARY).
	- Add --lifecycle-repo-filter TEXT to filter by repository substring (case-insensitive).
- --purge: Delete lifecycle events (completed moves, superseded partial moves, warnings, cycle summaries) from state.db. This only clears the lifecycle_events table - job_queue/asset_state are untouched. Requires --purge-age.
	- Add --purge-type TYPE to limit the purge to one event type (COMPLETED_MOVE, PARTIAL_MOVE, WARNING, CYCLE_SUMMARY). Omit to match every type.
	- Add --purge-repository TEXT to limit the purge to a repository substring (case-insensitive). Omit to match every repository.
	- --purge-age DAYS is required: only events at least DAYS old are removed. Use --purge-age 0 to remove every matching event regardless of age - this is a deliberate guard against accidentally wiping everything, since omitting the flag entirely is an error instead of silently defaulting to "all".
	- Add --dry-run to preview the matching count without deleting anything.
	- Examples:
		- python main.py --purge --purge-type WARNING --purge-repository my-repo --purge-age 10 (delete warnings for `my-repo` older than 10 days)
		- python main.py --purge --purge-type WARNING --purge-age 0 (delete every warning for every repository right now)
- --purge-jobs: Delete terminal job_queue rows (COMPLETED/FAILED/SUPERSEDED) from state.db; PENDING jobs are never touched, so the active queue can't be purged by accident. Deleting a job also removes its job_skip_details rows. Requires exactly one of --purge-age or --purge-oldest.
	- Add --purge-status STATUS to limit the purge to one status (COMPLETED, FAILED, SUPERSEDED). Omit to match every terminal status.
	- Add --purge-repository TEXT to limit the purge to a repository substring (case-insensitive). Omit to match every repository.
	- --purge-age DAYS: only delete rows at least this many days old, same semantics as --purge (based on each job's completed_at, falling back to updated_at/created_at). Use --purge-age 0 to remove every matching terminal job right now.
	- --purge-oldest N: alternative to --purge-age - delete the N oldest matching rows (by the same completed_at/updated_at/created_at fallback) regardless of their age. Useful when you know how many rows you want gone (for example, to shrink a state.db that has grown to 10,000+ jobs) but don't know what --purge-age value achieves that - check --queue-status --queue-report first for an age preview, or just use --purge-oldest directly.
	- Add --dry-run to preview the matching count without deleting anything.
	- Examples:
		- python main.py --purge-jobs --purge-status COMPLETED --purge-age 30 (delete completed jobs older than 30 days)
		- python main.py --purge-jobs --purge-oldest 2000 --dry-run (preview deleting the 2,000 oldest terminal jobs)
- --once: Force single-run mode even when polling is enabled.
- --single: Ingest at most one new notification and process at most one queue item, then exit. Prefers the just-ingested item if a new notification exists; otherwise falls back to the single oldest due job already in the queue. Useful for controlled verification against real data instead of risking hundreds of items in one run.
- --dry-run: Modifier for --single/--once/--poll/--drain-queue/--purge-state/--purge/--purge-jobs. For run modes, previews what would happen with no writes/deletes: no emails marked as read or moved to Trash, no files downloaded, no changes to state.db (job_queue/asset_state/lifecycle_events) or mapping.json. GitHub API and IMAP reads still happen (read-only) so the preview reflects real data. Fully repeatable - re-running leaves the same state every time. For purge commands, prints the matching count instead of deleting anything. Example: python main.py --single --dry-run.
- --poll: Force polling mode for this run.
- --daemon: Run the polling daemon the way the GUI's Start button, the "GHAADD daemon" shortcut and the start-at-login entries do. Like --poll, except that when polling.enabled is false the daemon starts but stays **idle**: it polls only when you press Poll now (the GUI button, the tray menu or `--poll-now`), one cycle per request, with no countdown. Stop, Check folders and the log switch work as usual.

Polling behavior:

- Polling runs when either:
	- --poll or --daemon is provided, or
	- polling.enabled is true in config.json.
- A plain `python main.py` with polling.enabled false does one ingest-and-process run and exits (as before). `--poll` always polls. `--daemon` with polling.enabled false idles until Poll now (the Settings checkbox "Enable polling" is this setting).
- --once always overrides polling and runs a single cycle.
- Polling output includes a computed next poll timestamp.
- After each ingest/process cycle, the app prints the next scheduled pending job (or `NONE` when the queue is empty).

## Configuration (config.json)

Example:

```json
{
	"processing": {
		"max_emails_to_process": 0,
		"recheck_intervals_minutes": [5, 15, 60, 360, 720, 1440],
		"destination_check_every_n_polls": 10,
		"default_limit": 10
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
	"terminal_log": {
		"enabled": false,
		"max_file_mb": 10,
		"keep_files": 30
	},
	"paths": {
		"default_download_dir": "D:",
		"default_subfolder": ""
	},
	"gui": {
		"refresh_seconds": 3,
		"status_message_seconds": 6,
		"tray": true,
		"notifications": true,
		"close_to_tray": false,
		"start_minimized": false,
		"start_daemon": false
	}
}
```

Key behavior:

- processing.max_emails_to_process
	- 0 means process all unread notifications.
	- > 0 limits processing to that many emails per cycle.
- processing.destination_check_every_n_polls
	- While polling, every Nth poll cycle checks whether each mapped repository destination folder still exists on disk and records a WARNING lifecycle event for any that are missing (for example, after a local folder was moved/renamed without updating mapping.json).
	- 0 disables the periodic check. Defaults to 10. This check also runs once as part of `--doctor`.
- processing.default_limit
	- The `limit` given to a repository entry when it is created (a new notification or the GUI's Add repository). 0 means new entries have no folder limit. Defaults to 10. Existing entries keep their own value. Applies at once, no daemon restart needed.
- paths.default_subfolder
	- The `subfolder` given to a new repository entry, appended to the repository folder (for example `@GitHub`). The default is an empty string, which means no subfolder. It must be a relative path (no drive letter, no `..`); anything else falls back to the default (no subfolder). Existing entries keep their own value. Applies at once.
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
- polling.enabled
	- true (the default) polls on a schedule. false: a daemon started with --daemon (the GUI, the shortcut, start at login) stays idle and polls only on Poll now; a plain `python main.py` does one run and exits; `--poll` still polls. Settings has a checkbox "Enable polling". Changing it makes the GUI offer a daemon restart.
- polling.interval_seconds, polling.jitter_min_seconds, polling.jitter_max_seconds
	- Next run delay is interval_seconds + random jitter.
- folders.ghaadd_root, folders.processing, folders.complete, folders.partial, folders.logs
	- Folder names used under each resolved base download path.
	- Defaults are GHAADD, Processing, Complete, Partial, and Logs.
- terminal_log.enabled
	- false disables terminal output logging.
	- true writes all terminal output (stdout and stderr) to a .log file for this run. Only real run modes (the polling daemon, --once, --single, --drain-queue) create a log file; --help, --version and the read-only/control commands (--queue-status, --doctor, --pause, ...) do not.
	- Can be overridden while a polling daemon runs with --log-on / --log-off (or the GUI toggle); the override lasts until the daemon stops.
	- Log files are written under: paths.default_download_dir/folders.ghaadd_root/folders.logs.
	- Each run creates a new log file named with app start time (format: YYYYMMDD_HHMMSS.log). This includes short CLI commands such as --queue-status, so they count towards keep_files.
	- This setting only affects the terminal-output mirror. Lifecycle events (completed moves, superseded partial moves, typed warnings such as API, destination, move, sanity-check, supersede, and premature-finalize) are always recorded in state.db regardless of this setting, and are viewable with --lifecycle-log.
- terminal_log.max_file_mb
	- Size limit per log file in MB (default 10). When the current file reaches it, the daemon continues in a new, newer-named log file (the first line says which file it continues). Lines are never split across files. 0 disables rollover.
- gui.refresh_seconds
	- How often the GUI re-reads the data it shows, in seconds (default 3, allowed 1 to 60). A longer time uses less CPU on a slow machine. Edit config.json by hand; the GUI reads it when it starts. The daemon ignores the `gui` section.
- gui.silenced_warning_types
	- The warning types that never pop up a tray notification (a list; default `["API", "LIMIT"]`). The Warnings tab and its unread counter still show every warning. `API` is "could not resolve a release's current commit from GitHub", which is routine when a release was replaced or removed; `LIMIT` has its own notification. Change it in Settings → Warning notifications → "Choose warnings…", a checklist of all types with a description each (All / None / Defaults) that has its own Save and Cancel: Save writes `config.json` and applies at once, independently of the Settings window's own Save (Cancel with unsaved ticks asks first). Types are `API`, `SANITY_CHECK`, `SKIPPED`, `MOVE`, `SUPERSEDE_MOVE`, `SUPERSEDE_FINALIZE`, `PREMATURE_FINALIZE`, `FOLDER_RENAMED`, `FOLDER_RENAMED_MOVE`, `FOLDER_RENAMED_MOVED`, `DESTINATION`, `MAPPING`, `MAILBOX`, `LIMIT` and `PARTIAL_MOVE` (superseded partial moves, "Partial move" in the tab).
- gui.start_minimized, gui.start_daemon
	- What the GUI does when it opens (true/false, both default false; both are checkboxes in Settings → Startup). `start_minimized` starts it the way the minimize button leaves it: hidden in the tray when the tray icon is on and `minimize_to_tray` is on, otherwise minimized to the taskbar. `start_daemon` starts the polling daemon if none is running (like the Start button; a running daemon is left alone). `python main_gui.py --minimized` and `--start-daemon` ask for the same for one launch, whatever the settings say.
- gui.tray, gui.notifications, gui.minimize_to_tray, gui.close_to_tray
	- Tray icon settings (true/false; they only matter when the optional tray packages are installed). `tray` (default true) switches the icon off; `notifications` (default true) the notifications; `minimize_to_tray` (default true on Windows/macOS, false on Linux) hides the window in the tray when it is minimized; `close_to_tray` (default false) makes the close button hide the window instead of closing the GUI (use the tray menu's Quit). They are checkboxes in Settings → System tray (or edit config.json by hand); the GUI reads them when it starts.
- gui.status_message_seconds
	- How long a message in the GUI's status bar stays before the default text returns, in seconds (default 6, allowed 2 to 60). Same rules as above.
- terminal_log.keep_files
	- Number of log files to keep (default 30, including the current one). The oldest GHAADD log files (names matching YYYYMMDD_HHMMSS.log) beyond this are deleted when the polling daemon starts and after every rollover. 0 keeps everything. Never applied in --dry-run.

## Starting the daemon automatically

The polling daemon can start by itself when you log in, so you do not have to press Start after every restart:

```bash
python main.py --install-autostart      # set it up (on Linux and macOS it also starts the daemon now)
python main.py --autostart-status       # is it set up?
python main.py --uninstall-autostart    # remove it (a running daemon is left running)
```

- **Linux:** installs a systemd *user* unit, `~/.config/systemd/user/ghaadd.service` (no root needed), and enables it. systemd restarts the daemon if it crashes, after 60 seconds; a normal stop (the GUI's Stop button or `--stop`) is respected and it stays stopped until you start it again. To have it run at boot without anyone logging in, run `loginctl enable-linger $USER` once. Logs go to the journal (`journalctl --user -u ghaadd`) and, if switched on, to the terminal log files. Inside WSL this needs systemd enabled for the distribution.
- **Windows (the default, `--autostart-mode auto`):** creates a scheduled task "GHAADD" that starts at login, runs the daemon itself without a window (`pythonw.exe main.py --poll`) and restarts it after a failure (every minute, up to 999 times; a normal Stop is not restarted). It runs as you, only while you are logged in, so it needs neither admin rights nor a stored password. If the task cannot be created, the registry Run key is used instead (starts the daemon at login, no restart after a crash).
- **Windows, other choices:** `--autostart-mode task` allows only the task (an error if it cannot be made), `--autostart-mode runkey` only the Run key. `--task-trigger manual` makes a task with no trigger: nothing starts by itself, you start it with `schtasks /Run /TN GHAADD` (or from the Task Scheduler window) and it is then kept running. `--uninstall-autostart` removes the task and the Run key, whichever exist, and `--autostart-status` shows both. Errors of a window-less daemon go to `ghaadd.daemon.stderr.log`.
- **macOS:** installs a launchd LaunchAgent, `~/Library/LaunchAgents/com.ghaadd.daemon.plist`, which starts at login and restarts the daemon after a failure only. (Written from the documentation; not yet tried on a real Mac.)
- **In the GUI:** Settings has a "Startup" section with a "Start the daemon when I log in" checkbox that does the same as `--install-autostart` / `--uninstall-autostart` when you press Save, and a "Create shortcuts…" button (see Shortcuts) that asks for a folder and makes them at once.
- `python main.py --daemon-detached` starts the daemon in the background and returns; it does nothing when one is already running (the daemon allows only one instance).

## Shortcuts (the GHAADD name and icon)

```bash
python main.py --create-shortcuts                       # Start menu (Windows) / application menu (Linux)
python main.py --create-shortcuts --shortcut-dir "D:\Launcher"   # or into a folder of your choice
python main.py --remove-shortcuts
```

- **Windows:** creates two shortcuts without a console window: **GHAADD** (opens the GUI, with the GHAADD icon) and **GHAADD daemon** (starts the polling daemon in the background and does nothing if one is running). The GHAADD shortcut also carries the app's Windows identity (`GHAADD.GUI`, the one the GUI sets for itself), which is what lets Windows show "GHAADD" with its own icon in the taskbar and the notification settings instead of "Python". Put them in the Start menu (the default) or in any folder, for example a launcher folder you start things from after a reboot.
- **Linux:** creates `~/.local/share/applications/ghaadd.desktop`, so GHAADD appears in the application menu with its icon.
- **In the GUI:** the Settings dialog's "Create shortcuts…" button opens a folder picker (starting in the Start menu folder) and makes the shortcuts there. The two Startup boxes "Start the GUI minimized" and "Start the daemon when the GUI starts" count as they are ticked at that moment, saved or not: the GUI shortcut is then made with `--minimized` and/or `--start-daemon`. Those options are fixed in the shortcut, so unticking a box later does not change an existing shortcut; press the button again to remake it.
- **Command line:** add `--shortcut-minimized` and/or `--shortcut-start-daemon` to `--create-shortcuts` for the same.
- Notifications need the optional tray packages (see System tray); the shortcut only sets the name and icon they appear under.

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
- When a release/tag is not found on GitHub during an automatic poll (SKIP: release_not_found), the job stays PENDING and is rechecked on the repository's normal re-check schedule, instead of completing immediately. This covers releases published before their assets/commit are attached. Once the re-check schedule is exhausted, the job is completed and its staged folder (if any) is finalized using the counters/working_dir recorded from earlier attempts: moved to Complete if fully accounted for, or to Partial if incomplete. A zero-result attempt (transient error, or a non-terminal SKIP/FAILED before the asset list is reached) never overwrites previously recorded non-zero counters, so a release that was already fully downloaded is not mistaken for incomplete just because it later disappeared upstream. When such a job is finalized to Complete this way, a PREMATURE_FINALIZE WARNING lifecycle event is recorded noting the release likely was superseded/replaced before the re-check schedule finished. Manual pending-job runs (--run-pending) still complete release_not_found immediately since they run on demand.
- When a queue job reaches a terminal state (COMPLETED or FAILED with no retries left), its release folder is moved to Complete.
- When a pending job is superseded and its file counters indicate completion, its staged folder is finalized to Complete or mapped destination.
- When a pending job is superseded and appears incomplete, its staged folder is moved to Partial for quarantine/inspection.

## Deletion Safety

GHAADD never recursively deletes a directory tree anywhere, whether under paths.default_download_dir or a mapped repository destination. The only filesystem removals that ever happen are:

- A repository's own `*.part` temp file, removed on a failed/interrupted download of that same file (always inside that release's own Processing folder).
- Now-empty leftover subfolders under GHAADD/Processing after a completed release folder is moved out (empty-directory removal only; stops immediately at a non-empty folder, and never goes above the Processing root).
- The app's own state.db and ghaadd.daemon.status.json files (fixed paths beside the app files, unrelated to paths.default_download_dir/mapping destinations), only removed via the explicit `--purge-state` command or daemon shutdown cleanup.

A mapping entry's `destination` (and `folder`/`subfolder`) is only ever used as a **move target**: finished release folders are moved there, never deleted from there. If a folder with the same name already exists at the target, GHAADD renames the incoming folder with a `(2)`, `(3)`, ... suffix instead of overwriting or deleting the existing one. Nothing pre-existing at a mapped destination is ever touched.

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

To clear out old lifecycle events (completed moves, superseded partial moves, warnings, cycle summaries) or old terminal job_queue rows instead of wiping the whole database, use `--purge`/`--purge-jobs` (see CLI options above) - for example, to clear warnings you've already reviewed:

```bash
python main.py --purge --purge-type WARNING --purge-age 0
```

## Running the tests

The `tests/` folder holds automated checks of the code itself (they are separate from `--doctor`, which checks your installation). They use temporary folders and a temporary database, never your real `mapping.json`, `config.json` or `state.db`, and need no mailbox or GitHub access.

```text
python -m unittest discover -s tests -t .
```

To see which lines of the code the tests do not exercise yet, install the development tools (`coverage`) and run:

```text
pip install -r requirements-dev.txt
python -m coverage run --source=modules -m unittest discover -s tests -t .
python -m coverage report --skip-empty
```

`python -m coverage html` writes a browsable report to `htmlcov/`. `requirements-dev.txt` is only needed for this; running GHAADD itself needs `requirements.txt` alone.

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