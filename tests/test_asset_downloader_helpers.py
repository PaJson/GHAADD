"""The small helpers of asset_downloader that the move and download tests do not reach: limits, counting, sizes,
folder-count bookkeeping and the edge cases of moving folders around.

Everything runs in temporary folders (see MoveTestCase). Run from the project root:
python -m unittest discover -s tests -t .
"""
import os
import unittest
from contextlib import closing
from unittest import mock

from modules import asset_downloader, db_manager
from tests.test_folder_naming_and_moves import MoveTestCase, REPO


class FormatAndSizeTests(unittest.TestCase):
    def test_format_bytes(self) -> None:
        self.assertEqual(asset_downloader._format_bytes(0), "0 B")
        self.assertEqual(asset_downloader._format_bytes(1536), "1.5 KB")
        self.assertEqual(asset_downloader._format_bytes(3 * 1024 ** 3), "3.0 GB")
        self.assertEqual(asset_downloader._format_bytes(5 * 1024 ** 4), "5.0 TB")
        self.assertEqual(asset_downloader._format_bytes(2048 * 1024 ** 4), "2048.0 TB")

    def test_directory_size_counts_every_file_below_it(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root:
            os.makedirs(os.path.join(root, "a", "b"))
            for name, size in (("x.bin", 10), (os.path.join("a", "y.bin"), 20), (os.path.join("a", "b", "z.bin"), 30)):
                with open(os.path.join(root, name), "wb") as handle:
                    handle.write(b"0" * size)
            self.assertEqual(asset_downloader._compute_directory_size_bytes(root), 60)
            with mock.patch.object(asset_downloader.os.path, "getsize", side_effect=OSError("gone")):
                self.assertEqual(asset_downloader._compute_directory_size_bytes(root), 0)

    def test_the_subdirectory_count_of_a_missing_folder_is_zero(self) -> None:
        self.assertEqual(asset_downloader._count_direct_subdirectories("/definitely/not/here"), 0)


class LimitAndCountTests(MoveTestCase):
    def test_the_limit_of_an_unknown_or_unnamed_repository_is_zero(self) -> None:
        self.write_mapping(limit=5)
        self.assertEqual(asset_downloader._resolve_repository_limit(None), 0)
        self.assertEqual(asset_downloader._resolve_repository_limit(""), 0)
        self.assertEqual(asset_downloader._resolve_repository_limit("other/repo"), 0)

    def test_only_a_positive_whole_number_is_a_limit(self) -> None:
        for value, expected in ((5, 5), (0, 0), (-3, 0), (True, 0), ("7", 0), (2.5, 0)):
            self.write_mapping(limit=value)
            self.assertEqual(asset_downloader._resolve_repository_limit(REPO), expected, value)

    def test_tracked_folders_default_to_release_and_pre_release(self) -> None:
        self.assertEqual(asset_downloader._resolve_tracked_release_type_folders(None), {"release", "pre-release"})
        self.assertEqual(asset_downloader._resolve_tracked_release_type_folders(""), {"release", "pre-release"})

    def test_tracked_folders_follow_the_mapping_setting(self) -> None:
        self.write_mapping(limit_folders=["Nightly", " ", "Release"])
        self.assertEqual(asset_downloader._resolve_tracked_release_type_folders(REPO), {"nightly", "release"})

    def test_tracked_folders_include_the_release_types_seen_in_the_queue(self) -> None:
        self.write_mapping()
        with closing(db_manager.open_database()) as connection:
            db_manager.enqueue_job(connection, REPO, "v1", release_type="Nightly")
        tracked = asset_downloader._resolve_tracked_release_type_folders(REPO)
        self.assertEqual(tracked, {"release", "pre-release", "nightly"})

    def test_a_broken_database_does_not_break_the_lookup(self) -> None:
        self.write_mapping()
        with mock.patch.object(asset_downloader, "open_database", side_effect=RuntimeError("locked")):
            tracked = asset_downloader._resolve_tracked_release_type_folders(REPO)
        self.assertEqual(tracked, {"release", "pre-release"})

    def make_destination(self) -> str:
        root = os.path.join(self.root, "counted")
        for release_type, count in (("Release", 3), ("Pre-release", 0), ("Other", 4)):
            os.makedirs(os.path.join(root, release_type))
            for index in range(count):
                os.makedirs(os.path.join(root, release_type, f"r{index}"))
        with open(os.path.join(root, "loose.txt"), "w", encoding="utf-8") as handle:
            handle.write("x")
        return root

    def test_counting_release_folders(self) -> None:
        root = self.make_destination()
        count = asset_downloader._count_repository_release_folders
        self.assertEqual(count(root, {"release", "pre-release"}), 4)  # 3 + an empty type folder counts as 1
        self.assertEqual(count(root, {"RELEASE "}), 3)                 # names are matched case-insensitively
        self.assertEqual(count(root, None), 8)                         # no filter: every type folder
        self.assertEqual(count(root, {"nightly"}), 0)
        self.assertEqual(count(os.path.join(self.root, "missing"), {"release"}), 0)

    def test_an_unreadable_destination_counts_as_zero(self) -> None:
        root = self.make_destination()
        with mock.patch.object(asset_downloader.os, "scandir", side_effect=OSError("denied")):
            self.assertEqual(asset_downloader._count_repository_release_folders(root, {"release"}), 0)


class FolderCountBookkeepingTests(MoveTestCase):
    def stored(self) -> dict:
        with closing(db_manager.open_database()) as connection:
            return db_manager.get_folder_counts(connection)

    def test_a_count_is_stored_and_replaced_and_removed(self) -> None:
        asset_downloader._record_folder_count(REPO, 4)
        self.assertEqual(self.stored()[REPO.lower()]["folder_count"], 4)
        asset_downloader._record_folder_count(REPO, 6)
        self.assertEqual(self.stored()[REPO.lower()]["folder_count"], 6)
        asset_downloader._record_folder_count(REPO, None)
        self.assertEqual(self.stored(), {})

    def test_nothing_is_stored_for_no_repository_or_in_a_dry_run(self) -> None:
        asset_downloader._record_folder_count(None, 3)
        self.dry_run = True
        asset_downloader._record_folder_count(REPO, 3)
        self.assertEqual(self.stored(), {})

    def test_a_database_error_is_swallowed(self) -> None:
        with mock.patch.object(asset_downloader, "open_database", side_effect=RuntimeError("locked")):
            asset_downloader._record_folder_count(REPO, 3)
            asset_downloader._prune_folder_counts([REPO])

    def test_pruning_keeps_only_the_listed_repositories_and_is_skipped_in_a_dry_run(self) -> None:
        asset_downloader._record_folder_count(REPO, 1)
        asset_downloader._record_folder_count("other/gone", 2)
        self.dry_run = True
        asset_downloader._prune_folder_counts([REPO])
        self.assertEqual(len(self.stored()), 2)
        self.dry_run = False
        asset_downloader._prune_folder_counts([REPO])
        self.assertEqual(set(self.stored()), {REPO.lower()})

    def test_clearing_a_resolved_warning_says_so(self) -> None:
        with mock.patch.object(asset_downloader, "clear_limit_warnings", lambda repo: 2):
            self.run_quietly(asset_downloader._clear_resolved_limit_warnings, REPO, "3 folder(s)")
            asset_downloader._clear_resolved_limit_warnings(None, "x")  # nothing to do, nothing raised


class MoveEdgeCaseTests(MoveTestCase):
    def test_a_loose_file_in_the_repository_folder_is_moved_too_and_never_overwrites(self) -> None:
        source = os.path.join(self.root, "src")
        target = os.path.join(self.root, "target")
        os.makedirs(os.path.join(source, "Release", "r1"))
        with open(os.path.join(source, "note.txt"), "w", encoding="utf-8") as handle:
            handle.write("new")
        os.makedirs(target)
        with open(os.path.join(target, "note.txt"), "w", encoding="utf-8") as handle:
            handle.write("old")
        moved = asset_downloader._move_complete_repo_tree(source, target)
        self.assertEqual(moved, 2)
        with open(os.path.join(target, "note.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "old")
        self.assertEqual(len(os.listdir(target)), 3)  # Release, note.txt and the renamed copy of the loose file
        self.assertFalse(os.path.exists(source))

    def test_an_unreadable_source_moves_nothing(self) -> None:
        with mock.patch.object(asset_downloader.os, "scandir", side_effect=OSError("denied")):
            self.assertEqual(asset_downloader._move_complete_repo_tree(self.root, os.path.join(self.root, "t")), 0)

    def test_a_release_type_folder_that_cannot_be_read_is_skipped(self) -> None:
        source = os.path.join(self.root, "src")
        os.makedirs(os.path.join(source, "Release", "r1"))
        real_scandir = os.scandir
        calls = {"n": 0}

        def flaky(path):
            calls["n"] += 1
            if calls["n"] == 2:  # the first call lists the repository folder, the second one the type folder
                raise OSError("denied")
            return real_scandir(path)

        with mock.patch.object(asset_downloader.os, "scandir", flaky):
            moved = asset_downloader._move_complete_repo_tree(source, os.path.join(self.root, "t"))
        self.assertEqual(moved, 0)
        self.assertTrue(os.path.isdir(os.path.join(source, "Release", "r1")))  # nothing lost

    def test_the_deferred_move_with_nothing_configured(self) -> None:
        with mock.patch.object(asset_downloader, "get_all_download_dirs", lambda config: []):
            result = self.run_quietly(asset_downloader.move_complete_folders_to_mapped_destinations)
        self.assertEqual(result["moved_release_folders"], 0)

    def test_the_deferred_move_ignores_odd_entries_and_missing_folders(self) -> None:
        os.makedirs(self.complete)
        with mock.patch.object(asset_downloader, "load_mapping", lambda: {"repositories": [
                "not a dict", {"repository": ""}, {"repository": REPO, "destination": self.destination}]}):
            result = self.run_quietly(asset_downloader.move_complete_folders_to_mapped_destinations)
        self.assertEqual(result["scanned_repo_roots"], 0)  # nothing in Complete for any of them

    def test_the_deferred_move_skips_a_repository_without_destination(self) -> None:
        self.write_mapping(destination="")
        self.make_release("app (owner)", "Release", "r1", root=self.complete)
        result = self.run_quietly(asset_downloader.move_complete_folders_to_mapped_destinations)
        self.assertEqual(result["skipped_without_destination"], 1)
        self.assertEqual(result["moved_release_folders"], 0)

    def test_the_deferred_move_skips_a_repository_whose_destination_resolves_to_nothing(self) -> None:
        self.write_mapping(destination=self.destination)
        self.make_release("app (owner)", "Release", "r1", root=self.complete)
        with mock.patch.object(asset_downloader, "_resolve_finalized_base_directory", lambda repo, default: (default, False, None)):
            result = self.run_quietly(asset_downloader.move_complete_folders_to_mapped_destinations)
        self.assertEqual(result["skipped_without_destination"], 1)

    def test_a_superseded_folder_outside_any_processing_folder_is_left_alone(self) -> None:
        stray = os.path.join(self.root, "stray", "release")
        os.makedirs(stray)
        self.assertIsNone(self.run_quietly(asset_downloader.move_processing_folder_to_partial, stray))
        self.assertTrue(os.path.isdir(stray))

    def test_cleaning_up_empty_parents_stops_at_a_folder_that_is_not_empty(self) -> None:
        base = os.path.join(self.root, "processing")
        deep = os.path.join(base, "a", "b", "c")
        os.makedirs(deep)
        with open(os.path.join(base, "a", "keep.txt"), "w", encoding="utf-8") as handle:
            handle.write("x")
        asset_downloader._remove_empty_processing_parents(deep, base)
        self.assertFalse(os.path.exists(os.path.join(base, "a", "b")))
        self.assertTrue(os.path.exists(os.path.join(base, "a", "keep.txt")))

    def test_cleaning_up_starts_from_the_nearest_existing_parent(self) -> None:
        base = os.path.join(self.root, "processing")
        os.makedirs(os.path.join(base, "a"))
        asset_downloader._remove_empty_processing_parents(os.path.join(base, "a", "gone", "deeper"), base)
        self.assertFalse(os.path.exists(os.path.join(base, "a")))
        self.assertTrue(os.path.isdir(base))  # the Processing root itself stays


if __name__ == "__main__":
    unittest.main()
