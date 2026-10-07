"""config.example.json and mapping.example.json are documentation (no code reads them), so they must stay correct
and complete: valid JSON, accepted by the validators, and showing every setting the app knows.

Run from the project root: python -m unittest discover -s tests -t .
"""
import json
import os
import unittest
from unittest import mock

from modules import config_manager, mapping_manager

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name: str) -> dict:
    with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
        return json.load(handle)


class MappingExampleTests(unittest.TestCase):
    def test_it_passes_the_same_validation_as_a_real_file(self) -> None:
        result = mapping_manager.validate_mapping_payload(load("mapping.example.json"))
        self.assertEqual((result["errors"], result["warnings"]), ([], []))

    def test_every_field_the_app_knows_is_shown(self) -> None:
        shown = {key for entry in load("mapping.example.json")["repositories"] for key in entry}
        self.assertEqual(set(mapping_manager._MAPPING_FIELD_ORDER) - shown, set())  # a new field must be added to the example
        self.assertEqual(shown - set(mapping_manager._MAPPING_FIELD_ORDER), set())

    def test_keys_are_written_in_the_order_the_app_writes_them(self) -> None:
        for entry in load("mapping.example.json")["repositories"]:
            self.assertEqual(list(entry), list(mapping_manager._MAPPING_FIELD_ORDER))

    def test_the_first_entry_is_what_a_new_repository_gets_with_the_defaults(self) -> None:
        plain = load("mapping.example.json")["repositories"][0]
        with mock.patch.object(config_manager, "load_config", lambda: {}):  # not the real config.json: the built-in defaults
            new = mapping_manager._build_skeleton_entry("example-org/plain-example", plain["last_notification"])
        for key in ("subfolder", "skiplist", "recheck_intervals", "limit", "limit_folders", "sanity_check", "last_finalized", "active"):
            self.assertEqual(plain[key], new[key], key)
        self.assertEqual(plain["folder"], new["folder"])

    def test_the_second_entry_shows_non_default_values(self) -> None:
        plain, custom = load("mapping.example.json")["repositories"]
        different = {key for key in custom if key not in ("repository", "destination") and custom[key] != plain[key]}
        self.assertTrue({"subfolder", "skiplist", "recheck_intervals", "limit", "limit_folders", "sanity_check", "shared_destination"} <= different, different)


class ConfigExampleTests(unittest.TestCase):
    def test_every_setting_has_a_getter_that_accepts_it(self) -> None:
        config = load("config.example.json")
        self.assertEqual(config_manager.get_default_subfolder(config), "")
        self.assertEqual(config_manager.get_default_repository_limit(config), 10)
        self.assertEqual(config_manager.get_gui_refresh_seconds(config), 3.0)
        self.assertEqual(config_manager.get_gui_status_message_seconds(config), 6.0)
        self.assertEqual(config_manager.get_polling_settings(config)["interval_seconds"], 300)
        self.assertEqual(config_manager.get_destination_check_every_n_polls(config), 10)

    def test_the_example_matches_the_built_in_defaults_of_the_new_entry_settings(self) -> None:
        config = load("config.example.json")
        self.assertEqual(config["paths"]["default_subfolder"], config_manager.DEFAULT_SUBFOLDER)
        self.assertEqual(config["processing"]["default_limit"], config_manager.DEFAULT_REPOSITORY_LIMIT)
        self.assertEqual(config["gui"]["refresh_seconds"], config_manager.DEFAULT_GUI_REFRESH_SECONDS)


if __name__ == "__main__":
    unittest.main()
