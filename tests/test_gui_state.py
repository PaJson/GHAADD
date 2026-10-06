"""Tests for the remembered GUI window state (config.json "gui.window").

Run from the project root: python -m unittest discover -s tests -t .
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from modules import config_manager, gui_state
from modules.gui_state import WindowState


class ParseGeometryTests(unittest.TestCase):
    def test_parses_positive_and_negative_positions(self) -> None:
        self.assertEqual(gui_state.parse_geometry("1280x720+40+60"), WindowState(1280, 720, 40, 60))
        self.assertEqual(gui_state.parse_geometry("900x520+-1920+-8"), WindowState(900, 520, -1920, -8))

    def test_rejects_garbage(self) -> None:
        for bad in ("", "1280x720", "wide", "1280x+40+60", "900x520-10+5"):
            with self.subTest(geometry=bad):
                self.assertIsNone(gui_state.parse_geometry(bad))

    def test_geometry_round_trip(self) -> None:
        for state in (WindowState(1280, 720, 40, 60), WindowState(900, 520, -1920, -8), WindowState(800, 600)):
            with self.subTest(state=state):
                text = gui_state.to_geometry(state)
                self.assertEqual(gui_state.parse_geometry(text) or WindowState(800, 600), state)


class LoadStateTests(unittest.TestCase):
    def test_missing_or_broken_section_gives_none(self) -> None:
        for config in ({}, {"gui": []}, {"gui": {"window": "x"}}, {"gui": {"window": {"width": 100}}},
                       {"gui": {"window": {"width": 0, "height": 5}}}, {"gui": {"window": {"width": "1", "height": 5}}}):
            with self.subTest(config=config):
                self.assertIsNone(gui_state.load_window_state(config))

    def test_loads_values_and_ignores_bad_position(self) -> None:
        config = {"gui": {"window": {"width": 1300, "height": 700, "x": "oops", "y": 20, "maximized": True}}}

        state = gui_state.load_window_state(config)

        self.assertEqual(state, WindowState(1300, 700, None, 20, True))


class FitToScreenTests(unittest.TestCase):
    SCREEN = (0, 0, 1920, 1080)

    def fit(self, state: WindowState) -> WindowState:
        return gui_state.fit_to_screen(state, *self.SCREEN, 900, 520)

    def test_keeps_a_normal_state(self) -> None:
        state = WindowState(1280, 720, 40, 60)
        self.assertEqual(self.fit(state), state)

    def test_shrinks_to_screen_but_not_below_minimum(self) -> None:
        self.assertEqual(self.fit(WindowState(5000, 4000, 0, 0)).width, 1920)
        self.assertEqual(self.fit(WindowState(5000, 4000, 0, 0)).height, 1080)
        self.assertEqual((self.fit(WindowState(100, 100)).width, self.fit(WindowState(100, 100)).height), (900, 520))

    def test_drops_position_when_off_screen(self) -> None:
        for x, y in ((4000, 10), (-2500, 10), (10, 3000), (10, -200)):
            with self.subTest(x=x, y=y):
                fitted = self.fit(WindowState(1280, 720, x, y))
                self.assertEqual((fitted.x, fitted.y), (None, None))

    def test_keeps_position_on_a_left_monitor(self) -> None:
        fitted = gui_state.fit_to_screen(WindowState(1280, 720, -1900, 40), -1920, 0, 3840, 1080, 900, 520)
        self.assertEqual((fitted.x, fitted.y), (-1900, 40))

    def test_keeps_maximized_flag(self) -> None:
        self.assertTrue(self.fit(WindowState(1280, 720, 0, 0, True)).maximized)


class PlacePopupTests(unittest.TestCase):
    SCREEN = (0, 0, 1920, 1080)

    def place(self, x, y, width=400, height=60):
        return gui_state.place_popup(x, y, width, height, *self.SCREEN)

    def test_normal_case_is_below_right_of_the_pointer(self) -> None:
        self.assertEqual(self.place(500, 300), (514, 318))

    def test_near_the_right_edge_it_moves_left_and_stays_on_screen(self) -> None:
        x, y = self.place(1900, 300, width=600)
        self.assertEqual(x + 600, 1920 - 8)
        self.assertEqual(y, 318)

    def test_near_the_bottom_edge_it_flips_above_the_pointer(self) -> None:
        x, y = self.place(500, 1060, height=80)
        self.assertEqual((x, y + 80 <= 1060), (514, True))

    def test_a_popup_wider_than_the_screen_still_starts_on_screen(self) -> None:
        x, _ = self.place(100, 100, width=5000)
        self.assertEqual(x, 8)

    def test_secondary_monitor_to_the_left_is_respected(self) -> None:
        x, _ = gui_state.place_popup(-100, 300, 400, 60, -1920, 0, 3840, 1080)
        self.assertEqual(x, -86)  # fits on the left monitor, no clamping needed


class SaveStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.config_path = os.path.join(self._temp_dir.name, "config.json")
        patcher = mock.patch.object(config_manager, "_config_file_path", lambda: self.config_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_round_trip_keeps_other_settings(self) -> None:
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump({"polling": {"interval_seconds": 300}}, handle)
        state = WindowState(1300, 700, -20, 30, True)

        self.assertTrue(gui_state.save_window_state(state))

        self.assertEqual(gui_state.load_window_state(), state)
        with open(self.config_path, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["polling"], {"interval_seconds": 300})
        self.assertEqual(data["gui"]["window"]["maximized"], True)

    def test_saving_the_same_state_again_does_not_rewrite(self) -> None:
        state = WindowState(1300, 700, 1, 2)
        gui_state.save_window_state(state)
        self.assertFalse(gui_state.save_window_state(state))

    def test_state_without_position_is_saved_without_it(self) -> None:
        gui_state.save_window_state(WindowState(1000, 600))
        self.assertEqual(gui_state.load_window_state(), WindowState(1000, 600))


if __name__ == "__main__":
    unittest.main()
