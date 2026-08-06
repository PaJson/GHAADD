from typing import Literal, Optional, TypedDict, Union


class QueueStatusOptions(TypedDict):
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
    hours: Optional[float]
    date: Optional[str]
    repo_filter: Optional[str]
    status: Optional[str]
    limit: str | int


class SkippedItemPreview(TypedDict):
    attempt_count: int
    item_key: Optional[str]
    file_name: Optional[str]
    reason: str
    recorded_at: float
    recorded_at_readable: str


class QueueJobPayload(TypedDict):
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
    skip_detail_count: int
    skipped_items_preview: list[SkippedItemPreview]


class NextPendingJobPayload(TypedDict):
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
    repo: str
    failed_count: int


class TopSuccessfulRepoPayload(TypedDict):
    repo: str
    success_count: int
    skip_count: int
    failed_count: int
    terminal_count: int
    terminal_success_rate_percent: Optional[float]


class TopSkippedItemPayload(TypedDict):
    item_label: str
    skip_count: int


class SkipReasonPayload(TypedDict):
    reason: str
    count: int


class QueueReportPayload(TypedDict):
    window_total_jobs: int
    status_breakdown: dict[str, int]
    terminal_jobs: int
    success_jobs: int
    skip_jobs: int
    failed_jobs: int
    retry_pending_jobs: int
    success_rate_percent: Optional[float]
    hard_failure_rate_percent: Optional[float]
    top_failed_repos: list[TopFailedRepoPayload]
    top_successful_repos: list[TopSuccessfulRepoPayload]
    top_skipped_items: list[TopSkippedItemPayload]
    skip_reasons: list[SkipReasonPayload]


class QueueStatusPayload(TypedDict):
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
    key: str
    name: Optional[str]
    url: str
    expected_size: Optional[int]
    expected_updated_at: Optional[str]


class SkippedItemPayload(TypedDict):
    item_key: Optional[str]
    file_name: Optional[str]
    reason: str


class DownloadResultPayload(TypedDict):
    status: Literal["SUCCESS", "SKIP", "FAILED"]
    downloaded_count: int
    skipped_count: int
    total_items: int
    skipped_items: list[SkippedItemPayload]


DownloadReleaseResult = Union[bool, str, DownloadResultPayload]


class NotificationPayload(TypedDict):
    repo: Optional[str]
    tag: Optional[str]
    release_type: Optional[str]
    email_id: Optional[str]


class QueuedNotificationPayload(NotificationPayload):
    email_ids: list[str]
