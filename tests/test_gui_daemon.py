"""Tests for the control bar logic (status view, button actions) and the detached launcher.

Run from the project root: python -m unittest discover -s tests -t .
"""
import os
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

from modules import config_manager, daemon_launcher, gui_daemon
from modules.gui_daemon import DaemonSnapshot, build_view

NOW = 1_000_000.0


def running(**overrides) -> DaemonSnapshot:
    fields = dict(running=True, pid=42, paused=False, next_poll_at=NOW + 125, current_repo=None, log_on=False)
    fields.update(overrides)
    return DaemonSnapshot(**fields)


class BuildViewTests(unittest.TestCase):
    def test_stopped_daemon_can_only_be_started(self) -> None:
        view = build_view(DaemonSnapshot(), NOW)

        self.assertEqual(view.dot, "stopped")
        self.assertEqual(view.status_text, "Daemon not running")
        self.assertTrue(view.start_enabled)
        self.assertFalse(any((view.stop_enabled, view.pause_enabled, view.poll_enabled, view.log_enabled)))

    def test_running_daemon_shows_pid_and_countdown(self) -> None:
        view = build_view(running(), NOW)

        self.assertEqual((view.dot, view.status_text), ("running", "Daemon running (PID 42)"))
        self.assertEqual(view.countdown_text, "Next poll in 02:05")
        self.assertFalse(view.start_enabled)
        self.assertTrue(all((view.stop_enabled, view.pause_enabled, view.poll_enabled, view.log_enabled)))
        self.assertEqual(view.pause_text, "Pause")

    def test_long_countdown_includes_hours(self) -> None:
        self.assertEqual(gui_daemon.format_countdown(3725), "1:02:05")
        self.assertEqual(gui_daemon.format_countdown(-5), "00:00")

    def test_paused_daemon_offers_resume_and_no_poll_now(self) -> None:
        view = build_view(running(paused=True, next_poll_at=None), NOW)

        self.assertEqual((view.dot, view.status_text), ("paused", "Daemon paused (PID 42)"))
        self.assertEqual(view.countdown_text, "Polling paused")
        self.assertEqual(view.pause_text, "Resume")
        self.assertFalse(view.poll_enabled)

    def test_cycle_in_progress_shows_the_job_or_polling(self) -> None:
        self.assertEqual(build_view(running(next_poll_at=None), NOW).countdown_text, "Polling…")
        self.assertEqual(build_view(running(next_poll_at=NOW - 3), NOW).countdown_text, "Polling…")
        view = build_view(running(next_poll_at=None, current_repo="o/r"), NOW)
        self.assertEqual(view.countdown_text, "Processing o/r")

    def test_log_checkbox_follows_effective_state(self) -> None:
        self.assertTrue(build_view(running(log_on=True), NOW).log_checked)
        self.assertFalse(build_view(running(log_on=False), NOW).log_checked)

    def test_restart_button_shows_only_when_a_running_daemon_needs_it(self) -> None:
        self.assertFalse(build_view(running(), NOW).restart_visible)
        self.assertFalse(build_view(DaemonSnapshot(restart_needed=True), NOW).restart_visible)
        view = build_view(running(restart_needed=True), NOW)
        self.assertTrue(view.restart_visible and view.restart_enabled)

    def test_restart_button_is_disabled_while_stopping(self) -> None:
        view = build_view(running(restart_needed=True), NOW, stopping_since=NOW - 2)
        self.assertTrue(view.restart_visible)
        self.assertFalse(view.restart_enabled)

    def test_start_pending_disables_start_until_timeout(self) -> None:
        view = build_view(DaemonSnapshot(), NOW, starting_since=NOW - 3)
        self.assertEqual(view.status_text, "Daemon starting…")
        self.assertFalse(view.start_enabled)

        expired = build_view(DaemonSnapshot(), NOW, starting_since=NOW - 60)
        self.assertEqual(expired.status_text, "Daemon not running")
        self.assertTrue(expired.start_enabled)

    def test_start_pending_is_forgotten_once_the_daemon_runs(self) -> None:
        view = build_view(running(), NOW, starting_since=NOW - 3)
        self.assertEqual(view.status_text, "Daemon running (PID 42)")

    def test_stop_pending_disables_everything_then_hints_then_gives_up(self) -> None:
        view = build_view(running(), NOW, stopping_since=NOW - 5)
        self.assertIn("stopping", view.status_text)
        self.assertNotIn("older version", view.status_text)
        self.assertFalse(any((view.stop_enabled, view.pause_enabled, view.poll_enabled, view.log_enabled)))

        self.assertIn("older version", build_view(running(), NOW, stopping_since=NOW - 60).status_text)

        gave_up = build_view(running(), NOW, stopping_since=NOW - 500)
        self.assertEqual(gave_up.status_text, "Daemon running (PID 42)")
        self.assertTrue(gave_up.stop_enabled)

    def test_stop_pending_ends_when_the_daemon_is_gone(self) -> None:
        view = build_view(DaemonSnapshot(), NOW, stopping_since=NOW - 5)
        self.assertEqual(view.status_text, "Daemon not running")
        self.assertTrue(view.start_enabled)


