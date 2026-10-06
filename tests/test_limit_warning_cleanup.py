"""Folder-limit warnings clear themselves once the folder is back under its limit.

Run from the project root: python -m unittest discover -s tests -t .
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from contextlib import closing
from unittest import mock

from modules import asset_downloader, config_manager, db_manager, mapping_manager


class LimitWarningCleanupTests(unittest.TestCase):
    REPO = "o/app"

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._temp_dir.cleanup)
        self.root = self._temp_dir.name
        db_path = os.path.join(self.root, "state.db")
        self.mapping_path = os.path.join(self.root, "mapping.json")
        config_path = os.path.join(self.root, "config.json")
        self.destination = os.path.join(self.root, "dest")
        os.makedirs(self.destination)
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump({"paths": {"default_download_dir": self.root}}, handle)
        self.write_mapping(limit=2)
        for target, name, value in (
            (db_manager, "get_state_db_path", lambda: db_path),
            (config_manager, "_config_file_path", lambda: config_path),
            (mapping_manager, "_mapping_file_path", lambda: self.mapping_path),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.release_dir = os.path.join(self.destination, "App", "Release")
        for name in ("a", "b", "c"):
            os.makedirs(os.path.join(self.release_dir, name))
        with closing(db_manager.open_database()) as connection:
            for count in (3, 3):
                db_manager.insert_lifecycle_event(
                    connection, "WARNING",
                    f"Folder limit warning: {self.REPO} currently has {count} folder(s) in 'x' (limit=2).",
                    category="LIMIT",
                )

    def write_mapping(self, limit: int) -> None:
        with open(self.mapping_path, "w", encoding="utf-8") as handle:
            json.dump({"repositories": [
                {"name": self.REPO, "destination": self.destination, "foldername": "App", "limit": limit}
            ]}, handle)

    def warnings(self) -> set[str]:
        with closing(db_manager.open_database()) as connection:
            return db_manager.get_repos_with_limit_warnings(connection)

    def sweep(self) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return asset_downloader.clear_all_resolved_limit_warnings()

    def test_a_folder_still_over_its_limit_keeps_its_warnings(self) -> None:
        self.assertEqual(self.sweep(), 0)
        self.assertEqual(self.warnings(), {self.REPO})

    def test_cleaning_the_folder_up_clears_the_warnings(self) -> None:
        os.rmdir(os.path.join(self.release_dir, "a"))
        self.assertEqual(self.sweep(), 2)
        self.assertEqual(self.warnings(), set())

    def test_raising_or_disabling_the_limit_clears_the_warnings(self) -> None:
        self.write_mapping(limit=0)
        self.assertEqual(self.sweep(), 2)

    def test_a_missing_destination_keeps_the_warnings(self) -> None:
        os.rename(self.destination, self.destination + "_gone")
        self.assertEqual(self.sweep(), 0)
        self.assertEqual(self.warnings(), {self.REPO})

    def test_the_check_after_a_move_clears_them_too(self) -> None:
        os.rmdir(os.path.join(self.release_dir, "a"))
        with contextlib.redirect_stdout(io.StringIO()):
            asset_downloader._warn_if_destination_limit_exceeded(self.REPO, os.path.join(self.destination, "App"))
        self.assertEqual(self.warnings(), set())

    def test_a_dry_run_deletes_nothing(self) -> None:
        os.rmdir(os.path.join(self.release_dir, "a"))
        with mock.patch("modules.lifecycle_logger.is_dry_run", return_value=True):
            self.assertEqual(self.sweep(), 0)
        self.assertEqual(self.warnings(), {self.REPO})


if __name__ == "__main__":
    unittest.main()
