"""Tests for the daemon control channel (pause/resume and forced polls).

Run from the project root: python -m unittest discover -s tests -t .
"""
import contextlib
import io
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from modules import daemon_control, db_manager, queue_worker
from modules.daemon_control import ControlWatcher


class FakeTime:
    """Deterministic clock: sleeping just advances it."""

    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _state(paused=False, request=None):
    return {"paused": paused, "poll_now_request": request}


class ControlStateTests(unittest.TestCase):
    """Control state round trips through a temporary state.db."""

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        db_path = os.path.join(self._temp_dir.name, "state.db")
        patcher = mock.patch.object(db_manager, "get_state_db_path", lambda: db_path)
        patcher.start()
        self.addCleanup(patcher.stop)

        # A long-lived connection, like the daemon's.
        self.connection = db_manager.open_database()
        self.addCleanup(self.connection.close)

    def read(self):
        return daemon_control.read_control_state(self.connection)

    def test_defaults_before_any_write(self) -> None:
        self.assertEqual(self.read(), _state())

    def test_set_paused_round_trip_keeps_request(self) -> None:
        stamp = daemon_control.request_poll_now()
        daemon_control.set_paused(True)
        self.assertEqual(self.read(), _state(True, stamp))
        daemon_control.set_paused(False)
        self.assertEqual(self.read(), _state(False, stamp))

    def test_pause_before_any_request_leaves_request_empty(self) -> None:
        daemon_control.set_paused(True)
        self.assertEqual(self.read(), _state(True, None))

    def test_requests_get_distinct_stamps(self) -> None:
        with mock.patch.object(daemon_control.time, "time", return_value=1000.0):
            first = daemon_control.request_poll_now()
            second = daemon_control.request_poll_now()
        self.assertNotEqual(first, second)
        self.assertEqual(self.read()["poll_now_request"], second)

    def test_reset_paused_on_startup_clears_only_pause(self) -> None:
        stamp = daemon_control.request_poll_now()
        daemon_control.set_paused(True)
        daemon_control.reset_paused_on_startup(self.connection)
        self.assertEqual(self.read(), _state(False, stamp))

    def test_watcher_sees_writes_from_another_connection(self) -> None:
        fake = FakeTime()
        watcher = ControlWatcher(connection=self.connection, clock=fake.clock, sleep=fake.sleep)
        original_sleep = fake.sleep

        def sleep_then_request(seconds: float) -> None:
            original_sleep(seconds)
            if fake.now >= 3:
                daemon_control.request_poll_now()

        watcher._sleep = sleep_then_request
        self.assertEqual(watcher.wait(600), "forced")
        self.assertLess(fake.now, 6)

    def test_watcher_keeps_last_state_when_database_errors(self) -> None:
        daemon_control.set_paused(True)
        calls = {"n": 0}
        real_read = daemon_control.read_control_state

        def flaky(connection):
            calls["n"] += 1
            if calls["n"] > 1:
                raise sqlite3.OperationalError("database is locked")
            return real_read(connection)

        with mock.patch.object(daemon_control, "read_control_state", flaky):
            reader = ControlWatcher._make_database_reader(self.connection)
            self.assertTrue(reader()["paused"])
            self.assertTrue(reader()["paused"])  # error -> last known state


class ControlWatcherTests(unittest.TestCase):
    def make_watcher(self, state_for_time, fake=None):
        fake = fake or FakeTime()
        watcher = ControlWatcher(
            read_state=lambda: state_for_time(fake.now),
            clock=fake.clock,
            sleep=fake.sleep,
            tick_seconds=1.0,
        )
        return watcher, fake

    def test_elapses_after_interval(self) -> None:
        watcher, fake = self.make_watcher(lambda t: _state())
        self.assertEqual(watcher.wait(10), "elapsed")
        self.assertGreaterEqual(fake.now, 10)
        self.assertLess(fake.now, 11)

    def test_countdown_freezes_while_paused(self) -> None:
        # Paused from t=3 to t=50; a 10s interval must still need 7s after resuming.
        watcher, fake = self.make_watcher(lambda t: _state(paused=3 <= t < 50))
        self.assertEqual(watcher.wait(10), "elapsed")
        self.assertGreaterEqual(fake.now, 50 + 6)
        self.assertLess(fake.now, 50 + 9)

    def test_forced_poll_ends_wait_early(self) -> None:
        watcher, fake = self.make_watcher(lambda t: _state(request=5.0 if t >= 4 else None))
        self.assertEqual(watcher.wait(600), "forced")
        self.assertLess(fake.now, 6)
        self.assertEqual(watcher.last_handled_request, 5.0)

    def test_request_already_in_file_at_startup_is_ignored(self) -> None:
        watcher, _ = self.make_watcher(lambda t: _state(request=7.0))
        self.assertEqual(watcher.wait(5), "elapsed")

    def test_handled_request_does_not_fire_twice(self) -> None:
        watcher, _ = self.make_watcher(lambda t: _state(request=5.0 if t >= 1 else None))
        self.assertEqual(watcher.wait(600), "forced")
        self.assertEqual(watcher.wait(5), "elapsed")

    def test_request_with_earlier_stamp_still_fires(self) -> None:
        # Clock stepped backwards: a "different" stamp counts even if it is lower.
        watcher, _ = self.make_watcher(lambda t: _state(request=100.0 if t < 2 else 50.0))
        self.assertEqual(watcher.wait(600), "forced")
        self.assertEqual(watcher.last_handled_request, 50.0)

    def test_request_during_pause_waits_for_resume(self) -> None:
        def state(t):
            return _state(paused=2 <= t < 20, request=5.0 if t >= 3 else None)

        watcher, fake = self.make_watcher(state)
        self.assertEqual(watcher.wait(600), "forced")
        self.assertGreaterEqual(fake.now, 20)
        self.assertLess(fake.now, 22)

    def test_on_change_reports_start_and_flips(self) -> None:
        calls = []
        watcher, _ = self.make_watcher(lambda t: _state(paused=2 <= t < 5))
        watcher.wait(8, on_change=lambda paused, next_at: calls.append((paused, next_at is None)))
        self.assertEqual(calls, [(False, False), (True, True), (False, False)])

    def test_disabled_watcher_ignores_control_file(self) -> None:
        def explode():
            raise AssertionError("control file must not be read")

        fake = FakeTime()
        watcher = ControlWatcher(enabled=False, read_state=explode, clock=fake.clock, sleep=fake.sleep)
        self.assertEqual(watcher.wait(3), "elapsed")


