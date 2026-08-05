import sys
sys.dont_write_bytecode = True

import csv
import json
import os
import random
import requests
import time
from config_manager import get_max_emails_to_process, get_polling_settings, get_recheck_intervals_minutes
from datetime import datetime, timedelta
from dotenv import load_dotenv
from typing import Literal, Optional, Tuple, Union, overload

__version__ = "0.5.5-beta"

# Load environment variables from .env.
load_dotenv()
GITHUB_TOKEN = os.getenv("GITHUB_PAT")

# Import application modules.
from listener import get_pending_notifications, mark_as_read_and_delete
from db_manager import (
    purge_state_database,
    open_database,
    enqueue_job,
    get_due_jobs,
    mark_job_completed,
    mark_job_failed,
    reschedule_job,
)
from downloader import download_release


def run_internal_smoke_tests():
    """Run internal smoke tests for baseline release-download behavior."""
    print("🚀 Starting Internal Smoke Tests...")
    
    # Test graceful failure handling with a non-existent repository.
    print("\n--- Test 1: Verifying Failure Baseline (Invalid Repo) ---")
    fail_result = download_release("github/this-repo-does-not-exist", "v99.9.9")
    print(f"Result (Expected 'SKIP' or False): {fail_result}")
    
    # Test successful download and state database updates.
    print("\n--- Test 2: Verifying Success Baseline (Official GitHub Repo) ---")
    # Use a stable official repository and tag for predictable test behavior.
    # This release includes a representative mix of assets and source archives.
    success_result = download_release("cli/cli", "v2.30.0") 
    print(f"Result (Expected True): {success_result}")
    
    # Test duplicate guarding by repeating a previously successful request.
    print("\n--- Test 3: Verifying Duplicate Guard (Re-running Success Path) ---")
    repeat_result = download_release("cli/cli", "v2.30.0")
    print(f"Result (Expected True with 'Skipping' console logs): {repeat_result}")
    
    print("\n🎉 Smoke tests complete.")


