"""The footer's queue figures: pending, due now, and how many due jobs are behind schedule.

A temporary state.db is used and the re-check intervals are fixed. Run from the project root:
python -m unittest discover -s tests -t .
"""
import contextlib
import os
import tempfile
import unittest
from unittest import mock

from modules import db_manager, gui_data, mapping_manager
from modules.file_cache import StatCache

NOW = 100_000.0
MINUTE = 60.0
INTERVALS = [5, 15, 30, 60, 120]  # minutes after the job was created


class QueueTextTests(unittest.TestCase):
    def test_the_text_for_each_situation(self) -> None:
        self.assertEqual(gui_data.queue_text(None), "")
        self.assertEqual(gui_data.queue_text(gui_data.QueueCounts(0, 0)), "Queue: nothing pending")
        self.assertEqual(gui_data.queue_text(gui_data.QueueCounts(53, 36)), "Queue: 36 due now, 17 due later")
        self.assertEqual(gui_data.queue_text(gui_data.QueueCounts(4, 0)), "Queue: 0 due now, 4 due later")
        self.assertEqual(gui_data.queue_text(gui_data.QueueCounts(2, 2)), "Queue: 2 due now, 0 due later")

    def test_jobs_that_are_behind_are_named(self) -> None:
        counts = gui_data.QueueCounts(pending=27, due=27, behind=26, catch_up_polls=5)
        self.assertEqual(gui_data.queue_text(counts), "Queue: 27 due now (26 behind schedule), 0 due later")
        self.assertEqual(gui_data.queue_text(gui_data.QueueCounts(5, 3, behind=0)), "Queue: 3 due now, 2 due later")

    def test_the_hover_text_explains_the_catching_up_only_when_there_is_some(self) -> None:
        plain = gui_data.queue_tip(gui_data.QueueCounts(3, 3, behind=0), "BASE")
        self.assertEqual(plain, "BASE")
        text = gui_data.queue_tip(gui_data.QueueCounts(27, 27, behind=26, catch_up_polls=5), "BASE")
        self.assertTrue(text.startswith("BASE"))
        self.assertIn("26 of the 27 due jobs are behind schedule", text)
        self.assertIn("needs 5 polls to catch up", text)
        self.assertIn("needs 1 poll to catch up", gui_data.queue_tip(gui_data.QueueCounts(2, 2, 2, 1), "B"))
        self.assertEqual(gui_data.queue_tip(None, "BASE"), "BASE")


class QueueCountTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._temp.cleanup)
        self.db_path = os.path.join(self._temp.name, "state.db")
        for patcher in (
            mock.patch.object(db_manager, "get_state_db_path", lambda: self.db_path),
            mock.patch.object(mapping_manager, "get_repository_recheck_intervals_minutes", lambda repo: list(INTERVALS)),
            # a cache of its own, so one test's answer is never served to the next
            mock.patch.object(
                gui_data, "_pending_cache", StatCache(gui_data._state_db_files, gui_data._load_pending_jobs, max_age=0.0)
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def add_job(self, next_check: float, attempt: int = 0, age_minutes: float = 0.0, status: str = "PENDING", repo="o/r") -> None:
        with contextlib.closing(db_manager.open_database()) as connection:
            job_id = db_manager.enqueue_job(connection, repo, f"v{next_check}{attempt}{age_minutes}", "Release", next_check_time=next_check)
            connection.execute(
                "UPDATE job_queue SET status = ?, attempt_count = ?, created_at = ? WHERE id = ?",
                (status, attempt, NOW - age_minutes * MINUTE, job_id),
            )
            connection.commit()

    def test_an_empty_queue(self) -> None:
        with contextlib.closing(db_manager.open_database()):
            pass
        self.assertEqual(gui_data.load_queue_counts(NOW), gui_data.QueueCounts(0, 0))

    def test_due_jobs_are_the_ones_whose_time_has_come(self) -> None:
        for when in (100.0, 200.0, 300.0, 5000.0):
            self.add_job(when, age_minutes=0.0)
        self.assertEqual((lambda c: (c.pending, c.due))(gui_data.load_queue_counts(250.0)), (4, 2))
        self.assertEqual(gui_data.load_queue_counts(300.0).due, 3)  # "due" includes right now
        self.assertEqual(gui_data.load_queue_counts(99.0).due, 0)
        self.assertEqual(gui_data.load_queue_counts(9999.0).due, 4)

    def test_only_pending_jobs_count(self) -> None:
        self.add_job(10.0)
        self.add_job(20.0)
        for status in ("COMPLETED", "FAILED", "SUPERSEDED"):
            self.add_job(30.0, status=status)
        counts = gui_data.load_queue_counts(NOW)
        self.assertEqual((counts.pending, counts.due), (2, 2))

    def test_jobs_added_later_are_seen(self) -> None:
        self.add_job(10.0)
        self.assertEqual(gui_data.load_queue_counts(NOW).pending, 1)
        self.add_job(20.0)
        self.add_job(30.0)
        self.assertEqual(gui_data.load_queue_counts(NOW).pending, 3)

    def test_the_figure_matches_the_one_the_daemon_acts_on(self) -> None:
        """The footer's "due now" is the daemon's "Processing N due queue job(s)": same table, same comparison."""
        for when in (100.0, 200.0, 300.0):
            self.add_job(when)
        with contextlib.closing(db_manager.open_database()) as connection:
            daemon_due = len(db_manager.get_due_jobs(connection, 250.0))
        self.assertEqual(gui_data.load_queue_counts(250.0).due, daemon_due)

    # ----- behind schedule
    def test_a_job_whose_next_step_is_also_past_is_behind(self) -> None:
        # created 200 min ago, one attempt done: its step 2 was due at 15 min, step 3 at 30 ... all long past
        self.add_job(NOW - 10, attempt=1, age_minutes=200)
        counts = gui_data.load_queue_counts(NOW)
        self.assertEqual((counts.due, counts.behind), (1, 1))
        self.assertEqual(counts.catch_up_polls, 5)  # steps at 15, 30, 60, 120 minutes are past, plus the current one

    def test_a_job_on_schedule_is_not_behind(self) -> None:
        # created 10 min ago, one attempt done: this poll's step (5 min) is done, the next is at 15 min: still ahead
        self.add_job(NOW - 5 * MINUTE, attempt=1, age_minutes=10)
        counts = gui_data.load_queue_counts(NOW)
        self.assertEqual((counts.due, counts.behind, counts.catch_up_polls), (1, 0, 1))

    def test_the_last_step_leaves_nothing_behind(self) -> None:
        self.add_job(NOW - 10, attempt=len(INTERVALS), age_minutes=500)  # this poll finishes the job
        counts = gui_data.load_queue_counts(NOW)
        self.assertEqual((counts.due, counts.behind, counts.catch_up_polls), (1, 0, 1))

    def test_a_job_that_is_not_due_is_never_behind(self) -> None:
        self.add_job(NOW + 10 * MINUTE, attempt=1, age_minutes=2)
        counts = gui_data.load_queue_counts(NOW)
        self.assertEqual((counts.pending, counts.due, counts.behind, counts.catch_up_polls), (1, 0, 0, 0))

    def test_a_mix_reports_the_slowest_job_for_the_catching_up(self) -> None:
        self.add_job(NOW - 10, attempt=1, age_minutes=200)  # behind, 5 polls
        self.add_job(NOW - 10, attempt=3, age_minutes=200)  # behind, steps at 60 and 120 past: 3 polls
        self.add_job(NOW - 10, attempt=5, age_minutes=200)  # on its last step
        self.add_job(NOW + 99 * MINUTE, attempt=1, age_minutes=1)  # later
        counts = gui_data.load_queue_counts(NOW)
        self.assertEqual((counts.pending, counts.due, counts.behind, counts.catch_up_polls), (4, 3, 2, 5))

    def test_the_lag_follows_the_clock_without_another_database_read(self) -> None:
        self.add_job(NOW - 5 * MINUTE, attempt=1, age_minutes=10)  # on schedule now
        self.assertEqual(gui_data.load_queue_counts(NOW).behind, 0)
        self.assertEqual(gui_data.load_queue_counts(NOW + 10 * MINUTE).behind, 1)  # 10 minutes on, step 2 has passed too


if __name__ == "__main__":
    unittest.main()
