"""Subfolder and limit given to new mapping entries come from config.json (paths.default_subfolder,
processing.default_limit) and fall back to "@GitHub" / 10.

Nothing here touches the real config.json, mapping.json or state.db.
Run from the project root: python -m unittest discover -s tests -t .
"""
import unittest
from unittest import mock

from modules import config_manager, gui_forms, mapping_manager


class GetterTests(unittest.TestCase):
    def test_defaults_when_unset(self) -> None:
        self.assertEqual(config_manager.get_default_subfolder({}), "@GitHub")
        self.assertEqual(config_manager.get_default_repository_limit({}), 10)

    def test_configured_values(self) -> None:
        config = {"paths": {"default_subfolder": " Downloads/GH "}, "processing": {"default_limit": 25}}
        self.assertEqual(config_manager.get_default_subfolder(config), "Downloads/GH")
        self.assertEqual(config_manager.get_default_repository_limit(config), 25)

    def test_empty_subfolder_and_zero_limit_are_allowed(self) -> None:
        config = {"paths": {"default_subfolder": ""}, "processing": {"default_limit": 0}}
        self.assertEqual(config_manager.get_default_subfolder(config), "")
        self.assertEqual(config_manager.get_default_repository_limit(config), 0)

    def test_unusable_values_fall_back(self) -> None:
        for bad in ("C:\\x", "/abs", "..\\up", "a/../b", 5, None):
            with self.subTest(subfolder=bad):
                self.assertEqual(config_manager.get_default_subfolder({"paths": {"default_subfolder": bad}}), "@GitHub")
        for bad in (-1, "x", None):
            with self.subTest(limit=bad):
                self.assertEqual(config_manager.get_default_repository_limit({"processing": {"default_limit": bad}}), 10)


class NewEntryTests(unittest.TestCase):
    def test_a_new_entry_uses_the_configured_defaults(self) -> None:
        config = {"paths": {"default_subfolder": "Releases"}, "processing": {"default_limit": 3}}
        with mock.patch.object(config_manager, "load_config", lambda: config):
            entry = mapping_manager._build_skeleton_entry("o/new", "")
        self.assertEqual((entry["subfolder"], entry["limit"]), ("Releases", 3))

    def test_a_new_entry_without_settings_keeps_the_old_defaults(self) -> None:
        with mock.patch.object(config_manager, "load_config", lambda: {}):
            entry = mapping_manager._build_skeleton_entry("o/new", "")
        self.assertEqual((entry["subfolder"], entry["limit"]), ("@GitHub", 10))


class SettingsFormTests(unittest.TestCase):
    def form(self, **overrides):
        form = {
            "recheck": "5", "max_emails": "0", "dest_check": "10", "default_limit": "10",
            "default_subfolder": "@GitHub", "interval": "300", "jitter_min": "5", "jitter_max": "30",
            "download_dir": ".", "log_enabled": False, "log_max_mb": "10", "log_keep": "30",
        }
        form.update(overrides)
        return form

    def test_saved_under_the_agreed_sections(self) -> None:
        result = gui_forms.build_settings_changes(self.form(default_limit="4", default_subfolder="GH"))
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.changes["processing.default_limit"], 4)
        self.assertEqual(result.changes["paths.default_subfolder"], "GH")

    def test_an_empty_subfolder_is_valid(self) -> None:
        self.assertTrue(gui_forms.build_settings_changes(self.form(default_subfolder="")).ok)

    def test_bad_values_are_reported(self) -> None:
        for overrides, fragment in (
            ({"default_subfolder": "..\\x"}, "Default subfolder"),
            ({"default_subfolder": "C:\\x"}, "Default subfolder"),
            ({"default_limit": "-2"}, "Default limit"),
            ({"default_limit": "x"}, "Default limit"),
        ):
            with self.subTest(**overrides):
                result = gui_forms.build_settings_changes(self.form(**overrides))
                self.assertFalse(result.ok)
                self.assertIn(fragment, " ".join(result.errors))


if __name__ == "__main__":
    unittest.main()
