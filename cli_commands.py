import sys
from typing import Callable

from cli_help import build_main_help_text
from db_manager import purge_state_database
from queue_reporting import parse_queue_status_options, print_queue_status


def handle_cli_args(args: list[str], version: str, run_smoke_tests: Callable[[], None]) -> bool:
    """Handle one-shot command-line operations."""
    if "--help" in args or "-h" in args:
        print(build_main_help_text(version))
        return True

    if "--queue-status" in args:
        try:
            queue_options = parse_queue_status_options(args)
        except ValueError as exc:
            print(f"Queue status option error: {exc}", file=sys.stderr)
            return True

        print_queue_status(
            as_json=queue_options["as_json"],
            limit=queue_options["limit"],
            hours=queue_options["hours"],
            date_value=queue_options["date"],
            status_filter=queue_options["status"],
            report=queue_options["report"],
            report_only=queue_options["report_only"],
            report_csv_path=queue_options["report_csv_path"],
        )
        return True

    if "--purge-state" in args:
        deleted = purge_state_database()
        if deleted:
            print("Deleted local state database: state.db")
        else:
            print("No local state database found to delete.")
        return True

    if "--smoke-test" in args:
        run_smoke_tests()
        return True

    return False
