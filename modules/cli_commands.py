"""Command-line flags: argument parsing and the dispatch of every one-shot command (queue, purge, report, doctor...)."""

import argparse
import json
import os
import sqlite3
import sys
from typing import Callable

from modules.daemon_control import (
    request_check_folders,
    request_poll_now,
    request_single_poll,
    request_stop,
    set_log_override,
    set_paused,
)
from modules.daemon_lock import is_daemon_running
from modules.db_manager import (
    get_jobs_by_ids,
    get_state_db_path,
    open_database,
    purge_job_queue_rows,
    purge_state_database,
    supersede_pending_jobs_by_ids,
)
from modules.asset_downloader import (
    move_complete_folders_to_mapped_destinations,
)
from modules.doctor_checks import run_doctor
from modules.dry_run_mode import is_dry_run
from modules.lifecycle_logger import list_lifecycle_events, purge_lifecycle_events
from modules.mapping_manager import validate_mapping_schema
from modules.queue_reports import build_queue_status_options, print_queue_status
from modules.queue_worker import process_selected_pending_jobs


# Options that only change what another command does, and the commands they work with. Given without one of those
# commands they would silently do nothing (and the program would carry on polling), so they are rejected instead.
MODIFIER_COMMANDS: dict[str, tuple[str, ...]] = {
    "json": ("queue_status", "mapping_validate", "doctor", "perf_report", "stats", "lifecycle_log", "move_complete_to_destination"),
    "queue_all": ("queue_status",),
    "queue_limit": ("queue_status",),
    "queue_hours": ("queue_status",),
    "queue_date": ("queue_status",),
    "queue_repo_filter": ("queue_status",),
    "queue_status_filter": ("queue_status",),
    "queue_report": ("queue_status",),
    "queue_report_only": ("queue_status",),
    "queue_report_csv": ("queue_status",),
    "lifecycle_limit": ("lifecycle_log",),
    "lifecycle_type": ("lifecycle_log",),
    "lifecycle_repo_filter": ("lifecycle_log",),
    "purge_type": ("purge",),
    "purge_status": ("purge_jobs",),
    "purge_repository": ("purge", "purge_jobs"),
    "purge_age": ("purge", "purge_jobs"),
    "purge_oldest": ("purge", "purge_jobs"),
    "autostart_mode": ("install_autostart",),
    "task_trigger": ("install_autostart",),
    "shortcut_dir": ("create_shortcuts", "remove_shortcuts"),
    "shortcut_minimized": ("create_shortcuts",),
    "shortcut_start_daemon": ("create_shortcuts",),
}


def _is_given(value: object) -> bool:
    """Return True when an option was actually passed (not None and not False)."""
    return value is not None and value is not False


def find_unused_options(parsed: argparse.Namespace) -> list[str]:
    """Messages for options that were given without a command they belong to (empty when all is well)."""
    problems = []
    for option, commands in MODIFIER_COMMANDS.items():
        if not _is_given(getattr(parsed, option, None)):
            continue
        if any(_is_given(getattr(parsed, command, None)) for command in commands):
            continue
        flags = " or ".join("--" + command.replace("_", "-") for command in commands)
        problems.append(f"--{option.replace('_', '-')} has no effect without {flags}")
    return problems


