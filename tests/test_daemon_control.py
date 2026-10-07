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


def _state(paused=False, request=None, log_override=None, stop=None, check=None, single=None):
    return {
        "paused": paused, "poll_now_request": request, "log_override": log_override, "stop_request": stop,
        "check_folders_request": check, "single_request": single,
    }


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

    def test_stop_request_round_trip_keeps_other_fields(self) -> None:
        daemon_control.set_paused(True)
        first = db_manager.set_daemon_stop_request(self.connection, 100.0)
        second = db_manager.set_daemon_stop_request(self.connection, 100.0)  # same clock reading

        self.assertEqual(first, 100.0)
        self.assertNotEqual(second, first)  # a repeated request still differs from the previous one
        self.assertEqual(self.read(), _state(paused=True, stop=second))

    def test_existing_database_gains_stop_request_column(self) -> None:
        old_path = os.path.join(self._temp_dir.name, "old.db")
        old = sqlite3.connect(old_path)
        old.row_factory = sqlite3.Row
        old.execute("CREATE TABLE daemon_control (id INTEGER PRIMARY KEY CHECK (id = 1), paused INTEGER NOT NULL DEFAULT 0, poll_now_request REAL, log_override INTEGER)")
        old.execute("INSERT INTO daemon_control (id, paused) VALUES (1, 1)")
        old.commit()
        old.close()
        with mock.patch.object(db_manager, "get_state_db_path", lambda: old_path):
            upgraded = db_manager.open_database()
            self.addCleanup(upgraded.close)
            self.assertEqual(daemon_control.read_control_state(upgraded), _state(paused=True))

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

    def test_log_override_round_trip_keeps_pause_and_request(self) -> None:
        stamp = daemon_control.request_poll_now()
        daemon_control.set_paused(True)
        daemon_control.set_log_override(True)
        self.assertEqual(self.read(), _state(True, stamp, True))
        daemon_control.set_log_override(False)
        self.assertEqual(self.read(), _state(True, stamp, False))
        daemon_control.set_log_override(None)
        self.assertEqual(self.read(), _state(True, stamp, None))

    def test_reset_log_override_on_startup_clears_only_override(self) -> None:
        stamp = daemon_control.request_poll_now()
        daemon_control.set_log_override(True)
        daemon_control.reset_log_override_on_startup(self.connection)
        self.assertEqual(self.read(), _state(False, stamp, None))

    def test_single_request_round_trips_and_every_request_differs(self) -> None:
        first = daemon_control.request_single_poll()
        second = daemon_control.request_single_poll()
        self.assertNotEqual(first, second)
        self.assertEqual(self.read()["single_request"], second)
        self.assertIsNone(self.read()["poll_now_request"])  # the other requests are not touched

    def test_pause_writes_keep_log_override(self) -> None:
        daemon_control.set_log_override(False)
        daemon_control.set_paused(True)
        self.assertIs(self.read()["log_override"], False)

    def test_existing_database_gains_log_override_column(self) -> None:
        # A state.db created by v1.1 has no log_override column.
        self.connection.execute("DROP TABLE daemon_control")
        self.connection.execute(
            "CREATE TABLE daemon_control (id INTEGER PRIMARY KEY CHECK (id = 1), "
            "paused INTEGER NOT NULL DEFAULT 0, poll_now_request REAL)"
        )
        self.connection.execute("INSERT INTO daemon_control (id, paused) VALUES (1, 1)")
        self.connection.commit()
        with contextlib.closing(db_manager.open_database()) as upgraded:
            self.assertEqual(daemon_control.read_control_state(upgraded), _state(True, None, None))

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

    def test_single_poll_request_ends_the_wait_as_single(self) -> None:
        watcher, fake = self.make_watcher(lambda t: _state(single=5.0 if t >= 4 else None))
        self.assertEqual(watcher.wait(600), "single")
        self.assertLess(fake.now, 6)
        self.assertFalse(watcher.forced_while_paused)

    def test_single_poll_request_works_while_paused_and_idle(self) -> None:
        watcher, fake = self.make_watcher(lambda t: _state(paused=True, single=5.0 if t >= 3 else None))
        self.assertEqual(watcher.wait(600), "single")
        self.assertTrue(watcher.forced_while_paused)  # the pause is not allowed to interrupt that one item
        watcher, fake = self.make_watcher(lambda t: _state(single=5.0 if t >= 3 else None))
        self.assertEqual(watcher.wait(0, idle=True), "single")

    def test_a_single_request_is_used_up_and_one_from_before_startup_is_ignored(self) -> None:
        watcher, _ = self.make_watcher(lambda t: _state(single=7.0))
        self.assertEqual(watcher.wait(5), "elapsed")
        watcher, _ = self.make_watcher(lambda t: _state(single=5.0 if t >= 1 else None))
        self.assertEqual(watcher.wait(600), "single")
        self.assertEqual(watcher.wait(5), "elapsed")

    def test_poll_now_wins_when_both_are_requested(self) -> None:
        watcher, _ = self.make_watcher(lambda t: _state(request=5.0 if t >= 2 else None, single=6.0 if t >= 2 else None))
        self.assertEqual(watcher.wait(600), "forced")
        self.assertEqual(watcher.wait(600), "single")  # the other one is still waiting its turn

    def test_idle_wait_has_no_countdown_and_ends_only_with_a_poll_now(self) -> None:
        watcher, fake = self.make_watcher(lambda t: _state(request=5.0 if t >= 5000 else None))
        reported = []
        self.assertEqual(watcher.wait(0, on_change=lambda paused, at: reported.append((paused, at)), idle=True), "forced")
        self.assertGreaterEqual(fake.now, 5000)  # a normal wait(0) would have returned at once
        self.assertEqual(reported, [(False, None)])  # no next-poll time is ever published

    def test_idle_wait_ends_with_a_stop(self) -> None:
        watcher, fake = self.make_watcher(lambda t: _state(stop=9.0 if t >= 7 else None))
        self.assertEqual(watcher.wait(0, idle=True), "stop")
        self.assertLess(fake.now, 9)

    def test_idle_wait_honours_poll_now_while_paused(self) -> None:
        watcher, fake = self.make_watcher(lambda t: _state(paused=True, request=5.0 if t >= 3 else None))
        self.assertEqual(watcher.wait(0, idle=True), "forced")
        self.assertLess(fake.now, 6)
        self.assertTrue(watcher.forced_while_paused)

    def test_request_during_pause_polls_at_once_and_the_pause_goes_on(self) -> None:
        def state(t):
            return _state(paused=2 <= t, request=5.0 if t >= 3 else None, stop=9.0 if t >= 100 else None)

        watcher, fake = self.make_watcher(state)
        self.assertEqual(watcher.wait(600), "forced")
        self.assertLess(fake.now, 6)  # not held back until a resume
        self.assertTrue(watcher.forced_while_paused)
        self.assertEqual(watcher.last_handled_request, 5.0)
        # the request is used up: the next wait stays frozen (until the stop at t=100) instead of firing again
        self.assertEqual(watcher.wait(600), "stop")
        self.assertGreaterEqual(fake.now, 100)

    def test_a_poll_made_while_paused_runs_to_the_end_but_the_next_cycle_pauses_again(self) -> None:
        state = {"paused": True, "request": None}
        watcher, _ = self.make_watcher(lambda t: _state(paused=state["paused"], request=state["request"]))
        state["request"] = 5.0
        self.assertEqual(watcher.wait(600), "forced")
        watcher.begin_cycle()
        self.assertFalse(watcher.checkpoint())  # the one poll the user asked for is not interrupted by the pause
        self.assertFalse(watcher.cycle_interrupted)
        watcher.begin_cycle()  # an ordinary cycle after it
        self.assertTrue(watcher.checkpoint())
        self.assertTrue(watcher.cycle_interrupted)

    def test_a_poll_made_while_paused_still_obeys_stop(self) -> None:
        state = {"stop": None}
        watcher, _ = self.make_watcher(lambda t: _state(paused=True, request=5.0 if t >= 1 else None, stop=state["stop"]))
        self.assertEqual(watcher.wait(600), "forced")
        watcher.begin_cycle()
        state["stop"] = 9.0
        self.assertTrue(watcher.checkpoint())

    def test_a_poll_made_while_not_paused_is_not_marked(self) -> None:
        watcher, _ = self.make_watcher(lambda t: _state(request=5.0 if t >= 2 else None))
        self.assertEqual(watcher.wait(600), "forced")
        self.assertFalse(watcher.forced_while_paused)

    def test_on_change_reports_start_and_flips(self) -> None:
        calls = []
        watcher, _ = self.make_watcher(lambda t: _state(paused=2 <= t < 5))
        watcher.wait(8, on_change=lambda paused, next_at: calls.append((paused, next_at is None)))
        self.assertEqual(calls, [(False, False), (True, True), (False, False)])

    def test_stop_request_ends_wait_immediately(self) -> None:
        watcher, fake = self.make_watcher(lambda t: _state(stop=9.0 if t >= 4 else None))
        self.assertEqual(watcher.wait(600), "stop")
        self.assertLess(fake.now, 6)
        self.assertTrue(watcher.stop_requested)

    def test_stop_request_works_while_paused(self) -> None:
        watcher, fake = self.make_watcher(lambda t: _state(paused=t >= 1, stop=9.0 if t >= 5 else None))
        self.assertEqual(watcher.wait(600), "stop")
        self.assertLess(fake.now, 8)

    def test_stop_request_left_over_from_an_earlier_run_is_ignored(self) -> None:
        watcher, _ = self.make_watcher(lambda t: _state(stop=7.0))
        self.assertEqual(watcher.wait(5), "elapsed")
        self.assertFalse(watcher.stop_requested)

    def test_newer_stop_request_than_the_one_at_startup_stops(self) -> None:
        watcher, _ = self.make_watcher(lambda t: _state(stop=7.0 if t < 2 else 8.0))
        self.assertEqual(watcher.wait(600), "stop")

    def test_checkpoint_interrupts_work_when_stop_requested(self) -> None:
        fake_state = {"stop": None}
        watcher, _ = self.make_watcher(lambda t: _state(stop=fake_state["stop"]))
        self.assertFalse(watcher.checkpoint())
        fake_state["stop"] = 9.0
        self.assertTrue(watcher.checkpoint())
        self.assertTrue(watcher.cycle_interrupted)
        self.assertTrue(watcher.stop_requested)

    def test_disabled_watcher_ignores_control_file(self) -> None:
        def explode():
            raise AssertionError("control file must not be read")

        fake = FakeTime()
        watcher = ControlWatcher(enabled=False, read_state=explode, clock=fake.clock, sleep=fake.sleep)
        self.assertEqual(watcher.wait(3), "elapsed")


