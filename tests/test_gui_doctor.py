"""The Doctor button's logic (first-run detection, report texts) and centring dialogs over the main window.

No window is opened and nothing real is read: paths and the environment are passed in.
Run from the project root: python -m unittest discover -s tests -t .
"""
import os
import tempfile
import unittest
from unittest import mock

from modules import gui_doctor, gui_state

FULL_ENV = {"GMAIL_USER": "me@example.invalid", "GMAIL_APP_PASSWORD": "secret"}


class FirstRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._temp_dir.cleanup)
        self.config = os.path.join(self._temp_dir.name, "config.json")
        self.mapping = os.path.join(self._temp_dir.name, "mapping.json")

    def touch(self, *paths: str) -> None:
        for path in paths:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{}")

    def reasons(self, environment=FULL_ENV) -> list[str]:
        return gui_doctor.first_run_reasons(environment, self.config, self.mapping)

    def test_a_complete_setup_is_not_a_first_run(self) -> None:
        self.touch(self.config, self.mapping)
        self.assertEqual(self.reasons(), [])

    def test_a_brand_new_install_lists_everything_that_is_missing(self) -> None:
        reasons = self.reasons({})
        self.assertEqual(len(reasons), 3)
        joined = " ".join(reasons)
        for fragment in ("config.json", "mapping.json", "GMAIL_USER and GMAIL_APP_PASSWORD"):
            self.assertIn(fragment, joined)

    def test_each_missing_piece_is_reported_on_its_own(self) -> None:
        self.touch(self.mapping)
        self.assertEqual([r.split(" ")[0] for r in self.reasons()], ["config.json"])
        self.touch(self.config)
        os.remove(self.mapping)
        self.assertEqual([r.split(" ")[0] for r in self.reasons()], ["mapping.json"])
        self.touch(self.mapping)
        self.assertIn("GMAIL_APP_PASSWORD not set", self.reasons({"GMAIL_USER": "me"})[0])
        self.assertIn("GMAIL_USER not set", self.reasons({"GMAIL_APP_PASSWORD": "x"})[0])

    def test_an_empty_value_counts_as_missing(self) -> None:
        self.touch(self.config, self.mapping)
        self.assertEqual(len(self.reasons({"GMAIL_USER": "", "GMAIL_APP_PASSWORD": ""})), 1)

    def test_the_real_environment_is_used_when_none_is_given(self) -> None:
        self.touch(self.config, self.mapping)
        with mock.patch.object(gui_doctor, "load_environment"), mock.patch.dict(os.environ, FULL_ENV):
            self.assertEqual(gui_doctor.first_run_reasons(None, self.config, self.mapping), [])


class ReportTextTests(unittest.TestCase):
    def report(self, errors=(), warnings=()):
        return {"ok": not errors, "platform": "win32", "errors": list(errors), "warnings": list(warnings), "checks": ["c"]}

    def test_summary_lines(self) -> None:
        self.assertEqual(gui_doctor.summary_line(self.report()), "Everything looks fine.")
        self.assertIn("2 warning(s) to look at", gui_doctor.summary_line(self.report(warnings=["a", "b"])))
        self.assertEqual(gui_doctor.summary_line(self.report(errors=["x"])), "1 problem(s) need fixing.")
        self.assertEqual(gui_doctor.summary_line(self.report(errors=["x"], warnings=["w"])), "1 problem(s) need fixing, 1 warning(s).")

    def test_the_button_is_highlighted_for_a_first_run_or_for_errors_only(self) -> None:
        self.assertFalse(gui_doctor.needs_attention([]))
        self.assertFalse(gui_doctor.needs_attention([], self.report(warnings=["w"])))  # warnings are not alarming
        self.assertTrue(gui_doctor.needs_attention(["first run"]))
        self.assertTrue(gui_doctor.needs_attention([], self.report(errors=["x"])))


class CenterTests(unittest.TestCase):
    SCREEN = (0, 0, 1920, 1080)

    def center(self, parent, size, screen=SCREEN):
        return gui_state.center_over(*parent, *size, *screen)

    def test_a_dialog_is_centred_over_its_parent(self) -> None:
        self.assertEqual(self.center((100, 100, 1000, 600), (400, 200)), (400, 300))

    def test_it_stays_on_the_screen(self) -> None:
        # Parent hanging off the bottom right: the dialog is pulled back inside the screen.
        x, y = self.center((1500, 800, 800, 600), (600, 400))
        self.assertLessEqual(x + 600, 1920 - 8)
        self.assertLessEqual(y + 400, 1080 - 8)
        # Parent hanging off the top left.
        self.assertEqual(self.center((-500, -300, 700, 500), (400, 200)), (8, 8))

    def test_a_second_monitor_to_the_left_works(self) -> None:
        screen = (-1920, 0, 3840, 1080)  # two monitors side by side
        self.assertEqual(self.center((-1800, 100, 1000, 600), (400, 200), screen), (-1500, 300))

    def test_a_dialog_bigger_than_the_screen_goes_to_the_corner(self) -> None:
        self.assertEqual(self.center((0, 0, 800, 600), (3000, 2000)), (8, 8))


if __name__ == "__main__":
    unittest.main()