def _format_timestamp(unix_timestamp):
    """Format a Unix timestamp for console output."""
    if unix_timestamp is None:
        return "-"
    try:
        return datetime.fromtimestamp(float(unix_timestamp)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return "-"


def _parse_queue_status_options(args):
    """Parse and validate --queue-status options."""
    options = {
        "as_json": "--json" in args,
        "limit": 10,
        "hours": None,
        "date": None,
        "status": None,
        "report": "--queue-report" in args,
        "report_only": "--queue-report-only" in args,
        "report_csv_path": None,
    }

    if options["report_only"]:
        options["report"] = True

    if "--queue-all" in args:
        options["limit"] = None

    if "--queue-limit" in args:
        index = args.index("--queue-limit")
        if index + 1 >= len(args):
            raise ValueError("Missing value for --queue-limit. Expected an integer >= 0.")
        try:
            limit_value = int(args[index + 1])
        except ValueError as exc:
            raise ValueError("Invalid --queue-limit value. Expected an integer >= 0.") from exc
        if limit_value < 0:
            raise ValueError("Invalid --queue-limit value. Expected an integer >= 0.")
        options["limit"] = None if limit_value == 0 else limit_value

    if "--queue-hours" in args:
        index = args.index("--queue-hours")
        if index + 1 >= len(args):
            raise ValueError("Missing value for --queue-hours. Expected a number > 0.")
        try:
            hours_value = float(args[index + 1])
        except ValueError as exc:
            raise ValueError("Invalid --queue-hours value. Expected a number > 0.") from exc
        if hours_value <= 0:
            raise ValueError("Invalid --queue-hours value. Expected a number > 0.")
        options["hours"] = hours_value

    if "--queue-date" in args:
        index = args.index("--queue-date")
        if index + 1 >= len(args):
            raise ValueError("Missing value for --queue-date. Expected YYYY-MM-DD.")
        date_value = args[index + 1]
        try:
            datetime.strptime(date_value, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("Invalid --queue-date value. Expected YYYY-MM-DD.") from exc
        options["date"] = date_value

    if options["hours"] is not None and options["date"] is not None:
        raise ValueError("Use only one of --queue-hours or --queue-date.")

    if "--queue-status-filter" in args:
        index = args.index("--queue-status-filter")
        if index + 1 >= len(args):
            raise ValueError(
                "Missing value for --queue-status-filter. Expected PENDING, COMPLETED, or FAILED."
            )
        status_value = args[index + 1].strip().upper()
        allowed_statuses = {"PENDING", "COMPLETED", "FAILED"}
        if status_value not in allowed_statuses:
            raise ValueError(
                "Invalid --queue-status-filter value. Expected PENDING, COMPLETED, or FAILED."
            )
        options["status"] = status_value

    if "--queue-report-csv" in args:
        index = args.index("--queue-report-csv")
        explicit_path = None
        if index + 1 < len(args):
            potential_path = args[index + 1].strip()
            if potential_path and not potential_path.startswith("--"):
                explicit_path = potential_path

        options["report_csv_path"] = explicit_path or _build_default_queue_report_csv_path(options)
        options["report"] = True

    return options


def _build_default_queue_report_csv_path(options):
    """Build a default report CSV filename for --queue-report-csv without PATH."""
    scope = "all"
    if options.get("date"):
        scope = f"date-{options['date']}"
    elif options.get("hours") is not None:
        hours_str = str(options["hours"]).replace(".", "p")
        scope = f"last-{hours_str}h"

    status_part = ""
    if options.get("status"):
        status_part = f"-{str(options['status']).lower()}"

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"queue-report-{scope}{status_part}-{timestamp}.csv"


def _write_queue_report_csv(report_data, output_path):
    """Write queue report data to a CSV file."""
    with open(output_path, "w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(
            csv_file,
            fieldnames=[
                "section",
                "metric",
                "repo",
                "value",
                "success_count",
                "skip_count",
                "failed_count",
                "terminal_count",
                "terminal_success_rate_percent",
            ],
        )
        writer.writeheader()

        writer.writerow({"section": "summary", "metric": "window_total_jobs", "value": report_data["window_total_jobs"]})
        writer.writerow({"section": "summary", "metric": "terminal_jobs", "value": report_data["terminal_jobs"]})
        writer.writerow({"section": "summary", "metric": "success_jobs", "value": report_data["success_jobs"]})
        writer.writerow({"section": "summary", "metric": "skip_jobs", "value": report_data["skip_jobs"]})
        writer.writerow({"section": "summary", "metric": "failed_jobs", "value": report_data["failed_jobs"]})
        writer.writerow({"section": "summary", "metric": "retry_pending_jobs", "value": report_data["retry_pending_jobs"]})
        writer.writerow(
            {
                "section": "summary",
                "metric": "success_rate_percent",
                "value": report_data["success_rate_percent"],
            }
        )
        writer.writerow(
            {
                "section": "summary",
                "metric": "hard_failure_rate_percent",
                "value": report_data["hard_failure_rate_percent"],
            }
        )

        for status_name in sorted(report_data.get("status_breakdown", {}).keys()):
            writer.writerow(
                {
                    "section": "status_breakdown",
                    "metric": status_name,
                    "value": report_data["status_breakdown"][status_name],
                }
            )

        for item in report_data.get("top_failed_repos", []):
            writer.writerow(
                {
                    "section": "top_failed_repos",
                    "metric": "failed_count",
                    "repo": item["repo"],
                    "value": item["failed_count"],
                    "failed_count": item["failed_count"],
                }
            )

        for item in report_data.get("top_successful_repos", []):
            writer.writerow(
                {
                    "section": "top_successful_repos",
                    "metric": "terminal_success_rate_percent",
                    "repo": item["repo"],
                    "value": item["terminal_success_rate_percent"],
                    "success_count": item["success_count"],
                    "skip_count": item["skip_count"],
                    "failed_count": item["failed_count"],
                    "terminal_count": item["terminal_count"],
                    "terminal_success_rate_percent": item["terminal_success_rate_percent"],
                }
            )


def _build_queue_scope(hours=None, date_value=None, status_filter=None, now_timestamp=None):
    """Build WHERE clause and params for queue history filtering."""
    now_timestamp = now_timestamp if now_timestamp is not None else time.time()
    conditions = []
    params = []

    if hours is not None:
        cutoff = now_timestamp - (float(hours) * 3600.0)
        conditions.append("created_at >= ?")
        params.append(float(cutoff))

    if date_value is not None:
        start_dt = datetime.strptime(date_value, "%Y-%m-%d")
        end_dt = start_dt + timedelta(days=1)
        conditions.append("created_at >= ?")
        conditions.append("created_at < ?")
        params.extend([float(start_dt.timestamp()), float(end_dt.timestamp())])

    if status_filter is not None:
        conditions.append("status = ?")
        params.append(status_filter)

    if not conditions:
        return "", []

    return "WHERE " + " AND ".join(conditions), params


def _collect_queue_status_data(limit=10, hours=None, date_value=None, status_filter=None, include_report=False):
    """Return queue status data for text or JSON rendering."""
    now_timestamp = time.time()
    scope_where, scope_params = _build_queue_scope(
        hours=hours,
        date_value=date_value,
        status_filter=status_filter,
        now_timestamp=now_timestamp,
    )

    with open_database() as connection:
        summary_rows = connection.execute(
            f"""
            SELECT status, COUNT(*) AS count
            FROM job_queue
            {scope_where}
            GROUP BY status
            ORDER BY status ASC
            """
            , tuple(scope_params)
        ).fetchall()

        total_jobs = sum(int(row["count"]) for row in summary_rows)
        pending_due_conditions = ["status = 'PENDING'", "next_check_time <= ?"]
        pending_due_params = list(scope_params)
        pending_due_params.append(float(now_timestamp))
        if scope_where:
            pending_due_where = f"{scope_where} AND " + " AND ".join(pending_due_conditions)
        else:
            pending_due_where = "WHERE " + " AND ".join(pending_due_conditions)

        pending_due = connection.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM job_queue
            {pending_due_where}
            """,
            tuple(pending_due_params),
        ).fetchone()

        next_pending_conditions = ["status = 'PENDING'"]
        next_pending_params = list(scope_params)
        if scope_where:
            next_pending_where = f"{scope_where} AND " + " AND ".join(next_pending_conditions)
        else:
            next_pending_where = "WHERE " + " AND ".join(next_pending_conditions)

        next_pending = connection.execute(
            f"""
            SELECT
                id,
                repo,
                tag,
                release_type,
                attempt_count,
                next_check_time,
                expected_commit,
                downloaded_count,
                skipped_count,
                total_items,
                last_result,
                created_at,
                updated_at,
                completed_at
            FROM job_queue
            {next_pending_where}
            ORDER BY next_check_time ASC, id ASC
            LIMIT 1
            """
            , tuple(next_pending_params)
        ).fetchone()

        recent_query = f"""
            SELECT
                id,
                repo,
                tag,
                release_type,
                status,
                attempt_count,
                next_check_time,
                expected_commit,
                downloaded_count,
                skipped_count,
                total_items,
                last_result,
                created_at,
                updated_at,
                completed_at
            FROM job_queue
            {scope_where}
            ORDER BY id DESC
        """
        recent_params = list(scope_params)
        if limit is not None:
            recent_query += "\n            LIMIT ?"
            recent_params.append(int(limit))
        recent_jobs = connection.execute(recent_query, tuple(recent_params)).fetchall()

        report_payload = None
        if include_report:
            failed_repo_rows = connection.execute(
                f"""
                SELECT repo, COUNT(*) AS failed_count
                FROM job_queue
                {scope_where}
                {"AND" if scope_where else "WHERE"} (status = 'FAILED' OR last_result = 'FAILED')
                GROUP BY repo
                ORDER BY failed_count DESC, repo ASC
                LIMIT 5
                """,
                tuple(scope_params),
            ).fetchall()

            successful_repo_rows = connection.execute(
                f"""
                SELECT
                    repo,
                    SUM(CASE WHEN last_result = 'SUCCESS' THEN 1 ELSE 0 END) AS success_count,
                    SUM(CASE WHEN last_result = 'SKIP' THEN 1 ELSE 0 END) AS skip_count,
                    SUM(CASE WHEN status = 'FAILED' OR last_result = 'FAILED' THEN 1 ELSE 0 END) AS failed_count,
                    SUM(CASE WHEN status IN ('COMPLETED', 'FAILED') THEN 1 ELSE 0 END) AS terminal_count
                FROM job_queue
                {scope_where}
                GROUP BY repo
                HAVING terminal_count > 0
                ORDER BY success_count DESC, skip_count DESC, terminal_count DESC, repo ASC
                LIMIT 5
                """,
                tuple(scope_params),
            ).fetchall()

            terminal_counts = connection.execute(
                f"""
                SELECT
                    SUM(CASE WHEN status IN ('COMPLETED', 'FAILED') THEN 1 ELSE 0 END) AS terminal_jobs,
                    SUM(CASE WHEN last_result = 'SUCCESS' THEN 1 ELSE 0 END) AS success_jobs,
                    SUM(CASE WHEN last_result = 'SKIP' THEN 1 ELSE 0 END) AS skip_jobs,
                    SUM(CASE WHEN status = 'FAILED' OR last_result = 'FAILED' THEN 1 ELSE 0 END) AS failed_jobs,
                    SUM(CASE WHEN status = 'PENDING' AND last_result = 'RETRY' THEN 1 ELSE 0 END) AS retry_pending_jobs
                FROM job_queue
                {scope_where}
                """,
                tuple(scope_params),
            ).fetchone()

            terminal_jobs = int(terminal_counts["terminal_jobs"] or 0)
            success_jobs = int(terminal_counts["success_jobs"] or 0)
            skip_jobs = int(terminal_counts["skip_jobs"] or 0)
            failed_jobs = int(terminal_counts["failed_jobs"] or 0)
            retry_pending_jobs = int(terminal_counts["retry_pending_jobs"] or 0)

            success_rate = None
            if terminal_jobs > 0:
                success_rate = round(((success_jobs + skip_jobs) / terminal_jobs) * 100.0, 2)

            hard_failure_rate = None
            if terminal_jobs > 0:
                hard_failure_rate = round((failed_jobs / terminal_jobs) * 100.0, 2)

            report_payload = {
                "window_total_jobs": int(total_jobs),
                "status_breakdown": {row["status"]: int(row["count"]) for row in summary_rows},
                "terminal_jobs": terminal_jobs,
                "success_jobs": success_jobs,
                "skip_jobs": skip_jobs,
                "failed_jobs": failed_jobs,
                "retry_pending_jobs": retry_pending_jobs,
                "success_rate_percent": success_rate,
                "hard_failure_rate_percent": hard_failure_rate,
                "top_failed_repos": [
                    {"repo": row["repo"], "failed_count": int(row["failed_count"])}
                    for row in failed_repo_rows
                ],
                "top_successful_repos": [
                    {
                        "repo": row["repo"],
                        "success_count": int(row["success_count"] or 0),
                        "skip_count": int(row["skip_count"] or 0),
                        "failed_count": int(row["failed_count"] or 0),
                        "terminal_count": int(row["terminal_count"] or 0),
                        "terminal_success_rate_percent": (
                            round(
                                (
                                    (
                                        int(row["success_count"] or 0)
                                        + int(row["skip_count"] or 0)
                                    )
                                    / int(row["terminal_count"])
                                )
                                * 100.0,
                                2,
                            )
                            if int(row["terminal_count"] or 0) > 0
                            else None
                        ),
                    }
                    for row in successful_repo_rows
                ],
            }

    status_counts = {row["status"]: int(row["count"]) for row in summary_rows}
    due_count = int(pending_due["count"]) if pending_due else 0

    next_pending_payload = None
    if next_pending is not None:
        next_pending_payload = {
            "id": int(next_pending["id"]),
            "repo": next_pending["repo"],
            "tag": next_pending["tag"],
            "release_type": next_pending["release_type"] or "Release",
            "attempt_count": int(next_pending["attempt_count"]),
            "next_check_time": float(next_pending["next_check_time"]),
            "next_check_time_readable": _format_timestamp(next_pending["next_check_time"]),
            "expected_commit": next_pending["expected_commit"] or "unknown",
            "downloaded_count": int(next_pending["downloaded_count"]),
            "skipped_count": int(next_pending["skipped_count"]),
            "total_items": int(next_pending["total_items"]),
            "last_result": next_pending["last_result"],
            "created_at": float(next_pending["created_at"]),
            "created_at_readable": _format_timestamp(next_pending["created_at"]),
            "updated_at": float(next_pending["updated_at"]),
            "updated_at_readable": _format_timestamp(next_pending["updated_at"]),
            "completed_at": float(next_pending["completed_at"]) if next_pending["completed_at"] is not None else None,
            "completed_at_readable": _format_timestamp(next_pending["completed_at"]),
        }

    recent_jobs_payload = []
    for job in recent_jobs:
        recent_jobs_payload.append(
            {
                "id": int(job["id"]),
                "status": job["status"],
                "repo": job["repo"],
                "tag": job["tag"],
                "release_type": job["release_type"] or "Release",
                "attempt_count": int(job["attempt_count"]),
                "next_check_time": float(job["next_check_time"]),
                "next_check_time_readable": _format_timestamp(job["next_check_time"]),
                "expected_commit": job["expected_commit"] or "unknown",
                "downloaded_count": int(job["downloaded_count"]),
                "skipped_count": int(job["skipped_count"]),
                "total_items": int(job["total_items"]),
                "last_result": job["last_result"],
                "created_at": float(job["created_at"]),
                "created_at_readable": _format_timestamp(job["created_at"]),
                "updated_at": float(job["updated_at"]),
                "updated_at_readable": _format_timestamp(job["updated_at"]),
                "completed_at": float(job["completed_at"]) if job["completed_at"] is not None else None,
                "completed_at_readable": _format_timestamp(job["completed_at"]),
            }
        )

    return {
        "captured_at": _format_timestamp(now_timestamp),
        "captured_at_unix": float(now_timestamp),
        "filters": {
            "hours": hours,
            "date": date_value,
            "status": status_filter,
            "limit": "all" if limit is None else int(limit),
        },
        "total_jobs": int(total_jobs),
        "status_counts": status_counts,
        "pending_due_now": due_count,
        "next_pending_job": next_pending_payload,
        "recent_jobs": recent_jobs_payload,
        "report": report_payload,
    }


def print_queue_status(
    as_json=False,
    limit=10,
    hours=None,
    date_value=None,
    status_filter=None,
    report=False,
    report_only=False,
    report_csv_path=None,
):
    """Print queue counts and scheduling details without processing jobs."""
    data = _collect_queue_status_data(
        limit=limit,
        hours=hours,
        date_value=date_value,
        status_filter=status_filter,
        include_report=report,
    )

    if as_json:
        print(json.dumps(data, indent=2, ensure_ascii=True))
        return

    print("Queue status")
    print("------------")

    active_filters = []
    if data["filters"]["hours"] is not None:
        active_filters.append(f"last {data['filters']['hours']} hour(s)")
    if data["filters"]["date"] is not None:
        active_filters.append(f"date={data['filters']['date']}")
    if data["filters"]["status"] is not None:
        active_filters.append(f"status={data['filters']['status']}")

    if active_filters:
        print("Filters: " + ", ".join(active_filters))

    print(f"Total jobs: {data['total_jobs']}")

    if data["total_jobs"] == 0:
        print("No queue rows found.")
        return

    for status in sorted(data["status_counts"].keys()):
        print(f"- {status}: {data['status_counts'][status]}")

    print(f"- PENDING due now: {data['pending_due_now']}")

    next_pending = data["next_pending_job"]
    if next_pending is None:
        print("Next pending job: -")
    else:
        print(
            "Next pending job: "
            f"#{next_pending['id']} {next_pending['repo']} {next_pending['tag']} "
            f"({next_pending['release_type']}, attempt={next_pending['attempt_count']}, "
            f"next_check={next_pending['next_check_time_readable']}, "
            f"expected_commit={next_pending['expected_commit']}, "
            f"downloaded={next_pending['downloaded_count']}, "
            f"skipped={next_pending['skipped_count']}, "
            f"total={next_pending['total_items']}, "
            f"last_result={next_pending['last_result'] or '-'}, "
            f"created={next_pending['created_at_readable']}, "
            f"updated={next_pending['updated_at_readable']}, "
            f"completed={next_pending['completed_at_readable']})"
        )

    report_data = data.get("report")
    if report_data is not None:
        print("\nQueue report")
        print("------------")
        print(f"Window total jobs: {report_data['window_total_jobs']}")
        print(f"Terminal jobs (COMPLETED/FAILED): {report_data['terminal_jobs']}")
        print(
            "Terminal outcomes: "
            f"success={report_data['success_jobs']}, "
            f"skip={report_data['skip_jobs']}, "
            f"failed={report_data['failed_jobs']}"
        )
        print(f"Pending retries: {report_data['retry_pending_jobs']}")

        if report_data["success_rate_percent"] is None:
            print("Success rate: -")
            print("Hard failure rate: -")
        else:
            print(f"Success rate: {report_data['success_rate_percent']}%")
            print(f"Hard failure rate: {report_data['hard_failure_rate_percent']}%")

        if report_data["top_failed_repos"]:
            print("Top failed repos:")
            for item in report_data["top_failed_repos"]:
                print(f"- {item['repo']}: {item['failed_count']}")
        else:
            print("Top failed repos: -")

        if report_data["top_successful_repos"]:
            print("Top successful repos:")
            for item in report_data["top_successful_repos"]:
                print(
                    f"- {item['repo']}: success={item['success_count']}, "
                    f"skip={item['skip_count']}, failed={item['failed_count']}, "
                    f"terminal={item['terminal_count']}, "
                    f"success_rate={item['terminal_success_rate_percent']}%"
                )
        else:
            print("Top successful repos: -")

        if report_csv_path:
            try:
                _write_queue_report_csv(report_data, report_csv_path)
                print(f"Report CSV written: {report_csv_path}")
            except OSError as exc:
                print(f"Failed to write report CSV '{report_csv_path}': {exc}", file=sys.stderr)

    if not report_only and data["recent_jobs"]:
        if data["filters"]["limit"] == "all":
            print("\nJobs (newest first):")
        else:
            print(f"\nJobs shown (newest first, limit={data['filters']['limit']}):")
        for job in data["recent_jobs"]:
            print(
                f"- #{job['id']} {job['status']} {job['repo']} {job['tag']} "
                f"({job['release_type']}, attempt={job['attempt_count']}, "
                f"next_check={job['next_check_time_readable']}, "
                f"expected_commit={job['expected_commit']}, "
                f"downloaded={job['downloaded_count']}, "
                f"skipped={job['skipped_count']}, "
                f"total={job['total_items']}, "
                f"last_result={job['last_result'] or '-'}, "
                f"created={job['created_at_readable']}, "
                f"updated={job['updated_at_readable']}, "
                f"completed={job['completed_at_readable']})"
            )


def handle_cli_args(args):
    """Handle one-shot command-line operations."""
    if "--help" in args or "-h" in args:
        print(
            f"GHAADD v{__version__}\n"
            "\nUsage: python main.py [OPTIONS]\n"
            "\nOptions:\n"
            "  --help, -h        Show this help message and exit.\n"
            "  --once            Run a single ingest-and-process cycle, then exit.\n"
            "  --poll            Force polling mode even if disabled in config.\n"
            "  --queue-status    Print current queue counts and scheduling details.\n"
            "    --json          Output --queue-status as JSON.\n"
            "    --queue-all     Show all matching jobs instead of a limited list.\n"
            "    --queue-limit N Show up to N jobs in history (0 means all).\n"
            "    --queue-hours H Filter jobs created in the last H hours.\n"
            "    --queue-date D  Filter jobs created on YYYY-MM-DD.\n"
            "    --queue-status-filter S  Filter by status: PENDING, COMPLETED, FAILED.\n"
            "    --queue-report  Print a compact report (rates and top failed repos).\n"
            "    --queue-report-only  Print only the report section (no job list).\n"
            "    --queue-report-csv [PATH]  Export the report section to CSV.\n"
            "  --purge-state     Delete the local state database (state.db).\n"
            "  --smoke-test      Run internal smoke tests for download behavior.\n"
            "\nWith no options, behaviour is determined by config.json (poll or single run)."
        )
        return True

    if "--queue-status" in args:
        try:
            queue_options = _parse_queue_status_options(args)
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
        run_internal_smoke_tests()
        return True

    return False


@overload
def get_current_commit_hash(repo: str, tag: str, include_reason: Literal[True]) -> Tuple[Optional[str], Optional[str]]:
    ...


@overload
def get_current_commit_hash(repo: str, tag: str, include_reason: Literal[False] = False) -> Optional[str]:
    ...


def get_current_commit_hash(
    repo: str,
    tag: str,
    include_reason: bool = False,
) -> Union[Optional[str], Tuple[Optional[str], Optional[str]]]:
    """Return current 7-char commit hash, optionally including failure reason."""
    url = f"https://api.github.com/repos/{repo}/commits/{tag}"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    try:
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code != 200:
            reason = f"http_{response.status_code}"
            return (None, reason) if include_reason else None

        sha = response.json().get("sha")
        if not sha:
            reason = "missing_sha"
            return (None, reason) if include_reason else None

        commit_hash = str(sha)[:7]
        return (commit_hash, None) if include_reason else commit_hash
    except requests.RequestException as exc:
        reason = f"request_exception:{exc.__class__.__name__}"
        return (None, reason) if include_reason else None


def ingest_notifications_once(connection):
    """Ingest unseen notifications into job_queue and delete emails immediately."""
    max_emails_to_process = get_max_emails_to_process()

    try:
        print(
            f"Fetching pending GitHub notifications (limit: "
            f"{'all' if max_emails_to_process == 0 else max_emails_to_process})..."
        )
        notifications = get_pending_notifications(
            limit=max_emails_to_process if max_emails_to_process > 0 else None
        )

        if not notifications:
            print("No pending notifications found.")
            return 0

        unique_notifications = []
        notifications_by_release = {}
        collapsed_notifications = []

        for notification in notifications:
            repo = notification.get("repo")
            tag = notification.get("tag")
            release_type = notification.get("release_type")
            email_id = notification.get("email_id")
            release_key = (repo, tag, release_type)

            if release_key in notifications_by_release:
                # Keep duplicate details for summary output.
                collapsed_notifications.append(notification)
                if email_id is not None:
                    notifications_by_release[release_key]["email_ids"].append(email_id)
                continue

            deduped_notification = dict(notification)
            deduped_notification["email_ids"] = [email_id] if email_id is not None else []
            notifications_by_release[release_key] = deduped_notification
            unique_notifications.append(deduped_notification)

        print(f"Found {len(unique_notifications)} unique notification(s) to ingest.")

        if collapsed_notifications:
            print(f"⏭️ Collapsed {len(collapsed_notifications)} duplicate notification(s) for already-seen repo/tag pairs:")
            for dup in collapsed_notifications:
                print(f"   - Repo: {dup.get('repo')} | Tag: {dup.get('tag')} | Type: {dup.get('release_type')}")
        print()

        emails_to_delete = []
        queued_count = 0
        now_timestamp = time.time()

        for idx, notification in enumerate(unique_notifications, 1):
            repo = notification.get("repo")
            tag = notification.get("tag")
            release_type = notification.get("release_type")
            email_ids = notification.get("email_ids", [])

            print(f"[{idx}/{len(unique_notifications)}] Queueing: {repo} ({tag})")

            try:
                expected_commit, commit_reason = get_current_commit_hash(repo, tag, include_reason=True)
                enqueue_job(
                    connection,
                    repo,
                    tag,
                    release_type=release_type,
                    next_check_time=now_timestamp,
                    expected_commit=expected_commit,
                )
                emails_to_delete.extend(email_ids)
                queued_count += 1
                if expected_commit:
                    print(f"✓ Queued as PENDING for {repo} {tag} (expected_commit={expected_commit})")
                else:
                    print(
                        f"✓ Queued as PENDING for {repo} {tag} "
                        f"(expected_commit=unknown, reason={commit_reason or 'unavailable'})"
                    )
            except Exception as e:
                print(f"✗ Error queueing {repo} {tag}: {str(e)}\n")
                continue

        if emails_to_delete:
            print(f"🧹 Cleaning up {len(emails_to_delete)} queued email(s)...")
            mark_as_read_and_delete(emails_to_delete)
            print("✓ Emails marked as read and moved to Trash.")

        print(f"Ingest complete. {queued_count} job(s) queued as PENDING.")
        return queued_count

    except Exception as e:
        print(f"Fatal error: {str(e)}", file=sys.stderr)
        return 0


def process_queue_once(connection):
    """Process due queue rows, compare commit hashes, and apply retry intervals."""
    retry_intervals_minutes = get_recheck_intervals_minutes()
    now_timestamp = time.time()
    due_jobs = get_due_jobs(connection, now_timestamp)

    if not due_jobs:
        print("No due queue jobs found.")
        return

    print(f"Processing {len(due_jobs)} due queue job(s)...")

    for index, job in enumerate(due_jobs, 1):
        job_id = job["id"]
        repo = job["repo"]
        tag = job["tag"]
        release_type = job["release_type"]
        attempt_count = int(job["attempt_count"])
        expected_commit = job["expected_commit"]

        print(
            f"[{index}/{len(due_jobs)}] Job #{job_id}: {repo} {tag} "
            f"(attempt={attempt_count}, expected_commit={expected_commit or 'unknown'})"
        )

        current_commit, current_commit_reason = get_current_commit_hash(repo, tag, include_reason=True)
        if expected_commit and current_commit and expected_commit != current_commit:
            print(f"   🔁 Commit changed: {expected_commit} -> {current_commit}")
        elif expected_commit and current_commit and expected_commit == current_commit:
            print(f"   ✅ Commit unchanged: {current_commit}")
        elif current_commit:
            print(f"   ℹ️ Commit baseline discovered: {current_commit}")
        else:
            print(
                "   ⚠️ Could not resolve current commit hash from GitHub API "
                f"(reason={current_commit_reason or 'unavailable'})."
            )

        try:
            result = download_release(repo, tag, release_type, include_stats=True)
        except Exception as e:
            print(f"   ❌ Processor error while downloading: {e}")
            result = {
                "status": "FAILED",
                "downloaded_count": 0,
                "skipped_count": 0,
                "total_items": 0,
            }

        if isinstance(result, dict):
            result_status = result.get("status", "FAILED")
            downloaded_count = int(result.get("downloaded_count", 0) or 0)
            skipped_count = int(result.get("skipped_count", 0) or 0)
            total_items = int(result.get("total_items", 0) or 0)
        else:
            result_status = "SUCCESS" if result is True else ("SKIP" if result == "SKIP" else "FAILED")
            downloaded_count = 0
            skipped_count = 0
            total_items = 0

        if result_status in ("SUCCESS", "SKIP"):
            mark_job_completed(
                connection,
                job_id,
                downloaded_count=downloaded_count,
                skipped_count=skipped_count,
                total_items=total_items,
                last_result=result_status,
            )
            if result_status == "SUCCESS":
                print("   ✅ Job completed successfully.")
            else:
                print("   ⏭️ Job completed with SKIP (release not found).")
            print(f"   📊 Files: downloaded={downloaded_count}, skipped={skipped_count}, total={total_items}")
            continue

        next_attempt_count = attempt_count + 1
        latest_commit = current_commit or expected_commit

        if next_attempt_count <= len(retry_intervals_minutes):
            delay_minutes = retry_intervals_minutes[next_attempt_count - 1]
            next_check_time = time.time() + (delay_minutes * 60)
            reschedule_job(
                connection,
                job_id,
                next_check_time=next_check_time,
                attempt_count=next_attempt_count,
                expected_commit=latest_commit,
                downloaded_count=downloaded_count,
                skipped_count=skipped_count,
                total_items=total_items,
                last_result="RETRY",
            )
            print(
                f"   🔄 Download failed; rescheduled in {delay_minutes} minute(s) "
                f"(attempt={next_attempt_count})."
            )
            print(f"   📊 Files: downloaded={downloaded_count}, skipped={skipped_count}, total={total_items}")
        else:
            mark_job_failed(
                connection,
                job_id,
                attempt_count=next_attempt_count,
                expected_commit=latest_commit,
                downloaded_count=downloaded_count,
                skipped_count=skipped_count,
                total_items=total_items,
                last_result="FAILED",
            )
            print(
                "   ❌ Download failed; no retry intervals remaining. "
                f"Marked FAILED at attempt={next_attempt_count}."
            )
            print(f"   📊 Files: downloaded={downloaded_count}, skipped={skipped_count}, total={total_items}")


def run_ingest_and_queue_cycle(connection):
    """Run one full cycle: ingest new emails, then process due queue jobs."""
    ingest_notifications_once(connection)
    process_queue_once(connection)


def run_polling_loop(interval_seconds, jitter_min_seconds, jitter_max_seconds):
    """Run processing continuously with randomized jitter between cycles."""
    if jitter_min_seconds > jitter_max_seconds:
        jitter_min_seconds, jitter_max_seconds = jitter_max_seconds, jitter_min_seconds

    print(
        f"Polling enabled. Base interval: {interval_seconds}s, jitter: {jitter_min_seconds}-{jitter_max_seconds}s."
    )
    print("Press Ctrl+C to stop.\n")

    cycle = 1
    with open_database() as connection:
        while True:
            started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"=== Poll cycle {cycle} @ {started} ===")
            run_ingest_and_queue_cycle(connection)

            jitter = random.randint(jitter_min_seconds, jitter_max_seconds)
            sleep_seconds = interval_seconds + jitter
            print(f"Next poll in {sleep_seconds}s ({interval_seconds}s + {jitter}s jitter).\n")
            time.sleep(sleep_seconds)
            cycle += 1


def main():
    """Run the main orchestration flow for ingest and queue processing."""
    args = sys.argv[1:]
    if handle_cli_args(args):
        return

    polling_settings = get_polling_settings()

    # Force single-run mode with --once, even when polling is enabled in config.
    once_mode = "--once" in args
    poll_enabled = not once_mode and (
        "--poll" in args
        or polling_settings["enabled"]
    )
    if once_mode or not poll_enabled:
        with open_database() as connection:
            run_ingest_and_queue_cycle(connection)
        return

    interval_seconds = polling_settings["interval_seconds"]
    jitter_min_seconds = polling_settings["jitter_min_seconds"]
    jitter_max_seconds = polling_settings["jitter_max_seconds"]

    try:
        run_polling_loop(interval_seconds, jitter_min_seconds, jitter_max_seconds)
    except KeyboardInterrupt:
        print("\nPolling stopped by user.")

if __name__ == "__main__":
    main()
