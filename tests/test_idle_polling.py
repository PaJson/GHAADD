"""polling.enabled = false: the daemon (--daemon) stays idle and polls only on Poll now; the command line is unchanged.

Nothing real is touched: the database, the watcher and the cycle are replaced. Run from the project root:
python -m unittest discover -s tests -t .
"""
import contextlib
import unittest
from unittest import mock

import main
from modules import cli_commands, daemon_lock, gui_daemon


class FakeWatcher:
    """Answers each wait() from a list and records how it was called."""

    def __init__(self, results, **_kwargs) -> None:
        self.results = list(results)
        self.calls: list[dict] = []
        self.stop_requested = False
        self.cycle_interrupted = False
        self.forced_while_paused = False
        self.last_handled_request = 1.0

    def begin_cycle(self) -> None:
        pass

    def checkpoint(self) -> bool:
        return False

    def wait(self, seconds, on_change=None, idle=False):
        self.calls.append({"seconds": seconds, "idle": idle})
        result = self.results.pop(0)
        if isinstance(result, BaseException):  # a result can also be something that goes wrong in the wait
            raise result
        return result


class FakeViewerPusher:
    """Stands in for the viewer push: records the switches and the stop instead of opening a connection."""

    instances: list["FakeViewerPusher"] = []

    def __init__(self, settings, **_kwargs) -> None:
        self.settings = settings
        self.switches: list[tuple] = []
        self.stopped = 0
        FakeViewerPusher.instances.append(self)

    def apply_override(self, override, configured) -> bool:
        self.switches.append((override, configured))
        return bool(configured if override is None else override)

    def stop(self) -> None:
        self.stopped += 1


