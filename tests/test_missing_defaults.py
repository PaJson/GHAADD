"""Adding the optional hand-edited settings that config.json lacks (Settings Save and the Open config.json button).

Only a temp config.json is touched. Run from the project root: python -m unittest discover -s tests -t .
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from modules import config_manager, gui_data

EXPECTED_KEYS = {
    "refresh_seconds", "status_message_seconds", "tray", "notifications", "minimize_to_tray", "close_to_tray",
    "start_minimized", "start_daemon", "silenced_warning_types",
}


class MissingDefaultsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._temp.cleanup)
        self.path = os.path.join(self._temp.name, "config.json")
        patcher = mock.patch.object(config_manager, "_config_file_path", lambda: self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, data: dict) -> None:
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def read(self) -> dict:
        with open(self.path, encoding="utf-8") as handle:
            return json.load(handle)

    def test_the_missing_gui_settings_are_added_with_their_defaults(self) -> None:
        self.write({"polling": {"interval_seconds": 600}})
        self.assertTrue(config_manager.add_missing_defaults())
        config = self.read()
        self.assertEqual(config["polling"], {"interval_seconds": 600})  # nothing else touched
        self.assertEqual(set(config["gui"]), EXPECTED_KEYS)
        self.assertEqual((config["gui"]["refresh_seconds"], config["gui"]["status_message_seconds"]), (3, 6))
        self.assertIs(config["gui"]["tray"], True)
        self.assertIs(config["gui"]["close_to_tray"], False)

    def test_existing_values_are_never_changed(self) -> None:
        self.write({"gui": {"tray": False, "refresh_seconds": 10, "window": {"width": 900}, "close_to_tray": "weird"}})
        config_manager.add_missing_defaults()
        gui = self.read()["gui"]
        self.assertIs(gui["tray"], False)
        self.assertEqual(gui["refresh_seconds"], 10)
        self.assertEqual(gui["close_to_tray"], "weird")  # even an unusual value is the user's to fix
        self.assertEqual(gui["window"], {"width": 900})
        self.assertEqual(set(gui) - {"window"}, EXPECTED_KEYS)

    def test_a_second_run_changes_nothing(self) -> None:
        self.write({})
        self.assertTrue(config_manager.add_missing_defaults())
        before = self.read()
        self.assertFalse(config_manager.add_missing_defaults())
        self.assertEqual(self.read(), before)

    def test_a_wrong_gui_section_is_replaced_by_a_real_one(self) -> None:
        self.write({"gui": "oops"})
        config_manager.add_missing_defaults()
        self.assertEqual(set(self.read()["gui"]), EXPECTED_KEYS)

    def test_the_added_settings_do_not_ask_for_a_daemon_restart(self) -> None:
        self.write({"polling": {"interval_seconds": 600}})
        before = config_manager.get_config_fingerprint(config_manager.load_config())
        config_manager.add_missing_defaults()
        self.assertEqual(config_manager.get_config_fingerprint(config_manager.load_config()), before)

    def test_the_getters_agree_with_the_written_defaults(self) -> None:
        self.write({})
        config_manager.add_missing_defaults()
        config = config_manager.load_config()
        self.assertEqual(config_manager.get_gui_refresh_seconds(config), 3.0)
        self.assertEqual(config_manager.get_gui_status_message_seconds(config), 6.0)
        self.assertTrue(config_manager.get_gui_tray_enabled(config))
        self.assertTrue(config_manager.get_gui_notifications_enabled(config))
        self.assertFalse(config_manager.get_gui_close_to_tray(config))
        self.assertEqual(config_manager.get_gui_minimize_to_tray(config), config_manager.get_gui_minimize_to_tray({}))

    def test_the_example_file_lists_every_one_of_them(self) -> None:
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.example.json"), encoding="utf-8") as handle:
            example = json.load(handle)
        self.assertTrue(EXPECTED_KEYS <= set(example["gui"]))

    def test_the_settings_form_does_not_need_them(self) -> None:
        self.write({})
        self.assertIn("recheck", gui_data.load_settings_form())  # the form works with or without the optional keys

    def test_the_launch_options_default_to_off_and_follow_the_file(self) -> None:
        self.write({})
        config = config_manager.load_config()
        self.assertFalse(config_manager.get_gui_start_minimized(config))
        self.assertFalse(config_manager.get_gui_start_daemon(config))
        self.write({"gui": {"start_minimized": True, "start_daemon": "yes"}})  # only a real boolean counts
        config = config_manager.load_config()
        self.assertTrue(config_manager.get_gui_start_minimized(config))
        self.assertFalse(config_manager.get_gui_start_daemon(config))

    def test_the_launch_options_go_through_the_settings_form(self) -> None:
        from modules import gui_forms

        self.write({"gui": {"start_daemon": True}})
        form = gui_data.load_settings_form()
        self.assertIs(form["start_daemon"], True)
        self.assertIs(form["start_minimized"], False)
        form["start_minimized"] = True
        result = gui_forms.build_settings_changes(form)
        self.assertEqual(result.errors, [])
        self.assertIs(result.changes["gui.start_minimized"], True)
        self.assertIs(result.changes["gui.start_daemon"], True)
        config_manager.set_config_values(result.changes)
        self.assertEqual(
            (self.read()["gui"]["start_minimized"], self.read()["gui"]["start_daemon"]), (True, True)
        )

    def test_the_tray_choices_go_through_the_settings_form(self) -> None:
        from modules import gui_forms

        self.write({"gui": {"tray": False, "close_to_tray": True, "minimize_to_tray": False}})
        form = gui_data.load_settings_form()
        self.assertEqual(
            (form["tray"], form["notifications"], form["minimize_to_tray"], form["close_to_tray"]),
            (False, True, False, True),
        )
        form.update({"tray": True, "notifications": False, "minimize_to_tray": True, "close_to_tray": False})
        result = gui_forms.build_settings_changes(form)
        config_manager.set_config_values(result.changes)
        gui = self.read()["gui"]
        self.assertEqual(
            (gui["tray"], gui["notifications"], gui["minimize_to_tray"], gui["close_to_tray"]), (True, False, True, False)
        )

    def test_the_polling_switch_goes_through_the_settings_form(self) -> None:
        from modules import gui_forms

        self.write({"polling": {"enabled": False, "interval_seconds": 300}})
        form = gui_data.load_settings_form()
        self.assertIs(form["polling_enabled"], False)
        form["polling_enabled"] = True
        result = gui_forms.build_settings_changes(form)
        self.assertEqual(result.errors, [])
        config_manager.set_config_values(result.changes)
        self.assertIs(self.read()["polling"]["enabled"], True)

    def test_the_config_path_is_public(self) -> None:
        self.assertEqual(config_manager.get_config_path(), self.path)


if __name__ == "__main__":
    unittest.main()
