"""Tests for the terminal log tailer (modules/log_tail.py).

Run from the project root: python -m unittest discover -s tests -t .
"""
import os
import tempfile
import unittest
from typing import Optional

from modules import log_files, log_tail
from modules.log_tail import LogTailer, TailUpdate, line_level, read_last_lines


def changed(update: Optional[TailUpdate]) -> TailUpdate:
    """The update of a poll that is expected to have found something; None fails the test right here."""
    assert update is not None, "the tailer reported nothing new"
    return update


class TailTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.directory = self._temp_dir.name

    def path(self, name: str) -> str:
        return os.path.join(self.directory, name)

    def write(self, name: str, text: str, mode: str = "w") -> str:
        path = self.path(name)
        with open(path, mode, encoding="utf-8", newline="") as handle:
            handle.write(text)
        return path

    def append_bytes(self, name: str, data: bytes) -> None:
        with open(self.path(name), "ab") as handle:
            handle.write(data)

    def tailer(self, max_lines: int = 1000) -> LogTailer:
        return LogTailer(lambda: self.directory, max_lines=max_lines)


class ReadLastLinesTests(TailTestCase):
    def test_returns_the_last_lines_and_the_offset_after_the_last_newline(self) -> None:
        path = self.write("a.log", "one\ntwo\nthree\nfour\n")
        lines, offset = read_last_lines(path, 2)
        self.assertEqual(lines, ["three", "four"])
        self.assertEqual(offset, len("one\ntwo\nthree\nfour\n"))

    def test_holds_back_an_unfinished_last_line(self) -> None:
        path = self.write("a.log", "one\ntwo\npart")
        lines, offset = read_last_lines(path, 10)
        self.assertEqual(lines, ["one", "two"])
        self.assertEqual(offset, len("one\ntwo\n"))

    def test_empty_file_and_single_unfinished_line(self) -> None:
        self.assertEqual(read_last_lines(self.write("a.log", ""), 5), ([], 0))
        self.assertEqual(read_last_lines(self.write("b.log", "no newline yet"), 5), ([], 0))

    def test_large_file_is_read_from_the_end_with_correct_lines(self) -> None:
        total = 20000
        path = self.write("a.log", "".join(f"line {i:05d} ✅\n" for i in range(total)))
        lines, offset = read_last_lines(path, 1000)
        self.assertEqual(len(lines), 1000)
        self.assertEqual(lines[0], f"line {total - 1000:05d} ✅")
        self.assertEqual(lines[-1], f"line {total - 1:05d} ✅")
        self.assertEqual(offset, os.path.getsize(path))

    def test_fewer_lines_than_requested_and_crlf(self) -> None:
        lines, _ = read_last_lines(self.write("a.log", "a\r\nb\r\n"), 100)
        self.assertEqual(lines, ["a", "b"])

    def test_lines_straddling_chunk_boundaries_are_never_cut(self) -> None:
        # Lines of ~100 bytes, so chunk boundaries fall inside lines; every returned line must be whole.
        path = self.write("a.log", "".join(f"{i:06d} " + "x" * 90 + "\n" for i in range(3000)))
        lines, _ = read_last_lines(path, 1500)
        self.assertEqual(len(lines), 1500)
        self.assertTrue(all(len(line) == 97 and line[:6].isdigit() for line in lines))


