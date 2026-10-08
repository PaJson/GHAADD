"""Only one GUI window: the lock, the "come forward" note, and a second process being turned away.

Only temporary folders are used. Run from the project root: python -m unittest discover -s tests -t .
"""
import importlib
import os
import subprocess
import sys
import tempfile
import unittest
from typing import Any
from unittest import mock

from modules import gui_instance

main_gui: Any
try:
    main_gui = importlib.import_module("main_gui")
except ImportError:  # no Tk on this machine
    main_gui = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class InstanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._temp.cleanup)
        self.addCleanup(gui_instance.release)
        self.directory = self._temp.name

    def test_the_first_window_gets_the_lock_and_a_second_one_does_not(self) -> None:
        self.assertTrue(gui_instance.acquire(self.directory))
        self.assertFalse(gui_instance.acquire(self.directory))

    def test_after_release_a_new_window_can_start(self) -> None:
        self.assertTrue(gui_instance.acquire(self.directory))
        gui_instance.release()
        self.assertTrue(gui_instance.acquire(self.directory))

    def test_the_note_is_taken_once(self) -> None:
        self.assertFalse(gui_instance.take_show_request(self.directory))
        self.assertTrue(gui_instance.request_show(self.directory))
        self.assertTrue(gui_instance.take_show_request(self.directory))
        self.assertFalse(gui_instance.take_show_request(self.directory))

    def test_two_notes_in_a_row_are_one(self) -> None:
        gui_instance.request_show(self.directory)
        gui_instance.request_show(self.directory)
        self.assertTrue(gui_instance.take_show_request(self.directory))
        self.assertFalse(gui_instance.take_show_request(self.directory))
        self.assertEqual([name for name in os.listdir(self.directory) if name.endswith(".tmp")], [])  # nothing left over

    def test_a_stale_note_is_dropped_when_a_window_starts(self) -> None:
        gui_instance.request_show(self.directory)
        self.assertTrue(gui_instance.acquire(self.directory))
        self.assertFalse(gui_instance.take_show_request(self.directory))

    def test_a_folder_that_cannot_hold_the_lock_does_not_block_the_start(self) -> None:
        not_a_folder = os.path.join(self.directory, "blocker")
        open(not_a_folder, "w").close()  # a file where the folder should be: nothing can be created inside it
        self.assertTrue(gui_instance.acquire(not_a_folder))
        self.assertFalse(gui_instance.request_show(not_a_folder))  # and the note simply cannot be written
        self.assertFalse(gui_instance.take_show_request(not_a_folder))

    def test_the_files_are_ignored_by_git(self) -> None:
        with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as handle:
            lines = {line.strip() for line in handle}
        self.assertIn(gui_instance.LOCK_FILE_NAME, lines)
        self.assertIn(gui_instance.SHOW_FILE_NAME, lines)


class OtherProcessTests(unittest.TestCase):
    def test_a_window_in_another_process_turns_this_one_away_until_it_ends(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            code = (
                "import sys\n"
                "from modules import gui_instance\n"
                f"print(gui_instance.acquire({directory!r}), flush=True)\n"
                "sys.stdin.readline()\n"  # hold the lock until the test closes stdin
            )
            holder = subprocess.Popen(
                [sys.executable, "-c", code], cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
            )
            self.addCleanup(holder.kill)
            assert holder.stdout is not None
            self.assertEqual(holder.stdout.readline().strip(), "True")
            try:
                self.assertFalse(gui_instance.acquire(directory))  # the first process is "the open window"
                self.assertTrue(gui_instance.request_show(directory))
            finally:
                assert holder.stdin is not None
                holder.stdin.close()
                holder.wait(timeout=20)
            self.addCleanup(gui_instance.release)
            self.assertTrue(gui_instance.acquire(directory))  # it ended, so the lock is free again
            self.assertFalse(gui_instance.take_show_request(directory))  # the note that was left was stale and dropped


@unittest.skipIf(main_gui is None, "tkinter is not available")
class SecondStartTests(unittest.TestCase):
    """Starting the GUI while one is open brings that window forward, however this start was configured."""

    def second_start(self, *argv: str, start_minimized_setting: bool = False) -> list[bool]:
        shown: list[bool] = []
        with mock.patch.object(sys, "argv", ["main_gui.py", *argv]), \
                mock.patch.object(main_gui.gui_instance, "acquire", lambda: False), \
                mock.patch.object(main_gui.gui_instance, "request_show", lambda: shown.append(True) or True), \
                mock.patch.object(main_gui.config_manager, "get_gui_start_minimized", lambda config=None: start_minimized_setting), \
                mock.patch.object(main_gui, "MainWindow", side_effect=AssertionError("a second window must not open")):
            main_gui.main()
        return shown

    def test_a_plain_second_start_asks_the_open_window_to_come_forward(self) -> None:
        self.assertEqual(self.second_start(), [True])

    def test_a_second_start_asking_to_be_minimized_still_brings_the_window_forward(self) -> None:
        self.assertEqual(self.second_start("--minimized"), [True])

    def test_the_start_minimized_setting_does_not_keep_the_window_hidden_either(self) -> None:
        self.assertEqual(self.second_start(start_minimized_setting=True), [True])
        self.assertEqual(self.second_start("--minimized", start_minimized_setting=True), [True])


if __name__ == "__main__":
    unittest.main()
