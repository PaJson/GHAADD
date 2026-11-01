"""The "Finish now" button's logic: skipping the waiting re-checks of one pending job.

Covers the db functions (`finish_job_now`, `get_pending_jobs_for_repo`), the picker lines and messages of
`gui_finish`, and that the worker really finalizes a job that was finished early. The dialog itself is not drawn.
"""
import time
import unittest
from typing import Any

from modules import db_manager, gui_finish
from tests.test_queue_worker_flow import REPO, WorkerTestCase, download_result


class FinishNowTests(WorkerTestCase):
    """Runs on the worker tests' fake world: two re-check steps ([5, 15] minutes), state.db in a temp folder."""

    def test_finishing_puts_the_job_on_its_last_step_and_makes_it_due(self) -> None:
        job_id = self.job()
        self.at_attempt(job_id, 1, due=time.time() + 9999)
        now = time.time()
        self.assertTrue(db_manager.finish_job_now(self.connection, job_id, 2, now=now))
        row = self.row(job_id)
        self.assertEqual((row["status"], row["attempt_count"], row["next_check_time"]), ("PENDING", 2, now))

    def test_finishing_never_lowers_the_attempt_or_postpones_the_check(self) -> None:
        job_id = self.job()
        self.at_attempt(job_id, 3, due=5.0)
        db_manager.finish_job_now(self.connection, job_id, 2, now=1000.0)
        row = self.row(job_id)
        self.assertEqual((row["attempt_count"], row["next_check_time"]), (3, 5.0))

    def test_a_job_that_is_not_pending_is_left_alone(self) -> None:
        job_id = self.job()
        db_manager.mark_job_completed(self.connection, job_id, 2, 1, 1, 2, "SUCCESS")
        self.assertFalse(db_manager.finish_job_now(self.connection, job_id, 2))
        self.assertEqual(self.row(job_id)["status"], "COMPLETED")

    def test_the_next_poll_finalizes_a_job_that_was_finished_early(self) -> None:
        job_id = self.job()
        working_dir = self.staged_dir()
        self.at_attempt(job_id, 1, working_dir=working_dir, due=time.time() + 9999)
        self.assertEqual(self.pending_ids(), [job_id])
        self.assertEqual(self.process()["due_jobs"], 0)  # not due: the schedule would make it wait

        db_manager.finish_job_now(self.connection, job_id, 2)
        self.download_results = [download_result(working_dir=working_dir)]
        stats = self.process()

        row = self.row(job_id)
        self.assertEqual((row["status"], row["last_result"]), ("COMPLETED", "SUCCESS"))
        self.assertEqual(self.calls["complete"], [working_dir])
        self.assertEqual(len(self.calls["download"]), 1)  # one more real check, not a shortcut
        self.assertEqual(stats["completed"], 1)

    def test_only_the_picked_job_is_touched(self) -> None:
        older, newer = self.job(tag="v1"), self.job(tag="v2")
        later = time.time() + 9999
        self.at_attempt(older, 1, due=later)
        self.at_attempt(newer, 1, due=later)
        db_manager.finish_job_now(self.connection, newer, 2)
        self.assertEqual(self.row(older)["attempt_count"], 1)
        self.assertEqual(self.row(older)["next_check_time"], later)
        self.assertEqual(self.row(newer)["attempt_count"], 2)

    def test_pending_jobs_of_a_repository_are_listed_newest_first_ignoring_case(self) -> None:
        first, second = self.job(tag="v1"), self.job(tag="v2")
        done = self.job(tag="v0")
        db_manager.mark_job_completed(self.connection, done, 2, 1, 1, 2, "SUCCESS")
        self.job(tag="x", repo="other/app")
        rows = db_manager.get_pending_jobs_for_repo(self.connection, REPO.upper())
        self.assertEqual([row["id"] for row in rows], [second, first])

    def test_the_gui_helpers_read_and_update_the_queue(self) -> None:
        older, newer = self.job(tag="v1"), self.job(tag="v2")
        choices = gui_finish.load_choices(REPO)
        self.assertEqual([choice.job_id for choice in choices], [newer, older])
        self.assertTrue(gui_finish.finish(REPO, newer))
        self.assertEqual(self.row(newer)["attempt_count"], 2)  # two steps in the patched mapping
        self.assertEqual(self.row(older)["attempt_count"], 0)


class PickerTextTests(unittest.TestCase):
    def rows(self) -> list[dict[str, Any]]:
        return [
            {"id": 9, "tag": "v2", "release_type": "Release", "attempt_count": 1, "next_check_time": 0.0},
            {"id": 4, "tag": "nightly", "release_type": "Prerelease", "attempt_count": 3, "next_check_time": 4_000_000_000.0},
        ]

    def test_each_line_names_the_release_the_step_and_the_next_check(self) -> None:
        choices = gui_finish.choices_from_rows(self.rows(), steps=5, now=100.0)
        self.assertEqual([choice.job_id for choice in choices], [9, 4])
        self.assertIn("v2", choices[0].label)
        self.assertIn("step 1 / 5", choices[0].label)
        self.assertIn("due now", choices[0].label)
        self.assertIn("(P) nightly", choices[1].label)
        self.assertIn("step 3 / 5", choices[1].label)
        self.assertNotIn("due now", choices[1].label)

    def test_busy_daemon_blocks_the_button(self) -> None:
        self.assertEqual(gui_finish.blocked_reason("o/app", False), "")
        self.assertIn("busy with o/app", gui_finish.blocked_reason("o/app", True))
        self.assertIn("busy", gui_finish.blocked_reason(None, True))

    def test_messages_say_whether_a_poll_was_requested(self) -> None:
        self.assertIn("poll was requested", gui_finish.result_text("o/app", True, None))
        self.assertIn("next runs", gui_finish.result_text("o/app", False, None))
        self.assertIn("Poll now failed: boom", gui_finish.result_text("o/app", True, "boom"))

    def test_intro_text_follows_the_number_of_jobs(self) -> None:
        self.assertIn("this job", gui_finish.intro_text("o/app", 1))
        self.assertIn("one of these jobs", gui_finish.intro_text("o/app", 2))


if __name__ == "__main__":
    unittest.main()