class CheckFoldersRequestTests(unittest.TestCase):
    """A check-folders request runs the callback once and does not disturb the countdown."""

    def make(self, state_for_time, on_check):
        fake = FakeTime()
        watcher = ControlWatcher(
            read_state=lambda: state_for_time(fake.now),
            clock=fake.clock,
            sleep=fake.sleep,
            on_check_folders=on_check,
        )
        return watcher, fake

    def test_a_new_request_runs_the_check_once_and_the_wait_continues(self) -> None:
        calls = []
        watcher, fake = self.make(lambda t: _state(check=5.0 if t >= 3 else None), lambda: calls.append(1))
        self.assertEqual(watcher.wait(10), "elapsed")
        self.assertEqual(calls, [1])
        self.assertGreaterEqual(fake.now, 10)  # the countdown was not reset

    def test_a_request_already_in_the_file_at_startup_is_ignored(self) -> None:
        calls = []
        watcher, _ = self.make(lambda t: _state(check=5.0), lambda: calls.append(1))
        watcher.wait(5)
        self.assertEqual(calls, [])

    def test_a_request_is_handled_while_paused_too(self) -> None:
        calls = []
        watcher, _ = self.make(lambda t: _state(paused=True, check=5.0 if t >= 2 else None), lambda: calls.append(1))
        with mock.patch.object(watcher, "_check_stop", side_effect=[False, False, False, False, True, True]):
            self.assertEqual(watcher.wait(600), "stop")
        self.assertEqual(calls, [1])

    def test_each_new_stamp_runs_it_again(self) -> None:
        calls = []
        watcher, _ = self.make(lambda t: _state(check=1.0 if t < 4 else 2.0) if t >= 1 else _state(), lambda: calls.append(1))
        watcher.wait(8)
        self.assertEqual(calls, [1, 1])

    def test_round_trip_through_the_database(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            with mock.patch.object(db_manager, "get_state_db_path", lambda: os.path.join(folder, "state.db")):
                self.assertIsNone(daemon_control.get_control_state()["check_folders_request"])
                first = daemon_control.request_check_folders()
                second = daemon_control.request_check_folders()
                self.assertNotEqual(first, second)
                self.assertEqual(daemon_control.get_control_state()["check_folders_request"], second)
                self.assertFalse(daemon_control.get_control_state()["paused"])


class LogOverrideWatcherTests(unittest.TestCase):
    """The watcher reports live log switches to its callback exactly once per change."""

    def make(self, override_for_time):
        fake = FakeTime()
        seen = []
        watcher = ControlWatcher(
            read_state=lambda: _state(log_override=override_for_time(fake.now)),
            clock=fake.clock,
            sleep=fake.sleep,
            on_log_override=seen.append,
        )
        return watcher, seen

    def test_wait_reports_each_change_once(self) -> None:
        watcher, seen = self.make(lambda t: True if 2 <= t < 5 else (False if t >= 5 else None))
        watcher.wait(8)
        self.assertEqual(seen, [True, False])

    def test_checkpoint_and_begin_cycle_report_changes(self) -> None:
        value = {"v": None}
        seen = []
        watcher = ControlWatcher(read_state=lambda: _state(log_override=value["v"]), on_log_override=seen.append)
        watcher.begin_cycle()
        self.assertEqual(seen, [])
        value["v"] = True
        watcher.checkpoint()
        watcher.checkpoint()
        self.assertEqual(seen, [True])
        value["v"] = None
        watcher.begin_cycle()
        self.assertEqual(seen, [True, None])

    def test_stale_override_is_reported_at_first_look(self) -> None:
        watcher, seen = self.make(lambda t: False)
        watcher.begin_cycle()
        self.assertEqual(seen, [False])

    def test_disabled_watcher_never_reports(self) -> None:
        fake = FakeTime()
        seen = []
        watcher = ControlWatcher(enabled=False, on_log_override=seen.append, clock=fake.clock, sleep=fake.sleep)
        watcher.begin_cycle()
        watcher.checkpoint()
        self.assertEqual(seen, [])


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
