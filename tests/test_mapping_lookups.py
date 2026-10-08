"""Reading mapping.json: the per-repository lookups (skiplist, recheck intervals, limit folders, active),
loading a missing or broken file, finding vanished destinations, and what validation accepts and rejects.

All inside a temporary folder; the real mapping.json and state.db are never touched.
Run from the project root: python -m unittest discover -s tests -t .
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from typing import Any, cast
from unittest import mock

from modules import config_manager, mapping_manager


class LookupTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._temp_dir.cleanup)
        self.root = self._temp_dir.name
        self.path = os.path.join(self.root, "mapping.json")
        self.config = {"processing": {"recheck_intervals_minutes": [5, 15, 60]}}
        for patcher in (
            mock.patch.object(mapping_manager, "_mapping_file_path", lambda: self.path),
            mock.patch.object(mapping_manager, "log_warning"),
            mock.patch.object(mapping_manager, "get_recheck_intervals_minutes", lambda: list(self.config["processing"]["recheck_intervals_minutes"])),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        mapping_manager._BACKED_UP_INVALID_MAPPING_SIGNATURES.clear()

    def write(self, *entries) -> None:
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"repositories": list(entries)}, handle)

    def quietly(self, function, *args):
        with contextlib.redirect_stdout(io.StringIO()):
            return function(*args)


class RepositoryLookupTests(LookupTestCase):
    def test_a_repository_is_found_whatever_its_capitalisation(self) -> None:
        self.write({"repository": "Owner/App", "destination": "x"})
        self.assertEqual((mapping_manager.get_repository_mapping("owner/app") or {})["destination"], "x")
        self.assertEqual((mapping_manager.get_repository_mapping("  OWNER/APP ") or {})["destination"], "x")

    def test_unknown_or_empty_names_find_nothing(self) -> None:
        self.write({"repository": "o/app"})
        for name in ("o/other", "", "   ", None):
            with self.subTest(name=name):
                self.assertIsNone(mapping_manager.get_repository_mapping(cast(str, name)))  # None on purpose: it must cope

    def test_default_folder_names(self) -> None:
        self.assertEqual(mapping_manager.build_default_folder("stenzek/duckstation"), "duckstation (stenzek)")
        self.assertEqual(mapping_manager.build_default_folder("solo"), "solo (solo)")
        self.assertEqual(mapping_manager.build_default_folder(""), "unknown (unknown)")
        self.assertEqual(mapping_manager.build_default_folder("/repo"), "repo (unknown)")


class SkiplistTests(LookupTestCase):
    def test_a_listed_release_type_is_skipped_whatever_its_capitalisation(self) -> None:
        self.write({"repository": "o/app", "skiplist": ["pre-RELEASE"]})
        self.assertTrue(mapping_manager.is_release_type_skipped("o/app", "Pre-release"))
        self.assertFalse(mapping_manager.is_release_type_skipped("o/app", "Release"))

    def test_no_release_type_counts_as_a_release(self) -> None:
        self.write({"repository": "o/app", "skiplist": ["Release"]})
        self.assertTrue(mapping_manager.is_release_type_skipped("o/app", None))
        self.assertTrue(mapping_manager.is_release_type_skipped("o/app", ""))

    def test_nothing_is_skipped_without_a_skiplist_or_for_an_unknown_repository(self) -> None:
        self.write({"repository": "o/app"}, {"repository": "o/empty", "skiplist": []})
        self.assertFalse(mapping_manager.is_release_type_skipped("o/app", "Release"))
        self.assertFalse(mapping_manager.is_release_type_skipped("o/empty", "Pre-release"))
        self.assertFalse(mapping_manager.is_release_type_skipped("o/unknown", "Release"))

    def test_both_types_in_the_skiplist_skip_everything(self) -> None:
        self.write({"repository": "o/app", "skiplist": ["Release", "Pre-release"]})
        self.assertTrue(mapping_manager.is_release_type_skipped("o/app", "Release"))
        self.assertTrue(mapping_manager.is_release_type_skipped("o/app", "Pre-release"))

    def test_junk_in_the_skiplist_is_ignored(self) -> None:
        self.write({"repository": "o/app", "skiplist": ["", "  ", 5, None, " Pre-release ", "pre-release"]})
        self.assertEqual(mapping_manager.get_repository_skiplist("o/app"), ["Pre-release"])
        self.write({"repository": "o/app", "skiplist": "Release"})  # not a list
        self.assertEqual(mapping_manager.get_repository_skiplist("o/app"), [])


class RecheckIntervalTests(LookupTestCase):
    def test_a_repository_override_wins(self) -> None:
        self.write({"repository": "o/app", "recheck_intervals": [3, 10, 30]})
        self.assertEqual(mapping_manager.get_repository_recheck_intervals_minutes("o/app"), [3, 10, 30])

    def test_the_global_list_is_used_when_there_is_no_usable_override(self) -> None:
        for override in (None, [], "5", [0, -3, "x", True], {"a": 1}):
            with self.subTest(override=override):
                entry: dict[str, Any] = {"repository": "o/app"}
                if override is not None:
                    entry["recheck_intervals"] = override
                self.write(entry)
                self.assertEqual(mapping_manager.get_repository_recheck_intervals_minutes("o/app"), [5, 15, 60])
        self.assertEqual(mapping_manager.get_repository_recheck_intervals_minutes("o/unknown"), [5, 15, 60])

    def test_bad_items_are_dropped_and_duplicates_collapsed(self) -> None:
        self.write({"repository": "o/app", "recheck_intervals": [10, "20", 10, 0, "x", True, 30.0]})
        self.assertEqual(mapping_manager.get_repository_recheck_intervals_minutes("o/app"), [10, 20, 30])

    def test_changing_the_returned_list_does_not_change_the_global_one(self) -> None:
        self.write({"repository": "o/app"})
        mapping_manager.get_repository_recheck_intervals_minutes("o/app").append(999)
        self.assertEqual(mapping_manager.get_repository_recheck_intervals_minutes("o/app"), [5, 15, 60])


class LimitFolderAndActiveTests(LookupTestCase):
    def test_limit_folders_are_cleaned_up(self) -> None:
        self.write({"repository": "o/app", "limit_folders": [" Release ", "release", "", "Pre-release", 3]})
        self.assertEqual(mapping_manager.get_repository_limit_release_type_folders("o/app"), ["Release", "Pre-release"])
        self.assertEqual(mapping_manager.get_repository_limit_release_type_folders("o/unknown"), [])

    def test_a_repository_is_active_unless_it_says_otherwise(self) -> None:
        self.write(
            {"repository": "o/on", "active": True},
            {"repository": "o/off", "active": False},
            {"repository": "o/unset"},
            {"repository": "o/odd", "active": "no"},
        )
        self.assertTrue(mapping_manager.is_repository_active("o/on"))
        self.assertFalse(mapping_manager.is_repository_active("o/off"))
        self.assertTrue(mapping_manager.is_repository_active("o/unset"))
        self.assertTrue(mapping_manager.is_repository_active("o/odd"))  # only a real false switches a repository off
        self.assertTrue(mapping_manager.is_repository_active("o/unknown"))


class LoadingTests(LookupTestCase):
    def test_a_missing_file_gives_an_empty_mapping(self) -> None:
        self.assertEqual(mapping_manager.load_mapping(), {"repositories": []})
        self.assertIsNone(mapping_manager.load_mapping_raw())

    def test_a_broken_file_gives_an_empty_mapping_and_is_backed_up(self) -> None:
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write('{"repositories": [ {"repository": ')
        self.assertEqual(mapping_manager.load_mapping(), {"repositories": []})
        backups = [name for name in os.listdir(self.root) if name.startswith("mapping.json_")]
        self.assertEqual(len(backups), 1)
        with open(os.path.join(self.root, backups[0]), encoding="utf-8") as handle:
            self.assertIn('"repository"', handle.read())  # the broken file is kept for you to look at
        mapping_manager.load_mapping()
        self.assertEqual(len([n for n in os.listdir(self.root) if n.startswith("mapping.json_")]), 1)  # once per version

    def test_wrong_shapes_give_an_empty_mapping(self) -> None:
        for text in ("[]", '"text"', "5", '{"repositories": {}}', '{"other": 1}'):
            with self.subTest(text=text):
                with open(self.path, "w", encoding="utf-8") as handle:
                    handle.write(text)
                self.assertEqual(mapping_manager.load_mapping(), {"repositories": []})

    def test_entries_that_are_not_objects_are_dropped(self) -> None:
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"repositories": [{"repository": "o/a"}, "junk", 5, None, ["x"]]}, handle)
        self.assertEqual(mapping_manager.load_mapping(), {"repositories": [{"repository": "o/a"}]})


class MissingDestinationTests(LookupTestCase):
    def test_only_mapped_destinations_that_are_gone_are_reported(self) -> None:
        present = os.path.join(self.root, "present")
        os.makedirs(present)
        gone = os.path.join(self.root, "gone")
        self.write(
            {"repository": "o/ok", "destination": present},
            {"repository": "o/gone", "destination": gone},
            {"repository": "o/blank", "destination": "   "},
            {"repository": "o/none"},
        )
        missing = mapping_manager.find_missing_mapped_destinations()
        self.assertEqual([m["name"] for m in missing], ["o/gone"])
        self.assertEqual(missing[0]["resolved_destination"], os.path.normpath(gone))

    def test_environment_variables_in_a_destination_are_expanded(self) -> None:
        target = os.path.join(self.root, "from-env")
        os.makedirs(target)
        with mock.patch.dict(os.environ, {"GHAADD_TEST_ROOT": self.root}):
            self.write({"repository": "o/env", "destination": os.path.join("%GHAADD_TEST_ROOT%", "from-env")
                        if os.name == "nt" else "$GHAADD_TEST_ROOT/from-env"})
            self.assertEqual(mapping_manager.find_missing_mapped_destinations(), [])

    def test_each_missing_destination_is_warned_about_once(self) -> None:
        self.write({"repository": "o/a", "destination": os.path.join(self.root, "x")},
                   {"repository": "o/b", "destination": os.path.join(self.root, "y")})
        self.assertEqual(self.quietly(mapping_manager.warn_about_missing_mapped_destinations), 2)
        warn = cast(mock.Mock, mapping_manager.log_warning)  # replaced by a mock in setUp
        self.assertEqual(warn.call_count, 2)
        self.assertTrue(all(call.args[0] == "MAPPING" for call in warn.call_args_list))


class ValidationTests(LookupTestCase):
    def check(self, *entries):
        return mapping_manager.validate_mapping_payload({"repositories": list(entries)})

    def errors(self, *entries) -> str:
        return " | ".join(self.check(*entries)["errors"])

    def test_a_good_entry_has_no_errors(self) -> None:
        result = self.check({
            "repository": "o/app", "folder": "App", "subfolder": "@GitHub", "destination": "X:\\a", "skiplist": [],
            "recheck_intervals": [5], "limit": 10, "limit_folders": ["Release"], "sanity_check": "same_tag",
            "last_notification": "", "last_finalized": "", "active": True,
        })
        self.assertEqual((result["ok"], result["errors"], result["warnings"]), (True, [], []))

    def test_the_shape_of_the_file_is_checked(self) -> None:
        self.assertFalse(mapping_manager.validate_mapping_payload([])["ok"])
        self.assertFalse(mapping_manager.validate_mapping_payload({"repositories": {}})["ok"])
        self.assertIn("must be an object", self.errors("not an object"))

    def test_the_repository_name_is_required_and_unique(self) -> None:
        self.assertIn("repository must be a non-empty string", self.errors({"destination": "x"}))
        self.assertIn("repository must be a non-empty string", self.errors({"repository": "  "}))
        self.assertIn("duplicates", self.errors({"repository": "o/App"}, {"repository": "O/APP"}))

    def test_wrong_types_are_reported_by_field(self) -> None:
        for field, value in (
            ("folder", 5), ("destination", []), ("subfolder", True), ("last_notification", 1), ("last_finalized", {}),
            ("limit", "10"), ("limit", -1), ("limit", 2.5), ("active", "yes"), ("active", 1),
            ("skiplist", "Release"), ("skiplist", [1]), ("limit_folders", "Release"), ("limit_folders", [None]),
            ("recheck_intervals", "5"), ("sanity_check", "sometimes"), ("sanity_check", 3),
        ):
            with self.subTest(field=field, value=value):
                self.assertIn(f".{field}", self.errors({"repository": "o/app", field: value}))

    def test_a_boolean_is_not_a_valid_limit(self) -> None:
        # Regression: True used to pass because bool is an int in Python (and then meant "limit 1").
        self.assertIn(".limit", self.errors({"repository": "o/app", "limit": True}))
        self.assertIn(".limit", self.errors({"repository": "o/app", "limit": False}))

    def test_zero_is_a_valid_limit_and_switches_the_check_off(self) -> None:
        self.assertEqual(self.errors({"repository": "o/app", "limit": 0}), "")

    def test_soft_problems_are_warnings_not_errors(self) -> None:
        result = self.check({
            "repository": "o/app", "destination": "", "folder": "", "recheck_intervals": [0, "x"],
            "limit_folders": [""], "skiplist": [" "], "mystery": 1,
        })
        self.assertTrue(result["ok"], result["errors"])
        joined = " | ".join(result["warnings"])
        for fragment in ("destination is empty", "folder is empty", "recheck_intervals contains invalid", "limit_folders contains empty",
                         "skiplist contains empty", "unknown key 'mystery'"):
            self.assertIn(fragment, joined)

    def test_two_repositories_sharing_one_target_folder_are_flagged(self) -> None:
        shared = {"destination": os.path.join(self.root, "d"), "folder": "Same", "subfolder": "@GitHub"}
        result = self.check({"repository": "o/a", **shared}, {"repository": "o/b", **shared}, {"repository": "o/c", **shared, "folder": "Other"})
        warning = [w for w in result["warnings"] if "same destination folder" in w]
        self.assertEqual(len(warning), 1)
        self.assertIn("o/a, o/b", warning[0])
        self.assertNotIn("o/c", warning[0])

    def shared(self, *marks):
        """Entries sharing one folder; each mark is True / False / None (= key absent)."""
        shared = {"destination": os.path.join(self.root, "d"), "folder": "Same", "subfolder": "@GitHub"}
        entries = []
        for number, mark in enumerate(marks):
            entry = {"repository": f"o/r{number}", **shared}
            if mark is not None:
                entry["shared_destination"] = mark
            entries.append(entry)
        return [w for w in self.check(*entries)["warnings"] if "same destination folder" in w]

    def test_a_shared_folder_marked_on_purpose_is_not_reported(self) -> None:
        self.assertEqual(len(self.shared(None, None)), 1)  # not marked: the notice stays
        self.assertEqual(len(self.shared(False, False)), 1)
        self.assertEqual(self.shared(True, True), [])
        self.assertEqual(self.shared(True, True, True), [])

    def test_a_group_is_only_silenced_when_every_member_is_marked(self) -> None:
        self.assertEqual(len(self.shared(True, None)), 1)
        self.assertEqual(len(self.shared(True, True, False)), 1)

    def test_one_marked_repository_alone_changes_nothing_for_other_groups(self) -> None:
        shared_a = {"destination": os.path.join(self.root, "a"), "folder": "A", "shared_destination": True}
        shared_b = {"destination": os.path.join(self.root, "b"), "folder": "B"}
        result = self.check({"repository": "o/a1", **shared_a}, {"repository": "o/a2", **shared_a},
                            {"repository": "o/b1", **shared_b}, {"repository": "o/b2", **shared_b})
        notices = [w for w in result["warnings"] if "same destination folder" in w]
        self.assertEqual(len(notices), 1)
        self.assertIn("o/b1, o/b2", notices[0])

    def test_the_notice_tells_how_to_silence_it(self) -> None:
        self.assertIn("shared_destination", self.shared(None, None)[0])

    def test_shared_destination_must_be_a_boolean(self) -> None:
        for value in ("yes", 1, [True]):
            with self.subTest(value=value):
                self.assertIn(".shared_destination must be a boolean", self.errors({"repository": "o/app", "shared_destination": value}))
        self.assertEqual(self.errors({"repository": "o/app", "shared_destination": True}), "")
        self.assertEqual(self.check({"repository": "o/app", "shared_destination": False})["warnings"], [])  # a known key

    def test_the_file_check_reports_a_missing_or_broken_file(self) -> None:
        self.assertFalse(mapping_manager.validate_mapping_schema()["ok"])  # no file
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{broken")
        self.assertIn("invalid JSON", " ".join(mapping_manager.validate_mapping_schema()["errors"]))
        self.write({"repository": "o/app"})
        self.assertTrue(mapping_manager.validate_mapping_schema()["ok"])


if __name__ == "__main__":
    unittest.main()
