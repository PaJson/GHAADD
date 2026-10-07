"""The per-repository file-count sanity check (mapping.json `sanity_check`): any_tag, same_tag, off.

Run from the project root: python -m unittest discover -s tests -t .
"""
import json
import os
import tempfile
import unittest
from contextlib import closing
from unittest import mock

from modules import db_manager, gui_forms, mapping_manager, queue_worker

REPO = "o/app"


class SanityCheckTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._temp_dir.cleanup)
        folder = self._temp_dir.name
        self.db_path = os.path.join(folder, "state.db")
        self.mapping_path = os.path.join(folder, "mapping.json")
        for target, name, value in (
            (db_manager, "get_state_db_path", lambda: self.db_path),
            (mapping_manager, "_mapping_file_path", lambda: self.mapping_path),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.set_mapping({})
        self.connection = db_manager.open_database()
        self.addCleanup(self.connection.close)

    def set_mapping(self, fields: dict) -> None:
        with open(self.mapping_path, "w", encoding="utf-8") as handle:
            json.dump({"repositories": [{"repository": REPO, "destination": folder_of(self), **fields}]}, handle)

    def finished_job(self, tag: str, total: int, release_type: str = "Release") -> int:
        job_id = db_manager.enqueue_job(self.connection, REPO, tag, release_type=release_type)
        db_manager.mark_job_completed(
            self.connection, job_id, attempt_count=6, downloaded_count=0, skipped_count=total,
            total_items=total, last_result="SUCCESS",
        )
        return job_id

    def check(self, job_id: int, tag: str, total: int, release_type: str = "Release") -> list:
        with mock.patch.object(queue_worker, "log_warning") as warn, mock.patch("builtins.print"):
            queue_worker._warn_if_file_count_changed_from_previous_success(
                self.connection, job_id, REPO, tag, release_type, total
            )
        return [call.args for call in warn.call_args_list]


def folder_of(case: SanityCheckTestCase) -> str:
    return case._temp_dir.name


class ModeTests(SanityCheckTestCase):
    def test_default_and_stored_modes(self) -> None:
        self.assertEqual(mapping_manager.get_repository_sanity_check_mode(REPO), "any_tag")
        for stored in ("same_tag", "off", "ANY_TAG"):
            self.set_mapping({"sanity_check": stored})
            self.assertEqual(mapping_manager.get_repository_sanity_check_mode(REPO), stored.lower())
        self.set_mapping({"sanity_check": "bogus"})
        self.assertEqual(mapping_manager.get_repository_sanity_check_mode(REPO), "any_tag")
        self.assertEqual(mapping_manager.get_repository_sanity_check_mode("o/unknown"), "any_tag")

    def test_validation_accepts_the_modes_and_rejects_others(self) -> None:
        for good in ("any_tag", "same_tag", "off"):
            payload = {"repositories": [{"repository": REPO, "destination": "", "sanity_check": good}]}
            self.assertEqual(mapping_manager.validate_mapping_payload(payload)["errors"], [], good)
        payload = {"repositories": [{"repository": REPO, "destination": "", "sanity_check": "sometimes"}]}
        self.assertTrue(any("sanity_check" in e for e in mapping_manager.validate_mapping_payload(payload)["errors"]))

    def test_new_entries_start_with_the_default(self) -> None:
        self.assertEqual(mapping_manager._build_skeleton_entry("o/new", "2026-10-07_10-00")["sanity_check"], "any_tag")


class BehaviourTests(SanityCheckTestCase):
    def test_any_tag_compares_with_the_previous_release_whatever_its_tag(self) -> None:
        self.finished_job("v1.0", 10)
        job = self.finished_job("v1.1", 12)
        warnings = self.check(job, "v1.1", 12)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0][0], "SANITY_CHECK")
        self.assertIn("Current=12, Previous=10", warnings[0][1])

    def test_same_tag_ignores_releases_with_other_tags(self) -> None:
        self.set_mapping({"sanity_check": "same_tag"})
        self.finished_job("nightly-macos", 3)
        job = self.finished_job("nightly-linux", 11)
        self.assertEqual(self.check(job, "nightly-linux", 11), [])

    def test_same_tag_still_catches_a_rolling_tag(self) -> None:
        self.set_mapping({"sanity_check": "same_tag"})
        self.finished_job("nightly", 19)
        job = self.finished_job("nightly", 18)
        self.assertEqual(len(self.check(job, "nightly", 18)), 1)

    def test_off_never_warns(self) -> None:
        self.set_mapping({"sanity_check": "off"})
        self.finished_job("nightly", 19)
        job = self.finished_job("nightly", 18)
        self.assertEqual(self.check(job, "nightly", 18), [])

    def test_an_unchanged_count_and_a_first_release_do_not_warn(self) -> None:
        first = self.finished_job("v1", 10)
        self.assertEqual(self.check(first, "v1", 10), [])  # nothing to compare with
        second = self.finished_job("v2", 10)
        self.assertEqual(self.check(second, "v2", 10), [])

    def test_the_other_release_type_is_not_compared(self) -> None:
        self.finished_job("v1", 10, release_type="Release")
        job = self.finished_job("v2-beta", 25, release_type="Pre-release")
        self.assertEqual(self.check(job, "v2-beta", 25, release_type="Pre-release"), [])

    def test_the_newest_earlier_release_is_the_one_compared(self) -> None:
        self.finished_job("v1", 5)
        self.finished_job("v2", 10)
        job = self.finished_job("v3", 10)
        self.assertEqual(self.check(job, "v3", 10), [])


class EditorTests(unittest.TestCase):
    def test_labels_round_trip(self) -> None:
        for value, label in gui_forms.SANITY_CHOICES:
            self.assertEqual(gui_forms.sanity_label(value), label)
            self.assertEqual(gui_forms.sanity_value(label), value)

    def test_missing_or_unknown_means_the_default(self) -> None:
        self.assertEqual(gui_forms.sanity_label(None), gui_forms.SANITY_CHOICES[0][1])
        self.assertEqual(gui_forms.sanity_label("bogus"), gui_forms.SANITY_CHOICES[0][1])
        self.assertEqual(gui_forms.sanity_value(""), "any_tag")

    def test_the_editor_saves_the_chosen_mode(self) -> None:
        form = {"destination": "K:\\Apps", "limit": "10", "sanity_check": "Off"}
        self.assertEqual(gui_forms.build_repo_changes(form).changes["sanity_check"], "off")
        form["sanity_check"] = "Compare same tag only"
        self.assertEqual(gui_forms.build_repo_changes(form).changes["sanity_check"], "same_tag")


class QueryTests(SanityCheckTestCase):
    def test_the_tag_match_is_optional_in_the_query(self) -> None:
        self.finished_job("v1", 3)
        current = self.finished_job("v2", 4)
        same = db_manager.get_previous_successful_completed_job(self.connection, REPO, "v2", "Release", current)
        anyone = db_manager.get_previous_successful_completed_job(
            self.connection, REPO, "v2", "Release", current, match_tag=False
        )
        self.assertIsNone(same)
        self.assertEqual(anyone["tag"], "v1")


if __name__ == "__main__":
    unittest.main()
