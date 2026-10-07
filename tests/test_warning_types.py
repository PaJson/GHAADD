"""Which warning types may pop up a tray notification (gui.silenced_warning_types).

Only a temp config.json is touched. Run from the project root: python -m unittest discover -s tests -t .
"""
import json
import os
import re
import tempfile
import unittest
from unittest import mock

from modules import config_manager, gui_data, warning_types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TypeTests(unittest.TestCase):
    def test_the_row_text_becomes_a_type_code(self) -> None:
        self.assertEqual(warning_types.type_of("API"), "API")
        self.assertEqual(warning_types.type_of("Partial move"), "PARTIAL_MOVE")
        self.assertEqual(warning_types.type_of("LIMIT (+3 earlier)"), "LIMIT")
        self.assertEqual(warning_types.type_of(None), "")

    def test_a_silenced_type_does_not_notify_and_the_others_do(self) -> None:
        silenced = ["API", "LIMIT"]
        self.assertFalse(warning_types.wants_notice("API", silenced))
        self.assertFalse(warning_types.wants_notice("LIMIT (+2 earlier)", silenced))
        self.assertTrue(warning_types.wants_notice("SANITY_CHECK", silenced))
        self.assertTrue(warning_types.wants_notice("Partial move", silenced))
        self.assertFalse(warning_types.wants_notice("Partial move", ["partial_move"]))  # case does not matter

    def test_the_tray_dot_counts_only_warnings_that_notify(self) -> None:
        kinds = ["API", "API", "SANITY_CHECK", "Partial move", "LIMIT"]
        self.assertEqual(warning_types.count_notifying(kinds, ["API", "LIMIT"]), 2)
        self.assertEqual(warning_types.count_notifying(kinds, []), 5)
        self.assertEqual(warning_types.count_notifying(kinds, ["API", "SANITY_CHECK", "PARTIAL_MOVE", "LIMIT"]), 0)
        self.assertEqual(warning_types.count_notifying([], ["API"]), 0)
        self.assertEqual(warning_types.count_notifying(["api"], ["API"]), 0)  # case does not matter

    def test_a_type_nobody_listed_notifies(self) -> None:
        self.assertTrue(warning_types.wants_notice("SOMETHING_NEW", warning_types.DEFAULT_SILENCED))

    def test_the_summary_counts_known_types_only(self) -> None:
        total = len(warning_types.KNOWN_TYPES)
        self.assertEqual(warning_types.summary([]), f"{total} of {total} warning types notify")
        self.assertEqual(warning_types.summary(["API", "LIMIT"]), f"{total - 2} of {total} warning types notify")
        self.assertEqual(warning_types.summary(["NOT_A_TYPE"]), f"{total} of {total} warning types notify")

    def test_every_listed_type_has_a_meaning_and_no_type_is_listed_twice(self) -> None:
        codes = [code for code, _ in warning_types.WARNING_TYPES]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertTrue(all(meaning.strip() for _, meaning in warning_types.WARNING_TYPES))
        self.assertTrue(set(warning_types.DEFAULT_SILENCED) <= set(codes))
        self.assertEqual(warning_types.describe("API")[:5], "Could")
        self.assertEqual(warning_types.describe("NOPE"), "")

    def test_every_type_the_app_logs_is_in_the_checklist(self) -> None:
        """A new log_warning("X", ...) in the code must also get a line in warning_types, or it could never be silenced."""
        logged = set()
        for name in os.listdir(os.path.join(ROOT, "modules")):
            if name.endswith(".py"):
                with open(os.path.join(ROOT, "modules", name), encoding="utf-8") as handle:
                    logged |= set(re.findall(r"log_warning\(\s*\"([A-Z_]+)\"", handle.read()))
        self.assertTrue(logged, "found no log_warning calls at all: the pattern needs updating")
        self.assertEqual(sorted(logged - set(warning_types.KNOWN_TYPES)), [])


class ConfigTests(unittest.TestCase):
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

    def test_the_default_is_api_and_limit(self) -> None:
        self.write({})
        self.assertEqual(config_manager.get_gui_silenced_warning_types(), ["API", "LIMIT"])

    def test_an_empty_list_means_everything_notifies(self) -> None:
        self.write({"gui": {"silenced_warning_types": []}})
        self.assertEqual(config_manager.get_gui_silenced_warning_types(), [])

    def test_the_list_is_cleaned_up(self) -> None:
        self.write({"gui": {"silenced_warning_types": [" sanity_check", "API", "api", ""]}})
        self.assertEqual(config_manager.get_gui_silenced_warning_types(), ["API", "SANITY_CHECK"])

    def test_a_value_that_is_not_a_list_of_strings_gives_the_default(self) -> None:
        for bad in ("API", 3, [1, 2], None, {"API": True}):
            self.write({"gui": {"silenced_warning_types": bad}})
            self.assertEqual(config_manager.get_gui_silenced_warning_types(), ["API", "LIMIT"], bad)

    def test_the_settings_form_carries_it_and_saving_round_trips(self) -> None:
        self.write({"gui": {"silenced_warning_types": ["MAILBOX"]}})
        self.assertEqual(gui_data.load_settings_form()["silenced_warning_types"], ["MAILBOX"])
        config_manager.set_config_values({"gui.silenced_warning_types": ["MOVE", "API"]})
        self.assertEqual(config_manager.get_gui_silenced_warning_types(), ["API", "MOVE"])

    def test_a_missing_key_is_added_with_the_default_and_never_overwritten(self) -> None:
        self.write({"gui": {}})
        config_manager.add_missing_defaults()
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["gui"]["silenced_warning_types"], ["API", "LIMIT"])
        self.write({"gui": {"silenced_warning_types": []}})
        config_manager.add_missing_defaults()
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["gui"]["silenced_warning_types"], [])  # the user's empty list stays

    def test_the_setting_does_not_ask_for_a_daemon_restart(self) -> None:
        self.write({})
        before = config_manager.get_config_fingerprint(config_manager.load_config())
        config_manager.set_config_values({"gui.silenced_warning_types": []})
        self.assertEqual(config_manager.get_config_fingerprint(config_manager.load_config()), before)


if __name__ == "__main__":
    unittest.main()
