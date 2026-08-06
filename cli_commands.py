import argparse
import sys
from typing import Callable

from db_manager import purge_state_database
from queue_reporting import build_queue_status_options, print_queue_status


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
    parser.add_argument("--smoke-test", action="store_true", help="Run internal smoke tests for download behavior.")

    queue_group = parser.add_argument_group("queue status/reporting options")
    queue_group.add_argument("--queue-status", action="store_true", help="Print current queue counts and scheduling details.")
    queue_group.add_argument("--json", action="store_true", help="Output --queue-status as JSON.")
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
        choices=("PENDING", "COMPLETED", "FAILED"),
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

    return parser.parse_args(args)


def handle_cli_command(parsed_args: argparse.Namespace, run_smoke_tests: Callable[[], None]) -> bool:
    """Execute one-shot command-line operations after parsing."""
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

    if parsed_args.smoke_test:
        run_smoke_tests()
        return True

    return False