class ConfigFingerprintTests(unittest.TestCase):
    BASE = {"polling": {"enabled": True, "interval_seconds": 300}, "processing": {"recheck_intervals_minutes": [5, 15]}}

    def fingerprint(self, config) -> str:
        return config_manager.get_config_fingerprint(config)

    def test_same_settings_give_the_same_fingerprint(self) -> None:
        reordered = {"processing": {"recheck_intervals_minutes": [5, 15]}, "polling": {"interval_seconds": 300, "enabled": True}}
        self.assertEqual(self.fingerprint(self.BASE), self.fingerprint(reordered))

    def test_gui_section_and_unknown_keys_do_not_matter(self) -> None:
        noisy = {**self.BASE, "gui": {"window": {"width": 1000, "height": 700}}, "something_else": 1}
        self.assertEqual(self.fingerprint(self.BASE), self.fingerprint(noisy))

    def test_writing_a_default_explicitly_does_not_matter(self) -> None:
        explicit = {**self.BASE, "processing": {**self.BASE["processing"], "max_emails_to_process": 0}}
        self.assertEqual(self.fingerprint(self.BASE), self.fingerprint(explicit))

    def test_a_changed_setting_changes_the_fingerprint(self) -> None:
        for change in (
            {"polling": {"enabled": True, "interval_seconds": 600}},
            {"processing": {"recheck_intervals_minutes": [5, 15, 30]}},
            {"paths": {"default_download_dir": "E:\\Downloads"}},
            {"terminal_log": {"enabled": True}},
        ):
            with self.subTest(change=change):
                self.assertNotEqual(self.fingerprint(self.BASE), self.fingerprint({**self.BASE, **change}))


