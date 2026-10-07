"""The queue worker's decisions: what happens to a notification, a due job and a half-finished download.

Covers ingesting notifications (duplicates, inactive repositories, skiplist, live release type, superseding an
older job), processing due jobs (re-check schedule, completion, failure, changed commit, vanished release,
pausing) and the helpers that protect already-downloaded files from being forgotten.

GitHub, the mailbox and the real downloads are replaced by fakes; state.db, mapping.json and every folder live in
a temporary directory. Run from the project root: python -m unittest discover -s tests -t .
"""
import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import closing
from unittest import mock

# queue_worker -> mailbox_listener refuses to import without credentials; they are never used to connect.
os.environ.setdefault("GMAIL_USER", "test@example.invalid")
os.environ.setdefault("GMAIL_APP_PASSWORD", "unused")

from modules import db_manager, mapping_manager, queue_worker  # noqa: E402

REPO = "o/app"


def download_result(status="SUCCESS", downloaded=3, skipped=1, total=4, working_dir=None, skip_reason=None, skipped_items=None):
    return {
        "status": status,
        "downloaded_count": downloaded,
        "skipped_count": skipped,
        "total_items": total,
        "skipped_items": skipped_items or [],
        "working_dir": working_dir,
        "skip_reason": skip_reason,
    }


class WorkerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._temp_dir.cleanup)
        self.root = self._temp_dir.name
        self.mapping_path = os.path.join(self.root, "mapping.json")
        db_path = os.path.join(self.root, "state.db")
        self.dry_run = False
        self.commit = "aaa1111"  # what the fake GitHub currently reports for every tag (None = unavailable)
        self.release_data = None  # what get_release_data returns
        self.download_results: list = []
        self.notifications: list = []
        self.calls: dict[str, list] = {k: [] for k in (
            "download", "complete", "partial", "warnings", "completed_logs", "partial_logs", "deleted", "trashed", "fetch")}
        self.write_mapping()

        def commit_hash(repo, tag, token, include_reason=False):
            if self.commit is None:
                return (None, "http_404") if include_reason else None
            return (self.commit, None) if include_reason else self.commit

        def download(repo, tag, release_type, include_stats=False):
            self.calls["download"].append((repo, tag, release_type))
            result = self.download_results.pop(0) if self.download_results else download_result()
            if isinstance(result, Exception):
                raise result
            return result

        def move_complete(working_dir, repo=None):
            self.calls["complete"].append(working_dir)
            return os.path.join(self.root, "done", os.path.basename(working_dir))

        def move_partial(working_dir):
            self.calls["partial"].append(working_dir)
            return os.path.join(self.root, "partial", os.path.basename(working_dir))

        def fetch(limit=None):
            self.calls["fetch"].append(limit)
            return list(self.notifications)

        patches = (
            (db_manager, "get_state_db_path", lambda: db_path),
            (mapping_manager, "_mapping_file_path", lambda: self.mapping_path),
            (mapping_manager, "get_recheck_intervals_minutes", lambda: [5, 15]),
            (mapping_manager, "is_dry_run", lambda: self.dry_run),
            (queue_worker, "is_dry_run", lambda: self.dry_run),
            (queue_worker, "publishing_current_job", lambda repo, tag: contextlib.nullcontext()),
            (queue_worker, "get_current_commit_hash", commit_hash),
            (queue_worker, "download_release", download),
            (queue_worker, "get_release_data", lambda repo, tag: self.release_data),
            (queue_worker, "get_pending_notifications", fetch),
            (queue_worker, "mark_as_read_and_delete", lambda ids: self.calls["deleted"].append(list(ids)) or True),
            (queue_worker, "move_unread_to_trash", lambda ids: self.calls["trashed"].append(list(ids)) or True),
            (queue_worker, "move_processing_folder_to_complete", move_complete),
            (queue_worker, "move_processing_folder_to_partial", move_partial),
            (queue_worker, "log_warning", lambda kind, message: self.calls["warnings"].append((kind, message))),
            (queue_worker, "log_completed_move", lambda *args: self.calls["completed_logs"].append(args)),
            (queue_worker, "log_partial_move", lambda *args: self.calls["partial_logs"].append(args)),
        )
        for target, name, value in patches:
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.connection = db_manager.open_database()
        self.addCleanup(self.connection.close)

    # ----- helpers
    def write_mapping(self, *entries) -> None:
        with open(self.mapping_path, "w", encoding="utf-8") as handle:
            json.dump({"repositories": list(entries)}, handle)

    def quietly(self, function, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return function(*args, **kwargs)

    def job(self, tag="v1", release_type="Release", commit="aaa1111", due=0.0, repo=REPO) -> int:
        return db_manager.enqueue_job(self.connection, repo, tag, release_type, due, commit)

    def row(self, job_id: int):
        return db_manager.get_jobs_by_ids(self.connection, [job_id])[0]

    def at_attempt(self, job_id: int, attempt: int, downloaded=0, skipped=0, total=0, working_dir=None, due=0.0) -> None:
        db_manager.reschedule_job(self.connection, job_id, due, attempt, "aaa1111", downloaded, skipped, total, working_dir)

    def staged_dir(self, name="rel1") -> str:
        path = os.path.join(self.root, "Processing", "app (o)", "Release", name)
        os.makedirs(path, exist_ok=True)
        return path

    def warning_kinds(self) -> list[str]:
        return [kind for kind, _message in self.calls["warnings"]]

    def pending_ids(self) -> list[int]:
        return [r["id"] for r in db_manager.get_pending_jobs(self.connection)]

    def process(self, **kwargs):
        return self.quietly(queue_worker.process_queue_once, self.connection, "token", **kwargs)

    def ingest(self, **kwargs):
        return self.quietly(queue_worker.ingest_notifications_once, self.connection, "token", **kwargs)


def notification(repo=REPO, tag="v1", release_type="Release", email_id=1) -> dict:
    return {"repo": repo, "tag": tag, "release_type": release_type, "email_id": email_id}


# --------------------------------------------------------------------------------------- ingesting
class IngestTests(WorkerTestCase):
    def test_a_new_notification_becomes_a_pending_job_and_its_email_is_cleaned_up(self) -> None:
        self.notifications = [notification(email_id=11)]
        stats, queued = self.ingest()

        (row,) = db_manager.get_pending_jobs(self.connection)
        self.assertEqual((row["repo"], row["tag"], row["release_type"], row["expected_commit"]), (REPO, "v1", "Release", "aaa1111"))
        self.assertEqual(self.calls["deleted"], [[11]])
        self.assertEqual((stats["notifications_found"], stats["notifications_queued"]), (1, 1))
        self.assertEqual(queued["job_id"], row["id"])
        self.assertIsNotNone(mapping_manager.get_repository_mapping(REPO))  # the repository got its mapping entry

    def test_nothing_waiting_gives_empty_stats(self) -> None:
        stats, queued = self.ingest()
        self.assertEqual((stats["notifications_found"], stats["notifications_queued"]), (0, 0))
        self.assertIsNone(queued)

    def test_the_same_release_announced_twice_is_queued_once_and_both_emails_are_cleaned(self) -> None:
        self.notifications = [notification(email_id=1), notification(email_id=2)]
        stats, _ = self.ingest()
        self.assertEqual(len(db_manager.get_pending_jobs(self.connection)), 1)
        self.assertEqual(self.calls["deleted"], [[1, 2]])
        self.assertEqual((stats["notifications_found"], stats["notifications_collapsed_duplicates"]), (1, 1))

    def test_a_release_and_a_pre_release_of_one_tag_are_different_jobs(self) -> None:
        self.notifications = [notification(release_type="Release", email_id=1), notification(release_type="Pre-release", email_id=2)]
        self.ingest()
        self.assertEqual(sorted(r["release_type"] for r in db_manager.get_pending_jobs(self.connection)), ["Pre-release", "Release"])

    def test_a_malformed_notification_is_dropped_and_its_email_cleaned(self) -> None:
        self.notifications = [notification(repo=None, email_id=5), notification(tag=None, email_id=6)]
        stats, _ = self.ingest()
        self.assertEqual(db_manager.get_pending_jobs(self.connection), [])
        self.assertEqual(stats["notifications_skipped_malformed"], 2)
        self.assertEqual(self.calls["deleted"], [[5, 6]])

    def test_an_inactive_repository_is_not_queued_and_its_email_is_kept_unread_in_the_trash(self) -> None:
        self.write_mapping({"repository": REPO, "active": False})
        self.notifications = [notification(email_id=7)]
        stats, _ = self.ingest()
        self.assertEqual(db_manager.get_pending_jobs(self.connection), [])
        self.assertEqual(stats["notifications_skipped_inactive"], 1)
        self.assertEqual(self.calls["trashed"], [[7]])
        self.assertEqual(self.calls["deleted"], [])  # not marked read: it stays visible as unread in the Trash

    def test_the_skiplist_blocks_queueing_and_logs_a_skipped_warning(self) -> None:
        self.write_mapping({"repository": REPO, "skiplist": ["Pre-release"]})
        self.notifications = [notification(release_type="Pre-release", email_id=8), notification(tag="v2", email_id=9)]
        stats, _ = self.ingest()
        self.assertEqual([r["tag"] for r in db_manager.get_pending_jobs(self.connection)], ["v2"])
        self.assertEqual(stats["notifications_skipped_skiplist"], 1)
        self.assertIn("SKIPPED", self.warning_kinds())
        self.assertEqual(self.calls["deleted"], [[8, 9]])  # a skipped release's email is cleaned like a queued one

    def test_the_live_release_type_beats_the_one_in_the_email(self) -> None:
        self.release_data = {"prerelease": True}
        self.notifications = [notification(release_type="Release")]
        self.ingest()
        self.assertEqual(db_manager.get_pending_jobs(self.connection)[0]["release_type"], "Pre-release")

    def test_the_skiplist_uses_the_live_release_type(self) -> None:
        self.write_mapping({"repository": REPO, "skiplist": ["Pre-release"]})
        self.release_data = {"prerelease": True}  # the email said Release, GitHub says Pre-release
        self.notifications = [notification(release_type="Release")]
        stats, _ = self.ingest()
        self.assertEqual((db_manager.get_pending_jobs(self.connection), stats["notifications_skipped_skiplist"]), ([], 1))

    def test_a_new_notification_supersedes_the_older_pending_job_of_that_release(self) -> None:
        old = self.job(commit="old0000")
        self.notifications = [notification()]
        self.ingest()
        self.assertEqual(self.row(old)["status"], "SUPERSEDED")
        self.assertEqual(self.row(old)["last_result"], "SUPERSEDED_REPLACED_BY_NEW_NOTIFICATION")
        (new,) = db_manager.get_pending_jobs(self.connection)
        self.assertNotEqual(new["id"], old)
        self.assertEqual(new["expected_commit"], "aaa1111")

    def test_a_job_that_was_fully_staged_is_finalized_when_it_is_replaced(self) -> None:
        old = self.job()
        working_dir = self.staged_dir()
        self.at_attempt(old, 1, downloaded=3, skipped=1, total=4, working_dir=working_dir, due=time.time() + 9999)
        self.notifications = [notification()]
        self.ingest()
        self.assertEqual(self.calls["complete"], [working_dir])  # the finished files are not abandoned
        self.assertEqual(self.row(old)["last_result"], "SUPERSEDED_REPLACED_BY_NEW_NOTIFICATION_FINALIZED")
        self.assertIn("SUPERSEDE_FINALIZE", self.warning_kinds())

    def test_a_half_staged_job_is_quarantined_in_partial_when_it_is_replaced(self) -> None:
        old = self.job()
        working_dir = self.staged_dir()
        self.at_attempt(old, 1, downloaded=1, skipped=0, total=4, working_dir=working_dir, due=time.time() + 9999)
        self.notifications = [notification()]
        self.ingest()
        self.assertEqual(self.calls["partial"], [working_dir])
        self.assertEqual(self.calls["complete"], [])
        self.assertEqual(self.row(old)["last_result"], "SUPERSEDED_REPLACED_BY_NEW_NOTIFICATION_INCOMPLETE_MOVED")
        self.assertEqual(len(self.calls["partial_logs"]), 1)

    def test_an_unavailable_commit_still_queues_the_job(self) -> None:
        self.commit = None
        self.notifications = [notification()]
        self.ingest()
        self.assertIsNone(db_manager.get_pending_jobs(self.connection)[0]["expected_commit"])

    def test_one_failing_notification_does_not_stop_the_others(self) -> None:
        self.notifications = [notification(tag="bad", email_id=1), notification(tag="good", email_id=2)]
        real = queue_worker.enqueue_job

        def flaky(connection, repo, tag, **kwargs):
            if tag == "bad":
                raise RuntimeError("database is locked")
            return real(connection, repo, tag, **kwargs)

        with mock.patch.object(queue_worker, "enqueue_job", flaky):
            stats, _ = self.ingest()
        self.assertEqual([r["tag"] for r in db_manager.get_pending_jobs(self.connection)], ["good"])
        self.assertEqual((stats["notifications_errors"], stats["notifications_queued"]), (1, 1))
        self.assertEqual(self.calls["deleted"], [[2]])  # the failed one's email stays, so it is retried next poll

    def test_a_pause_leaves_the_remaining_notifications_in_the_mailbox(self) -> None:
        self.notifications = [notification(tag="a", email_id=1), notification(tag="b", email_id=2)]
        checks = iter([False, True])
        stats, _ = self.ingest(should_pause=lambda: next(checks))
        self.assertEqual([r["tag"] for r in db_manager.get_pending_jobs(self.connection)], ["a"])
        self.assertEqual(self.calls["deleted"], [[1]])
        self.assertEqual(stats["notifications_queued"], 1)

    def test_the_email_limit_is_passed_on(self) -> None:
        self.ingest(notification_limit=1)
        self.assertEqual(self.calls["fetch"][-1], 1)
        with mock.patch.object(queue_worker, "get_max_emails_to_process", lambda: 0):
            self.ingest()
        self.assertIsNone(self.calls["fetch"][-1])  # 0 means all

    def test_a_failure_to_read_the_mailbox_is_survived(self) -> None:
        with mock.patch.object(queue_worker, "get_pending_notifications", side_effect=RuntimeError("imap down")), \
                contextlib.redirect_stderr(io.StringIO()):
            stats, queued = self.ingest()
        self.assertEqual(stats["notifications_found"], 0)
        self.assertIsNone(queued)

    def test_an_email_that_cannot_be_cleaned_up_is_reported(self) -> None:
        self.notifications = [notification()]
        with mock.patch.object(queue_worker, "mark_as_read_and_delete", lambda ids: False):
            self.ingest()
        self.assertIn("MAILBOX", self.warning_kinds())
        self.assertEqual(len(db_manager.get_pending_jobs(self.connection)), 1)  # the job is queued regardless

    def test_a_dry_run_queues_nothing(self) -> None:
        self.dry_run = True
        self.notifications = [notification()]
        self.ingest()
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM job_queue").fetchone()[0], 0)


# ------------------------------------------------------------------------------- processing due jobs
class ProcessQueueTests(WorkerTestCase):
    """The mapping's re-check list is [5, 15] minutes, so a job has 2 re-checks after its first run."""

    def test_nothing_due_does_nothing(self) -> None:
        self.job(due=time.time() + 9999)
        stats = self.process()
        self.assertEqual(stats["due_jobs"], 0)
        self.assertEqual(self.calls["download"], [])

    def test_the_first_run_downloads_and_schedules_the_first_recheck(self) -> None:
        job_id = self.job()
        created = self.row(job_id)["created_at"]
        self.download_results = [download_result(downloaded=3, skipped=1, total=4, working_dir=self.staged_dir())]

        stats = self.process()

        row = self.row(job_id)
        self.assertEqual((row["status"], row["attempt_count"], row["last_result"]), ("PENDING", 1, "RETRY"))
        self.assertAlmostEqual(row["next_check_time"], created + 5 * 60, delta=1)  # measured from creation, not from now
        self.assertEqual((row["downloaded_count"], row["skipped_count"], row["total_items"]), (3, 1, 4))
        self.assertEqual(self.calls["download"], [(REPO, "v1", "Release")])
        self.assertEqual((stats["retried"], stats["completed"], stats["failed"]), (1, 0, 0))
        self.assertEqual(self.calls["complete"], [])  # files stay in Processing until the plan is complete

    def test_the_second_recheck_uses_the_second_interval(self) -> None:
        job_id = self.job()
        created = self.row(job_id)["created_at"]
        self.at_attempt(job_id, 1)
        self.process()
        self.assertAlmostEqual(self.row(job_id)["next_check_time"], created + 15 * 60, delta=1)
        self.assertEqual(self.row(job_id)["attempt_count"], 2)

    def test_the_last_attempt_completes_the_job_and_moves_the_files_out_of_processing(self) -> None:
        job_id = self.job()
        working_dir = self.staged_dir()
        self.at_attempt(job_id, 2)
        self.download_results = [download_result(working_dir=working_dir)]

        stats = self.process()

        row = self.row(job_id)
        self.assertEqual((row["status"], row["attempt_count"], row["last_result"]), ("COMPLETED", 3, "SUCCESS"))
        completed_at = self.connection.execute("SELECT completed_at FROM job_queue WHERE id = ?", (job_id,)).fetchone()[0]
        self.assertIsNotNone(completed_at)
        self.assertEqual(self.calls["complete"], [working_dir])
        self.assertEqual(len(self.calls["completed_logs"]), 1)
        self.assertEqual((stats["completed"], stats["downloaded_files"], stats["skipped_files"]), (1, 3, 1))

    def test_the_last_attempt_failing_marks_the_job_failed(self) -> None:
        job_id = self.job()
        self.at_attempt(job_id, 2)
        self.download_results = [download_result(status="FAILED", downloaded=0, skipped=0, total=0)]
        stats = self.process()
        self.assertEqual((self.row(job_id)["status"], self.row(job_id)["last_result"]), ("FAILED", "FAILED"))
        self.assertEqual((stats["failed"], stats["completed"]), (1, 0))

    def test_a_crashing_download_is_a_failed_attempt_not_a_crashed_worker(self) -> None:
        first, second = self.job(tag="a"), self.job(tag="b")
        self.download_results = [RuntimeError("boom"), download_result()]
        stats = self.process()
        self.assertEqual(stats["due_jobs"], 2)
        self.assertEqual(self.row(first)["attempt_count"], 1)  # rescheduled for another try
        self.assertEqual(self.row(second)["attempt_count"], 1)
        self.assertEqual(self.row(first)["status"], "PENDING")

    def test_a_failed_recheck_does_not_erase_what_an_earlier_attempt_downloaded(self) -> None:
        job_id = self.job()
        working_dir = self.staged_dir()
        self.at_attempt(job_id, 1, downloaded=3, skipped=1, total=4, working_dir=working_dir)
        self.download_results = [download_result(status="FAILED", downloaded=0, skipped=0, total=0)]
        self.process()
        row = self.row(job_id)
        self.assertEqual((row["downloaded_count"], row["skipped_count"], row["total_items"]), (3, 1, 4))
        self.assertEqual(row["working_dir"], working_dir)

    def test_skipped_files_are_recorded_with_the_attempt(self) -> None:
        job_id = self.job()
        self.download_results = [download_result(skipped_items=[{"item_key": "a:1", "file_name": "a.zip", "reason": "already downloaded"}])]
        self.process()
        rows = self.connection.execute("SELECT attempt_count, reason FROM job_skip_details WHERE job_id = ?", (job_id,)).fetchall()
        self.assertEqual([tuple(r) for r in rows], [(1, "already downloaded")])

    def test_a_changed_commit_supersedes_the_job_and_queues_a_new_one_without_downloading(self) -> None:
        job_id = self.job(commit="old0000")
        stats = self.process()
        self.assertEqual(self.row(job_id)["status"], "SUPERSEDED")
        self.assertEqual(self.row(job_id)["last_result"], "SUPERSEDED_COMMIT_CHANGED")
        (new,) = db_manager.get_pending_jobs(self.connection)
        self.assertEqual(new["expected_commit"], "aaa1111")
        self.assertEqual(self.calls["download"], [])
        self.assertEqual(stats["superseded"], 1)  # Regression: used to be reported as 0

    def test_a_changed_commit_does_not_queue_a_second_job_for_the_new_commit(self) -> None:
        old = self.job(commit="old0000")
        existing = self.job(commit="aaa1111", due=time.time() + 9999)
        self.process()
        self.assertEqual(self.row(old)["status"], "SUPERSEDED")
        self.assertEqual(self.pending_ids(), [existing])

    def test_an_unavailable_commit_is_warned_about_but_the_download_goes_ahead(self) -> None:
        job_id = self.job()
        self.commit = None
        self.process()
        self.assertIn("API", self.warning_kinds())
        self.assertEqual(len(self.calls["download"]), 1)
        self.assertEqual(self.row(job_id)["expected_commit"], "aaa1111")  # the earlier baseline is kept

    def test_a_vanished_release_keeps_being_rechecked_until_the_plan_is_complete(self) -> None:
        job_id = self.job()
        self.download_results = [download_result("SKIP", 0, 0, 0, skip_reason="release_not_found")]
        self.process()
        self.assertEqual((self.row(job_id)["status"], self.row(job_id)["attempt_count"]), ("PENDING", 1))

    def test_a_release_that_vanished_after_being_fully_staged_is_finalized_as_complete(self) -> None:
        job_id = self.job()
        working_dir = self.staged_dir()
        self.at_attempt(job_id, 2, downloaded=3, skipped=1, total=4, working_dir=working_dir)
        self.download_results = [download_result("SKIP", 0, 0, 0, skip_reason="release_not_found")]

        self.process()

        row = self.row(job_id)
        self.assertEqual((row["status"], row["downloaded_count"], row["total_items"]), ("COMPLETED", 3, 4))
        self.assertEqual(self.calls["complete"], [working_dir])
        self.assertIn("PREMATURE_FINALIZE", self.warning_kinds())

    def test_a_release_that_vanished_half_staged_is_quarantined_not_finalized(self) -> None:
        job_id = self.job()
        working_dir = self.staged_dir()
        self.at_attempt(job_id, 2, downloaded=1, skipped=0, total=4, working_dir=working_dir)
        self.download_results = [download_result("SKIP", 0, 0, 0, skip_reason="release_not_found")]

        self.process()

        self.assertEqual(self.calls["partial"], [working_dir])
        self.assertEqual(self.calls["complete"], [])
        self.assertEqual(len(self.calls["partial_logs"]), 1)
        self.assertNotIn("PREMATURE_FINALIZE", self.warning_kinds())

    def test_a_pause_stops_before_the_next_job_and_leaves_it_due(self) -> None:
        first, second = self.job(tag="a"), self.job(tag="b")
        checks = iter([False, True])
        stats = self.process(should_pause=lambda: next(checks))
        self.assertEqual(len(self.calls["download"]), 1)
        self.assertEqual(self.row(second)["attempt_count"], 0)
        self.assertEqual(self.row(second)["status"], "PENDING")
        self.assertEqual(stats["due_jobs"], 2)

    def test_specific_jobs_run_even_when_they_are_not_due(self) -> None:
        job_id = self.job(due=time.time() + 9999)
        self.process(job_filter_ids=[job_id])
        self.assertEqual(self.row(job_id)["attempt_count"], 1)

    def test_the_due_limit_caps_the_cycle(self) -> None:
        for n in range(3):
            self.job(tag=f"t{n}", due=float(n))
        self.process(limit=2)
        self.assertEqual(len(self.calls["download"]), 2)

    def test_duplicate_pending_jobs_are_tidied_before_processing(self) -> None:
        first, duplicate = self.job(), self.job()
        self.process()
        self.assertEqual(self.row(duplicate)["last_result"], "SUPERSEDED_DUPLICATE_PENDING")
        self.assertEqual(len(self.calls["download"]), 1)

    def test_a_dry_run_changes_nothing_in_the_queue(self) -> None:
        job_id = self.job()
        self.dry_run = True
        self.process()
        row = self.row(job_id)
        self.assertEqual((row["status"], row["attempt_count"]), ("PENDING", 0))
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM job_skip_details").fetchone()[0], 0)


class ManualCheckTests(WorkerTestCase):
    def test_a_manual_check_runs_the_job_without_moving_its_schedule(self) -> None:
        job_id = self.job(due=time.time() + 9999)
        before = self.row(job_id)["next_check_time"]
        processed, skipped, missing = self.quietly(queue_worker.process_selected_pending_jobs, self.connection, "token", [job_id])
        row = self.row(job_id)
        self.assertEqual((processed, skipped, missing), (1, [], []))
        self.assertEqual((row["status"], row["attempt_count"], row["next_check_time"], row["last_result"]), ("PENDING", 0, before, "MANUAL_CHECK"))
        self.assertEqual(len(self.calls["download"]), 1)

    def test_finished_and_unknown_jobs_are_reported_not_run(self) -> None:
        done = self.job()
        db_manager.mark_job_completed(self.connection, done)
        processed, skipped, missing = self.quietly(queue_worker.process_selected_pending_jobs, self.connection, "token", [done, 9999])
        self.assertEqual((processed, skipped, missing), (0, [done], [9999]))
        self.assertEqual(self.calls["download"], [])

    def test_no_ids_or_no_matches(self) -> None:
        self.assertEqual(self.quietly(queue_worker.process_selected_pending_jobs, self.connection, "token", []), (0, [], []))
        self.assertEqual(self.quietly(queue_worker.process_selected_pending_jobs, self.connection, "token", [42]), (0, [], [42]))

    def test_a_manual_check_after_the_last_attempt_completes_the_job(self) -> None:
        job_id = self.job()
        self.at_attempt(job_id, 2)
        self.quietly(queue_worker.process_selected_pending_jobs, self.connection, "token", [job_id])
        self.assertEqual(self.row(job_id)["status"], "COMPLETED")

    def test_a_changed_commit_supersedes_instead_of_downloading(self) -> None:
        job_id = self.job(commit="old0000")
        self.quietly(queue_worker.process_selected_pending_jobs, self.connection, "token", [job_id])
        self.assertEqual(self.row(job_id)["status"], "SUPERSEDED")
        self.assertEqual(self.calls["download"], [])
        self.assertEqual(len(self.pending_ids()), 1)


class FullCycleTests(WorkerTestCase):
    def test_a_cycle_ingests_then_processes_and_writes_one_summary(self) -> None:
        self.notifications = [notification()]
        summaries = []
        with mock.patch.object(queue_worker, "log_cycle_summary", lambda ingest, queue: summaries.append((ingest, queue)) or "summary"):
            self.quietly(queue_worker.run_ingest_and_queue_cycle, self.connection, "token")
        self.assertEqual(len(self.calls["download"]), 1)  # the new job was processed in the same cycle
        (ingest_stats, queue_stats), = summaries
        self.assertEqual((ingest_stats["notifications_queued"], queue_stats["due_jobs"]), (1, 1))

    def test_a_pause_during_the_ingest_skips_the_queue_part(self) -> None:
        self.job()
        summaries = []
        with mock.patch.object(queue_worker, "log_cycle_summary", lambda ingest, queue: summaries.append(queue) or "summary"):
            self.quietly(queue_worker.run_ingest_and_queue_cycle, self.connection, "token", lambda: True)
        self.assertEqual(self.calls["download"], [])
        self.assertEqual(summaries[0]["due_jobs"], 0)


# --------------------------------------------------------------------------------------- helpers
class CounterProtectionTests(unittest.TestCase):
    """A weaker attempt must never overwrite what an earlier one already achieved."""

    def row(self, downloaded=3, skipped=1, total=4, working_dir="/work/a"):
        return {"downloaded_count": downloaded, "skipped_count": skipped, "total_items": total, "working_dir": working_dir}

    def test_a_first_attempt_has_nothing_to_protect(self) -> None:
        self.assertEqual(queue_worker._preserve_best_known_counters(self.row(0, 0, 0, None), 2, 1, 5, "/w"), (2, 1, 5, "/w"))

    def test_an_attempt_that_found_nothing_keeps_the_earlier_counts_and_folder(self) -> None:
        self.assertEqual(queue_worker._preserve_best_known_counters(self.row(), 0, 0, 0, None), (3, 1, 4, "/work/a"))
        self.assertEqual(queue_worker._preserve_best_known_counters(self.row(), 0, 0, 0, "/work/b"), (3, 1, 4, "/work/b"))

    def test_an_attempt_that_got_less_far_than_a_complete_one_does_not_win(self) -> None:
        self.assertEqual(queue_worker._preserve_best_known_counters(self.row(), 1, 1, 4, "/work/b"), (3, 1, 4, "/work/b"))

    def test_an_attempt_that_got_as_far_or_further_wins(self) -> None:
        self.assertEqual(queue_worker._preserve_best_known_counters(self.row(), 4, 0, 4, "/w"), (4, 0, 4, "/w"))
        self.assertEqual(queue_worker._preserve_best_known_counters(self.row(), 5, 1, 6, "/w"), (5, 1, 6, "/w"))  # the release grew

    def test_progress_on_an_incomplete_earlier_attempt_is_not_protected(self) -> None:
        self.assertEqual(queue_worker._preserve_best_known_counters(self.row(1, 0, 4), 0, 1, 4, "/w"), (0, 1, 4, "/w"))

    def test_when_everything_is_accounted_for(self) -> None:
        accounted = queue_worker._has_all_release_items_accounted
        self.assertTrue(accounted(3, 1, 4))
        self.assertTrue(accounted(4, 1, 4))
        self.assertFalse(accounted(3, 0, 4))
        self.assertFalse(accounted(0, 0, 0))  # an empty release is not "complete"

    def test_only_a_vanished_release_ends_the_plan_early(self) -> None:
        self.assertTrue(queue_worker._is_terminal_skip_reason("release_not_found"))
        for reason in (None, "", "rate_limited", "other"):
            self.assertFalse(queue_worker._is_terminal_skip_reason(reason))


class FolderRelocationTests(WorkerTestCase):
    def test_the_same_folder_or_no_folder_is_left_alone(self) -> None:
        old = self.staged_dir("a")
        for previous, new in ((old, old), (old, None), (None, old), (old, old.upper() if os.name == "nt" else old)):
            with self.subTest(previous=previous, new=new):
                self.quietly(queue_worker._handle_working_dir_relocation, previous, new, REPO, "v1", 1)
        self.assertEqual((self.calls["partial"], self.calls["warnings"]), ([], []))

    def test_a_folder_abandoned_by_a_rename_is_quarantined_and_reported(self) -> None:
        old, new = self.staged_dir("old name"), self.staged_dir("new name")
        self.quietly(queue_worker._handle_working_dir_relocation, old, new, REPO, "v1", 7)
        self.assertEqual(self.calls["partial"], [old])
        self.assertEqual(self.warning_kinds(), ["FOLDER_RENAMED", "FOLDER_RENAMED_MOVED"])

    def test_a_previous_folder_that_no_longer_exists_is_ignored(self) -> None:
        self.quietly(queue_worker._handle_working_dir_relocation, os.path.join(self.root, "gone"), self.staged_dir("n"), REPO, "v1", 7)
        self.assertEqual((self.calls["partial"], self.calls["warnings"]), ([], []))

    def test_a_failing_quarantine_is_reported_not_raised(self) -> None:
        old, new = self.staged_dir("old"), self.staged_dir("new")
        with mock.patch.object(queue_worker, "move_processing_folder_to_partial", side_effect=OSError("denied")):
            self.quietly(queue_worker._handle_working_dir_relocation, old, new, REPO, "v1", 7)
        self.assertEqual(self.warning_kinds(), ["FOLDER_RENAMED", "FOLDER_RENAMED_MOVE"])


class FinalizeStagedFolderTests(WorkerTestCase):
    def test_a_finished_folder_is_moved_logged_and_recorded_on_the_repository(self) -> None:
        self.write_mapping({"repository": REPO, "last_finalized": ""})
        working_dir = self.staged_dir()
        done = self.quietly(queue_worker._finalize_staged_release_folder, working_dir, REPO, "v1", "abc")
        self.assertEqual(done, os.path.join(self.root, "done", "rel1"))
        self.assertEqual(self.calls["completed_logs"], [(REPO, "v1", "abc", done)])
        self.assertNotEqual(mapping_manager.get_repository_mapping(REPO)["last_finalized"], "")

    def test_nothing_to_finalize_without_a_folder(self) -> None:
        self.assertIsNone(self.quietly(queue_worker._finalize_staged_release_folder, None, REPO, "v1"))
        self.assertIsNone(self.quietly(queue_worker._finalize_staged_release_folder, "", REPO, "v1"))
        self.assertEqual(self.calls["complete"], [])

    def test_the_completed_log_can_be_left_out(self) -> None:
        self.quietly(queue_worker._finalize_staged_release_folder, self.staged_dir(), REPO, "v1", None, False)
        self.assertEqual(self.calls["completed_logs"], [])

    def test_a_failed_move_is_reported_and_returns_nothing(self) -> None:
        with mock.patch.object(queue_worker, "move_processing_folder_to_complete", side_effect=OSError("disk full")):
            self.assertIsNone(self.quietly(queue_worker._finalize_staged_release_folder, self.staged_dir(), REPO, "v1"))
        self.assertEqual(self.warning_kinds(), ["MOVE"])
        self.assertEqual(self.calls["completed_logs"], [])


if __name__ == "__main__":
    unittest.main()