class LogTailerTests(TailTestCase):
    def test_no_log_files_reports_once_then_stays_quiet(self) -> None:
        tailer = self.tailer()
        first = changed(tailer.poll())
        self.assertEqual((first.reset, first.lines, first.file_name), (True, [], None))
        self.assertIsNone(tailer.poll())

    def test_first_poll_shows_the_last_lines_then_only_new_ones(self) -> None:
        self.write("20261006_100000.log", "a\nb\nc\n")
        tailer = self.tailer(max_lines=2)

        first = changed(tailer.poll())
        self.assertEqual((first.reset, first.lines, first.file_name), (True, ["b", "c"], "20261006_100000.log"))
        self.assertIsNone(tailer.poll())  # nothing new

        self.append_bytes("20261006_100000.log", b"d\ne\n")
        update = changed(tailer.poll())
        self.assertEqual((update.reset, update.lines), (False, ["d", "e"]))

    def test_partial_lines_wait_for_their_newline(self) -> None:
        self.write("20261006_100000.log", "start\n")
        tailer = self.tailer()
        tailer.poll()

        self.append_bytes("20261006_100000.log", b"half a li")
        self.assertIsNone(tailer.poll())
        self.append_bytes("20261006_100000.log", b"ne\nnext\n")
        self.assertEqual(changed(tailer.poll()).lines, ["half a line", "next"])

    def test_a_multibyte_character_split_across_writes_is_not_garbled(self) -> None:
        self.write("20261006_100000.log", "")
        tailer = self.tailer()
        tailer.poll()
        emoji = "✅ done\n".encode("utf-8")
        self.append_bytes("20261006_100000.log", emoji[:2])  # half of the 3-byte check mark
        self.assertIsNone(tailer.poll())
        self.append_bytes("20261006_100000.log", emoji[2:])
        self.assertEqual(changed(tailer.poll()).lines, ["✅ done"])

    def test_truncated_file_is_reloaded(self) -> None:
        self.write("20261006_100000.log", "old 1\nold 2\nold 3\n")
        tailer = self.tailer()
        tailer.poll()
        self.write("20261006_100000.log", "new\n")  # replaced by something shorter
        update = changed(tailer.poll())
        self.assertEqual((update.reset, update.lines), (True, ["new"]))

    def test_a_backlog_that_is_too_large_is_replaced_by_the_last_lines(self) -> None:
        self.write("20261006_100000.log", "first\n")
        tailer = self.tailer(max_lines=3)
        tailer.poll()
        big = "".join(f"row {i}\n" for i in range(log_tail.MAX_CATCHUP_BYTES // 6 + 100))
        self.append_bytes("20261006_100000.log", big.encode("utf-8"))

        update = changed(tailer.poll())

        self.assertTrue(update.reset)
        self.assertEqual(len(update.lines), 3)
        self.assertTrue(update.lines[-1].startswith("row "))

    def test_a_new_run_starts_a_fresh_view(self) -> None:
        self.write("20261006_100000.log", "old run line\n")
        tailer = self.tailer()
        tailer.poll()
        self.write("20261006_110000.log", "Logging enabled. Writing terminal output to: x\nhello\n")

        update = changed(tailer.poll())

        self.assertTrue(update.reset and update.changed_file)
        self.assertEqual(update.lines, ["Logging enabled. Writing terminal output to: x", "hello"])
        self.assertEqual(update.file_name, "20261006_110000.log")

    def test_size_rollover_continues_without_a_reset_and_loses_nothing(self) -> None:
        self.write("20261006_100000.log", "a\nb\n")
        tailer = self.tailer()
        tailer.poll()
        # The daemon writes the rest of the old file, then rolls over to a newer file.
        self.append_bytes("20261006_100000.log", b"c\nd\n")
        self.write("20261006_100000_2.log", "--- Log continued from 20261006_100000.log ---\ne\n")

        update = changed(tailer.poll())

        self.assertFalse(update.reset)
        self.assertEqual(update.lines, ["c", "d", "--- Log continued from 20261006_100000.log ---", "e"])
        self.assertEqual(update.file_name, "20261006_100000_2.log")
        # ... and the new file keeps being followed.
        self.append_bytes("20261006_100000_2.log", b"f\n")
        self.assertEqual(changed(tailer.poll()).lines, ["f"])
        self.assertIsNone(tailer.poll())

    def test_opening_on_a_rolled_over_file_prefills_from_the_previous_one(self) -> None:
        self.write("20261006_100000.log", "".join(f"old {i}\n" for i in range(10)))
        self.write("20261006_100000_2.log", "--- Log continued from 20261006_100000.log ---\nnew 1\nnew 2\n")

        first = changed(self.tailer(max_lines=6).poll())

        self.assertEqual(first.lines, ["old 6", "old 7", "old 8", "old 9",
                                       "--- Log continued from 20261006_100000.log ---", "new 1", "new 2"][-6:])
        self.assertEqual(first.file_name, "20261006_100000_2.log")

    def test_the_followed_file_disappearing_switches_to_what_is_left(self) -> None:
        self.write("20261006_100000.log", "gone soon\n")
        tailer = self.tailer()
        tailer.poll()
        os.remove(self.path("20261006_100000.log"))
        self.write("20261006_120000.log", "survivor\n")

        update = changed(tailer.poll())

        self.assertEqual((update.reset, update.lines), (True, ["survivor"]))

    def test_other_files_in_the_folder_are_ignored(self) -> None:
        self.write("Warning.log", "old plain log\n")
        self.write("notes.txt", "not a log\n")
        update = changed(self.tailer().poll())
        self.assertIsNone(update.file_name)

    def test_missing_directory_is_just_no_log(self) -> None:
        tailer = LogTailer(lambda: os.path.join(self.directory, "nope"))
        self.assertIsNone(changed(tailer.poll()).file_name)

    def test_works_with_files_written_by_rolling_log_file(self) -> None:
        writer = log_files.RollingLogFile(self.directory, max_bytes=200, keep_files=0)
        self.addCleanup(writer.close)
        tailer = self.tailer()
        seen: list[str] = []
        for number in range(40):
            writer.write(f"message {number:03d} with some padding to fill the file\n")
            update = tailer.poll()
            if update is not None:
                if update.reset:
                    seen.clear()
                seen.extend(update.lines)
        writer.flush()
        update = tailer.poll()
        if update is not None:
            seen.extend(update.lines)

        messages = [line for line in seen if line.startswith("message ")]
        self.assertEqual(messages, [f"message {n:03d} with some padding to fill the file" for n in range(40)])
        self.assertGreater(len(log_files.list_log_files(self.directory)), 2)  # it really rolled over


class LineLevelTests(unittest.TestCase):
    def test_classifies_warning_and_error_lines(self) -> None:
        self.assertEqual(line_level("   ❌ Processor error while downloading: boom"), "error")
        self.assertEqual(line_level("Traceback (most recent call last):"), "error")
        self.assertEqual(line_level("   ⚠️ Could not resolve current commit hash"), "warning")
        self.assertIsNone(line_level("   ✅ All mapped destinations are present."))
        self.assertIsNone(line_level("=== Poll cycle 3 @ 2026-10-06 12:00:00 ==="))


if __name__ == "__main__":
    unittest.main()
