"""mapping.json 2.0 key names (repository, folder, recheck_intervals, limit_folders, last_notification, active)
and the automatic upgrade of a file that still uses the old ones.

Nothing here touches the real mapping.json, state.db or daemon lock.
Run from the project root: python -m unittest discover -s tests -t .
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from modules import mapping_manager

# The example from the planning message, in the old format.
LEGACY_ENTRY = {
    "name": "twofas/2fas-ios",
    "destination": "K:\\Apps",
    "foldername": "2FAS (iOS)",
    "subfolder": "@GitHub",
    "limit": 10,
    "limit_release_type_folders": ["Release", "Pre-release"],
    "recheck_intervals_minutes": [],
    "skiplist": [],
    "last_notification_seen": "2026-09-15_21-06",
    "last_finalized": "2026-09-15_23-06",
    "paused": True,
}


class UpgradeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._temp_dir.cleanup)
        self.path = os.path.join(self._temp_dir.name, "mapping.json")
        for patcher in (
            mock.patch.object(mapping_manager, "_mapping_file_path", lambda: self.path),
            mock.patch.object(mapping_manager, "log_warning"),  # keep the test out of the real state.db
            mock.patch("builtins.print"),
            mock.patch.object(mapping_manager, "is_dry_run", lambda: False),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_legacy(self, *entries: dict) -> str:
        text = json.dumps({"repositories": list(entries)}, indent=2)
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return text

    def read_file(self) -> dict:
        with open(self.path, encoding="utf-8") as handle:
            return json.load(handle)

    def backups(self) -> list[str]:
        return sorted(name for name in os.listdir(self._temp_dir.name) if ".v1.bak" in name)


class ReadingTests(UpgradeTestCase):
    def test_an_old_file_is_understood_without_being_rewritten(self) -> None:
        original = self.write_legacy(LEGACY_ENTRY)

        entry = mapping_manager.load_mapping()["repositories"][0]

        self.assertEqual(entry["repository"], "twofas/2fas-ios")
        self.assertEqual(entry["folder"], "2FAS (iOS)")
        self.assertEqual(entry["last_notification"], "2026-09-15_21-06")
        self.assertEqual(entry["limit_folders"], ["Release", "Pre-release"])
        self.assertEqual(entry["recheck_intervals"], [])
        self.assertIs(entry["active"], False)  # paused: true
        for old_key in ("name", "foldername", "paused", "last_notification_seen", "recheck_intervals_minutes"):
            self.assertNotIn(old_key, entry)
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), original)  # reading never writes
        self.assertEqual(self.backups(), [])

    def test_paused_false_means_active_and_a_missing_flag_stays_missing(self) -> None:
        self.write_legacy({**LEGACY_ENTRY, "paused": False}, {"name": "a/b"})
        first, second = mapping_manager.load_mapping()["repositories"]
        self.assertIs(first["active"], True)
        self.assertNotIn("active", second)
        self.assertTrue(mapping_manager.is_repository_active("a/b"))  # no flag = active

    def test_a_new_key_wins_over_an_old_one(self) -> None:
        self.write_legacy({"name": "a/b", "repository": "a/b", "paused": True, "active": True, "foldername": "Old", "folder": "New"})
        entry = mapping_manager.load_mapping()["repositories"][0]
        self.assertEqual((entry["folder"], entry["active"]), ("New", True))
        self.assertNotIn("foldername", entry)

    def test_an_invalid_paused_value_is_reported_under_its_new_name(self) -> None:
        payload = {"repositories": [{"name": "a/b", "destination": "", "paused": "yes"}]}
        errors = mapping_manager.validate_mapping_payload(payload)["errors"]
        self.assertTrue(any(".active must be a boolean" in error for error in errors), errors)
        self.assertIn("paused", payload["repositories"][0])  # the caller's data is not changed

    def test_validation_accepts_an_old_file(self) -> None:
        self.write_legacy(LEGACY_ENTRY)
        self.assertEqual(mapping_manager.validate_mapping_schema()["errors"], [])


class WritingTests(UpgradeTestCase):
    def test_the_first_write_upgrades_the_file_and_keeps_a_backup(self) -> None:
        original = self.write_legacy(LEGACY_ENTRY, {**LEGACY_ENTRY, "name": "a/b", "paused": False})

        self.assertTrue(mapping_manager.mark_repository_finalized("a/b", "2026-10-07_10-00"))

        written = self.read_file()["repositories"]
        by_name = {entry["repository"]: entry for entry in written}
        self.assertEqual(set(by_name), {"twofas/2fas-ios", "a/b"})
        self.assertIs(by_name["twofas/2fas-ios"]["active"], False)
        self.assertIs(by_name["a/b"]["active"], True)
        self.assertEqual(by_name["a/b"]["last_finalized"], "2026-10-07_10-00")
        for entry in written:
            self.assertTrue(set(entry) <= set(mapping_manager._MAPPING_FIELD_ORDER), set(entry))
        (backup,) = self.backups()
        with open(os.path.join(self._temp_dir.name, backup), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), original)  # the old file, untouched
        mapping_manager.mark_repository_finalized("a/b", "2026-10-07_11-00")
        self.assertEqual(len(self.backups()), 1)  # an upgraded file is not backed up again

    def test_the_keys_are_written_in_the_new_order(self) -> None:
        self.write_legacy(LEGACY_ENTRY)
        self.assertTrue(mapping_manager.migrate_mapping_file())
        self.assertEqual(
            list(self.read_file()["repositories"][0]),
            ["repository", "folder", "subfolder", "destination", "skiplist", "recheck_intervals", "limit",
             "limit_folders", "last_notification", "last_finalized", "active"],
        )
        with open(self.path, encoding="utf-8") as handle:
            self.assertIn('"limit_folders": ["Release", "Pre-release"]', handle.read())  # arrays stay on one line

    def test_migrating_twice_does_nothing_the_second_time(self) -> None:
        self.write_legacy(LEGACY_ENTRY)
        self.assertTrue(mapping_manager.migrate_mapping_file())
        self.assertFalse(mapping_manager.migrate_mapping_file())
        self.assertEqual(len(self.backups()), 1)

    def test_an_existing_backup_is_never_overwritten(self) -> None:
        with open(self.path + ".v1.bak", "w", encoding="utf-8") as handle:
            handle.write("older backup")
        self.write_legacy(LEGACY_ENTRY)
        mapping_manager.migrate_mapping_file()
        with open(self.path + ".v1.bak", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "older backup")
        self.assertEqual(len(self.backups()), 2)

    def daemon(self, running: bool, mapping_format) -> mock._patch:
        return mock.patch(
            "modules.daemon_lock.get_daemon_status", return_value={"running": running, "mapping_format": mapping_format}
        )

    def test_an_older_daemon_postpones_the_upgrade(self) -> None:
        original = self.write_legacy(LEGACY_ENTRY)
        with self.daemon(True, None):  # started before 2.0: it does not publish a mapping format
            self.assertFalse(mapping_manager.migrate_mapping_file())
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), original)

    def test_an_older_daemon_blocks_writes_that_would_upgrade_the_file(self) -> None:
        original = self.write_legacy(LEGACY_ENTRY)
        with self.daemon(True, None):
            with self.assertRaises(mapping_manager.MappingValidationError) as raised:
                mapping_manager.update_repository_fields("twofas/2fas-ios", {"limit": 5})
        self.assertIn("Restart the daemon", str(raised.exception))
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), original)

    def test_a_current_daemon_or_no_daemon_does_not_block_the_upgrade(self) -> None:
        self.write_legacy(LEGACY_ENTRY)
        with self.daemon(True, mapping_manager.MAPPING_FORMAT):
            self.assertTrue(mapping_manager.migrate_mapping_file())
        self.write_legacy(LEGACY_ENTRY)
        with self.daemon(False, None):
            self.assertTrue(mapping_manager.migrate_mapping_file())

    def test_the_daemon_publishes_the_mapping_format(self) -> None:
        from modules import daemon_lock

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder, \
                mock.patch.object(daemon_lock, "get_daemon_status_path", lambda: os.path.join(folder, "status.json")):
            daemon_lock._write_daemon_status()
            self.assertEqual(daemon_lock._read_status_payload()["mapping_format"], mapping_manager.MAPPING_FORMAT)

    def test_ensure_mapping_file_upgrades_an_old_file(self) -> None:
        self.write_legacy(LEGACY_ENTRY)
        self.assertFalse(mapping_manager.ensure_mapping_file())  # nothing was created
        self.assertEqual(self.read_file()["repositories"][0]["repository"], "twofas/2fas-ios")

    def test_a_dry_run_changes_nothing(self) -> None:
        original = self.write_legacy(LEGACY_ENTRY)
        with mock.patch.object(mapping_manager, "is_dry_run", lambda: True):
            self.assertFalse(mapping_manager.migrate_mapping_file())
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), original)

    def test_a_current_file_is_left_alone(self) -> None:
        self.write_legacy({"repository": "a/b", "active": True})
        self.assertFalse(mapping_manager.migrate_mapping_file())
        self.assertEqual(self.backups(), [])

    def test_new_entries_have_the_2_0_keys_and_are_active(self) -> None:
        entry = mapping_manager._build_skeleton_entry("Owner/Repo", "")
        self.assertEqual(
            list(entry),
            ["repository", "folder", "subfolder", "destination", "skiplist", "recheck_intervals", "limit",
             "limit_folders", "sanity_check", "last_notification", "last_finalized", "active"],
        )
        self.assertIs(entry["active"], True)
        self.assertEqual(entry["folder"], "Repo (Owner)")

    def test_an_inactive_repository_is_reported_inactive(self) -> None:
        self.write_legacy({"name": "a/b", "paused": True}, {"name": "c/d", "paused": False})
        self.assertFalse(mapping_manager.is_repository_active("a/b"))
        self.assertTrue(mapping_manager.is_repository_active("c/d"))
        self.assertTrue(mapping_manager.is_repository_active("x/unknown"))


if __name__ == "__main__":
    unittest.main()
