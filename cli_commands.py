import argparse
import json
import sys
from typing import Callable

from db_manager import (
    get_jobs_by_ids,
    open_database,
    purge_state_database,
    supersede_pending_jobs_by_ids,
)
from asset_downloader import (
    move_done_folders_to_mapped_destinations,
    quarantine_orphaned_processing_folders,
)
from doctor_checks import run_doctor
from mapping_manager import validate_mapping_schema
from queue_reports import build_queue_status_options, print_queue_status
from queue_worker import process_selected_pending_jobs


def parse_cli_args(args: list[str], version: str) -> argparse.Namespace:
    """Parse command-line arguments for the main entrypoint."""
    parser = argparse.ArgumentParser(
        prog="python main.py",
        description=f"GHAADD v{version}",
        epilog="With no options, behaviour is determined by config.json (poll or single run).",
    )

    parser.add_argument("--once", action="store_true", help="Run a single ingest-and-process cycle, then exit.")
    parser.add_argument("--poll", action="store_true", help="Force polling mode even if disabled in config.")
    parser.add_argument("--purge-state", action="store_true", help="Delete the local state database (state.db).")
    parser.add_argument(
        "--move-done-to-destination",
        action="store_true",
        help="Retry moving repository folders from Done to configured mapping destinations.",
    )
    parser.add_argument(
        "--quarantine-orphaned-processing",
        action="store_true",
        help=(
            "Preview orphaned Processing folders; add --yes to move them "
            "into GHAADD/Superseded/Orphaned."
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help=(
            "Confirm maintenance actions that move files when used with "
            "--quarantine-orphaned-processing."
        ),
    )
    parser.add_argument("--smoke-test", action="store_true", help="Run internal smoke tests for download behavior.")
    parser.add_argument("--mapping-validate", action="store_true", help="Validate mapping.json schema and report issues.")
    parser.add_argument("--doctor", action="store_true", help="Run environment and cross-platform diagnostics.")
    parser.add_argument(
        "--run-pending",
        nargs="+",
        type=int,
        metavar="JOB",
        help="Run the selected pending jobs immediately without changing their retry schedule.",
    )

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

    return parser.parse_args(args)


def handle_cli_command(parsed_args: argparse.Namespace, run_smoke_tests: Callable[[], None]) -> bool:
    """Execute one-shot command-line operations after parsing."""
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
        deleted = purge_state_database()
        if deleted:
            print("Deleted local state database: state.db")
        else:
            print("No local state database found to delete.")
        return True

    if parsed_args.move_done_to_destination:
        summary = move_done_folders_to_mapped_destinations()
        if parsed_args.json:
            print(json.dumps(summary, indent=2))
        return True

    if parsed_args.quarantine_orphaned_processing:
        dry_run = not bool(parsed_args.yes)
        if dry_run:
            print(
                "Running orphaned Processing cleanup in preview mode. "
                "Add --yes to execute file moves."
            )
        with open_database() as connection:
            summary = quarantine_orphaned_processing_folders(
                connection,
                dry_run=dry_run,
            )
        if parsed_args.json:
            print(json.dumps(summary, indent=2))
        return True

    if parsed_args.smoke_test:
        run_smoke_tests()
        return True

    return False