class ReadSnapshotTests(unittest.TestCase):
    STATUS = {
        "running": True, "pid": 7, "started_at": 1.0, "paused": False, "next_poll_at": 99.0,
        "last_forced_poll_handled": None, "current_job": {"repo": "o/r", "tag": "v1", "since": 2.0},
    }

    def patches(self, status, control=None, config_log=True):
        control_patch = (
            mock.patch.object(gui_daemon.daemon_control, "get_control_state", return_value=control)
            if not isinstance(control, Exception)
            else mock.patch.object(gui_daemon.daemon_control, "get_control_state", side_effect=control)
        )
        return (
            mock.patch.object(gui_daemon.daemon_lock, "get_daemon_status", return_value=status),
            control_patch,
            mock.patch.object(
                gui_daemon.config_manager, "get_terminal_log_settings", return_value={"enabled": config_log}
            ),
        )

    def snapshot(self, status, control=None, config_log=True) -> DaemonSnapshot:
        first, second, third = self.patches(status, control, config_log)
        with first, second, third:
            return gui_daemon.read_snapshot()

    def test_stopped(self) -> None:
        snap = self.snapshot({**self.STATUS, "running": False, "pid": None}, config_log=True)
        self.assertFalse(snap.running)
        self.assertTrue(snap.log_on)

    def test_running_uses_control_table_for_pause_and_log(self) -> None:
        control = {"paused": True, "poll_now_request": None, "log_override": False}
        snap = self.snapshot(self.STATUS, control, config_log=True)

        self.assertEqual((snap.running, snap.pid, snap.current_repo), (True, 7, "o/r"))
        self.assertTrue(snap.paused)  # from the control table, not the (lagging) status file
        self.assertFalse(snap.log_on)  # override False beats terminal_log.enabled = true

    def test_log_follows_config_without_override(self) -> None:
        control = {"paused": False, "poll_now_request": None, "log_override": None}
        self.assertTrue(self.snapshot(self.STATUS, control, config_log=True).log_on)
        self.assertFalse(self.snapshot(self.STATUS, control, config_log=False).log_on)

    def test_restart_needed_only_when_the_published_fingerprint_differs(self) -> None:
        control = {"paused": False, "poll_now_request": None, "log_override": None}
        with mock.patch.object(gui_daemon.config_manager, "get_config_fingerprint", return_value="aaa"):
            same = self.snapshot({**self.STATUS, "config_fingerprint": "aaa"}, control)
            different = self.snapshot({**self.STATUS, "config_fingerprint": "bbb"}, control)
            unknown = self.snapshot({**self.STATUS, "config_fingerprint": None}, control)  # older daemon

        self.assertFalse(same.restart_needed)
        self.assertTrue(different.restart_needed)
        self.assertFalse(unknown.restart_needed)

    def test_locked_database_falls_back_to_the_status_file(self) -> None:
        snap = self.snapshot({**self.STATUS, "paused": True}, sqlite3.OperationalError("database is locked"))
        self.assertTrue(snap.running)
        self.assertTrue(snap.paused)


class ActionTests(unittest.TestCase):
    def test_actions_return_none_on_success_and_a_message_on_failure(self) -> None:
        with mock.patch.object(gui_daemon.daemon_control, "set_paused") as set_paused:
            self.assertIsNone(gui_daemon.do_set_paused(True))
            set_paused.assert_called_once_with(True)
        with mock.patch.object(gui_daemon.daemon_control, "request_stop", side_effect=sqlite3.OperationalError("locked")):
            self.assertIn("locked", gui_daemon.do_stop())
        with mock.patch.object(gui_daemon.daemon_control, "set_log_override") as set_log:
            self.assertIsNone(gui_daemon.do_set_log(False))
            set_log.assert_called_once_with(False)
        with mock.patch.object(gui_daemon.daemon_control, "request_poll_now") as poll:
            self.assertIsNone(gui_daemon.do_poll_now())
            poll.assert_called_once_with()

    def test_start_refuses_when_a_daemon_is_already_running(self) -> None:
        with mock.patch.object(gui_daemon.daemon_lock, "is_daemon_running", return_value=True):
            with mock.patch.object(gui_daemon.daemon_launcher, "start_daemon") as start:
                self.assertIn("already running", gui_daemon.do_start())
                start.assert_not_called()

    def test_start_launches_and_reports_launch_failures(self) -> None:
        with mock.patch.object(gui_daemon.daemon_lock, "is_daemon_running", return_value=False):
            with mock.patch.object(gui_daemon.daemon_launcher, "start_daemon", return_value=1234):
                self.assertIsNone(gui_daemon.do_start())
            with mock.patch.object(gui_daemon.daemon_launcher, "start_daemon", side_effect=OSError("no python")):
                self.assertEqual(gui_daemon.do_start(), "no python")


