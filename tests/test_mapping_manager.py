"""Tests for the mapping.json write path (update_mapping and its callers).

Run from the project root: python -m unittest discover -s tests -t .
"""
import json
import multiprocessing
import os
import tempfile
import unittest
from unittest import mock

from modules import dry_run_mode, mapping_manager


def _increment_worker(mapping_path: str, iterations: int) -> None:
    """Child process: bump a shared counter `iterations` times via update_mapping."""
    mapping_manager._mapping_file_path = lambda: mapping_path

    def _bump(payload):
        payload["repositories"][0]["counter"] = payload["repositories"][0].get("counter", 0) + 1

    for _ in range(iterations):
        mapping_manager.update_mapping(_bump)


class MappingWriteTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.mapping_path = os.path.join(self._temp_dir.name, "mapping.json")

        path_patch = mock.patch.object(
            mapping_manager, "_mapping_file_path", lambda: self.mapping_path
        )
        path_patch.start()
        self.addCleanup(path_patch.stop)

        # log_warning writes to the real state.db; keep tests away from it.
        warning_patch = mock.patch.object(mapping_manager, "log_warning")
        self.warning_mock = warning_patch.start()
        self.addCleanup(warning_patch.stop)

        self.addCleanup(dry_run_mode.set_dry_run, False)

    def write_mapping(self, repositories: list[dict]) -> None:
        with open(self.mapping_path, "w", encoding="utf-8") as handle:
            json.dump({"repositories": repositories}, handle)

    def read_mapping(self) -> list[dict]:
        with open(self.mapping_path, "r", encoding="utf-8") as handle:
            return json.load(handle)["repositories"]


