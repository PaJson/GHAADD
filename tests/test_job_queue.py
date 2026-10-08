"""The job queue in state.db: finding, scheduling, superseding and finishing jobs, plus the duplicate-download
memory (asset_state) and the purge helpers.

These functions decide what is downloaded, when it is re-checked and what is never queued twice. Everything runs
against a throw-away database; the real state.db is never opened. Run from the project root:
python -m unittest discover -s tests -t .
"""
import os
import tempfile
import unittest
from contextlib import closing
from unittest import mock

from modules import db_manager


class QueueTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._temp_dir.cleanup)
        db_path = os.path.join(self._temp_dir.name, "state.db")
        patcher = mock.patch.object(db_manager, "get_state_db_path", lambda: db_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.connection = db_manager.open_database()
        self.addCleanup(self.connection.close)

    def job(self, repo="o/app", tag="v1", release_type: str | None = "Release", due=0.0, commit=None) -> int:
        return db_manager.enqueue_job(self.connection, repo, tag, release_type, due, commit)

    def row(self, job_id: int):
        return db_manager.get_jobs_by_ids(self.connection, [job_id])[0]

    def ids(self, rows) -> list[int]:
        return [r["id"] for r in rows]


class EnqueueAndFindTests(QueueTestCase):
    def test_a_new_job_starts_pending_with_nothing_downloaded(self) -> None:
        job_id = self.job(commit="abc1234")
        row = self.row(job_id)
        self.assertEqual((row["status"], row["attempt_count"], row["expected_commit"]), ("PENDING", 0, "abc1234"))
        self.assertEqual((row["downloaded_count"], row["skipped_count"], row["total_items"]), (0, 0, 0))
        self.assertIsNone(row["working_dir"])

    def test_without_a_schedule_a_job_is_due_immediately(self) -> None:
        job_id = db_manager.enqueue_job(self.connection, "o/app", "v1")
        self.assertEqual(self.row(job_id)["next_check_time"], 0.0)

    def test_the_same_release_is_found_by_repo_tag_and_type(self) -> None:
        job_id = self.job()
        find = lambda **kw: db_manager.get_pending_job_for_release(self.connection, **{"repo": "o/app", "tag": "v1", **kw})
        self.assertEqual(find(release_type="Release")["id"], job_id)
        self.assertIsNone(find(release_type="Pre-release"))
        self.assertIsNone(find(tag="v2", release_type="Release"))
        self.assertIsNone(find(repo="o/other", release_type="Release"))

    def test_a_job_without_a_release_type_matches_only_a_search_without_one(self) -> None:
        job_id = self.job(release_type=None)
        self.assertEqual(db_manager.get_pending_job_for_release(self.connection, "o/app", "v1", None)["id"], job_id)
        self.assertIsNone(db_manager.get_pending_job_for_release(self.connection, "o/app", "v1", "Release"))

    def test_the_commit_can_narrow_the_search_and_a_job_can_be_excluded(self) -> None:
        first = self.job(commit="aaa")
        second = self.job(commit="bbb")
        find = lambda **kw: db_manager.get_pending_job_for_release(self.connection, "o/app", "v1", "Release", **kw)
        self.assertEqual(find(expected_commit="bbb")["id"], second)
        self.assertIsNone(find(expected_commit="ccc"))
        self.assertEqual(find(exclude_job_id=first)["id"], second)
        self.assertEqual(find()["id"], first)  # the oldest wins

    def test_finished_jobs_are_not_pending(self) -> None:
        job_id = self.job()
        db_manager.mark_job_completed(self.connection, job_id)
        self.assertIsNone(db_manager.get_pending_job_for_release(self.connection, "o/app", "v1", "Release"))
        self.assertEqual(db_manager.get_pending_jobs_for_release(self.connection, "o/app", "v1", "Release"), [])
        self.assertEqual(db_manager.get_pending_jobs(self.connection), [])

    def test_all_pending_jobs_of_a_release_come_oldest_first(self) -> None:
        first, second = self.job(), self.job()
        other = self.job(tag="v2")
        rows = db_manager.get_pending_jobs_for_release(self.connection, "o/app", "v1", "Release")
        self.assertEqual(self.ids(rows), [first, second])
        self.assertNotIn(other, self.ids(rows))


class SchedulingTests(QueueTestCase):
    def test_only_due_pending_jobs_are_returned_in_schedule_order(self) -> None:
        late = self.job(tag="late", due=300.0)
        early = self.job(tag="early", due=100.0)
        future = self.job(tag="future", due=900.0)
        done = self.job(tag="done", due=50.0)
        db_manager.mark_job_completed(self.connection, done)

        due = db_manager.get_due_jobs(self.connection, 400.0)

        self.assertEqual(self.ids(due), [early, late])
        self.assertNotIn(future, self.ids(due))

    def test_a_job_exactly_at_its_time_is_due_and_the_limit_applies(self) -> None:
        first, second = self.job(due=100.0), self.job(due=100.0)
        self.assertEqual(self.ids(db_manager.get_due_jobs(self.connection, 100.0)), [first, second])
        self.assertEqual(self.ids(db_manager.get_due_jobs(self.connection, 100.0, limit=1)), [first])
        self.assertEqual(db_manager.get_due_jobs(self.connection, 99.0), [])

    def test_the_next_job_is_the_one_scheduled_first(self) -> None:
        self.assertIsNone(db_manager.get_next_pending_job(self.connection))
        self.job(tag="b", due=500.0)
        earliest = self.job(tag="a", due=200.0)
        done = self.job(tag="c", due=1.0)
        db_manager.mark_job_completed(self.connection, done)
        self.assertEqual(db_manager.get_next_pending_job(self.connection)["id"], earliest)

    def test_rescheduling_keeps_the_job_pending_and_records_the_attempt(self) -> None:
        job_id = self.job(due=100.0)
        db_manager.reschedule_job(
            self.connection, job_id, 700.0, 2, expected_commit="abc", downloaded_count=3, skipped_count=1,
            total_items=4, working_dir="/work/a", last_result="RETRY",
        )
        row = self.row(job_id)
        self.assertEqual((row["status"], row["next_check_time"], row["attempt_count"]), ("PENDING", 700.0, 2))
        self.assertEqual((row["downloaded_count"], row["skipped_count"], row["total_items"]), (3, 1, 4))
        self.assertEqual((row["working_dir"], row["last_result"]), ("/work/a", "RETRY"))
        self.assertEqual(self.ids(db_manager.get_due_jobs(self.connection, 600.0)), [])  # no longer due
        self.assertEqual(self.ids(db_manager.get_due_jobs(self.connection, 700.0)), [job_id])

    def test_rescheduling_without_a_working_dir_keeps_the_old_one(self) -> None:
        job_id = self.job()
        db_manager.reschedule_job(self.connection, job_id, 10.0, 1, working_dir="/work/a")
        db_manager.reschedule_job(self.connection, job_id, 20.0, 2)
        self.assertEqual(self.row(job_id)["working_dir"], "/work/a")

    def test_a_manual_check_updates_counters_without_touching_the_schedule(self) -> None:
        job_id = self.job(due=123.0)
        db_manager.reschedule_job(self.connection, job_id, 123.0, 3)
        db_manager.update_job_for_manual_check(self.connection, job_id, "abc", 5, 2, 7, "/work/b")
        row = self.row(job_id)
        self.assertEqual((row["next_check_time"], row["attempt_count"]), (123.0, 3))
        self.assertEqual((row["downloaded_count"], row["skipped_count"], row["total_items"]), (5, 2, 7))
        self.assertEqual((row["status"], row["last_result"], row["working_dir"]), ("PENDING", "MANUAL_CHECK", "/work/b"))


class FinishingJobsTests(QueueTestCase):
    def test_completed_records_counts_and_a_finish_time(self) -> None:
        job_id = self.job()
        db_manager.mark_job_completed(self.connection, job_id, attempt_count=3, downloaded_count=4, skipped_count=1, total_items=5)
        row = self.connection.execute("SELECT * FROM job_queue WHERE id = ?", (job_id,)).fetchone()
        self.assertEqual((row["status"], row["attempt_count"], row["last_result"]), ("COMPLETED", 3, "SUCCESS"))
        self.assertEqual((row["downloaded_count"], row["skipped_count"], row["total_items"]), (4, 1, 5))
        self.assertIsNotNone(row["completed_at"])

    def test_completed_without_an_attempt_count_keeps_the_old_one(self) -> None:
        job_id = self.job()
        db_manager.reschedule_job(self.connection, job_id, 10.0, 4)
        db_manager.mark_job_completed(self.connection, job_id)
        self.assertEqual(self.row(job_id)["attempt_count"], 4)

    def test_failed_and_superseded_record_their_state(self) -> None:
        failed, superseded = self.job(tag="a"), self.job(tag="b")
        db_manager.mark_job_failed(self.connection, failed, attempt_count=6, expected_commit="abc", last_result="GAVE_UP")
        db_manager.mark_job_superseded(self.connection, superseded, expected_commit="def")
        self.assertEqual(
            (self.row(failed)["status"], self.row(failed)["attempt_count"], self.row(failed)["last_result"]),
            ("FAILED", 6, "GAVE_UP"),
        )
        self.assertEqual((self.row(superseded)["status"], self.row(superseded)["last_result"]), ("SUPERSEDED", "SUPERSEDED"))
        self.assertEqual(self.row(superseded)["expected_commit"], "def")
        self.assertEqual(db_manager.get_pending_jobs(self.connection), [])

    def test_rescheduling_a_finished_job_reopens_it(self) -> None:
        job_id = self.job()
        db_manager.mark_job_failed(self.connection, job_id)
        db_manager.reschedule_job(self.connection, job_id, 5.0, 1)
        row = self.connection.execute("SELECT status, completed_at FROM job_queue WHERE id = ?", (job_id,)).fetchone()
        self.assertEqual(row["status"], "PENDING")
        self.assertIsNone(row["completed_at"])


class SupersedingTests(QueueTestCase):
    def test_a_new_notification_replaces_the_pending_jobs_of_that_release_only(self) -> None:
        mine = [self.job(), self.job()]
        other_type = self.job(release_type="Pre-release")
        other_tag = self.job(tag="v2")
        other_repo = self.job(repo="o/other")
        completed = self.job()
        db_manager.mark_job_completed(self.connection, completed)

        count = db_manager.supersede_pending_jobs_for_release(self.connection, "o/app", "v1", "Release", "newcommit")

        self.assertEqual(count, 2)
        for job_id in mine:
            self.assertEqual(self.row(job_id)["status"], "SUPERSEDED")
            self.assertEqual(self.row(job_id)["last_result"], "SUPERSEDED_REPLACED_BY_NEW_NOTIFICATION")
            self.assertEqual(self.row(job_id)["expected_commit"], "newcommit")
        for job_id in (other_type, other_tag, other_repo):
            self.assertEqual(self.row(job_id)["status"], "PENDING")
        self.assertEqual(self.row(completed)["status"], "COMPLETED")  # history is not rewritten

    def test_without_a_replacement_commit_the_old_one_is_kept(self) -> None:
        job_id = self.job(commit="old")
        db_manager.supersede_pending_jobs_for_release(self.connection, "o/app", "v1", "Release")
        self.assertEqual(self.row(job_id)["expected_commit"], "old")

    def test_nothing_to_supersede_returns_zero(self) -> None:
        self.assertEqual(db_manager.supersede_pending_jobs_for_release(self.connection, "o/app", "v1", "Release"), 0)

    def test_removing_by_id_touches_only_pending_jobs_and_returns_them(self) -> None:
        pending, other, completed = self.job(tag="a"), self.job(tag="b"), self.job(tag="c")
        db_manager.mark_job_completed(self.connection, completed)

        removed = db_manager.supersede_pending_jobs_by_ids(self.connection, [pending, completed, 9999, pending])

        self.assertEqual(self.ids(removed), [pending])
        self.assertEqual(self.row(pending)["status"], "SUPERSEDED")
        self.assertEqual(self.row(pending)["last_result"], "SUPERSEDED_MANUAL_REMOVE")
        self.assertEqual(self.row(other)["status"], "PENDING")
        self.assertEqual(self.row(completed)["status"], "COMPLETED")
        self.assertEqual(db_manager.supersede_pending_jobs_by_ids(self.connection, []), [])
        self.assertEqual(db_manager.get_jobs_by_ids(self.connection, []), [])

    def test_lookup_by_id_is_sorted_and_ignores_unknown_ids(self) -> None:
        first, second = self.job(tag="a"), self.job(tag="b")
        self.assertEqual(self.ids(db_manager.get_jobs_by_ids(self.connection, [second, first, 9999])), [first, second])

    def test_duplicate_pending_jobs_keep_only_the_oldest(self) -> None:
        keep, dup1, dup2 = self.job(commit="aaa"), self.job(commit="aaa"), self.job(commit="aaa")
        other_commit = self.job(commit="bbb")  # a different commit is a different job, not a duplicate
        other_tag = self.job(tag="v2", commit="aaa")

        count = db_manager.supersede_duplicate_pending_jobs(self.connection)

        self.assertEqual(count, 2)
        self.assertEqual(self.row(keep)["status"], "PENDING")
        for job_id in (dup1, dup2):
            self.assertEqual((self.row(job_id)["status"], self.row(job_id)["last_result"]), ("SUPERSEDED", "SUPERSEDED_DUPLICATE_PENDING"))
        self.assertEqual(self.row(other_commit)["status"], "PENDING")
        self.assertEqual(self.row(other_tag)["status"], "PENDING")

    def test_no_duplicates_means_nothing_changes(self) -> None:
        self.job(tag="a")
        self.job(tag="b")
        self.assertEqual(db_manager.supersede_duplicate_pending_jobs(self.connection), 0)
        self.assertEqual(len(db_manager.get_pending_jobs(self.connection)), 2)


class SkipDetailsTests(QueueTestCase):
    def details(self, job_id: int) -> list[tuple]:
        rows = self.connection.execute(
            "SELECT attempt_count, item_key, file_name, reason FROM job_skip_details WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()
        return [tuple(r) for r in rows]

    def test_skipped_items_are_stored_with_their_reason(self) -> None:
        job_id = self.job()
        db_manager.save_job_skip_details(
            self.connection, job_id, 2,
            [{"item_key": "asset:1", "file_name": "a.zip", "reason": "already downloaded"}, {"item_key": "asset:2"}, "junk", None],
        )
        self.assertEqual(self.details(job_id), [(2, "asset:1", "a.zip", "already downloaded"), (2, "asset:2", None, "unknown")])

    def test_nothing_to_save_changes_nothing(self) -> None:
        job_id = self.job()
        db_manager.save_job_skip_details(self.connection, job_id, 1, [])
        db_manager.save_job_skip_details(self.connection, job_id, 1, ["junk"])
        self.assertEqual(self.details(job_id), [])

    def test_purging_a_job_removes_its_skip_details(self) -> None:
        job_id = self.job()
        db_manager.save_job_skip_details(self.connection, job_id, 1, [{"item_key": "k", "reason": "r"}])
        db_manager.mark_job_completed(self.connection, job_id)
        db_manager.purge_job_queue_rows(self.connection)
        self.assertEqual(self.details(job_id), [])


class PurgeTests(QueueTestCase):
    def finish(self, job_id: int, status: str, age_days: float) -> None:
        stamp = "CAST(strftime('%s', 'now') AS REAL) - ?"
        self.connection.execute(
            f"UPDATE job_queue SET status = ?, created_at = {stamp}, updated_at = {stamp}, completed_at = {stamp} WHERE id = ?",
            (status, age_days * 86400, age_days * 86400, age_days * 86400, job_id),
        )
        self.connection.commit()

    def remaining(self) -> list[int]:
        return [r[0] for r in self.connection.execute("SELECT id FROM job_queue ORDER BY id")]

    def test_pending_jobs_are_never_purged(self) -> None:
        pending = self.job()
        old = self.job(tag="old")
        self.finish(old, "COMPLETED", 400)
        self.assertEqual(db_manager.purge_job_queue_rows(self.connection, min_age_days=0), 1)
        self.assertEqual(self.remaining(), [pending])

    def test_age_status_and_repo_filters(self) -> None:
        old_done, new_done, old_failed, other_repo = self.job(tag="a"), self.job(tag="b"), self.job(tag="c"), self.job(repo="x/y")
        self.finish(old_done, "COMPLETED", 40)
        self.finish(new_done, "COMPLETED", 1)
        self.finish(old_failed, "FAILED", 40)
        self.finish(other_repo, "COMPLETED", 40)

        self.assertEqual(db_manager.purge_job_queue_rows(self.connection, status="COMPLETED", repo_filter="O/APP", min_age_days=30, dry_run=True), 1)
        self.assertEqual(self.remaining(), [old_done, new_done, old_failed, other_repo])  # a dry run deletes nothing
        self.assertEqual(db_manager.purge_job_queue_rows(self.connection, status="COMPLETED", repo_filter="o/app", min_age_days=30), 1)
        self.assertEqual(self.remaining(), [new_done, old_failed, other_repo])

    def test_the_oldest_n_are_removed(self) -> None:
        ids = [self.job(tag=str(n)) for n in range(4)]
        for age, job_id in zip((40, 30, 20, 10), ids):
            self.finish(job_id, "COMPLETED", age)
        self.assertEqual(db_manager.purge_job_queue_rows(self.connection, oldest_count=2, dry_run=True), 2)
        self.assertEqual(db_manager.purge_job_queue_rows(self.connection, oldest_count=2), 2)
        self.assertEqual(self.remaining(), ids[2:])


class LifecycleEventTests(QueueTestCase):
    def add(self, event_type="WARNING", repo="o/app", message="m", age_days=0.0) -> int:
        event_id = db_manager.insert_lifecycle_event(self.connection, event_type, message, repo=repo)
        self.connection.execute(
            "UPDATE lifecycle_events SET created_at = CAST(strftime('%s', 'now') AS REAL) - ? WHERE id = ?",
            (age_days * 86400, event_id),
        )
        self.connection.commit()
        return event_id

    def test_events_come_newest_first_and_can_be_filtered(self) -> None:
        old = self.add(age_days=5)
        new = self.add(event_type="COMPLETED_MOVE")
        other = self.add(repo="x/Other")
        self.assertEqual(self.ids(db_manager.get_lifecycle_events(self.connection, limit=None)), [other, new, old])
        self.assertEqual(self.ids(db_manager.get_lifecycle_events(self.connection, event_type="COMPLETED_MOVE")), [new])
        self.assertEqual(self.ids(db_manager.get_lifecycle_events(self.connection, repo_filter="OTHER")), [other])
        self.assertEqual(self.ids(db_manager.get_lifecycle_events(self.connection, limit=1)), [other])

    def test_purge_by_type_repo_and_age(self) -> None:
        old_warning = self.add(age_days=40)
        new_warning = self.add()
        old_move = self.add(event_type="COMPLETED_MOVE", age_days=40)
        self.assertEqual(db_manager.purge_lifecycle_events(self.connection, event_type="WARNING", min_age_days=30, dry_run=True), 1)
        self.assertEqual(db_manager.purge_lifecycle_events(self.connection, event_type="WARNING", min_age_days=30), 1)
        left = self.ids(db_manager.get_lifecycle_events(self.connection, limit=None))
        self.assertEqual(sorted(left), sorted([new_warning, old_move]))
        self.assertNotIn(old_warning, left)
        self.assertEqual(db_manager.purge_lifecycle_events(self.connection, min_age_days=0), 2)
        self.assertEqual(db_manager.get_lifecycle_events(self.connection, limit=None), [])

    def test_the_newest_event_id_is_zero_for_an_empty_table(self) -> None:
        self.assertEqual(db_manager.get_max_event_id(self.connection), 0)
        event_id = self.add()
        self.assertEqual(db_manager.get_max_event_id(self.connection), event_id)

    def test_the_gui_can_ask_which_events_still_exist(self) -> None:
        first, second = self.add(), self.add()
        self.assertEqual(db_manager.purge_lifecycle_events(self.connection, repo_filter="nothing-matches"), 0)
        self.connection.execute("DELETE FROM lifecycle_events WHERE id = ?", (first,))
        self.connection.commit()
        self.assertEqual(db_manager.get_existing_event_ids(self.connection, [first, second, 9999]), {second})
        self.assertEqual(db_manager.get_existing_event_ids(self.connection, []), set())


class AssetStateTests(QueueTestCase):
    """The memory that stops a file from being downloaded again."""

    def setUp(self) -> None:
        super().setUp()
        self.base = os.path.join(self._temp_dir.name, "release")
        os.makedirs(self.base)

    def write(self, name: str, text: str = "data") -> None:
        with open(os.path.join(self.base, name), "w", encoding="utf-8") as handle:
            handle.write(text)

    def save(self, key: str, name: str, signature: str | None = None) -> None:
        db_manager.save_state_entry(self.connection, "o/app|v1", key, name, name, signature, 4, 1.5, '"etag"', self.base)

    def test_a_saved_file_is_remembered_with_its_local_size(self) -> None:
        self.write("app.zip", "1234")
        self.save("asset:1", "app.zip", "asset:1|app.zip|4")
        entry = db_manager.load_release_state(self.connection, "o/app|v1")["asset:1"]
        self.assertEqual((entry["file_name"], entry["file_path"], entry["size"]), ("app.zip", "app.zip", 4))
        self.assertEqual((entry["etag"], entry["expected_signature"], entry["local_size"]), ('"etag"', "asset:1|app.zip|4", 4))
        self.assertIsNotNone(entry["local_mtime"])

    def test_saving_again_updates_instead_of_duplicating(self) -> None:
        self.write("app.zip", "1234")
        self.save("asset:1", "app.zip", "old")
        self.save("asset:1", "app.zip", "new")
        state = db_manager.load_release_state(self.connection, "o/app|v1")
        self.assertEqual(list(state), ["asset:1"])
        self.assertEqual(state["asset:1"]["expected_signature"], "new")

    def test_a_file_that_is_not_on_disk_has_no_local_size(self) -> None:
        self.save("asset:1", "missing.zip")
        entry = db_manager.load_release_state(self.connection, "o/app|v1")["asset:1"]
        self.assertEqual((entry["local_size"], entry["local_mtime"]), (None, None))

    def test_each_release_has_its_own_memory(self) -> None:
        self.write("app.zip")
        self.save("asset:1", "app.zip")
        self.assertEqual(db_manager.load_release_state(self.connection, "o/app|v2"), {})

    def test_pruning_forgets_files_that_vanished_or_are_no_longer_part_of_the_release(self) -> None:
        for name in ("keep.zip", "gone.zip", "dropped.zip"):
            self.write(name)
            self.save(name, name)
        os.remove(os.path.join(self.base, "gone.zip"))

        db_manager.prune_release_state(self.connection, "o/app|v1", {"keep.zip", "gone.zip"}, self.base)

        self.assertEqual(list(db_manager.load_release_state(self.connection, "o/app|v1")), ["keep.zip"])
        self.assertTrue(os.path.exists(os.path.join(self.base, "dropped.zip")))  # pruning never deletes files

    def test_pruning_with_nothing_stale_changes_nothing(self) -> None:
        self.write("keep.zip")
        self.save("keep.zip", "keep.zip")
        db_manager.prune_release_state(self.connection, "o/app|v1", {"keep.zip"}, self.base)
        self.assertEqual(list(db_manager.load_release_state(self.connection, "o/app|v1")), ["keep.zip"])


class StateFileTests(unittest.TestCase):
    def test_purging_the_state_database_removes_only_that_file(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            db_path = os.path.join(folder, "state.db")
            neighbour = os.path.join(folder, "mapping.json")
            for path in (db_path, neighbour):
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("x")
            with mock.patch.object(db_manager, "get_state_db_path", lambda: db_path):
                self.assertTrue(db_manager.purge_state_database())
                self.assertFalse(db_manager.purge_state_database())  # nothing left to remove
            self.assertFalse(os.path.exists(db_path))
            self.assertTrue(os.path.exists(neighbour))


if __name__ == "__main__":
    unittest.main()