class PauseCheckpointTests(unittest.TestCase):
    def test_checkpoint_flags_interruption_only_when_paused(self) -> None:
        paused = {"value": False}
        watcher = ControlWatcher(read_state=lambda: _state(paused["value"]))
        self.assertFalse(watcher.checkpoint())
        self.assertFalse(watcher.cycle_interrupted)

        paused["value"] = True
        self.assertTrue(watcher.checkpoint())
        self.assertTrue(watcher.cycle_interrupted)

        watcher.begin_cycle()
        self.assertFalse(watcher.cycle_interrupted)

    def test_zero_second_wait_holds_while_paused_then_returns_on_resume(self) -> None:
        fake = FakeTime()
        watcher = ControlWatcher(
            read_state=lambda: _state(paused=fake.now < 30),
            clock=fake.clock,
            sleep=fake.sleep,
        )
        self.assertEqual(watcher.wait(0), "elapsed")
        self.assertGreaterEqual(fake.now, 30)
        self.assertLess(fake.now, 32)


class PausedCycleTests(unittest.TestCase):
    """A pause request stops work at the next boundary and leaves the rest untouched."""

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        db_path = os.path.join(self._temp_dir.name, "state.db")
        patcher = mock.patch.object(db_manager, "get_state_db_path", lambda: db_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.connection = db_manager.open_database()
        self.addCleanup(self.connection.close)

        # The app prints emoji; capture output so a cp1252 console can't break the tests.
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def test_paused_queue_processing_leaves_jobs_pending(self) -> None:
        for tag in ("v1", "v2"):
            db_manager.enqueue_job(self.connection, "owner/repo", tag, next_check_time=1.0)

        stats = queue_worker.process_queue_once(self.connection, None, should_pause=lambda: True)

        self.assertEqual(stats["due_jobs"], 2)
        self.assertEqual(stats["completed"] + stats["failed"] + stats["retried"], 0)
        statuses = [row["status"] for row in self.connection.execute("SELECT status FROM job_queue")]
        self.assertEqual(statuses, ["PENDING", "PENDING"])
        self.assertIn("Pause requested", self.output.getvalue())

    def test_paused_ingest_leaves_emails_in_mailbox(self) -> None:
        notifications = [
            {"repo": "owner/a", "tag": "v1", "release_type": "Release", "email_id": "1"},
            {"repo": "owner/b", "tag": "v2", "release_type": "Release", "email_id": "2"},
        ]
        with mock.patch.object(queue_worker, "get_pending_notifications", return_value=notifications),                 mock.patch.object(queue_worker, "mark_as_read_and_delete") as delete_mock,                 mock.patch.object(queue_worker, "upsert_repository_mapping") as mapping_mock:
            stats, queued = queue_worker.ingest_notifications_once(
                self.connection, None, should_pause=lambda: True
            )

        self.assertIsNone(queued)
        self.assertEqual(stats["notifications_found"], 2)
        self.assertEqual(stats["notifications_queued"], 0)
        delete_mock.assert_not_called()
        mapping_mock.assert_not_called()
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM job_queue").fetchone()[0], 0)

    def test_cycle_skips_queue_processing_when_paused_after_ingest(self) -> None:
        with mock.patch.object(queue_worker, "ingest_notifications_once", return_value=(queue_worker._empty_ingest_cycle_stats(), None)),                 mock.patch.object(queue_worker, "process_queue_once") as queue_mock,                 mock.patch.object(queue_worker, "log_cycle_summary", return_value="summary"):
            queue_worker.run_ingest_and_queue_cycle(self.connection, None, should_pause=lambda: True)
        queue_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