def parse_cli_args(args: list[str], version: str) -> argparse.Namespace:
    """Parse command-line arguments for the main entrypoint."""
    parser = argparse.ArgumentParser(
        prog="python main.py",
        description=f"GHAADD v{version}",
        epilog="With no options, behaviour is determined by config.json (poll or single run).",
    )

    parser.add_argument("--once", action="store_true", help="Run a single ingest-and-process cycle, then exit.")
    parser.add_argument("--poll", action="store_true", help="Force polling mode even if disabled in config.")
    parser.add_argument(
        "--daemon",
        action="store_true",
        help=(
            "Run the polling daemon the way the GUI, the shortcut and the start-at-login entries do: like --poll, "
            "except that when polling.enabled is false in config.json it stays idle and polls only on Poll now (--poll-now)."
        ),
    )
    parser.add_argument(
        "--single",
        action="store_true",
        help=(
            "Ingest at most one new notification and process at most one queue item, then exit. "
            "Prefers the just-ingested item; falls back to the oldest due job if no new notification exists."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Modifier for --single/--once/--poll/--drain-queue/--purge-state/--purge/--purge-jobs: "
            "preview actions without any writes/deletes (no emails marked/deleted, no files downloaded, "
            "no state.db/mapping.json changes; purge commands report matching counts without deleting)."
        ),
    )
    parser.add_argument(
        "--drain-queue",
        action="store_true",
        help=(
            "Skip email ingestion entirely and only process due queue jobs, "
            "repeating on schedule until every pending job is completed/failed, then exit."
        ),
    )
    parser.add_argument("--purge-state", action="store_true", help="Delete the local state database (state.db).")
    parser.add_argument(
        "--move-complete-to-destination",
        action="store_true",
        help="Retry moving repository folders from Complete to configured mapping destinations.",
    )
    parser.add_argument(
        "--smoke-test", action="store_true", help="Run internal smoke tests for download behavior.")
    parser.add_argument("--mapping-validate", action="store_true", help="Validate mapping.json schema and report issues.")
    parser.add_argument("--doctor", action="store_true", help="Run environment and cross-platform diagnostics.")
    parser.add_argument(
        "--perf-report",
        action="store_true",
        help="Measure data sizes, growth and the timings of the routine queries (read-only, safe while the daemon runs; --json for machine output).",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help=(
            "Show statistics: repositories mapped/active, jobs per day/week/month/year/lifetime, the busiest and the "
            "biggest repositories and the age of the history (read-only, safe while the daemon runs; --json for machine output)."
        ),
    )
    parser.add_argument(
        "--run-pending",
        nargs="+",
        type=int,
        metavar="JOB",
        help="Run the selected pending jobs immediately without changing their retry schedule.",
    )

    control_group = parser.add_argument_group("running-daemon control options")
    control_group.add_argument("--pause", action="store_true", help="Pause polling in the running daemon (the countdown freezes).")
    control_group.add_argument("--resume", action="store_true", help="Resume polling in the running daemon.")
    control_group.add_argument("--stop", action="store_true", help="Stop the running polling daemon gracefully (the job in progress finishes first).")
    control_group.add_argument("--check-folders", action="store_true", help="Make the running daemon check destinations and folder limits right away.")
    control_group.add_argument("--poll-now", action="store_true", help="Make the running daemon poll right away (resets its countdown).")
    control_group.add_argument("--poll-one", action="store_true", help="Make the running daemon poll one notification and process one queue item (like --single, but in the running daemon; resets its countdown).")

    control_group.add_argument("--log-on", action="store_true", help="Start writing terminal output to a new .log file in the running daemon (until it stops or --log-off).")
    control_group.add_argument("--log-off", action="store_true", help="Stop writing the .log file in the running daemon.")

    autostart_group = parser.add_argument_group("autostart options")
    autostart_group.add_argument("--install-autostart", action="store_true", help="Start the polling daemon automatically at login (Linux: systemd user unit; Windows: per-user Run entry).")
    autostart_group.add_argument("--autostart-mode", choices=("auto", "task", "runkey"), default=None, help="Windows only, with --install-autostart: auto = a Task Scheduler task that runs the daemon and restarts it after a failure, or the registry Run key if the task cannot be created (default); task = only the task; runkey = only the Run key (starts the daemon at login, no restart).")
    autostart_group.add_argument("--task-trigger", choices=("logon", "manual"), default=None, help="With --autostart-mode task: start at login (default) or only when you start the task yourself (schtasks /Run /TN GHAADD).")
    autostart_group.add_argument("--uninstall-autostart", action="store_true", help="Remove the automatic start at login (a running daemon is left running).")
    autostart_group.add_argument("--autostart-status", action="store_true", help="Show whether the daemon starts automatically at login.")
    autostart_group.add_argument("--create-shortcuts", action="store_true", help="Create the GHAADD shortcuts (Windows: \"GHAADD\" for the window and \"GHAADD daemon\" in the Start menu, so notifications say GHAADD instead of Python; Linux: an application-menu launcher).")
    autostart_group.add_argument("--remove-shortcuts", action="store_true", help="Remove the shortcuts made by --create-shortcuts.")
    autostart_group.add_argument("--shortcut-dir", metavar="FOLDER", help="Put the shortcuts in this folder instead of the Start menu / application menu (for example your own launcher folder).")
    autostart_group.add_argument("--shortcut-minimized", action="store_true", help="With --create-shortcuts: the GUI shortcut starts the window minimized (it runs main_gui.py --minimized).")
    autostart_group.add_argument("--shortcut-start-daemon", action="store_true", help="With --create-shortcuts: the GUI shortcut also starts the daemon if none is running (main_gui.py --start-daemon).")
    autostart_group.add_argument("--daemon-detached", action="store_true", help="Start the polling daemon in the background (no console window) and return; does nothing if one is running.")

    queue_group = parser.add_argument_group("queue status/reporting options")
    queue_group.add_argument("--queue-status", action="store_true", help="Print current queue counts and scheduling details.")
    queue_group.add_argument("--json", action="store_true", help="Output JSON for compatible commands (queue-status, mapping-validate, doctor).")
    queue_group.add_argument("--queue-all", action="store_true", help="Show all matching jobs instead of a limited list.")
    queue_group.add_argument("--queue-limit", type=int, help="Show up to N jobs in history (0 means all).")
    queue_group.add_argument("--queue-hours", type=float, help="Filter jobs created in the last H hours.")
    queue_group.add_argument("--queue-date", help="Filter jobs created on YYYY-MM-DD.")
    queue_group.add_argument(
        "--queue-repo-filter",
        help="Filter by repository name substring (case-insensitive).",
    )
    queue_group.add_argument(
        "--queue-status-filter",
        choices=("PENDING", "COMPLETED", "FAILED", "SUPERSEDED"),
        help="Filter by status.",
    )
    queue_group.add_argument("--queue-report", action="store_true", help="Print a compact report.")
    queue_group.add_argument(
        "--queue-report-only",
        action="store_true",
        help="Print only the report section (no job list).",
    )
    queue_group.add_argument(
        "--queue-report-csv",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help="Export the report section to CSV.",
    )

    queue_maintenance_group = parser.add_argument_group("queue maintenance options")
    queue_maintenance_group.add_argument(
        "--queue-remove-pending-ids",
        nargs="+",
        type=int,
        metavar="ID",
        help="Mark specific pending job IDs as SUPERSEDED (removes them from pending queue).",
    )

    lifecycle_group = parser.add_argument_group("lifecycle log options")
    lifecycle_group.add_argument(
        "--lifecycle-log",
        action="store_true",
        help="Print recent lifecycle events (completed moves, superseded partial moves, warnings).",
    )
    lifecycle_group.add_argument(
        "--lifecycle-limit",
        type=int,
        help="Limit how many lifecycle events to print (0 means all). Default 20.",
    )
    lifecycle_group.add_argument(
        "--lifecycle-type",
        choices=("COMPLETED_MOVE", "PARTIAL_MOVE", "WARNING", "CYCLE_SUMMARY"),
        help="Filter lifecycle events by type.",
    )
    lifecycle_group.add_argument(
        "--lifecycle-repo-filter",
        help="Filter lifecycle events by repository name substring (case-insensitive).",
    )

    purge_group = parser.add_argument_group("lifecycle purge options")
    purge_group.add_argument(
        "--purge",
        action="store_true",
        help=(
            "Delete lifecycle events (completed moves, superseded partial moves, warnings, "
            "cycle summaries) from state.db. Combine with --purge-type/--purge-repository to "
            "narrow it down; requires --purge-age."
        ),
    )
    purge_group.add_argument(
        "--purge-type",
        choices=("COMPLETED_MOVE", "PARTIAL_MOVE", "WARNING", "CYCLE_SUMMARY"),
        help="Limit --purge to one lifecycle event type. Omit to match every type.",
    )
    purge_group.add_argument(
        "--purge-jobs",
        action="store_true",
        help=(
            "Delete terminal job_queue rows (COMPLETED/FAILED/SUPERSEDED - PENDING jobs are never "
            "touched) from state.db. Combine with --purge-status/--purge-repository to narrow it "
            "down; requires --purge-age."
        ),
    )
    purge_group.add_argument(
        "--purge-status",
        choices=("COMPLETED", "FAILED", "SUPERSEDED"),
        help="Limit --purge-jobs to one job status. Omit to match every terminal status.",
    )
    purge_group.add_argument(
        "--purge-repository",
        help=(
            "Limit --purge/--purge-jobs to a repository name substring (case-insensitive). "
            "Omit to match every repository."
        ),
    )
    purge_group.add_argument(
        "--purge-age",
        type=int,
        metavar="DAYS",
        help=(
            "Required with --purge, and one of --purge-age/--purge-oldest required with "
            "--purge-jobs: only delete rows at least this many days old (lifecycle events use "
            "created_at, job_queue rows use completed_at). Use 0 to delete every matching row "
            "regardless of age."
        ),
    )
    purge_group.add_argument(
        "--purge-oldest",
        type=int,
        metavar="N",
        help=(
            "Alternative to --purge-age, --purge-jobs only: delete the N oldest matching "
            "job_queue rows (by completed_at/updated_at/created_at) regardless of their age. "
            "Useful when you know how many rows you want gone (for example, to shrink a large "
            "state.db) but don't know what --purge-age value that corresponds to - check "
            "--queue-status --queue-report for an age preview first."
        ),
    )

    parsed = parser.parse_args(args)
    problems = find_unused_options(parsed)
    if problems:
        parser.error("; ".join(problems) + ". Nothing was started.")
    # The two options with a default were left unset above so that "given" could be told from "not given".
    parsed.autostart_mode = parsed.autostart_mode or "auto"
    parsed.task_trigger = parsed.task_trigger or "logon"
    return parsed


