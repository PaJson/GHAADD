import csv
import json
import sys
import time
from datetime import datetime, timedelta
from typing import Optional

from modules.db_manager import open_database
from modules.payload_types import (
    NextPendingJobPayload,
    QueueJobPayload,
    QueueReportPayload,
    QueueStatusOptions,
    QueueStatusPayload,
    SkippedItemPreview,
    SkipReasonPayload,
    TopFailedRepoPayload,
    TopSkippedItemPayload,
    TopSuccessfulRepoPayload,
)


def _format_timestamp(unix_timestamp):
    """Format a Unix timestamp for console output."""
    if unix_timestamp is None:
        return "-"
    try:
        return datetime.fromtimestamp(float(unix_timestamp)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return "-"


def _build_skipped_item_preview(row) -> SkippedItemPreview:
    """Build a typed skipped-item preview payload from a database row."""
    return {
        "attempt_count": int(row["attempt_count"]),
        "item_key": row["item_key"],
        "file_name": row["file_name"],
        "reason": row["reason"],
        "recorded_at": float(row["recorded_at"]),
        "recorded_at_readable": _format_timestamp(row["recorded_at"]),
    }


def _build_next_pending_job_payload(
    row,
    skip_detail_count: int,
    skipped_items_preview: list[SkippedItemPreview],
) -> NextPendingJobPayload:
    """Build a typed next-pending-job payload from a database row."""
    return {
        "id": int(row["id"]),
        "repo": row["repo"],
        "tag": row["tag"],
        "release_type": row["release_type"] or "Release",
        "attempt_count": int(row["attempt_count"]),
        "next_check_time": float(row["next_check_time"]),
        "next_check_time_readable": _format_timestamp(row["next_check_time"]),
        "expected_commit": row["expected_commit"] or "unknown",
        "downloaded_count": int(row["downloaded_count"]),
        "skipped_count": int(row["skipped_count"]),
        "total_items": int(row["total_items"]),
        "last_result": row["last_result"],
        "created_at": float(row["created_at"]),
        "created_at_readable": _format_timestamp(row["created_at"]),
        "updated_at": float(row["updated_at"]),
        "updated_at_readable": _format_timestamp(row["updated_at"]),
        "completed_at": float(row["completed_at"]) if row["completed_at"] is not None else None,
        "completed_at_readable": _format_timestamp(row["completed_at"]),
        "skip_detail_count": skip_detail_count,
        "skipped_items_preview": skipped_items_preview,
    }


def _build_queue_job_payload(
    row,
    skip_detail_count: int,
    skipped_items_preview: list[SkippedItemPreview],
    previous_success_tag: Optional[str] = None,
    previous_success_total_items: Optional[int] = None,
    file_count_delta_vs_previous_success: Optional[int] = None,
) -> QueueJobPayload:
    """Build a typed queue-job payload from a database row."""
    return {
        "id": int(row["id"]),
        "status": row["status"],
        "repo": row["repo"],
        "tag": row["tag"],
        "release_type": row["release_type"] or "Release",
        "attempt_count": int(row["attempt_count"]),
        "next_check_time": float(row["next_check_time"]),
        "next_check_time_readable": _format_timestamp(row["next_check_time"]),
        "expected_commit": row["expected_commit"] or "unknown",
        "downloaded_count": int(row["downloaded_count"]),
        "skipped_count": int(row["skipped_count"]),
        "total_items": int(row["total_items"]),
        "last_result": row["last_result"],
        "created_at": float(row["created_at"]),
        "created_at_readable": _format_timestamp(row["created_at"]),
        "updated_at": float(row["updated_at"]),
        "updated_at_readable": _format_timestamp(row["updated_at"]),
        "completed_at": float(row["completed_at"]) if row["completed_at"] is not None else None,
        "completed_at_readable": _format_timestamp(row["completed_at"]),
        "previous_success_tag": previous_success_tag,
        "previous_success_total_items": previous_success_total_items,
        "file_count_delta_vs_previous_success": file_count_delta_vs_previous_success,
        "skip_detail_count": skip_detail_count,
        "skipped_items_preview": skipped_items_preview,
    }


def _build_top_failed_repo_payload(row) -> TopFailedRepoPayload:
    """Build a typed top-failed-repo payload."""
    return {"repo": row["repo"], "failed_count": int(row["failed_count"])}


def _build_top_successful_repo_payload(row) -> TopSuccessfulRepoPayload:
    """Build a typed top-successful-repo payload."""
    terminal_count = int(row["terminal_count"] or 0)
    success_count = int(row["success_count"] or 0)
    skip_count = int(row["skip_count"] or 0)
    failed_count = int(row["failed_count"] or 0)
    return {
        "repo": row["repo"],
        "success_count": success_count,
        "skip_count": skip_count,
        "failed_count": failed_count,
        "terminal_count": terminal_count,
        "terminal_success_rate_percent": (
            round(((success_count + skip_count) / terminal_count) * 100.0, 2)
            if terminal_count > 0
            else None
        ),
    }


def _build_top_skipped_item_payload(row) -> TopSkippedItemPayload:
    """Build a typed top-skipped-item payload."""
    return {"item_label": row["item_label"], "skip_count": int(row["skip_count"])}


def _build_skip_reason_payload(row) -> SkipReasonPayload:
    """Build a typed skip-reason payload."""
    return {"reason": row["reason"], "count": int(row["count"])}


def _build_queue_report_payload(
    total_jobs: int,
    summary_rows,
    terminal_jobs: int,
    success_jobs: int,
    skip_jobs: int,
    failed_jobs: int,
    retry_pending_jobs: int,
    supersede_finalized_jobs: int,
    supersede_incomplete_moved_jobs: int,
    failed_repo_rows,
    successful_repo_rows,
    top_skipped_items,
    skip_reason_rows,
) -> QueueReportPayload:
    """Build the typed queue-report payload from aggregated query rows."""
    success_rate = None
    if terminal_jobs > 0:
        success_rate = round(((success_jobs + skip_jobs) / terminal_jobs) * 100.0, 2)

    hard_failure_rate = None
    if terminal_jobs > 0:
        hard_failure_rate = round((failed_jobs / terminal_jobs) * 100.0, 2)

    return {
        "window_total_jobs": int(total_jobs),
        "status_breakdown": {row["status"]: int(row["count"]) for row in summary_rows},
        "terminal_jobs": terminal_jobs,
        "success_jobs": success_jobs,
        "skip_jobs": skip_jobs,
        "failed_jobs": failed_jobs,
        "retry_pending_jobs": retry_pending_jobs,
        "supersede_finalized_jobs": supersede_finalized_jobs,
        "supersede_incomplete_moved_jobs": supersede_incomplete_moved_jobs,
        "success_rate_percent": success_rate,
        "hard_failure_rate_percent": hard_failure_rate,
        "top_failed_repos": [_build_top_failed_repo_payload(row) for row in failed_repo_rows],
        "top_successful_repos": [_build_top_successful_repo_payload(row) for row in successful_repo_rows],
        "top_skipped_items": [_build_top_skipped_item_payload(row) for row in top_skipped_items],
        "skip_reasons": [_build_skip_reason_payload(row) for row in skip_reason_rows],
    }


def build_queue_status_options(
    as_json: bool = False,
    queue_all: bool = False,
    queue_limit: Optional[int] = None,
    queue_hours: Optional[float] = None,
    queue_date: Optional[str] = None,
    queue_repo_filter: Optional[str] = None,
    queue_status_filter: Optional[str] = None,
    queue_report: bool = False,
    queue_report_only: bool = False,
    queue_report_csv: Optional[str] = None,
) -> QueueStatusOptions:
    """Build and validate queue-status options from parsed CLI values."""
    options: QueueStatusOptions = {
        "as_json": as_json,
        "limit": 10,
        "hours": None,
        "date": None,
        "repo_filter": None,
        "status": None,
        "report": queue_report,
        "report_only": queue_report_only,
        "report_csv_path": None,
    }

    if options["report_only"]:
        options["report"] = True

    if queue_all:
        options["limit"] = None

    if queue_limit is not None:
        limit_value = int(queue_limit)
        if limit_value < 0:
            raise ValueError("Invalid --queue-limit value. Expected an integer >= 0.")
        options["limit"] = None if limit_value == 0 else limit_value

    if queue_hours is not None:
        hours_value = float(queue_hours)
        if hours_value <= 0:
            raise ValueError("Invalid --queue-hours value. Expected a number > 0.")
        options["hours"] = hours_value

    if queue_date is not None:
        date_value = queue_date
        try:
            datetime.strptime(date_value, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("Invalid --queue-date value. Expected YYYY-MM-DD.") from exc
        options["date"] = date_value

    if options["hours"] is not None and options["date"] is not None:
        raise ValueError("Use only one of --queue-hours or --queue-date.")

    if queue_repo_filter is not None:
        repo_filter_value = queue_repo_filter.strip()
        if not repo_filter_value:
            raise ValueError("Invalid --queue-repo-filter value. Expected a non-empty string.")
        options["repo_filter"] = repo_filter_value

    if queue_status_filter is not None:
        status_value = queue_status_filter.strip().upper()
        options["status"] = status_value

    if queue_report_csv is not None:
        explicit_path = queue_report_csv.strip() or None
        options["report_csv_path"] = explicit_path or _build_default_queue_report_csv_path(options)
        options["report"] = True

    return options


def _build_default_queue_report_csv_path(options: QueueStatusOptions) -> str:
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


def _write_queue_report_csv(report_data: QueueReportPayload, output_path: str) -> None:
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
                "metric": "supersede_finalized_jobs",
                "value": report_data["supersede_finalized_jobs"],
            }
        )
        writer.writerow(
            {
                "section": "summary",
                "metric": "supersede_incomplete_moved_jobs",
                "value": report_data["supersede_incomplete_moved_jobs"],
            }
        )
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

        for item in report_data.get("top_skipped_items", []):
            writer.writerow(
                {
                    "section": "top_skipped_items",
                    "metric": "skip_count",
                    "repo": item["item_label"],
                    "value": item["skip_count"],
                }
            )

        for item in report_data.get("skip_reasons", []):
            writer.writerow(
                {
                    "section": "skip_reasons",
                    "metric": item["reason"],
                    "value": item["count"],
                }
            )


