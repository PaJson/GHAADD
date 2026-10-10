"""When the daemon checks destinations and folder limits: at start, every N polls, and on request.

Nothing here touches the real mailbox, daemon lock, mapping or state.db.
Run from the project root: python -m unittest discover -s tests -t .
"""
import contextlib
import io
import os
import tempfile
import threading
import unittest
from unittest import mock

import main
from modules import daemon_control, db_manager


_NO_VIEWER = {"enabled": False, "url": "", "token": "", "name": "test"}


class FolderCheckTriggerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._temp_dir.cleanup)
        db_path = os.path.join(self._temp_dir.name, "state.db")
        self.destinations = mock.Mock(return_value=0)
        self.limits = mock.Mock(return_value=0)
        for patcher in (
            mock.patch.object(db_manager, "get_state_db_path", lambda: db_path),
            mock.patch.object(main, "update_daemon_status"),
            mock.patch.object(main, "is_dry_run", lambda: False),
            mock.patch.object(main, "warn_about_missing_mapped_destinations", self.destinations),
            mock.patch.object(main, "check_folder_limits", self.limits),
            mock.patch.object(main, "clear_all_resolved_limit_warnings"),
            mock.patch.object(main, "run_scheduled_backup", lambda: None),  # never write a real backup
            # Never the real config.json: with the viewer switched on there, the loop would send to the real viewer.
            mock.patch.object(main, "get_viewer_settings", lambda: _NO_VIEWER),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_loop(self, every: int, cycle) -> str:
        output = io.StringIO()
        with mock.patch.object(main, "get_destination_check_every_n_polls", lambda: every), \
                mock.patch.object(main, "run_ingest_and_queue_cycle", cycle), \
                contextlib.redirect_stdout(output):
            main.run_polling_loop(interval_seconds=600, jitter_min_seconds=0, jitter_max_seconds=0)
        return output.getvalue()

    @staticmethod
    def stop_after(cycles: int):
        seen = []

        def cycle(connection, token, should_pause=None):
            seen.append(1)
            if len(seen) == cycles:
                daemon_control.request_stop()

        return cycle, seen

    def test_the_check_runs_at_start_before_the_first_poll(self) -> None:
        order = []
        self.destinations.side_effect = lambda: order.append("check") or 0

        def cycle(connection, token, should_pause=None):
            order.append("poll")
            daemon_control.request_stop()

        output = self.run_loop(10, cycle)

        self.assertEqual(order[:2], ["check", "poll"])
        self.assertIn("Folder check (at start)", output)

    def test_it_repeats_every_n_polls(self) -> None:
        polls = []

        def cycle(connection, token, should_pause=None):
            polls.append(1)
            if len(polls) == 5:
                raise KeyboardInterrupt  # ends the loop; the check after poll 5 never runs

        with mock.patch.object(daemon_control.ControlWatcher, "wait", return_value="elapsed"):
            with self.assertRaises(KeyboardInterrupt):
                self.run_loop(2, cycle)

        self.assertEqual(self.limits.call_count, 3)  # at start, after poll 2 and after poll 4
        self.assertEqual(self.destinations.call_count, 3)

    def test_an_interval_of_zero_switches_the_automatic_checks_off(self) -> None:
        cycle, _ = self.stop_after(1)
        self.run_loop(0, cycle)
        self.limits.assert_not_called()
        self.destinations.assert_not_called()

    def test_a_request_during_the_countdown_runs_the_check_and_keeps_waiting(self) -> None:
        cycle, _ = self.stop_after(10**6)  # never stops by itself
        timer = threading.Timer(0.4, daemon_control.request_check_folders)
        stopper = threading.Timer(2.5, daemon_control.request_stop)
        for timer_ in (timer, stopper):
            timer_.start()
            self.addCleanup(timer_.cancel)

        output = self.run_loop(0, cycle)  # no automatic checks, so any call comes from the request

        self.assertEqual(self.limits.call_count, 1)
        self.assertIn("Folder check (requested)", output)
        self.assertIn("Stop requested", output)

    def test_a_failing_check_does_not_stop_the_daemon(self) -> None:
        self.limits.side_effect = RuntimeError("boom")
        cycle, seen = self.stop_after(1)
        output = self.run_loop(10, cycle)
        self.assertEqual(len(seen), 1)
        self.assertIn("Could not check folder limits: boom", output)


if __name__ == "__main__":
    unittest.main()
