"""Tests for the graceful stop request and the published current job.

Nothing here touches the real mailbox, daemon lock or state.db.
Run from the project root: python -m unittest discover -s tests -t .
"""
import contextlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

import main
from modules import daemon_control, daemon_lock, db_manager


class PollingLoopStopTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)  # the loop leaves its connection open
        self.addCleanup(self._temp_dir.cleanup)
        db_path = os.path.join(self._temp_dir.name, "state.db")
        for target, replacement in (
            (db_manager, mock.patch.object(db_manager, "get_state_db_path", lambda: db_path)),
            (main, mock.patch.object(main, "update_daemon_status")),
            (main, mock.patch.object(main, "is_dry_run", lambda: False)),
            (main, mock.patch.object(main, "get_destination_check_every_n_polls", lambda: 0)),
            (main, mock.patch.object(main, "run_scheduled_backup", lambda: None)),  # never write a real backup
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    def run_loop(self) -> str:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            main.run_polling_loop(interval_seconds=600, jitter_min_seconds=0, jitter_max_seconds=0)
        return output.getvalue()

    def test_stop_during_the_countdown_exits_the_loop(self) -> None:
        with mock.patch.object(main, "run_ingest_and_queue_cycle"):
            timer = threading.Timer(0.3, daemon_control.request_stop)
            timer.start()
            self.addCleanup(timer.cancel)
            started = time.monotonic()

            output = self.run_loop()

        self.assertLess(time.monotonic() - started, 5)
        self.assertIn("Stop requested", output)

    def test_stop_during_a_cycle_exits_without_waiting(self) -> None:
        calls = []

        def cycle(connection, token, should_pause=None):
            calls.append(1)
            daemon_control.request_stop()
            assert should_pause is not None
            self.assertTrue(should_pause())  # the next safe boundary sees the stop

        with mock.patch.object(main, "run_ingest_and_queue_cycle", cycle):
            started = time.monotonic()
            output = self.run_loop()

        self.assertEqual(len(calls), 1)  # no second cycle
        self.assertLess(time.monotonic() - started, 3)
        self.assertIn("Stop requested", output)

    def test_stop_request_from_an_earlier_run_does_not_stop_a_new_daemon(self) -> None:
        daemon_control.request_stop()  # left behind in state.db by a previous run
        cycles = []

        def cycle(connection, token, should_pause=None):
            cycles.append(1)
            if len(cycles) == 2:
                raise KeyboardInterrupt  # end the test loop after the second cycle

        with mock.patch.object(main, "run_ingest_and_queue_cycle", cycle):
            with mock.patch.object(daemon_control.ControlWatcher, "wait", return_value="elapsed"):
                with self.assertRaises(KeyboardInterrupt):
                    self.run_loop()

        self.assertEqual(len(cycles), 2)


class CurrentJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.status_path = os.path.join(self._temp_dir.name, "status.json")
        patcher = mock.patch.object(daemon_lock, "get_daemon_status_path", lambda: self.status_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(setattr, daemon_lock, "_lock_held", False)

    def read_status(self) -> dict:
        with open(self.status_path, encoding="utf-8") as handle:
            return json.load(handle)

    def test_without_the_daemon_lock_nothing_is_written(self) -> None:
        with daemon_lock.publishing_current_job("o/r", "v1"):
            pass
        self.assertFalse(os.path.exists(self.status_path))

    def test_job_is_published_while_running_and_cleared_after(self) -> None:
        daemon_lock._lock_held = True
        with daemon_lock.publishing_current_job("o/r", "v1"):
            current = self.read_status()["current_job"]
            self.assertEqual((current["repo"], current["tag"]), ("o/r", "v1"))
        self.assertIsNone(self.read_status()["current_job"])

    def test_job_is_cleared_even_when_the_download_raises(self) -> None:
        daemon_lock._lock_held = True
        with self.assertRaises(RuntimeError):
            with daemon_lock.publishing_current_job("o/r", "v1"):
                raise RuntimeError("boom")
        self.assertIsNone(self.read_status()["current_job"])

    def test_status_reports_current_job_only_for_a_running_daemon(self) -> None:
        daemon_lock.update_daemon_status(current_job={"repo": "o/r", "tag": "v1", "since": 1.0})
        with mock.patch.object(daemon_lock, "is_daemon_running", lambda: False):
            self.assertIsNone(daemon_lock.get_daemon_status()["current_job"])
        with mock.patch.object(daemon_lock, "is_daemon_running", lambda: True):
            self.assertEqual((daemon_lock.get_daemon_status()["current_job"] or {})["repo"], "o/r")


if __name__ == "__main__":
    unittest.main()