class IdleLoopTests(unittest.TestCase):
    def run_loop(self, results, idle, viewer_enabled=False, dry_run=False):
        watcher = FakeWatcher(results)
        cycles = []
        FakeViewerPusher.instances = []
        self.watcher_kwargs: dict = {}

        @contextlib.contextmanager
        def fake_database():
            yield object()

        def fake_watcher(**kwargs):
            self.watcher_kwargs = kwargs
            return watcher

        with contextlib.ExitStack() as stack:
            for name, value in (
                ("open_database", fake_database),
                ("ControlWatcher", fake_watcher),
                ("ViewerPusher", FakeViewerPusher),
                ("get_viewer_settings", lambda: {"enabled": viewer_enabled, "url": "http://v:8888", "token": "t", "name": "pc"}),
                ("reset_push_override_on_startup", lambda c: None),
                ("run_ingest_and_queue_cycle", lambda *a, **k: cycles.append(1)),
                ("update_daemon_status", lambda **k: None),
                ("reset_paused_on_startup", lambda c: None),
                ("reset_log_override_on_startup", lambda c: None),
                ("get_destination_check_every_n_polls", lambda: 0),
                ("clear_all_resolved_limit_warnings", lambda: 0),
                ("is_dry_run", lambda: dry_run),
                ("run_scheduled_backup", lambda: None),  # backups are on by default: never write into the real app folder
            ):
                stack.enter_context(mock.patch.object(main, name, value))
            stack.enter_context(contextlib.redirect_stdout(open(__import__("os").devnull, "w", encoding="utf-8")))
            main.run_polling_loop(300, 5, 30, None, "fingerprint", idle=idle)
        return watcher, cycles

    def test_the_viewer_push_starts_from_the_config_follows_live_switches_and_stops_on_exit(self) -> None:
        self.run_loop(["stop"], idle=False, viewer_enabled=True)
        (pusher,) = FakeViewerPusher.instances
        self.assertEqual(pusher.switches, [(None, True)])  # viewer.enabled starts it at once
        self.assertEqual(pusher.stopped, 1)
        self.watcher_kwargs["on_push_override"](False)  # --push-off arrives through the watcher
        self.assertEqual(pusher.switches, [(None, True), (False, True)])

    def test_the_viewer_push_is_stopped_even_when_the_loop_ends_with_an_error(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            self.run_loop([KeyboardInterrupt()], idle=True, viewer_enabled=True)
        self.assertEqual(FakeViewerPusher.instances[0].stopped, 1)

    def test_a_dry_run_never_pushes(self) -> None:
        self.run_loop(["stop"], idle=True, viewer_enabled=True, dry_run=True)
        self.assertEqual(FakeViewerPusher.instances, [])
        self.assertIsNone(self.watcher_kwargs["on_push_override"])

    def test_an_idle_daemon_waits_first_and_polls_once_per_request(self) -> None:
        watcher, cycles = self.run_loop(["forced", "forced", "stop"], idle=True)
        self.assertEqual(len(cycles), 2)  # one poll for each Poll now, none on its own
        self.assertEqual(len(watcher.calls), 3)
        self.assertTrue(all(call["idle"] for call in watcher.calls))  # never a countdown

    def test_a_single_poll_runs_only_one_item_and_the_next_full_poll_runs_everything(self) -> None:
        single_runs = []
        with mock.patch.object(main, "run_single_cycle", lambda *a, **k: single_runs.append(1)):
            watcher, cycles = self.run_loop(["single", "forced", "stop"], idle=True)
        self.assertEqual(len(single_runs), 1)  # the first request
        self.assertEqual(len(cycles), 1)  # the second request was a full poll

    def test_a_single_poll_in_a_scheduled_daemon_replaces_one_full_cycle(self) -> None:
        single_runs = []
        with mock.patch.object(main, "run_single_cycle", lambda *a, **k: single_runs.append(1)):
            watcher, cycles = self.run_loop(["single", "elapsed", "stop"], idle=False)
        self.assertEqual(len(cycles), 2)  # the first cycle at start, and the one after "elapsed"
        self.assertEqual(len(single_runs), 1)  # the cycle after the single request

    def test_stopping_while_idle_polls_nothing(self) -> None:
        watcher, cycles = self.run_loop(["stop"], idle=True)
        self.assertEqual(cycles, [])

    def test_a_normal_daemon_polls_at_once_and_counts_down(self) -> None:
        watcher, cycles = self.run_loop(["elapsed", "stop"], idle=False)
        self.assertEqual(len(cycles), 2)
        self.assertEqual([call["idle"] for call in watcher.calls], [False, False])
        self.assertGreater(watcher.calls[0]["seconds"], 0)


class CommandLineTests(unittest.TestCase):
    def test_the_daemon_flag_exists_and_poll_keeps_its_meaning(self) -> None:
        self.assertTrue(cli_commands.parse_cli_args(["--daemon"], "x").daemon)
        parsed = cli_commands.parse_cli_args(["--poll"], "x")
        self.assertTrue(parsed.poll)
        self.assertFalse(parsed.daemon)

    def test_every_launcher_uses_the_daemon_flag(self) -> None:
        from modules import autostart, daemon_launcher

        self.assertEqual(daemon_launcher.build_start_command()[2:], ["--daemon"])
        self.assertIn(" --daemon\n", autostart.systemd_unit_text("/usr/bin/python3", "/opt/g/main.py", "/opt/g"))


class SinglePollCommandTests(unittest.TestCase):
    def test_the_flag_asks_the_running_daemon(self) -> None:
        parsed = cli_commands.parse_cli_args(["--poll-one"], "x")
        self.assertTrue(parsed.poll_one)
        with mock.patch.object(cli_commands, "is_daemon_running", return_value=True), mock.patch.object(
            cli_commands, "request_single_poll"
        ) as request:
            self.assertTrue(cli_commands.handle_cli_command(parsed, lambda: None))
        request.assert_called_once_with()

    def test_without_a_daemon_nothing_is_written(self) -> None:
        parsed = cli_commands.parse_cli_args(["--poll-one"], "x")
        with mock.patch.object(cli_commands, "is_daemon_running", return_value=False), mock.patch.object(
            cli_commands, "request_single_poll"
        ) as request:
            cli_commands.handle_cli_command(parsed, lambda: None)
        request.assert_not_called()

    def test_the_gui_action_writes_the_request(self) -> None:
        with mock.patch.object(gui_daemon.daemon_control, "request_single_poll") as request:
            self.assertIsNone(gui_daemon.do_single_poll())
        request.assert_called_once_with()


class GuiViewTests(unittest.TestCase):
    def view(self, **fields):
        return gui_daemon.build_view(gui_daemon.DaemonSnapshot(running=True, pid=7, **fields), now=1000.0)

    def test_an_idle_daemon_says_so_and_can_be_polled(self) -> None:
        view = self.view(polling_idle=True)
        self.assertEqual(view.countdown_text, "Polling is off: use Poll now")
        self.assertTrue(view.poll_enabled)
        self.assertTrue(view.stop_enabled)

    def test_paused_and_processing_still_win(self) -> None:
        self.assertEqual(self.view(polling_idle=True, paused=True).countdown_text, "Polling paused")
        self.assertEqual(self.view(polling_idle=True, current_repo="a/b").countdown_text, "Processing a/b")

    def test_a_normal_daemon_is_unchanged(self) -> None:
        self.assertEqual(self.view(next_poll_at=1065.0).countdown_text, "Next poll in 01:05")

    def test_the_status_file_carries_the_flag(self) -> None:
        with mock.patch.object(daemon_lock, "is_daemon_running", return_value=True), mock.patch.object(
            daemon_lock, "_read_status_payload", return_value={"pid": 5, "polling_idle": True}
        ):
            self.assertIs(daemon_lock.get_daemon_status()["polling_idle"], True)
        with mock.patch.object(daemon_lock, "is_daemon_running", return_value=True), mock.patch.object(
            daemon_lock, "_read_status_payload", return_value={"pid": 5}
        ):
            self.assertIs(daemon_lock.get_daemon_status()["polling_idle"], False)  # an older daemon
        with mock.patch.object(daemon_lock, "is_daemon_running", return_value=False):
            self.assertIs(daemon_lock.get_daemon_status()["polling_idle"], False)


if __name__ == "__main__":
    unittest.main()