def _build_queue_scope(
    hours: Optional[float] = None,
    date_value: Optional[str] = None,
    repo_filter: Optional[str] = None,
    status_filter: Optional[str] = None,
    now_timestamp: Optional[float] = None,
    table_alias: str = "",
) -> tuple[str, list[float | str]]:
    """Build WHERE clause and params for queue history filtering."""
    now_timestamp = now_timestamp if now_timestamp is not None else time.time()
    conditions = []
    params = []
    prefix = f"{table_alias}." if table_alias else ""

    if hours is not None:
        cutoff = now_timestamp - (float(hours) * 3600.0)
        conditions.append(f"{prefix}created_at >= ?")
        params.append(float(cutoff))

    if date_value is not None:
        start_dt = datetime.strptime(date_value, "%Y-%m-%d")
        end_dt = start_dt + timedelta(days=1)
        conditions.append(f"{prefix}created_at >= ?")
        conditions.append(f"{prefix}created_at < ?")
        params.extend([float(start_dt.timestamp()), float(end_dt.timestamp())])

    if repo_filter is not None:
        conditions.append(f"LOWER({prefix}repo) LIKE ?")
        params.append(f"%{repo_filter.lower()}%")

    if status_filter is not None:
        conditions.append(f"{prefix}status = ?")
        params.append(status_filter)

    if not conditions:
        return "", []

    return "WHERE " + " AND ".join(conditions), params


