"""Tests for the switchable terminal log (main.TerminalLog).

Run from the project root: python -m unittest discover -s tests -t .
"""
import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

import main
from modules.log_files import list_log_files


class TerminalLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.directory = self._temp_dir.name
        self.console = io.StringIO()
        self.tees = (main.TeeStream(self.console, None), main.TeeStream(self.console, None))
        # Output of the log object itself goes through the tee, as in the app.
        redirect = contextlib.redirect_stdout(self.tees[0])
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def make(self, enabled=False, **overrides):
        settings = {
            "enabled": enabled,
            "directory": self.directory,
            "max_file_mb": 10,
            "keep_files": 30,
            **overrides,
        }
        log = main.TerminalLog(settings, self.tees)
        self.addCleanup(log.stop)
        return log

    def logs(self):
        return list_log_files(self.directory)

    def read(self, path):
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_off_by_default_writes_no_file(self) -> None:
        log = self.make(enabled=False)
        print("hello")
        self.assertFalse(log.active)
        self.assertEqual(self.logs(), [])
        self.assertIn("hello", self.console.getvalue())

    def test_switching_on_mid_run_starts_a_new_file(self) -> None:
        log = self.make(enabled=False)
        print("before")
        log.apply_override(True)
        print("during")
        self.assertTrue(log.active)
        (path,) = self.logs()
        content = self.read(path)
        self.assertIn("during", content)
        self.assertNotIn("before", content)

    def test_switching_off_closes_file_and_keeps_console(self) -> None:
        log = self.make(enabled=True)
        log.start()
        log.apply_override(False)
        print("after")
        self.assertFalse(log.active)
        (path,) = self.logs()
        self.assertNotIn("after", self.read(path))
        self.assertIn("after", self.console.getvalue())

    def test_each_switch_on_gets_its_own_file(self) -> None:
        log = self.make(enabled=False)
        log.apply_override(True)
        log.apply_override(False)
        log.apply_override(True)
        self.assertEqual(len(self.logs()), 2)

    def test_none_returns_to_config_default(self) -> None:
        log = self.make(enabled=True)
        log.start()
        log.apply_override(False)
        self.assertFalse(log.active)
        log.apply_override(None)
        self.assertTrue(log.active)

        quiet = self.make(enabled=False)
        quiet.apply_override(True)
        quiet.apply_override(None)
        self.assertFalse(quiet.active)

    def test_repeated_switch_is_a_noop(self) -> None:
        log = self.make(enabled=False)
        log.apply_override(True)
        log.apply_override(True)
        log.apply_override(False)
        log.apply_override(False)
        self.assertEqual(len(self.logs()), 1)

    def test_unwritable_directory_leaves_logging_off(self) -> None:
        blocker = os.path.join(self.directory, "blocker")
        with open(blocker, "w", encoding="utf-8") as handle:
            handle.write("x")
        log = self.make(enabled=False, directory=os.path.join(blocker, "logs"))
        with contextlib.redirect_stderr(io.StringIO()):
            log.apply_override(True)
        self.assertFalse(log.active)
        print("still works")
        self.assertIn("still works", self.console.getvalue())

    def test_switch_on_prunes_old_files(self) -> None:
        for stamp in ("20200101_000001", "20200101_000002", "20200101_000003"):
            with open(os.path.join(self.directory, f"{stamp}.log"), "w", encoding="utf-8"):
                pass
        log = self.make(enabled=False, keep_files=2)
        log.apply_override(True)
        self.assertEqual(len(self.logs()), 2)

    def test_setup_routes_output_and_honours_config(self) -> None:
        settings = {"enabled": False, "directory": self.directory, "max_file_mb": 10, "keep_files": 30}
        original = (main.sys.stdout, main.sys.stderr)
        self.addCleanup(lambda: setattr(main.sys, "stdout", original[0]))
        self.addCleanup(lambda: setattr(main.sys, "stderr", original[1]))

        with mock.patch.object(main, "get_terminal_log_settings", return_value=settings):
            log = main.setup_terminal_logging({})
        self.addCleanup(log.stop)
        self.assertIsInstance(main.sys.stdout, main.TeeStream)
        self.assertFalse(log.active)
        self.assertEqual(self.logs(), [])


class WhichRunsWriteALogTests(unittest.TestCase):
    """Only real run modes mirror their output to a log file; --help and read-only commands do not."""

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.directory = self._temp_dir.name
        settings = {"enabled": True, "directory": self.directory, "max_file_mb": 10, "keep_files": 0}
        original = (main.sys.stdout, main.sys.stderr, main.sys.argv)
        self.addCleanup(lambda: (setattr(main.sys, "stdout", original[0]), setattr(main.sys, "stderr", original[1]),
                                 setattr(main.sys, "argv", original[2])))
        for patcher in (
            mock.patch.object(main, "load_config", return_value={}),
            mock.patch.object(main, "get_terminal_log_settings", return_value=settings),
            mock.patch.object(main, "get_polling_settings", return_value={
                "enabled": False, "interval_seconds": 300, "jitter_min_seconds": 0, "jitter_max_seconds": 0}),
            mock.patch.object(main, "acquire_daemon_lock", return_value=contextlib.nullcontext()),
            mock.patch.object(main, "ensure_mapping_file", return_value=False),
            mock.patch.object(main, "open_database", return_value=contextlib.nullcontext()),
            mock.patch.object(main, "run_ingest_and_queue_cycle"),
            mock.patch.object(main, "_ORIGINAL_STDOUT", io.StringIO()),  # keep --help text out of the test output
            mock.patch.object(main, "_ORIGINAL_STDERR", io.StringIO()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def logs(self) -> list[str]:
        return [name for name in os.listdir(self.directory) if name.endswith(".log")]

    def run_main(self, argv: list[str], handled: bool) -> None:
        main.sys.argv = ["main.py", *argv]
        with mock.patch.object(main, "handle_cli_command", return_value=handled), \
                contextlib.redirect_stdout(io.StringIO()):
            main.main()

    def test_setup_alone_does_not_open_a_file(self) -> None:
        log = main.setup_terminal_logging({})
        self.addCleanup(log.stop)
        self.assertFalse(log.active)
        self.assertEqual(self.logs(), [])

    def test_a_read_only_command_leaves_no_log_file(self) -> None:
        self.run_main(["--queue-status"], handled=True)
        self.assertEqual(self.logs(), [])

    def test_help_leaves_no_log_file(self) -> None:
        main.sys.argv = ["main.py", "--help"]
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
            main.main()
        self.assertEqual(self.logs(), [])

    def test_a_run_mode_still_writes_its_log(self) -> None:
        self.run_main(["--once"], handled=False)
        logs = self.logs()
        self.assertEqual(len(logs), 1)
        with open(os.path.join(self.directory, logs[0]), encoding="utf-8") as handle:
            self.assertIn("Logging enabled", handle.read())


if __name__ == "__main__":
    unittest.main()