def handle_cli_command(parsed_args: argparse.Namespace, run_smoke_tests: Callable[[], None]) -> bool:
    """Execute one-shot command-line operations after parsing."""
    if parsed_args.install_autostart or parsed_args.uninstall_autostart or parsed_args.autostart_status:
        from modules import autostart  # imported here: only these commands need it

        if parsed_args.install_autostart:
            result = autostart.install_autostart(mode=parsed_args.autostart_mode, trigger=parsed_args.task_trigger)
            print(result.message, file=sys.stdout if result.ok else sys.stderr)
        elif parsed_args.uninstall_autostart:
            result = autostart.uninstall_autostart()
            print(result.message, file=sys.stdout if result.ok else sys.stderr)
        else:
            status = autostart.autostart_status()
            print(f"Autostart: {status.detail}")
        return True

    if parsed_args.create_shortcuts or parsed_args.remove_shortcuts:
        from modules import shortcuts  # imported here: only these commands need it

        if parsed_args.create_shortcuts:
            result = shortcuts.create_shortcuts(
                parsed_args.shortcut_dir,
                options=shortcuts.gui_options(parsed_args.shortcut_minimized, parsed_args.shortcut_start_daemon),
            )
        else:
            result = shortcuts.remove_shortcuts(parsed_args.shortcut_dir)
        print(result.message, file=sys.stdout if result.ok else sys.stderr)
        return True

    if parsed_args.daemon_detached:
        if is_daemon_running():
            print("A GHAADD daemon is already running.")
            return True
        from modules import daemon_launcher

        try:
            print(f"Daemon started in the background (PID {daemon_launcher.start_daemon()}).")
        except OSError as exc:
            print(f"Could not start the daemon: {exc}", file=sys.stderr)
        return True

    if parsed_args.perf_report:
        from modules import perf_report  # imported here: it pulls in the GUI data modules, no other command needs them

        report = perf_report.collect()
        print(json.dumps(report, indent=2) if parsed_args.json else perf_report.format_report(report))
        return True

    if parsed_args.stats:
        from modules import stats  # imported here: no other command needs it

        report = stats.collect()
        print(json.dumps(report, indent=2) if parsed_args.json else stats.format_report(report))
        return True

    if parsed_args.doctor:
        doctor_report = run_doctor()
        if parsed_args.json:
            print(json.dumps(doctor_report, indent=2))
            return True

        if doctor_report["ok"]:
            print("Doctor checks passed.")
        else:
            print("Doctor checks found blocking issues.")

        print(f"Platform: {doctor_report['platform']}")

        if doctor_report["errors"]:
            print("Errors:")
            for error_text in doctor_report["errors"]:
                print(f"- {error_text}")

        if doctor_report["warnings"]:
            print("Warnings:")
            for warning_text in doctor_report["warnings"]:
                print(f"- {warning_text}")

        if doctor_report["checks"]:
            print("Checks:")
            for check_text in doctor_report["checks"]:
                print(f"- {check_text}")

        return True

    if parsed_args.mapping_validate:
        validation_result = validate_mapping_schema()
        if parsed_args.json:
            print(json.dumps(validation_result, indent=2))
            return True

        if validation_result["ok"]:
            print("Mapping validation passed.")
        else:
            print("Mapping validation failed.")

        if validation_result["errors"]:
            print("Errors:")
            for error_text in validation_result["errors"]:
                print(f"- {error_text}")

        if validation_result["warnings"]:
            print("Warnings:")
            for warning_text in validation_result["warnings"]:
                print(f"- {warning_text}")

        return True

    if (
        parsed_args.pause or parsed_args.resume or parsed_args.poll_now or parsed_args.poll_one or parsed_args.stop
        or parsed_args.log_on or parsed_args.log_off or parsed_args.check_folders
    ):
        if parsed_args.pause and parsed_args.resume:
            print("Control option error: --pause and --resume cannot be combined.", file=sys.stderr)
            return True
        if parsed_args.log_on and parsed_args.log_off:
            print("Control option error: --log-on and --log-off cannot be combined.", file=sys.stderr)
            return True

        # A stopped daemon clears pause on startup and ignores older requests, so writing would mislead.
        if not is_daemon_running():
            print("No GHAADD daemon is running; nothing to control.", file=sys.stderr)
            return True

        try:
            if parsed_args.pause:
                set_paused(True)
                print("Pause requested. The daemon freezes its countdown within about a second.")
            if parsed_args.resume:
                set_paused(False)
                print("Resume requested.")
            if parsed_args.log_on:
                set_log_override(True)
                print("Log on requested. The daemon starts a new .log file within about a second.")
            if parsed_args.log_off:
                set_log_override(False)
                print("Log off requested. The daemon closes its .log file within about a second.")
            if parsed_args.poll_now:
                request_poll_now()
                print("Forced poll requested. It runs within a second, also while the daemon is paused (it then stays paused).")
            if parsed_args.poll_one:
                request_single_poll()
                print("Single poll requested. The daemon takes at most one notification and processes at most one queue item within a second.")
            if parsed_args.check_folders:
                request_check_folders()
                print("Folder check requested. The daemon checks destinations and folder limits within about a second.")
            if parsed_args.stop:
                request_stop()
                print("Stop requested. The daemon exits after the job in progress finishes.")
        except sqlite3.OperationalError as error:
            print(f"Could not update the control state ({error}); try again.", file=sys.stderr)
        return True

    if parsed_args.run_pending:
        requested_ids = sorted({int(job_id) for job_id in parsed_args.run_pending})
        invalid_ids = [job_id for job_id in requested_ids if job_id <= 0]
        if invalid_ids:
            print(
                "Run-pending option error: invalid job ID(s). Expected positive integers only.",
                file=sys.stderr,
            )
            return True

        with open_database() as connection:
            processed_count, skipped_ids, missing_ids = process_selected_pending_jobs(connection, None, requested_ids)

        print(f"Manual pending-job run complete. Processed {processed_count} job(s).")
        if skipped_ids:
            print("Skipped (not pending):")
            for job_id in skipped_ids:
                print(f"- #{job_id}")
        if missing_ids:
            print("Skipped (not found):")
            for job_id in missing_ids:
                print(f"- #{job_id}")
        return True

    if parsed_args.queue_remove_pending_ids:
        requested_ids = sorted({int(job_id) for job_id in parsed_args.queue_remove_pending_ids})
        invalid_ids = [job_id for job_id in requested_ids if job_id <= 0]
        if invalid_ids:
            print(
                "Queue maintenance option error: invalid job ID(s). "
                "Expected positive integers only.",
                file=sys.stderr,
            )
            return True

        with open_database() as connection:
            removed_rows = supersede_pending_jobs_by_ids(connection, requested_ids)
            all_rows = get_jobs_by_ids(connection, requested_ids)

        removed_ids = {int(row["id"]) for row in removed_rows}
        all_rows_by_id = {int(row["id"]): row for row in all_rows}
        not_found_ids = [job_id for job_id in requested_ids if job_id not in all_rows_by_id]
        not_pending_rows = [
            all_rows_by_id[job_id]
            for job_id in requested_ids
            if job_id in all_rows_by_id and job_id not in removed_ids
        ]

        print(
            f"Queue maintenance complete. Removed {len(removed_rows)} pending job(s) "
            "(status -> SUPERSEDED)."
        )

        if removed_rows:
            print("Removed pending jobs:")
            for row in removed_rows:
                print(
                    f"- #{int(row['id'])} {row['repo']} {row['tag']} "
                    f"({row['release_type'] or 'Release'}, expected_commit={row['expected_commit'] or 'unknown'})"
                )

        if not_pending_rows:
            print("Skipped (not pending):")
            for row in not_pending_rows:
                print(
                    f"- #{int(row['id'])} status={row['status']} {row['repo']} {row['tag']} "
                    f"({row['release_type'] or 'Release'})"
                )

        if not_found_ids:
            print("Skipped (not found): " + ", ".join(str(job_id) for job_id in not_found_ids))

        return True

    if parsed_args.queue_status:
        try:
            queue_options = build_queue_status_options(
                as_json=parsed_args.json,
                queue_all=parsed_args.queue_all,
                queue_limit=parsed_args.queue_limit,
                queue_hours=parsed_args.queue_hours,
                queue_date=parsed_args.queue_date,
                queue_repo_filter=parsed_args.queue_repo_filter,
                queue_status_filter=parsed_args.queue_status_filter,
                queue_report=parsed_args.queue_report,
                queue_report_only=parsed_args.queue_report_only,
                queue_report_csv=parsed_args.queue_report_csv,
            )
        except ValueError as exc:
            print(f"Queue status option error: {exc}", file=sys.stderr)
            return True

        print_queue_status(
            as_json=queue_options["as_json"],
            limit=queue_options["limit"],
            hours=queue_options["hours"],
            date_value=queue_options["date"],
            repo_filter=queue_options["repo_filter"],
            status_filter=queue_options["status"],
            report=queue_options["report"],
            report_only=queue_options["report_only"],
            report_csv_path=queue_options["report_csv_path"],
        )
        return True

    if parsed_args.purge_state:
        if is_dry_run():
            if os.path.exists(get_state_db_path()):
                print("[DRY-RUN] Would delete local state database: state.db")
            else:
                print("[DRY-RUN] No local state database found to delete.")
            return True

        deleted = purge_state_database()
        if deleted:
            print("Deleted local state database: state.db")
        else:
            print("No local state database found to delete.")
        return True

    if parsed_args.purge:
        if parsed_args.purge_oldest is not None:
            print(
                "Purge option error: --purge-oldest is only supported with --purge-jobs, not --purge.",
                file=sys.stderr,
            )
            return True
        if parsed_args.purge_age is None:
            print(
                "Purge option error: --purge-age is required (use --purge-age 0 to delete "
                "every matching event regardless of age).",
                file=sys.stderr,
            )
            return True
        if parsed_args.purge_age < 0:
            print("Purge option error: --purge-age must be >= 0.", file=sys.stderr)
            return True

        matched_count = purge_lifecycle_events(
            event_type=parsed_args.purge_type,
            repo_filter=parsed_args.purge_repository,
            min_age_days=parsed_args.purge_age,
            dry_run=is_dry_run(),
        )

        filter_bits = [
            f"type={parsed_args.purge_type}" if parsed_args.purge_type else "type=ALL",
            f"repository~='{parsed_args.purge_repository}'" if parsed_args.purge_repository else "repository=ALL",
            f"age>={parsed_args.purge_age}d",
        ]
        action_label = "[DRY-RUN] Would purge" if is_dry_run() else "Purged"
        print(f"{action_label} {matched_count} lifecycle event(s) ({', '.join(filter_bits)}).")
        return True

    if parsed_args.purge_jobs:
        has_purge_age = parsed_args.purge_age is not None
        has_purge_oldest = parsed_args.purge_oldest is not None

        if has_purge_age and has_purge_oldest:
            print("Purge option error: use only one of --purge-age or --purge-oldest, not both.", file=sys.stderr)
            return True
        if not has_purge_age and not has_purge_oldest:
            print(
                "Purge option error: --purge-age or --purge-oldest is required (use --purge-age 0 "
                "to delete every matching job regardless of age, or --purge-oldest N to delete the "
                "N oldest matching jobs).",
                file=sys.stderr,
            )
            return True
        if has_purge_age and parsed_args.purge_age < 0:
            print("Purge option error: --purge-age must be >= 0.", file=sys.stderr)
            return True
        if has_purge_oldest and parsed_args.purge_oldest <= 0:
            print("Purge option error: --purge-oldest must be a positive integer.", file=sys.stderr)
            return True

        with open_database() as connection:
            matched_count = purge_job_queue_rows(
                connection,
                status=parsed_args.purge_status,
                repo_filter=parsed_args.purge_repository,
                min_age_days=parsed_args.purge_age if has_purge_age else None,
                oldest_count=parsed_args.purge_oldest if has_purge_oldest else None,
                dry_run=is_dry_run(),
            )

        filter_bits = [
            f"status={parsed_args.purge_status}" if parsed_args.purge_status else "status=ALL (terminal only, PENDING never touched)",
            f"repository~='{parsed_args.purge_repository}'" if parsed_args.purge_repository else "repository=ALL",
            f"age>={parsed_args.purge_age}d" if has_purge_age else f"oldest {parsed_args.purge_oldest}",
        ]
        action_label = "[DRY-RUN] Would purge" if is_dry_run() else "Purged"
        print(f"{action_label} {matched_count} job_queue row(s) ({', '.join(filter_bits)}).")
        return True

    if parsed_args.move_complete_to_destination:
        summary = move_complete_folders_to_mapped_destinations()
        if parsed_args.json:
            print(json.dumps(summary, indent=2))
        return True

    if parsed_args.smoke_test:
        run_smoke_tests()
        return True

    if parsed_args.lifecycle_log:
        limit = parsed_args.lifecycle_limit
        if limit is not None:
            if limit < 0:
                print(
                    "Lifecycle log option error: invalid --lifecycle-limit value. "
                    "Expected an integer >= 0.",
                    file=sys.stderr,
                )
                return True
            limit = None if limit == 0 else limit
        else:
            limit = 20

        events = list_lifecycle_events(
            limit=limit,
            event_type=parsed_args.lifecycle_type,
            repo_filter=parsed_args.lifecycle_repo_filter,
        )

        if parsed_args.json:
            print(json.dumps(events, indent=2))
            return True

        if not events:
            print("No lifecycle events found.")
            return True

        for event in events:
            label = event["category"] or event["event_type"]
            print(f"[{event['created_at_readable']}] {label}: {event['message']}")
        return True

    return False