def _collect_queue_status_data(
    limit: Optional[int] = 10,
    hours: Optional[float] = None,
    date_value: Optional[str] = None,
    repo_filter: Optional[str] = None,
    status_filter: Optional[str] = None,
    include_report: bool = False,
) -> QueueStatusPayload:
    """Return queue status data for text or JSON rendering."""
    now_timestamp = time.time()
    scope_where, scope_params = _build_queue_scope(
        hours=hours,
        date_value=date_value,
        repo_filter=repo_filter,
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
            """,
            tuple(scope_params),
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
            """,
            tuple(next_pending_params),
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

        previous_success_comparison_by_job_id: dict[int, tuple[str, int, int]] = {}
        for job in recent_jobs:
            if job["status"] != "COMPLETED" or job["last_result"] != "SUCCESS":
                continue

            previous_success_row = connection.execute(
                """
                SELECT
                    tag,
                    total_items
                FROM job_queue
                WHERE id <> ?
                  AND status = 'COMPLETED'
                  AND last_result = 'SUCCESS'
                  AND repo = ?
                  AND (
                        tag = ?
                        OR (tag IS NULL AND ? IS NULL)
                      )
                  AND (
                        release_type = ?
                        OR (release_type IS NULL AND ? IS NULL)
                      )
                ORDER BY completed_at DESC, id DESC
                LIMIT 1
                """,
                (
                    int(job["id"]),
                    job["repo"],
                    job["tag"],
                    job["tag"],
                    job["release_type"],
                    job["release_type"],
                ),
            ).fetchone()
            if previous_success_row is None:
                continue

            previous_total_items = int(previous_success_row["total_items"] or 0)
            current_total_items = int(job["total_items"] or 0)
            previous_success_comparison_by_job_id[int(job["id"])] = (
                str(previous_success_row["tag"] or "unknown"),
                previous_total_items,
                current_total_items - previous_total_items,
            )

        skip_detail_rows = []
        skip_detail_count_rows = []
        details_job_ids = {int(job["id"]) for job in recent_jobs}
        if next_pending is not None:
            details_job_ids.add(int(next_pending["id"]))

        if details_job_ids:
            placeholders = ", ".join(["?"] * len(details_job_ids))
            details_params = tuple(sorted(details_job_ids))
            skip_detail_rows = connection.execute(
                f"""
                SELECT job_id, attempt_count, item_key, file_name, reason, recorded_at
                FROM job_skip_details
                WHERE job_id IN ({placeholders})
                ORDER BY id DESC
                """,
                details_params,
            ).fetchall()

            skip_detail_count_rows = connection.execute(
                f"""
                SELECT job_id, COUNT(*) AS detail_count
                FROM job_skip_details
                WHERE job_id IN ({placeholders})
                GROUP BY job_id
                """,
                details_params,
            ).fetchall()

        skip_detail_count_map = {
            int(row["job_id"]): int(row["detail_count"])
            for row in skip_detail_count_rows
        }
        skip_detail_preview_map: dict[int, list[SkippedItemPreview]] = {}
        for row in skip_detail_rows:
            job_id = int(row["job_id"])
            preview = skip_detail_preview_map.setdefault(job_id, [])
            if len(preview) >= 5:
                continue
            preview.append(_build_skipped_item_preview(row))

        report_payload: Optional[QueueReportPayload] = None
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
                    SUM(CASE WHEN status = 'PENDING' AND last_result = 'RETRY' THEN 1 ELSE 0 END) AS retry_pending_jobs,
                    SUM(
                        CASE
                            WHEN last_result IN (
                                'SUPERSEDED_REPLACED_BY_NEW_NOTIFICATION_FINALIZED',
                                'SUPERSEDED_COMMIT_CHANGED_FINALIZED'
                            )
                            THEN 1
                            ELSE 0
                        END
                    ) AS supersede_finalized_jobs,
                    SUM(
                        CASE
                            WHEN last_result IN (
                                'SUPERSEDED_REPLACED_BY_NEW_NOTIFICATION_INCOMPLETE_MOVED',
                                'SUPERSEDED_COMMIT_CHANGED_INCOMPLETE_MOVED'
                            )
                            THEN 1
                            ELSE 0
                        END
                    ) AS supersede_incomplete_moved_jobs
                FROM job_queue
                {scope_where}
                """,
                tuple(scope_params),
            ).fetchone()

            scope_where_q, scope_params_q = _build_queue_scope(
                hours=hours,
                date_value=date_value,
                repo_filter=repo_filter,
                status_filter=status_filter,
                now_timestamp=now_timestamp,
                table_alias="q",
            )
            top_skipped_items = connection.execute(
                f"""
                SELECT
                    COALESCE(d.file_name, d.item_key, 'unknown') AS item_label,
                    COUNT(*) AS skip_count
                FROM job_skip_details d
                INNER JOIN job_queue q ON q.id = d.job_id
                {scope_where_q}
                GROUP BY item_label
                ORDER BY skip_count DESC, item_label ASC
                LIMIT 5
                """,
                tuple(scope_params_q),
            ).fetchall()
            skip_reason_rows = connection.execute(
                f"""
                SELECT
                    COALESCE(d.reason, 'unknown') AS reason,
                    COUNT(*) AS count
                FROM job_skip_details d
                INNER JOIN job_queue q ON q.id = d.job_id
                {scope_where_q}
                GROUP BY COALESCE(d.reason, 'unknown')
                ORDER BY count DESC, reason ASC
                LIMIT 5
                """,
                tuple(scope_params_q),
            ).fetchall()

            terminal_jobs = int(terminal_counts["terminal_jobs"] or 0)
            success_jobs = int(terminal_counts["success_jobs"] or 0)
            skip_jobs = int(terminal_counts["skip_jobs"] or 0)
            failed_jobs = int(terminal_counts["failed_jobs"] or 0)
            retry_pending_jobs = int(terminal_counts["retry_pending_jobs"] or 0)
            supersede_finalized_jobs = int(terminal_counts["supersede_finalized_jobs"] or 0)
            supersede_incomplete_moved_jobs = int(terminal_counts["supersede_incomplete_moved_jobs"] or 0)

            report_payload = _build_queue_report_payload(
                total_jobs=total_jobs,
                summary_rows=summary_rows,
                terminal_jobs=terminal_jobs,
                success_jobs=success_jobs,
                skip_jobs=skip_jobs,
                failed_jobs=failed_jobs,
                retry_pending_jobs=retry_pending_jobs,
                supersede_finalized_jobs=supersede_finalized_jobs,
                supersede_incomplete_moved_jobs=supersede_incomplete_moved_jobs,
                failed_repo_rows=failed_repo_rows,
                successful_repo_rows=successful_repo_rows,
                top_skipped_items=top_skipped_items,
                skip_reason_rows=skip_reason_rows,
            )

    status_counts = {row["status"]: int(row["count"]) for row in summary_rows}
    due_count = int(pending_due["count"]) if pending_due else 0

    next_pending_payload: Optional[NextPendingJobPayload] = None
    if next_pending is not None:
        next_pending_payload = _build_next_pending_job_payload(
            next_pending,
            skip_detail_count_map.get(int(next_pending["id"]), 0),
            skip_detail_preview_map.get(int(next_pending["id"]), []),
        )

    recent_jobs_payload: list[QueueJobPayload] = []
    for job in recent_jobs:
        previous_comparison = previous_success_comparison_by_job_id.get(int(job["id"]))
        recent_jobs_payload.append(
            _build_queue_job_payload(
                job,
                skip_detail_count_map.get(int(job["id"]), 0),
                skip_detail_preview_map.get(int(job["id"]), []),
                previous_success_tag=previous_comparison[0] if previous_comparison else None,
                previous_success_total_items=previous_comparison[1] if previous_comparison else None,
                file_count_delta_vs_previous_success=previous_comparison[2] if previous_comparison else None,
            )
        )

    return {
        "captured_at": _format_timestamp(now_timestamp),
        "captured_at_unix": float(now_timestamp),
        "filters": {
            "hours": hours,
            "date": date_value,
            "repo_filter": repo_filter,
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
    as_json: bool = False,
    limit: Optional[int] = 10,
    hours: Optional[float] = None,
    date_value: Optional[str] = None,
    repo_filter: Optional[str] = None,
    status_filter: Optional[str] = None,
    report: bool = False,
    report_only: bool = False,
    report_csv_path: Optional[str] = None,
) -> None:
    """Print queue counts and scheduling details without processing jobs."""
    data = _collect_queue_status_data(
        limit=limit,
        hours=hours,
        date_value=date_value,
        repo_filter=repo_filter,
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
    if data["filters"]["repo_filter"] is not None:
        active_filters.append(f"repo~{data['filters']['repo_filter']}")
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
        if next_pending["skipped_items_preview"]:
            preview_text = "; ".join(
                f"{item['file_name'] or item['item_key'] or 'unknown'} [{item['reason']}]"
                for item in next_pending["skipped_items_preview"]
            )
            print(
                "  Skipped item details: "
                f"{next_pending['skip_detail_count']} total, preview: {preview_text}"
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
        print(f"Supersede-finalized jobs: {report_data['supersede_finalized_jobs']}")
        print(
            "Supersede-incomplete moved jobs: "
            f"{report_data['supersede_incomplete_moved_jobs']}"
        )

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

        if report_data["top_skipped_items"]:
            print("Top skipped items:")
            for item in report_data["top_skipped_items"]:
                print(f"- {item['item_label']}: {item['skip_count']}")
        else:
            print("Top skipped items: -")

        if report_data["skip_reasons"]:
            print("Skip reasons:")
            for item in report_data["skip_reasons"]:
                print(f"- {item['reason']}: {item['count']}")
        else:
            print("Skip reasons: -")

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
            if job["skipped_items_preview"]:
                preview_text = "; ".join(
                    f"{item['file_name'] or item['item_key'] or 'unknown'} [{item['reason']}]"
                    for item in job["skipped_items_preview"]
                )
                print(
                    "  Skipped item details: "
                    f"{job['skip_detail_count']} total, preview: {preview_text}"
                )
