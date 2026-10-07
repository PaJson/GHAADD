"""The queue job in progress ("Processing 3 of 36: owner/repo (Release) tag @ commit") published for the GUI.

Fakes only: no network, and the real status file is never touched. Run from the project root:
python -m unittest discover -s tests -t .
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from modules import daemon_lock, gui_daemon, queue_worker
from tests.test_queue_worker_flow import REPO, WorkerTestCase


class WorkerPublishesProgressTests(WorkerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.published: list[tuple] = []
        self.cleared = 0
        for name, value in (
            ("publish_queue_progress", lambda *args: self.published.append(args)),
            ("clear_queue_progress", lambda: setattr(self, "cleared", self.cleared + 1)),
        ):
            patcher = mock.patch.object(queue_worker, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_each_job_is_published_in_order_with_its_details(self) -> None:
        self.job(tag="a", release_type="Pre-release", commit="aaa1111")
        self.job(tag="b", release_type="Release", commit="aaa1111", due=0.5)
        self.process()
        self.assertEqual(
            self.published,
            [(1, 2, REPO, "a", "Pre-release", "aaa1111"), (2, 2, REPO, "b", "Release", "aaa1111")],
        )
        self.assertEqual(self.cleared, 1)  # once, when the cycle is over

    def test_nothing_due_publishes_nothing_but_still_clears(self) -> None:
        self.process()
        self.assertEqual(self.published, [])
        self.assertEqual(self.cleared, 1)

    def test_it_is_cleared_when_a_pause_stops_the_cycle(self) -> None:
        self.job(tag="a")
        self.process(should_pause=lambda: True)
        self.assertEqual(self.published, [])
        self.assertEqual(self.cleared, 1)

    def test_it_is_cleared_when_the_cycle_crashes(self) -> None:
        self.job(tag="a")
        with mock.patch.object(queue_worker, "get_current_commit_hash", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.process()
        self.assertEqual(len(self.published), 1)  # it got as far as the job
        self.assertEqual(self.cleared, 1)


class StatusFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._temp.cleanup)
        self.path = os.path.join(self._temp.name, "status.json")
        patcher = mock.patch.object(daemon_lock, "get_daemon_status_path", lambda: self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(setattr, daemon_lock, "_lock_held", False)

    def status(self) -> dict:
        with open(self.path, encoding="utf-8") as handle:
            return json.load(handle)

    def test_without_the_daemon_lock_nothing_is_written(self) -> None:
        daemon_lock.publish_queue_progress(1, 2, "o/r", "v1")
        daemon_lock.clear_queue_progress()
        self.assertFalse(os.path.exists(self.path))

    def test_published_then_cleared(self) -> None:
        daemon_lock._lock_held = True
        daemon_lock.publish_queue_progress(3, 36, "o/r", "v1", "Release", "abc1234")
        progress = self.status()["queue_progress"]
        self.assertEqual((progress["index"], progress["total"], progress["repo"]), (3, 36, "o/r"))
        self.assertEqual((progress["tag"], progress["release_type"], progress["commit"]), ("v1", "Release", "abc1234"))
        self.assertIn("since", progress)
        daemon_lock.clear_queue_progress()
        self.assertIsNone(self.status()["queue_progress"])

    def test_the_status_reader_returns_it_only_for_a_running_daemon(self) -> None:
        daemon_lock._lock_held = True
        daemon_lock.publish_queue_progress(1, 5, "o/r", "v1")
        with mock.patch.object(daemon_lock, "is_daemon_running", return_value=True):
            progress = daemon_lock.get_daemon_status()["queue_progress"]
            self.assertIsNotNone(progress)
            self.assertEqual((progress or {})["total"], 5)
        with mock.patch.object(daemon_lock, "is_daemon_running", return_value=False):
            self.assertIsNone(daemon_lock.get_daemon_status()["queue_progress"])


class GuiTextTests(unittest.TestCase):
    PROGRESS = gui_daemon.QueueProgress(3, 36, "RPCS3/rpcs3-binaries-win", "build-2bf8677308020508a78f2", "Release", "2bf8677308020508", 100.0)

    def test_the_raw_status_is_checked_before_it_is_believed(self) -> None:
        good = {"index": 3, "total": 36, "repo": "o/r", "tag": "v1", "release_type": "Release", "commit": "abc", "since": 5.0}
        self.assertEqual(gui_daemon.parse_progress(good), gui_daemon.QueueProgress(3, 36, "o/r", "v1", "Release", "abc", 5.0))
        for bad in (None, "x", {}, {"index": 0, "total": 5, "repo": "o/r"}, {"index": 6, "total": 5, "repo": "o/r"},
                    {"index": "a", "total": 5, "repo": "o/r"}, {"index": 1, "total": 5, "repo": ""}, {"index": 1, "total": 5}):
            self.assertIsNone(gui_daemon.parse_progress(bad), bad)

    def test_the_footer_line_has_the_count_the_repo_type_tag_and_commit(self) -> None:
        text = gui_daemon.progress_summary(self.PROGRESS)
        self.assertEqual(text, "Processing 3 of 36: RPCS3/rpcs3-binaries-win (Release) build-2bf8677308020508a… @ 2bf8677")
        self.assertEqual(len(text.split(" (Release) ")[1].split(" @ ")[0]), 24)  # the tag is cut to 24 characters
        self.assertEqual(gui_daemon.progress_summary(None), "")

    def test_missing_details_are_left_out(self) -> None:
        bare = gui_daemon.QueueProgress(1, 1, "o/r")
        self.assertEqual(gui_daemon.progress_summary(bare), "Processing 1 of 1: o/r")

    def test_the_hover_text_is_complete(self) -> None:
        text = gui_daemon.progress_detail(self.PROGRESS, now=165.0)
        for expected in (
            "job 3 of 36", "Repository: RPCS3/rpcs3-binaries-win", "Release type: Release",
            "Tag: build-2bf8677308020508a78f2", "Commit: 2bf8677308020508", "On this job for 01:05",
        ):
            self.assertIn(expected, text)

    def test_while_a_job_is_in_progress_the_countdown_gives_way_to_the_footer_text(self) -> None:
        snapshot = gui_daemon.DaemonSnapshot(running=True, pid=7, progress=self.PROGRESS, current_repo="o/r")
        view = gui_daemon.build_view(snapshot, 200.0)
        self.assertEqual(view.countdown_text, "")  # it would only repeat what the footer's middle says
        self.assertEqual(view.status_text, "Daemon running (PID 7)")
        self.assertTrue(view.stop_enabled)

    def test_paused_and_idle_wording_still_win_over_the_progress(self) -> None:
        paused = gui_daemon.DaemonSnapshot(running=True, paused=True, progress=self.PROGRESS)
        self.assertEqual(gui_daemon.build_view(paused, 200.0).countdown_text, "Polling paused")

    def test_without_progress_the_old_texts_are_unchanged(self) -> None:
        self.assertEqual(
            gui_daemon.build_view(gui_daemon.DaemonSnapshot(running=True, current_repo="o/r"), 1.0).countdown_text,
            "Processing o/r",
        )
        self.assertEqual(
            gui_daemon.build_view(gui_daemon.DaemonSnapshot(running=True, next_poll_at=65.0), 5.0).countdown_text,
            "Next poll in 01:00",
        )


if __name__ == "__main__":
    unittest.main()
