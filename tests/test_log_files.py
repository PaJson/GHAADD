"""Tests for terminal log naming, rollover and retention (modules/log_files.py).

Run from the project root: python -m unittest discover -s tests -t .
"""
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from modules import dry_run_mode
from modules.log_files import RollingLogFile, list_log_files, new_log_path, prune_log_files


class LogFilesTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.directory = self._temp_dir.name
        self.addCleanup(dry_run_mode.set_dry_run, False)

    def touch(self, name: str) -> str:
        path = os.path.join(self.directory, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("x")
        return path

    def names(self) -> list[str]:
        return [os.path.basename(path) for path in list_log_files(self.directory)]


class NamingTests(LogFilesTestCase):
    def test_list_ignores_foreign_files_and_orders_counters_numerically(self) -> None:
        for name in (
            "20260101_000000_10.log",
            "20260101_000000_2.log",
            "20260101_000000.log",
            "20250101_000000.log",
            "notes.txt",
            "20260101_000000.log.bak",
        ):
            self.touch(name)

        self.assertEqual(
            self.names(),
            [
                "20250101_000000.log",
                "20260101_000000.log",
                "20260101_000000_2.log",
                "20260101_000000_10.log",
            ],
        )

    def test_new_log_path_adds_counter_on_collision(self) -> None:
        now = datetime(2026, 10, 5, 12, 0, 0)

        first = new_log_path(self.directory, now)
        self.touch(os.path.basename(first))
        second = new_log_path(self.directory, now)

        self.assertEqual(os.path.basename(first), "20261005_120000.log")
        self.assertEqual(os.path.basename(second), "20261005_120000_2.log")


class PruneTests(LogFilesTestCase):
    def test_keeps_newest_files_and_foreign_files(self) -> None:
        for day in range(1, 6):
            self.touch(f"2026010{day}_000000.log")
        keep_me = self.touch("notes.txt")

        removed = prune_log_files(self.directory, keep_files=2)

        self.assertEqual(removed, 3)
        self.assertEqual(self.names(), ["20260104_000000.log", "20260105_000000.log"])
        self.assertTrue(os.path.exists(keep_me))

    def test_never_removes_protected_file(self) -> None:
        oldest = self.touch("20260101_000000.log")
        self.touch("20260102_000000.log")
        self.touch("20260103_000000.log")

        prune_log_files(self.directory, keep_files=1, protect=oldest)

        self.assertEqual(self.names(), ["20260101_000000.log", "20260103_000000.log"])

    def test_zero_keeps_everything(self) -> None:
        self.touch("20260101_000000.log")
        self.touch("20260102_000000.log")

        self.assertEqual(prune_log_files(self.directory, keep_files=0), 0)
        self.assertEqual(len(self.names()), 2)

    def test_dry_run_removes_nothing(self) -> None:
        self.touch("20260101_000000.log")
        self.touch("20260102_000000.log")
        dry_run_mode.set_dry_run(True)

        self.assertEqual(prune_log_files(self.directory, keep_files=1), 0)
        self.assertEqual(len(self.names()), 2)


class RollingLogFileTests(LogFilesTestCase):
    def read(self, path: str) -> str:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def test_rolls_over_at_line_boundary_with_header(self) -> None:
        log = RollingLogFile(self.directory, max_bytes=20, keep_files=0)
        first_path = log.path
        self.addCleanup(log.close)

        log.write("0123456789")
        self.assertEqual(log.path, first_path)
        log.write("0123456789\n")
        second_path = log.path
        log.write("after\n")

        self.assertNotEqual(first_path, second_path)
        self.assertEqual(self.read(first_path).replace("\r\n", "\n"), "01234567890123456789\n")
        second = self.read(second_path).replace("\r\n", "\n")
        self.assertEqual(
            second,
            f"--- Log continued from {os.path.basename(first_path)} ---\nafter\n",
        )

    def test_does_not_split_a_line_written_in_fragments(self) -> None:
        log = RollingLogFile(self.directory, max_bytes=5, keep_files=0)
        first_path = log.path
        self.addCleanup(log.close)

        log.write("a long line without newline yet")
        self.assertEqual(log.path, first_path)
        log.write("\n")

        self.assertNotEqual(log.path, first_path)

    def test_zero_max_bytes_never_rolls(self) -> None:
        log = RollingLogFile(self.directory, max_bytes=0, keep_files=0)
        first_path = log.path
        self.addCleanup(log.close)

        for _ in range(50):
            log.write("a line of text\n")

        self.assertEqual(log.path, first_path)
        self.assertEqual(len(self.names()), 1)

    def test_rollover_prunes_old_files_but_not_current(self) -> None:
        for day in range(1, 4):
            self.touch(f"2025010{day}_000000.log")
        log = RollingLogFile(self.directory, max_bytes=5, keep_files=2)
        self.addCleanup(log.close)
        first_path = log.path

        log.write("hello\n")

        remaining = list_log_files(self.directory)
        self.assertEqual(len(remaining), 2)
        self.assertEqual(remaining[-1], log.path)
        self.assertNotEqual(log.path, first_path)

    def test_failed_rollover_keeps_writing_to_current_file(self) -> None:
        log = RollingLogFile(self.directory, max_bytes=5, keep_files=0)
        first_path = log.path
        self.addCleanup(log.close)

        with mock.patch.object(RollingLogFile, "_open", side_effect=OSError("disk full")):
            log.write("hello\n")
            log.write("world\n")

        self.assertEqual(log.path, first_path)
        self.assertEqual(self.read(first_path).replace("\r\n", "\n"), "hello\nworld\n")


if __name__ == "__main__":
    unittest.main()