class UpdateMappingTests(MappingWriteTestCase):
    def test_creates_file_sorted_with_collapsed_arrays(self) -> None:
        def _add(payload):
            payload["repositories"].append({"name": "zed/zebra", "recheck_intervals_minutes": [5, 15]})
            payload["repositories"].append({"name": "amy/apple"})

        mapping_manager.update_mapping(_add)

        self.assertEqual([r["name"] for r in self.read_mapping()], ["amy/apple", "zed/zebra"])
        with open(self.mapping_path, encoding="utf-8") as handle:
            self.assertIn('"recheck_intervals_minutes": [5, 15]', handle.read())

    def test_returns_mutator_result(self) -> None:
        self.assertEqual(mapping_manager.update_mapping(lambda payload: "done"), "done")

    def test_no_change_does_not_rewrite_file(self) -> None:
        self.write_mapping([{"name": "b/b"}, {"name": "a/a"}])  # deliberately unsorted
        with open(self.mapping_path, "rb") as handle:
            before = handle.read()

        mapping_manager.update_mapping(lambda payload: None)

        with open(self.mapping_path, "rb") as handle:
            self.assertEqual(handle.read(), before)

    def test_leaves_no_temp_files_behind(self) -> None:
        mapping_manager.update_mapping(lambda payload: payload["repositories"].append({"name": "a/a"}))

        leftovers = [n for n in os.listdir(self._temp_dir.name) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_mutator_error_keeps_old_file_and_releases_lock(self) -> None:
        self.write_mapping([{"name": "a/a"}])

        def _fail(payload):
            payload["repositories"].append({"name": "b/b"})
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            mapping_manager.update_mapping(_fail)

        self.assertEqual([r["name"] for r in self.read_mapping()], ["a/a"])
        # Lock must be free again, otherwise this would time out.
        mapping_manager.update_mapping(lambda payload: None)

    def test_dry_run_runs_mutator_but_writes_nothing(self) -> None:
        dry_run_mode.set_dry_run(True)

        result = mapping_manager.update_mapping(
            lambda payload: payload["repositories"].append({"name": "a/a"}) or "ran"
        )

        self.assertEqual(result, "ran")
        self.assertFalse(os.path.exists(self.mapping_path))

    def test_lock_timeout_raises_clear_error(self) -> None:
        with mock.patch.object(mapping_manager, "_MAPPING_LOCK_TIMEOUT_SECONDS", 0.2):
            lock = mapping_manager.FileLock(mapping_manager._mapping_lock_path())
            with lock:
                with self.assertRaises(mapping_manager.MappingLockTimeout):
                    mapping_manager.update_mapping(lambda payload: None)

    def test_concurrent_processes_lose_no_updates(self) -> None:
        self.write_mapping([{"name": "a/a", "counter": 0}])
        workers, iterations = 4, 15

        context = multiprocessing.get_context("spawn")
        processes = [
            context.Process(target=_increment_worker, args=(self.mapping_path, iterations))
            for _ in range(workers)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=120)
            self.assertEqual(process.exitcode, 0)

        self.assertEqual(self.read_mapping()[0]["counter"], workers * iterations)


class RepositoryWriterTests(MappingWriteTestCase):
    def test_upsert_creates_skeleton_and_warns(self) -> None:
        created, updated = mapping_manager.upsert_repository_mapping("owner/repo", "2026-01-01_00-00")

        self.assertEqual((created, updated), (True, False))
        entry = self.read_mapping()[0]
        self.assertEqual(entry["name"], "owner/repo")
        self.assertEqual(entry["last_notification_seen"], "2026-01-01_00-00")
        self.assertIs(entry["paused"], False)
        self.warning_mock.assert_called_once()

    def test_upsert_existing_updates_stamp_only_when_changed(self) -> None:
        mapping_manager.upsert_repository_mapping("owner/repo", "2026-01-01_00-00")

        self.assertEqual(
            mapping_manager.upsert_repository_mapping("owner/repo", "2026-01-02_00-00"), (False, True)
        )
        self.assertEqual(
            mapping_manager.upsert_repository_mapping("owner/repo", "2026-01-02_00-00"), (False, False)
        )
        self.assertEqual(self.read_mapping()[0]["last_notification_seen"], "2026-01-02_00-00")

    def test_mark_finalized(self) -> None:
        mapping_manager.upsert_repository_mapping("owner/repo", "2026-01-01_00-00")

        self.assertTrue(mapping_manager.mark_repository_finalized("owner/repo", "2026-01-03_00-00"))
        self.assertFalse(mapping_manager.mark_repository_finalized("owner/repo", "2026-01-03_00-00"))
        self.assertFalse(mapping_manager.mark_repository_finalized("unknown/repo", "2026-01-03_00-00"))
        self.assertEqual(self.read_mapping()[0]["last_finalized"], "2026-01-03_00-00")


class UpdateRepositoryFieldsTests(MappingWriteTestCase):
    def test_stale_gui_edit_preserves_daemon_updates(self) -> None:
        """The lost-update case: daemon writes between GUI load and GUI save."""
        mapping_manager.upsert_repository_mapping("owner/repo", "2026-01-01_00-00")
        gui_copy = mapping_manager.get_repository_mapping("owner/repo")  # GUI loads, then edits...
        assert gui_copy is not None

        mapping_manager.mark_repository_finalized("owner/repo", "2026-02-02_00-00")  # ...daemon writes
        mapping_manager.upsert_repository_mapping("owner/repo", "2026-02-03_00-00")

        self.assertEqual(gui_copy["last_finalized"], "")  # stale, as in a real GUI
        self.assertTrue(
            mapping_manager.update_repository_fields("owner/repo", {"destination": "D:/Games", "limit": 5})
        )

        entry = self.read_mapping()[0]
        self.assertEqual((entry["destination"], entry["limit"]), ("D:/Games", 5))
        self.assertEqual(entry["last_finalized"], "2026-02-02_00-00")
        self.assertEqual(entry["last_notification_seen"], "2026-02-03_00-00")

    def test_returns_false_when_nothing_changes(self) -> None:
        mapping_manager.upsert_repository_mapping("owner/repo")

        self.assertFalse(mapping_manager.update_repository_fields("owner/repo", {"paused": False}))

    def test_rejects_daemon_owned_fields_and_name(self) -> None:
        mapping_manager.upsert_repository_mapping("owner/repo")

        for field in ("last_finalized", "last_notification_seen", "name"):
            with self.assertRaises(ValueError):
                mapping_manager.update_repository_fields("owner/repo", {field: "x"})

    def test_unknown_repository_raises_key_error(self) -> None:
        with self.assertRaises(KeyError):
            mapping_manager.update_repository_fields("owner/missing", {"limit": 1})

    def test_repository_matching_is_case_insensitive(self) -> None:
        mapping_manager.upsert_repository_mapping("Owner/Repo")

        self.assertTrue(mapping_manager.update_repository_fields("owner/repo", {"paused": True}))
        self.assertIs(self.read_mapping()[0]["paused"], True)


class ValidatedEditTests(MappingWriteTestCase):
    def file_bytes(self) -> bytes:
        with open(self.mapping_path, "rb") as handle:
            return handle.read()

    def test_invalid_value_is_rejected_and_file_unchanged(self) -> None:
        mapping_manager.upsert_repository_mapping("owner/repo", "2026-01-01_00-00")
        before = self.file_bytes()

        for bad_changes in ({"limit": -1}, {"paused": "yes"}, {"skiplist": "Release"}):
            with self.assertRaises(mapping_manager.MappingValidationError) as raised:
                mapping_manager.update_repository_fields("owner/repo", bad_changes)
            self.assertTrue(raised.exception.errors)

        self.assertEqual(self.file_bytes(), before)

    def test_preexisting_error_elsewhere_does_not_block_valid_edit(self) -> None:
        self.write_mapping([{"name": "a/a", "limit": -5}, {"name": "b/b"}])

        self.assertTrue(mapping_manager.update_repository_fields("b/b", {"limit": 3}))

        self.assertEqual({r["name"]: r.get("limit") for r in self.read_mapping()}, {"a/a": -5, "b/b": 3})

    def test_validate_mapping_payload_matches_file_validation(self) -> None:
        self.write_mapping([{"name": "a/a", "limit": -1}])

        from_file = mapping_manager.validate_mapping_schema()
        from_payload = mapping_manager.validate_mapping_payload({"repositories": [{"name": "a/a", "limit": -1}]})

        self.assertFalse(from_file["ok"])
        self.assertEqual(from_file["errors"], from_payload["errors"])


class AddRemoveRepositoryTests(MappingWriteTestCase):
    def test_add_creates_defaults_with_empty_notification_stamp(self) -> None:
        mapping_manager.add_repository("Owner/Repo", {"destination": "D:/Games", "limit": 3})

        entry = self.read_mapping()[0]
        self.assertEqual(entry["name"], "Owner/Repo")
        self.assertEqual((entry["destination"], entry["limit"]), ("D:/Games", 3))
        self.assertEqual(entry["foldername"], "Repo (Owner)")
        self.assertEqual(entry["last_notification_seen"], "")
        self.assertIs(entry["paused"], False)
        self.warning_mock.assert_not_called()

    def test_add_keeps_list_sorted(self) -> None:
        mapping_manager.add_repository("zed/zebra")
        mapping_manager.add_repository("amy/apple")

        self.assertEqual([r["name"] for r in self.read_mapping()], ["amy/apple", "zed/zebra"])

    def test_add_duplicate_is_rejected_case_insensitively(self) -> None:
        mapping_manager.add_repository("owner/repo")
        before = self.read_mapping()

        with self.assertRaises(mapping_manager.MappingValidationError):
            mapping_manager.add_repository("OWNER/REPO")

        self.assertEqual(self.read_mapping(), before)

    def test_add_rejects_malformed_names(self) -> None:
        for bad_name in ("", "norepo", "a/b/c", "has space/repo", "/repo", "owner/"):
            with self.assertRaises(mapping_manager.MappingValidationError):
                mapping_manager.add_repository(bad_name)

        self.assertFalse(os.path.exists(self.mapping_path))

    def test_add_rejects_daemon_owned_fields_and_invalid_values(self) -> None:
        with self.assertRaises(ValueError):
            mapping_manager.add_repository("owner/repo", {"last_finalized": "x"})
        with self.assertRaises(mapping_manager.MappingValidationError):
            mapping_manager.add_repository("owner/repo", {"limit": -1})

        self.assertFalse(os.path.exists(self.mapping_path))

    def test_remove_repository(self) -> None:
        mapping_manager.add_repository("owner/one")
        mapping_manager.add_repository("owner/two")

        self.assertTrue(mapping_manager.remove_repository("OWNER/ONE"))
        self.assertFalse(mapping_manager.remove_repository("owner/one"))

        self.assertEqual([r["name"] for r in self.read_mapping()], ["owner/two"])


if __name__ == "__main__":
    unittest.main()
