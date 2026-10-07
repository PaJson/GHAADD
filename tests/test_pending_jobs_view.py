"""A repository can have several pending jobs (one per build/release): the Recheck cell says so and lists them.

A temporary state.db is used. Run from the project root: python -m unittest discover -s tests -t .
"""
import contextlib
import os
import tempfile
import unittest
from unittest import mock

from modules import db_manager, repo_overview

NOW = 1_000_000.0


class FormatTests(unittest.TestCase):
    def test_the_step_shows_the_other_waiting_jobs(self) -> None:
        self.assertEqual(repo_overview.format_step(2, 5, 1), "2 / 5")
        self.assertEqual(repo_overview.format_step(2, 5, 0), "2 / 5")
        self.assertEqual(repo_overview.format_step(2, 5, 3), "2 / 5 (+2)")
        self.assertEqual(repo_overview.format_step(None, 5, 2), "0 / 5 (+1)")

    def test_the_note_lists_every_job_earliest_first_and_marks_the_due_ones(self) -> None:
        jobs = [
            {"tag": "build-aaa", "release_type": "Release", "attempts": 4, "next_check": NOW - 600},
            {"tag": "build-bbb", "release_type": "Pre-release", "attempts": 1, "next_check": NOW + 600},
        ]
        note = repo_overview.format_pending_note(jobs, 5, NOW)
        lines = note.splitlines()
        self.assertIn("2 jobs of this repository are waiting", lines[0])
        self.assertTrue(lines[1].startswith("• (R) build-aaa: step 4 / 5, next check "))
        self.assertTrue(lines[1].endswith("(due now)"))
        self.assertTrue(lines[2].startswith("• (P) build-bbb: step 1 / 5, next check "))
        self.assertFalse(lines[2].endswith("(due now)"))

    def test_one_job_or_none_needs_no_note(self) -> None:
        self.assertEqual(repo_overview.format_pending_note([], 5, NOW), "")
        self.assertEqual(repo_overview.format_pending_note([{"tag": "v1", "attempts": 1, "next_check": NOW}], 5, NOW), "")


class RowTests(unittest.TestCase):
    def summary(self, **fields) -> dict:
        base = {
            "last_activity": NOW, "latest_tag": "build-ccc", "latest_release_type": "Release", "latest_status": "PENDING",
            "latest_updated_at": NOW, "latest_downloaded": 3, "latest_skipped": 0, "latest_total": 3,
            "pending_next_check": NOW - 60, "pending_attempts": 2, "pending_count": 1, "pending_jobs": [],
        }
        base.update(fields)
        return base

    def rows(self, summary: dict):
        return repo_overview.build_rows([{"repository": "o/r"}], {"o/r": summary}, NOW, lambda entry: 5)

    def test_several_pending_jobs_show_in_the_recheck_cell_and_the_note(self) -> None:
        jobs = [
            {"tag": "build-aaa", "release_type": "Release", "attempts": 2, "next_check": NOW - 60},
            {"tag": "build-bbb", "release_type": "Release", "attempts": 1, "next_check": NOW - 30},
            {"tag": "build-ccc", "release_type": "Release", "attempts": 1, "next_check": NOW - 10},
        ]
        (row,) = self.rows(self.summary(pending_count=3, pending_jobs=jobs))
        self.assertEqual((row.step, row.pending_count), ("2 / 5 (+2)", 3))
        self.assertEqual(len(row.pending_note.splitlines()), 4)
        self.assertEqual(row.tag, "(R) build-ccc")  # the Tag cell still shows the newest release

    def test_one_pending_job_looks_as_before(self) -> None:
        (row,) = self.rows(self.summary())
        self.assertEqual((row.step, row.pending_count, row.pending_note), ("2 / 5", 1, ""))

    def test_nothing_pending_has_no_step(self) -> None:
        (row,) = self.rows(self.summary(pending_next_check=None, pending_attempts=0, pending_count=0))
        self.assertEqual((row.step, row.pending_count), ("-", 0))

    def test_a_summary_from_before_the_counts_existed_still_works(self) -> None:
        old = self.summary()
        del old["pending_count"], old["pending_jobs"]
        (row,) = self.rows(old)
        self.assertEqual((row.step, row.pending_note), ("2 / 5", ""))


class DatabaseSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._temp.cleanup)
        patcher = mock.patch.object(db_manager, "get_state_db_path", lambda: os.path.join(self._temp.name, "state.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.connection = db_manager.open_database()
        self.addCleanup(self.connection.close)

    def add(self, repo: str, tag: str, due: float, attempts: int = 0, status: str = "PENDING") -> None:
        job_id = db_manager.enqueue_job(self.connection, repo, tag, "Release", next_check_time=due)
        self.connection.execute(
            "UPDATE job_queue SET attempt_count = ?, status = ? WHERE id = ?", (attempts, status, job_id)
        )
        self.connection.commit()

    def test_every_pending_job_of_a_repository_is_counted_and_listed_earliest_first(self) -> None:
        self.add("Owner/Repo", "newest", 300.0, attempts=1)
        self.add("Owner/Repo", "oldest", 100.0, attempts=4)
        self.add("Owner/Repo", "middle", 200.0, attempts=2)
        summary = db_manager.get_repo_job_summaries(self.connection)["owner/repo"]
        self.assertEqual(summary["pending_count"], 3)
        self.assertEqual([job["tag"] for job in summary["pending_jobs"]], ["oldest", "middle", "newest"])
        self.assertEqual(summary["pending_next_check"], 100.0)  # what the row's Next check cell shows
        self.assertEqual(summary["pending_attempts"], 4)  # ... and its Recheck cell
        self.assertEqual(summary["pending_jobs"][0]["attempts"], 4)
        self.assertEqual(summary["latest_tag"], "middle")  # the row's Tag cell: the newest by id, not by check time

    def test_finished_and_superseded_jobs_are_not_pending(self) -> None:
        self.add("o/r", "a", 100.0)
        for status in ("COMPLETED", "FAILED", "SUPERSEDED"):
            self.add("o/r", f"x-{status}", 50.0, status=status)
        summary = db_manager.get_repo_job_summaries(self.connection)["o/r"]
        self.assertEqual((summary["pending_count"], len(summary["pending_jobs"])), (1, 1))

    def test_a_repository_without_pending_jobs_has_none(self) -> None:
        self.add("o/r", "a", 100.0, status="COMPLETED")
        summary = db_manager.get_repo_job_summaries(self.connection)["o/r"]
        self.assertEqual((summary["pending_count"], summary["pending_jobs"], summary["pending_next_check"]), (0, [], None))

    def test_the_repositories_do_not_mix(self) -> None:
        self.add("a/one", "t1", 100.0)
        self.add("b/two", "t2", 200.0)
        self.add("b/two", "t3", 300.0)
        summaries = db_manager.get_repo_job_summaries(self.connection)
        self.assertEqual((summaries["a/one"]["pending_count"], summaries["b/two"]["pending_count"]), (1, 2))


if __name__ == "__main__":
    unittest.main()
