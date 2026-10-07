"""Folder naming and the code that moves finished downloads: Processing -> Complete / Partial / mapped destination.

This is the code that touches your files, so the routing, name collisions, dry-run and the "never delete
anything that is not empty" promise are checked here, all inside temporary folders (real config.json,
mapping.json and state.db are replaced for the duration of each test).
Run from the project root: python -m unittest discover -s tests -t .
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from modules import asset_downloader, config_manager, db_manager, mapping_manager

REPO = "owner/app"
REPO_FOLDER = "app (owner)"  # what asset_downloader derives from "owner/app"


class SanitizeTests(unittest.TestCase):
    def test_characters_windows_does_not_allow(self) -> None:
        self.assertEqual(asset_downloader.sanitize_folder_name('a<b>c"d|e?f*g'), "a_b_c_d_e_f_g")
        self.assertEqual(asset_downloader.sanitize_folder_name("a/b\\c"), "a_b_c")

    def test_a_colon_becomes_a_dash(self) -> None:
        self.assertEqual(asset_downloader.sanitize_folder_name("Nightly: build 5"), "Nightly- build 5")

    def test_outer_spaces_are_trimmed_and_inner_ones_kept(self) -> None:
        self.assertEqual(asset_downloader.sanitize_folder_name("  My App  v1  "), "My App  v1")

    def test_nothing_to_name_gives_unknown_or_an_empty_name(self) -> None:
        self.assertEqual(asset_downloader.sanitize_folder_name(None), "unknown")
        self.assertEqual(asset_downloader.sanitize_folder_name(""), "unknown")
        self.assertEqual(asset_downloader.sanitize_folder_name("   "), "")  # callers add their own fallback

    def test_unicode_is_kept(self) -> None:
        self.assertEqual(asset_downloader.sanitize_folder_name("Café 🎉 日本"), "Café 🎉 日本")

    def test_a_nested_path_is_sanitized_part_by_part_and_cannot_climb_out(self) -> None:
        join = os.path.join
        self.assertEqual(asset_downloader._sanitize_folder_path("@GitHub/Nightly"), join("@GitHub", "Nightly"))
        self.assertEqual(asset_downloader._sanitize_folder_path("a\\b"), join("a", "b"))
        self.assertEqual(asset_downloader._sanitize_folder_path("../../evil/./x"), join("evil", "x"))
        self.assertEqual(asset_downloader._sanitize_folder_path("a:b/c?d"), join("a-b", "c_d"))
        self.assertEqual(asset_downloader._sanitize_folder_path(""), "")
        self.assertEqual(asset_downloader._sanitize_folder_path("../.."), "")

    def test_slashes_at_the_ends_do_not_create_an_unknown_folder(self) -> None:
        # Regression: "@GitHub/" used to become "@GitHub/unknown".
        join = os.path.join
        self.assertEqual(asset_downloader._sanitize_folder_path("@GitHub/"), "@GitHub")
        self.assertEqual(asset_downloader._sanitize_folder_path("/@GitHub"), "@GitHub")
        self.assertEqual(asset_downloader._sanitize_folder_path("a//b/ /c\\"), join("a", "b", "c"))
        self.assertEqual(asset_downloader._sanitize_folder_path("   "), "")

    def test_the_default_parent_folder_of_a_repository(self) -> None:
        self.assertEqual(asset_downloader._build_repo_parent_folder("owner/app"), "app (owner)")
        self.assertEqual(asset_downloader._build_repo_parent_folder(""), "unknown (unknown)")
        self.assertEqual(asset_downloader._build_repo_parent_folder("owner"), "owner (owner)")


class ReleaseFolderNameTests(unittest.TestCase):
    def build(self, release: dict, raw_name: str = "Big update") -> str:
        with mock.patch.object(asset_downloader, "get_short_commit_hash", lambda repo, tag, headers: "abc1234"):
            return asset_downloader.build_folder_name(REPO, release, {}, raw_name)

    def test_date_name_tag_and_commit(self) -> None:
        release = {"published_at": "2026-10-06T13:24:00Z", "tag_name": "v1.0"}
        self.assertEqual(self.build(release), "2026-10-06_13-24, Big update, v1.0, abc1234")

    def test_unsafe_characters_are_removed_from_every_part(self) -> None:
        release = {"published_at": "2026-10-06T13:24:00Z", "tag_name": "rel/1:2"}
        self.assertEqual(self.build(release, "Night: ly*"), "2026-10-06_13-24, Night- ly_, rel_1-2, abc1234")

    def test_missing_date_and_tag(self) -> None:
        self.assertEqual(self.build({}), "unknown-date, Big update, unknown-tag, abc1234")

    def test_the_default_name_is_the_release_title_else_the_tag(self) -> None:
        with mock.patch.object(asset_downloader, "get_short_commit_hash", lambda repo, tag, headers: "abc1234"):
            titled = asset_downloader.generate_folder_name(
                REPO, {"name": "Title", "tag_name": "v2", "published_at": "2026-01-02T03:04:05Z"}, {}
            )
            untitled = asset_downloader.generate_folder_name(
                REPO, {"name": "", "tag_name": "v2", "published_at": "2026-01-02T03:04:05Z"}, {}
            )
        self.assertEqual(titled, "2026-01-02_03-04, Title, v2, abc1234")
        self.assertEqual(untitled, "2026-01-02_03-04, v2, v2, abc1234")


class ExpectedSignatureTests(unittest.TestCase):
    def test_only_a_key_gives_no_signature(self) -> None:
        self.assertIsNone(asset_downloader.build_expected_signature({"key": "asset:1"}))

    def test_known_metadata_is_joined_in_a_fixed_order(self) -> None:
        item = {"key": "asset:1", "name": "app.zip", "expected_size": 123, "expected_updated_at": "2026-10-06T10:00:00Z"}
        self.assertEqual(asset_downloader.build_expected_signature(item), "asset:1|app.zip|123|2026-10-06T10:00:00Z")

    def test_a_size_of_zero_still_counts(self) -> None:
        self.assertEqual(asset_downloader.build_expected_signature({"key": "k", "expected_size": 0}), "k|0")

    def test_the_same_file_with_another_size_has_another_signature(self) -> None:
        one = asset_downloader.build_expected_signature({"key": "k", "name": "a", "expected_size": 1})
        two = asset_downloader.build_expected_signature({"key": "k", "name": "a", "expected_size": 2})
        self.assertNotEqual(one, two)


class CollisionTests(unittest.TestCase):
    def test_a_free_name_is_kept_and_a_taken_one_gets_a_number(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root:
            target = os.path.join(root, "release")
            self.assertEqual(asset_downloader._resolve_directory_name_collision(target), target)
            os.makedirs(target)
            self.assertEqual(asset_downloader._resolve_directory_name_collision(target), target + " (2)")
            os.makedirs(target + " (2)")
            self.assertEqual(asset_downloader._resolve_directory_name_collision(target), target + " (3)")


class MoveTestCase(unittest.TestCase):
    """A throw-away download root, destination, config, mapping and database."""

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._temp_dir.cleanup)
        self.root = self._temp_dir.name
        self.downloads = os.path.join(self.root, "downloads")
        self.destination = os.path.join(self.root, "dest")
        os.makedirs(self.destination)
        self.ghaadd = os.path.join(self.downloads, "GHAADD")
        self.processing = os.path.join(self.ghaadd, "Processing")
        self.complete = os.path.join(self.ghaadd, "Complete")
        self.partial = os.path.join(self.ghaadd, "Partial")
        self.mapping_path = os.path.join(self.root, "mapping.json")
        config_path = os.path.join(self.root, "config.json")
        db_path = os.path.join(self.root, "state.db")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump({"paths": {"default_download_dir": self.downloads}}, handle)
        self.warnings: list[tuple] = []
        self.write_mapping()
        for target, name, value in (
            (db_manager, "get_state_db_path", lambda: db_path),
            (config_manager, "_config_file_path", lambda: config_path),
            (mapping_manager, "_mapping_file_path", lambda: self.mapping_path),
            (asset_downloader, "is_dry_run", lambda: self.dry_run),
            (asset_downloader, "log_warning", lambda *args, **kwargs: self.warnings.append(args)),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.dry_run = False

    def write_mapping(self, **entry_fields) -> None:
        entries = []
        if entry_fields is not None and entry_fields != {"none": True}:
            entries.append({"repository": REPO, **entry_fields})
        with open(self.mapping_path, "w", encoding="utf-8") as handle:
            json.dump({"repositories": entries}, handle)

    def make_release(self, *parts: str, files: dict | None = None, root: str | None = None) -> str:
        """Create <root or Processing>/<parts...> with a couple of files and return its path."""
        path = os.path.join(root or self.processing, *parts)
        os.makedirs(path)
        for name, text in (files or {"app.zip": "payload", "notes.txt": "hello"}).items():
            with open(os.path.join(path, name), "w", encoding="utf-8") as handle:
                handle.write(text)
        return path

    def run_quietly(self, function, *args):
        with contextlib.redirect_stdout(io.StringIO()):
            return function(*args)

    def tree(self, base: str) -> list[str]:
        found = []
        for folder, _dirs, files in os.walk(base):
            found.extend(os.path.relpath(os.path.join(folder, name), base).replace(os.sep, "/") for name in files)
        return sorted(found)

    def read(self, *parts: str) -> str:
        with open(os.path.join(*parts), encoding="utf-8") as handle:
            return handle.read()


class MoveToCompleteTests(MoveTestCase):
    def test_without_a_mapped_destination_the_release_lands_in_complete(self) -> None:
        self.write_mapping()  # a mapping entry without a destination
        source = self.make_release(REPO_FOLDER, "Release", "2026-10-06_13-24, Big, v1, abc1234")

        target = self.run_quietly(asset_downloader.move_processing_folder_to_complete, source, REPO)

        expected = os.path.join(self.complete, REPO_FOLDER, "Release", "2026-10-06_13-24, Big, v1, abc1234")
        self.assertEqual(os.path.normpath(target), os.path.normpath(expected))
        self.assertEqual(self.read(target, "app.zip"), "payload")
        self.assertEqual(self.read(target, "notes.txt"), "hello")
        self.assertFalse(os.path.exists(source))

    def test_the_processing_root_survives_and_empty_branches_are_tidied(self) -> None:
        source = self.make_release(REPO_FOLDER, "Release", "rel1")
        self.run_quietly(asset_downloader.move_processing_folder_to_complete, source, REPO)
        self.assertTrue(os.path.isdir(self.processing))  # never removed
        self.assertFalse(os.path.exists(os.path.join(self.processing, REPO_FOLDER)))  # empty branch pruned

    def test_a_sibling_release_still_being_processed_is_not_touched(self) -> None:
        finished = self.make_release(REPO_FOLDER, "Release", "rel1")
        other = self.make_release(REPO_FOLDER, "Release", "rel2")
        self.run_quietly(asset_downloader.move_processing_folder_to_complete, finished, REPO)
        self.assertEqual(self.tree(other), ["app.zip", "notes.txt"])
        self.assertTrue(os.path.isdir(os.path.join(self.processing, REPO_FOLDER, "Release")))

    def test_a_mapped_destination_gets_destination_folder_subfolder_and_release_type(self) -> None:
        self.write_mapping(destination=self.destination, folder="My App", subfolder="@GitHub")
        source = self.make_release(REPO_FOLDER, "Pre-release", "rel1")

        target = self.run_quietly(asset_downloader.move_processing_folder_to_complete, source, REPO)

        expected = os.path.join(self.destination, "My App", "@GitHub", "Pre-release", "rel1")
        self.assertEqual(os.path.normpath(target), os.path.normpath(expected))
        self.assertEqual(self.read(expected, "app.zip"), "payload")
        self.assertEqual(self.tree(self.complete), [])  # nothing left behind in Complete

    def test_a_default_folder_name_is_used_when_the_mapping_has_none(self) -> None:
        self.write_mapping(destination=self.destination, subfolder="")
        source = self.make_release(REPO_FOLDER, "Release", "rel1")
        target = self.run_quietly(asset_downloader.move_processing_folder_to_complete, source, REPO)
        self.assertEqual(
            os.path.normpath(target), os.path.normpath(os.path.join(self.destination, REPO_FOLDER, "Release", "rel1"))
        )

    def test_a_subfolder_cannot_climb_out_of_the_destination(self) -> None:
        self.write_mapping(destination=self.destination, folder="My App", subfolder="../../escape")
        source = self.make_release(REPO_FOLDER, "Release", "rel1")
        target = self.run_quietly(asset_downloader.move_processing_folder_to_complete, source, REPO)
        self.assertTrue(os.path.normpath(target).startswith(os.path.normpath(self.destination) + os.sep), target)
        self.assertFalse(os.path.exists(os.path.join(self.root, "escape")))

    def test_an_unsafe_folder_name_is_sanitized(self) -> None:
        self.write_mapping(destination=self.destination, folder="My: App?", subfolder="")
        source = self.make_release(REPO_FOLDER, "Release", "rel1")
        target = self.run_quietly(asset_downloader.move_processing_folder_to_complete, source, REPO)
        self.assertIn(os.path.join(self.destination, "My- App_"), os.path.normpath(target))

    def test_a_missing_destination_falls_back_to_complete_and_warns(self) -> None:
        missing = os.path.join(self.root, "not-there")
        self.write_mapping(destination=missing, folder="My App")
        source = self.make_release(REPO_FOLDER, "Release", "rel1")

        target = self.run_quietly(asset_downloader.move_processing_folder_to_complete, source, REPO)

        self.assertTrue(os.path.normpath(target).startswith(os.path.normpath(self.complete)), target)
        self.assertEqual(self.read(target, "app.zip"), "payload")
        self.assertFalse(os.path.exists(missing))  # the missing destination is not invented
        self.assertTrue(any(args[0] == "DESTINATION" for args in self.warnings), self.warnings)

    def test_an_existing_release_folder_is_never_overwritten(self) -> None:
        self.write_mapping(destination=self.destination, folder="My App", subfolder="")
        taken = self.make_release("My App", "Release", "rel1", files={"keep.txt": "old"}, root=self.destination)
        source = self.make_release(REPO_FOLDER, "Release", "rel1")

        target = self.run_quietly(asset_downloader.move_processing_folder_to_complete, source, REPO)

        self.assertEqual(os.path.normpath(target), os.path.normpath(taken + " (2)"))
        self.assertEqual(self.tree(taken), ["keep.txt"])
        self.assertEqual(self.read(taken, "keep.txt"), "old")
        self.assertEqual(self.read(target, "app.zip"), "payload")

    def test_a_dry_run_moves_nothing(self) -> None:
        self.write_mapping(destination=self.destination, folder="My App", subfolder="")
        source = self.make_release(REPO_FOLDER, "Release", "rel1")
        self.dry_run = True

        target = self.run_quietly(asset_downloader.move_processing_folder_to_complete, source, REPO)

        self.assertIn("My App", target)  # it says where it would go
        self.assertEqual(self.tree(source), ["app.zip", "notes.txt"])
        self.assertEqual(self.tree(self.destination), [])

    def test_nothing_happens_for_unusable_input(self) -> None:
        outside = os.path.join(self.root, "elsewhere", "rel1")
        os.makedirs(outside)
        for argument in ("", None, os.path.join(self.processing, "missing"), outside):
            with self.subTest(argument=argument):
                self.assertIsNone(self.run_quietly(asset_downloader.move_processing_folder_to_complete, argument, REPO))
        self.assertTrue(os.path.isdir(outside))  # a folder outside Processing is left alone


class MoveToPartialTests(MoveTestCase):
    def test_a_superseded_release_moves_to_partial_keeping_its_relative_path(self) -> None:
        source = self.make_release(REPO_FOLDER, "Release", "rel1")
        target = self.run_quietly(asset_downloader.move_processing_folder_to_partial, source)
        self.assertEqual(
            os.path.normpath(target), os.path.normpath(os.path.join(self.partial, REPO_FOLDER, "Release", "rel1"))
        )
        self.assertEqual(self.tree(target), ["app.zip", "notes.txt"])
        self.assertFalse(os.path.exists(source))
        self.assertTrue(os.path.isdir(self.processing))

    def test_an_existing_partial_folder_is_not_overwritten(self) -> None:
        taken = self.make_release(REPO_FOLDER, "Release", "rel1", files={"keep.txt": "old"}, root=self.partial)
        source = self.make_release(REPO_FOLDER, "Release", "rel1")
        target = self.run_quietly(asset_downloader.move_processing_folder_to_partial, source)
        self.assertEqual(os.path.normpath(target), os.path.normpath(taken + " (2)"))
        self.assertEqual(self.read(taken, "keep.txt"), "old")

    def test_a_dry_run_moves_nothing(self) -> None:
        source = self.make_release(REPO_FOLDER, "Release", "rel1")
        self.dry_run = True
        self.run_quietly(asset_downloader.move_processing_folder_to_partial, source)
        self.assertEqual(self.tree(source), ["app.zip", "notes.txt"])
        self.assertFalse(os.path.exists(self.partial))

    def test_nothing_happens_for_unusable_input(self) -> None:
        for argument in ("", None, os.path.join(self.processing, "missing")):
            with self.subTest(argument=argument):
                self.assertIsNone(self.run_quietly(asset_downloader.move_processing_folder_to_partial, argument))


class DeferredMoveTests(MoveTestCase):
    """Complete -> mapped destination, for releases finished before a destination was configured."""

    def test_releases_waiting_in_complete_move_to_the_destination(self) -> None:
        self.write_mapping(destination=self.destination, folder="My App", subfolder="@GitHub")
        self.make_release(REPO_FOLDER, "Release", "rel1", root=self.complete)
        self.make_release(REPO_FOLDER, "Pre-release", "rel2", root=self.complete)

        stats = self.run_quietly(asset_downloader.move_complete_folders_to_mapped_destinations)

        base = os.path.join(self.destination, "My App", "@GitHub")
        self.assertEqual(self.read(base, "Release", "rel1", "app.zip"), "payload")
        self.assertEqual(self.read(base, "Pre-release", "rel2", "notes.txt"), "hello")
        self.assertEqual(stats["moved_release_folders"], 2)
        self.assertEqual(stats["scanned_repo_roots"], 1)
        self.assertFalse(os.path.exists(os.path.join(self.complete, REPO_FOLDER)))  # emptied branch removed

    def test_a_repository_without_a_destination_stays_in_complete(self) -> None:
        self.write_mapping()
        kept = self.make_release(REPO_FOLDER, "Release", "rel1", root=self.complete)
        stats = self.run_quietly(asset_downloader.move_complete_folders_to_mapped_destinations)
        self.assertEqual(self.tree(kept), ["app.zip", "notes.txt"])
        self.assertEqual((stats["moved_release_folders"], stats["skipped_without_destination"]), (0, 1))

    def test_a_missing_destination_keeps_the_files_and_warns(self) -> None:
        self.write_mapping(destination=os.path.join(self.root, "not-there"), folder="My App")
        kept = self.make_release(REPO_FOLDER, "Release", "rel1", root=self.complete)
        stats = self.run_quietly(asset_downloader.move_complete_folders_to_mapped_destinations)
        self.assertEqual(self.tree(kept), ["app.zip", "notes.txt"])
        self.assertEqual(stats["missing_destination_warnings"], 1)
        self.assertTrue(any(args[0] == "DESTINATION" for args in self.warnings))

    def test_folders_of_unmapped_repositories_are_left_alone(self) -> None:
        self.write_mapping(destination=self.destination, folder="My App", subfolder="")
        stranger = self.make_release("other (someone)", "Release", "rel1", root=self.complete)
        self.run_quietly(asset_downloader.move_complete_folders_to_mapped_destinations)
        self.assertEqual(self.tree(stranger), ["app.zip", "notes.txt"])

    def test_an_existing_release_folder_in_the_destination_is_never_overwritten(self) -> None:
        self.write_mapping(destination=self.destination, folder="My App", subfolder="")
        taken = self.make_release("My App", "Release", "rel1", files={"keep.txt": "old"}, root=self.destination)
        self.make_release(REPO_FOLDER, "Release", "rel1", root=self.complete)
        self.run_quietly(asset_downloader.move_complete_folders_to_mapped_destinations)
        self.assertEqual(self.read(taken, "keep.txt"), "old")
        self.assertEqual(self.read(taken + " (2)", "app.zip"), "payload")

    def test_nothing_to_do_without_a_complete_folder(self) -> None:
        self.write_mapping(destination=self.destination, folder="My App")
        stats = self.run_quietly(asset_downloader.move_complete_folders_to_mapped_destinations)
        self.assertEqual(stats["moved_release_folders"], 0)


if __name__ == "__main__":
    unittest.main()
