"""Shared TypedDict shapes passed between the queue worker, the reports and the CLI (no logic lives here)."""

from typing import Literal, Optional, TypedDict, Union


class QueueStatusOptions(TypedDict):
    """The parsed options of --queue-status / --queue-report."""
    as_json: bool
    limit: Optional[int]
    hours: Optional[float]
    date: Optional[str]
    repo_filter: Optional[str]
    status: Optional[str]
    report: bool
    report_only: bool
    report_csv_path: Optional[str]


class QueueStatusFilters(TypedDict):
    """The filters (hours, date, repo, status, limit) echoed back in the status payload."""
    hours: Optional[float]
    date: Optional[str]
    repo_filter: Optional[str]
    status: Optional[str]
    limit: str | int


class SkippedItemPreview(TypedDict):
    """A skipped item as shown in the reports: how often, which file and why."""
    attempt_count: int
    item_key: Optional[str]
    file_name: Optional[str]
    reason: str
    recorded_at: float
    recorded_at_readable: str


class QueueJobPayload(TypedDict):
    """One job_queue row as reported by --queue-status."""
    id: int
    status: str
    repo: str
    tag: str
    release_type: str
    attempt_count: int
    next_check_time: float
    next_check_time_readable: str
    expected_commit: str
    downloaded_count: int
    skipped_count: int
    total_items: int
    last_result: Optional[str]
    created_at: float
    created_at_readable: str
    updated_at: float
    updated_at_readable: str
    completed_at: Optional[float]
    completed_at_readable: str
    folder_bytes: Optional[int]
    folder_files: Optional[int]
    previous_success_tag: Optional[str]
    previous_success_total_items: Optional[int]
    file_count_delta_vs_previous_success: Optional[int]
    skip_detail_count: int
    skipped_items_preview: list[SkippedItemPreview]


class NextPendingJobPayload(TypedDict):
    """The next pending job (the one due soonest)."""
    id: int
    repo: str
    tag: str
    release_type: str
    attempt_count: int
    next_check_time: float
    next_check_time_readable: str
    expected_commit: str
    downloaded_count: int
    skipped_count: int
    total_items: int
    last_result: Optional[str]
    created_at: float
    created_at_readable: str
    updated_at: float
    updated_at_readable: str
    completed_at: Optional[float]
    completed_at_readable: str
    skip_detail_count: int
    skipped_items_preview: list[SkippedItemPreview]


class TopFailedRepoPayload(TypedDict):
    """A repository with its number of failed jobs."""
    repo: str
    failed_count: int


class TopSuccessfulRepoPayload(TypedDict):
    """A repository with its success, skip, failed and terminal job counts."""
    repo: str
    success_count: int
    skip_count: int
    failed_count: int
    terminal_count: int
    terminal_success_rate_percent: Optional[float]


class TopSkippedItemPayload(TypedDict):
    """A skipped item with how many times it was skipped."""
    item_label: str
    skip_count: int


class SkipReasonPayload(TypedDict):
    """A skip reason with its count."""
    reason: str
    count: int


class PurgeableJobAgeSummary(TypedDict):
    """A terminal job with its age, as a candidate for --purge."""
    id: int
    repo: str
    tag: str
    release_type: str
    status: str
    age_days: float
    effective_timestamp: float
    effective_timestamp_readable: str


class PurgeAgePreviewEntry(TypedDict):
    """How many jobs a purge of at least `age_days` days would remove."""
    age_days: int
    would_purge_count: int


class QueueReportPayload(TypedDict):
    """The figures of --queue-report: status breakdown, success/skip/failed counts and the top lists."""
    window_total_jobs: int
    status_breakdown: dict[str, int]
    terminal_jobs: int
    success_jobs: int
    skip_jobs: int
    failed_jobs: int
    retry_pending_jobs: int
    supersede_finalized_jobs: int
    supersede_incomplete_moved_jobs: int
    success_rate_percent: Optional[float]
    hard_failure_rate_percent: Optional[float]
    top_failed_repos: list[TopFailedRepoPayload]
    top_successful_repos: list[TopSuccessfulRepoPayload]
    top_skipped_items: list[TopSkippedItemPayload]
    skip_reasons: list[SkipReasonPayload]
    oldest_purgeable_job: Optional[PurgeableJobAgeSummary]
    newest_purgeable_job: Optional[PurgeableJobAgeSummary]
    purge_age_preview: list[PurgeAgePreviewEntry]


class QueueStatusPayload(TypedDict):
    """The full result of --queue-status (also its --json output)."""
    captured_at: str
    captured_at_unix: float
    filters: QueueStatusFilters
    total_jobs: int
    status_counts: dict[str, int]
    pending_due_now: int
    next_pending_job: Optional[NextPendingJobPayload]
    recent_jobs: list[QueueJobPayload]
    report: Optional[QueueReportPayload]


class ReleaseAssetQueueItem(TypedDict):
    """One release asset to download: key, name, url and the size/time expected from GitHub."""
    key: str
    name: Optional[str]
    url: str
    expected_size: Optional[int]
    expected_updated_at: Optional[str]


class SkippedItemPayload(TypedDict):
    """One skipped asset: its key, file name and the reason."""
    item_key: Optional[str]
    file_name: Optional[str]
    reason: str


class DownloadResultPayload(TypedDict):
    """Result of download_release(): the status, the counts and the skipped items."""
    status: Literal["SUCCESS", "SKIP", "FAILED"]
    downloaded_count: int
    skipped_count: int
    total_items: int
    skipped_items: list[SkippedItemPayload]
    working_dir: Optional[str]
    skip_reason: Optional[str]


DownloadReleaseResult = Union[bool, str, DownloadResultPayload]


class NotificationPayload(TypedDict):
    """One parsed notification e-mail: repository, tag, release type and the e-mail id."""
    repo: Optional[str]
    tag: Optional[str]
    release_type: Optional[str]
    email_id: Optional[str]


class QueuedNotificationPayload(NotificationPayload):
    """A notification after duplicates were collapsed: all e-mail ids that belong to it."""
    email_ids: list[str]


class IngestCycleStats(TypedDict):
    """Counters of one ingest cycle (found, collapsed, queued, skipped...)."""
    notifications_found: int
    notifications_collapsed_duplicates: int
    notifications_queued: int
    notifications_skipped_malformed: int
    notifications_skipped_inactive: int
    notifications_skipped_skiplist: int
    notifications_errors: int


class QueueCycleStats(TypedDict):
    """Counters of one queue cycle (due, completed, failed, retried, superseded...)."""
    due_jobs: int
    completed: int
    failed: int
    retried: int
    superseded: int
    downloaded_files: int
    skipped_files: int


class QueuedItemInfo(TypedDict):
    """Identity of the one item just queued (--single processes exactly this job)."""
    repo: str
    tag: str
    release_type: Optional[str]
    job_id: Optional[int]