class LauncherTests(unittest.TestCase):
    def test_command_runs_main_in_polling_mode(self) -> None:
        command = daemon_launcher.build_start_command()
        self.assertEqual(os.path.basename(command[1]), "main.py")
        self.assertEqual(command[2:], ["--poll"])

    def test_pythonw_is_swapped_for_a_console_python(self) -> None:
        with mock.patch.object(daemon_launcher.os.path, "isfile", return_value=True):
            swapped = daemon_launcher.get_python_executable(os.path.join("C:", os.sep, "Py", "pythonw.exe"))
        self.assertEqual(os.path.basename(swapped), "python.exe")
        self.assertEqual(daemon_launcher.get_python_executable("/usr/bin/python3"), "/usr/bin/python3")

    def start_with_fake_popen(self, stderr_path):
        with mock.patch.object(daemon_launcher, "get_stderr_path", return_value=stderr_path):
            with mock.patch.object(daemon_launcher.subprocess, "Popen") as popen:
                popen.return_value.pid = 4321
                pid = daemon_launcher.start_daemon()
        self.assertEqual(pid, 4321)
        return popen.call_args.kwargs

    def test_daemon_is_started_detached_without_console_streams(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            kwargs = self.start_with_fake_popen(os.path.join(folder, "stderr.log"))

        self.assertEqual((kwargs["stdin"], kwargs["stdout"]), (subprocess.DEVNULL,) * 2)
        if os.name == "nt":
            self.assertTrue(kwargs["creationflags"] & 0x00000008)  # DETACHED_PROCESS
            self.assertTrue(kwargs["creationflags"] & 0x00000200)  # CREATE_NEW_PROCESS_GROUP
        else:
            self.assertTrue(kwargs["start_new_session"])

    def test_child_gets_utf8_output_so_emoji_cannot_crash_it(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            kwargs = self.start_with_fake_popen(os.path.join(folder, "stderr.log"))
        self.assertEqual(kwargs["env"]["PYTHONIOENCODING"], "utf-8")

    def test_crash_output_is_kept_in_a_file_not_discarded(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            target = os.path.join(folder, "stderr.log")
            kwargs = self.start_with_fake_popen(target)
            self.assertEqual(kwargs["stderr"].name, target)
            self.assertTrue(kwargs["stderr"].closed)  # our copy is closed once the child has its own
            self.assertTrue(os.path.exists(target))

    def test_an_unwritable_stderr_file_falls_back_to_discarding(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            kwargs = self.start_with_fake_popen(os.path.join(folder, "no_such_folder", "stderr.log"))
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)


class OutputEncodingTests(unittest.TestCase):
    """Regression: a redirected stdout used the cp1252 locale encoding and the first emoji killed the daemon."""

    def test_emoji_do_not_crash_a_cp1252_stream_after_configuring(self) -> None:
        import io

        import main

        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict", write_through=True)
        with self.assertRaises(UnicodeEncodeError):  # the bug, reproduced
            stream.write("\U0001f4e5 Found 16 unread release notifications.\n")

        stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict", newline="\n", write_through=True)
        with mock.patch.object(main.sys, "stdout", stream), mock.patch.object(main.sys, "stderr", io.StringIO()):
            main._configure_output_encoding()
            stream.write("\U0001f4e5 Found 16 unread release notifications.\n")  # must not raise now
        self.assertEqual(stream.encoding, "utf-8")
        self.assertEqual(stream.buffer.getvalue().decode("utf-8"), "\U0001f4e5 Found 16 unread release notifications.\n")

    def test_streams_without_reconfigure_are_left_alone(self) -> None:
        import main

        class Plain:
            pass

        with mock.patch.object(main.sys, "stdout", Plain()), mock.patch.object(main.sys, "stderr", None):
            main._configure_output_encoding()  # e.g. under pythonw: nothing to do, must not raise


if __name__ == "__main__":
    unittest.main()
