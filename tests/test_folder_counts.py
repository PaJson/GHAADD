"""The "12 / 15" folder counts: stored by the daemon's limit checks, shown in the Mappings table.

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

from modules import asset_downloader, config_manager, db_manager, gui_data, mapping_manager, repo_overview


class FormatLimitTests(unittest.TestCase):
    def test_a_counted_repository_reads_count_over_limit(self) -> None:
        text, over, note = repo_overview.format_limit(15, {"folder_count": 12, "counted_at": 1_000_000.0})
        self.assertEqual((text, over), ("12 / 15", False))
        self.assertIn("12 of 15", note)

    def test_over_the_limit_is_flagged(self) -> None:
        self.assertTrue(repo_overview.format_limit(10, {"folder_count": 11, "counted_at": 1.0})[1])
        self.assertFalse(repo_overview.format_limit(10, {"folder_count": 10, "counted_at": 1.0})[1])  # at the limit is fine

    def test_without_a_count_or_limit_the_cell_stays_as_configured(self) -> None:
        self.assertEqual(repo_overview.format_limit(15, None), ("15", False, ""))
        self.assertEqual(repo_overview.format_limit(0, {"folder_count": 5, "counted_at": 1.0}), ("0", False, ""))
        self.assertEqual(repo_overview.format_limit(None, None), ("", False, ""))
        self.assertEqual(repo_overview.format_limit("abc", {"folder_count": 5, "counted_at": 1.0}), ("abc", False, ""))

    def test_rows_use_the_count_and_flag_the_ones_over_their_limit(self) -> None:
        entries = [{"repository": "o/a", "limit": 10}, {"repository": "o/b", "limit": 10}]
        counts = {"o/a": {"folder_count": 12, "counted_at": 1.0}, "o/b": {"folder_count": 3, "counted_at": 1.0}}
        rows = {r.repo: r for r in repo_overview.build_rows(entries, {}, 0.0, lambda entry: 5, folder_counts=counts)}
        self.assertEqual((rows["o/a"].limit, rows["o/a"].limit_warning), ("12 / 10", True))
        self.assertEqual((rows["o/b"].limit, rows["o/b"].limit_warning), ("3 / 10", False))


class TotalFilesTests(unittest.TestCase):
    def test_the_total_is_shown_as_a_plain_number(self) -> None:
        summary = {"latest_tag": "v1", "latest_downloaded": 10, "latest_skipped": 6, "latest_total": 16}
        self.assertEqual(repo_overview.format_total_files(summary), "16")
        summary.update(latest_downloaded=0, latest_skipped=16)
        self.assertEqual(repo_overview.format_total_files(summary), "16")

    def test_nothing_known_shows_a_dash(self) -> None:
        self.assertEqual(repo_overview.format_total_files(None), "-")
        self.assertEqual(repo_overview.format_total_files({"latest_tag": None}), "-")
        self.assertEqual(repo_overview.format_total_files({"latest_tag": "v1", "latest_total": 0}), "-")


class FolderCountTests(unittest.TestCase):
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
        self.write_mapping(limit=5)
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

    def write_mapping(self, limit: int) -> None:
        with open(self.mapping_path, "w", encoding="utf-8") as handle:
            json.dump({"repositories": [
                {"repository": self.REPO, "destination": self.destination, "folder": "App", "limit": limit}
            ]}, handle)

    def counts(self) -> dict:
        with closing(db_manager.open_database()) as connection:
            return db_manager.get_folder_counts(connection)

    def check(self) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return asset_downloader.check_folder_limits()

    def test_the_periodic_check_stores_the_count_even_when_within_the_limit(self) -> None:
        self.assertEqual(self.check(), 0)
        self.assertEqual(self.counts()[self.REPO]["folder_count"], 3)

    def test_the_count_follows_the_folders(self) -> None:
        self.check()
        os.makedirs(os.path.join(self.release_dir, "d"))
        self.check()
        self.assertEqual(self.counts()[self.REPO]["folder_count"], 4)

    def test_a_repository_with_a_warning_keeps_its_count_fresh_without_a_second_warning(self) -> None:
        self.write_mapping(limit=2)
        self.assertEqual(self.check(), 1)
        os.rmdir(os.path.join(self.release_dir, "a"))
        self.assertEqual(self.check(), 0)
        self.assertEqual(self.counts()[self.REPO]["folder_count"], 2)

    def test_the_check_clears_the_warnings_of_a_repository_that_is_back_under_its_limit(self) -> None:
        self.write_mapping(limit=2)
        self.assertEqual(self.check(), 1)  # 3 folders, limit 2: warned
        os.rmdir(os.path.join(self.release_dir, "a"))
        os.rmdir(os.path.join(self.release_dir, "b"))
        self.assertEqual(self.check(), 0)  # what the "Check folders" button runs
        self.assertEqual(self.counts()[self.REPO]["folder_count"], 1)
        with closing(db_manager.open_database()) as connection:
            self.assertEqual(db_manager.get_repos_with_limit_warnings(connection), set())

    def test_the_per_poll_clean_up_records_the_count_too(self) -> None:
        self.write_mapping(limit=2)
        self.check()
        os.rmdir(os.path.join(self.release_dir, "a"))
        with contextlib.redirect_stdout(io.StringIO()):
            asset_downloader.clear_all_resolved_limit_warnings()
        self.assertEqual(self.counts()[self.REPO]["folder_count"], 2)

    def test_no_limit_or_a_removed_repository_drops_the_count(self) -> None:
        self.check()
        self.write_mapping(limit=0)
        self.check()
        self.assertEqual(self.counts(), {})
        self.write_mapping(limit=5)
        self.check()
        with open(self.mapping_path, "w", encoding="utf-8") as handle:
            json.dump({"repositories": []}, handle)
        self.check()
        self.assertEqual(self.counts(), {})

    def test_a_dry_run_stores_nothing(self) -> None:
        with mock.patch.object(asset_downloader, "is_dry_run", return_value=True):
            self.check()
        self.assertEqual(self.counts(), {})

    def test_the_mappings_table_shows_the_stored_count(self) -> None:
        self.check()
        gui_data._counts_cache.invalidate()
        gui_data._limit_cache.invalidate()
        gui_data._summaries_cache.invalidate()
        (row,) = gui_data.load_repo_table().rows
        self.assertEqual((row.limit, row.limit_warning), ("3 / 5", False))


if __name__ == "__main__":
    unittest.main()
