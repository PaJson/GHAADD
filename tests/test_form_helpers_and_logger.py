"""Smaller pieces that are easy to get subtly wrong: the editor's text parsers, the GUI's "Open folder" agreeing with
where the downloader really puts files, the lifecycle event logger, and the --doctor checks.

Everything runs against temporary files and a temporary state.db.
Run from the project root: python -m unittest discover -s tests -t .
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("GMAIL_USER", "test@example.invalid")  # asset_downloader's imports need these to exist
os.environ.setdefault("GMAIL_APP_PASSWORD", "unused")

from modules import asset_downloader, db_manager, doctor_checks, gui_forms, lifecycle_logger, mapping_manager  # noqa: E402


class ParserTests(unittest.TestCase):
    def test_lists_are_shown_as_comma_separated_text(self) -> None:
        self.assertEqual(gui_forms.format_list([5, 15]), "5, 15")
        self.assertEqual(gui_forms.format_list([]), "")
        self.assertEqual(gui_forms.format_list(None), "")

    def test_text_lists_are_trimmed_deduplicated_and_keep_their_first_spelling(self) -> None:
        self.assertEqual(gui_forms.parse_str_list(" Release, ,pre-release,RELEASE , Pre-release"), ["Release", "pre-release"])
        self.assertEqual(gui_forms.parse_str_list(""), [])
        self.assertEqual(gui_forms.parse_str_list(None), [])

    def test_number_lists_accept_positive_whole_numbers_only(self) -> None:
        errors: list[str] = []
        self.assertEqual(gui_forms.parse_int_list("5, 15,5 ,30", "Recheck", errors), [5, 15, 30])
        self.assertEqual(errors, [])
        for text in ("0", "-5", "1.5", "abc", "5, x"):
            with self.subTest(text=text):
                errors = []
                gui_forms.parse_int_list(text, "Recheck", errors)
                self.assertTrue(errors and errors[0].startswith("Recheck:"), errors)

    def test_a_single_number(self) -> None:
        errors: list[str] = []
        self.assertEqual(gui_forms.parse_int("12", "Limit", errors), 12)
        self.assertEqual(gui_forms.parse_int(" 0 ", "Limit", errors), 0)
        self.assertEqual(errors, [])
        for bad, fragment in (("", "whole number"), ("x", "whole number"), ("-", "whole number"), ("1.5", "whole number"), (None, "whole number"), ("-1", "0 or more")):
            with self.subTest(text=bad):
                errors = []
                self.assertIsNone(gui_forms.parse_int(bad, "Limit", errors))
                self.assertIn(fragment, errors[0])

    def test_a_minimum_is_enforced(self) -> None:
        errors: list[str] = []
        self.assertIsNone(gui_forms.parse_int("5", "Interval", errors, minimum=10))
        self.assertIn("10 or more", errors[0])

    def test_sanity_choices_round_trip_and_unknown_values_mean_the_default(self) -> None:
        for key, label in gui_forms.SANITY_CHOICES:
            self.assertEqual(gui_forms.sanity_value(gui_forms.sanity_label(key)), key)
            self.assertEqual(gui_forms.sanity_label(key), label)
        for odd in (None, "", "bogus", 5):
            self.assertEqual(gui_forms.sanity_value(gui_forms.sanity_label(odd)), "any_tag")
        self.assertEqual(gui_forms.sanity_value("not a label"), "any_tag")
        self.assertEqual(gui_forms.sanity_label(" SAME_TAG "), dict(gui_forms.SANITY_CHOICES)["same_tag"])


class OpenFolderMatchesDownloaderTests(unittest.TestCase):
    """The GUI's "Open folder" must point where the downloader really writes."""

    ENTRIES = (
        ("o/app", "App", "@GitHub"),
        ("o/app", "", "@GitHub/Nightly"),
        ("stenzek/duckstation", "DuckStation: the emulator?", "GitHub"),
        ("o/app", "Näme with spaces", ""),
        ("o/app", "App", "../../etc"),
        ("o/app", "App", "a:b/c*d"),
        ("o/app", "App", "/@GitHub/"),
    )

    def test_same_folders_for_the_same_settings(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root:
            for repo, folder, subfolder in self.ENTRIES:
                with self.subTest(repo=repo, folder=folder, subfolder=subfolder):
                    entry = {"repository": repo, "destination": root, "folder": folder, "subfolder": subfolder}
                    with mock.patch.object(asset_downloader, "get_repository_mapping", lambda name, entry=entry: entry):
                        expected, uses_mapping, warning = asset_downloader._resolve_finalized_base_directory(repo, "unused")
                    self.assertTrue(uses_mapping, warning)
                    os.makedirs(expected, exist_ok=True)
                    opened = gui_forms.resolve_open_folder(repo, root, folder, subfolder)
                    assert opened is not None
                    self.assertEqual(os.path.normcase(opened), os.path.normcase(os.path.normpath(expected)))


class LoggerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._temp_dir.cleanup)
        self.root = self._temp_dir.name
        self.dry_run = False
        db_path = os.path.join(self.root, "state.db")
        for target, name, value in (
            (db_manager, "get_state_db_path", lambda: db_path),
            (lifecycle_logger, "is_dry_run", lambda: self.dry_run),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class LifecycleLoggerTests(LoggerTestCase):
    def test_a_completed_move_is_recorded_with_a_short_commit(self) -> None:
        lifecycle_logger.log_completed_move("o/app", "v1", "abcdef1234567", "X:\\dest\\rel")
        (event,) = lifecycle_logger.list_lifecycle_events()
        self.assertEqual((event["event_type"], event["repo"], event["tag"]), ("COMPLETED_MOVE", "o/app", "v1"))
        self.assertEqual(event["destination_path"], "X:\\dest\\rel")
        self.assertEqual(event["message"], "Completed [o/app v1 (abcdef1)] moved to [X:\\dest\\rel]")
        self.assertEqual(event["commit_hash"], "abcdef1234567")

    def test_a_partial_move_and_an_unknown_commit(self) -> None:
        lifecycle_logger.log_partial_move("o/app", "v1", None, "X:\\partial")
        (event,) = lifecycle_logger.list_lifecycle_events()
        self.assertEqual(event["event_type"], "PARTIAL_MOVE")
        self.assertEqual(event["message"], "Superseded [o/app v1 (unknown)] moved to [X:\\partial]")

    def test_warnings_get_an_upper_case_category(self) -> None:
        lifecycle_logger.log_warning("limit", "m1")
        lifecycle_logger.log_warning("", "m2")
        lifecycle_logger.log_warning(None, "m3")
        categories = {e["message"]: e["category"] for e in lifecycle_logger.list_lifecycle_events(limit=None)}
        self.assertEqual(categories, {"m1": "LIMIT", "m2": "GENERAL", "m3": "GENERAL"})

    def test_events_are_listed_newest_first_with_filters_and_a_readable_time(self) -> None:
        lifecycle_logger.log_completed_move("o/one", "v1", None, "a")
        lifecycle_logger.log_completed_move("x/two", "v2", None, "b")
        lifecycle_logger.log_warning("MAPPING", "w")
        events = lifecycle_logger.list_lifecycle_events(limit=None)
        self.assertEqual([e["tag"] for e in events], [None, "v2", "v1"])  # the warning was written last
        self.assertEqual(len(lifecycle_logger.list_lifecycle_events(limit=2)), 2)
        self.assertEqual([e["repo"] for e in lifecycle_logger.list_lifecycle_events(repo_filter="TWO")], ["x/two"])
        self.assertEqual(len(lifecycle_logger.list_lifecycle_events(event_type="WARNING")), 1)
        self.assertRegex(events[0]["created_at_readable"], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")

    def test_purge_counts_first_and_then_deletes(self) -> None:
        lifecycle_logger.log_warning("A", "one")
        lifecycle_logger.log_completed_move("o/app", "v1", None, "a")
        self.assertEqual(lifecycle_logger.purge_lifecycle_events(event_type="WARNING", dry_run=True), 1)
        self.assertEqual(len(lifecycle_logger.list_lifecycle_events(limit=None)), 2)
        self.assertEqual(lifecycle_logger.purge_lifecycle_events(event_type="WARNING"), 1)
        self.assertEqual([e["event_type"] for e in lifecycle_logger.list_lifecycle_events(limit=None)], ["COMPLETED_MOVE"])

    def test_a_dry_run_leaves_no_trace(self) -> None:
        self.dry_run = True
        lifecycle_logger.log_warning("A", "x")
        lifecycle_logger.log_completed_move("o/app", "v1", None, "a")
        lifecycle_logger.log_cycle_summary()
        self.assertEqual(lifecycle_logger.clear_limit_warnings("o/app"), 0)
        self.dry_run = False
        self.assertEqual(lifecycle_logger.list_lifecycle_events(limit=None), [])

    def test_logging_never_raises_even_when_the_database_cannot_be_opened(self) -> None:
        bad = os.path.join(self.root, "no-such-folder", "state.db")
        with mock.patch.object(db_manager, "get_state_db_path", lambda: bad):
            lifecycle_logger.log_warning("A", "x")
            lifecycle_logger.log_completed_move("o/app", "v1", None, "a")
            self.assertEqual(lifecycle_logger.clear_limit_warnings("o/app"), 0)
            self.assertEqual(lifecycle_logger.repos_with_limit_warnings(), set())

    def test_the_cycle_summary_reads_the_same_in_the_log_and_on_screen(self) -> None:
        message = lifecycle_logger.log_cycle_summary(
            {"notifications_found": 3, "notifications_queued": 2, "notifications_skipped_malformed": 0,
             "notifications_skipped_inactive": 1, "notifications_skipped_skiplist": 0, "notifications_errors": 0,
             "notifications_collapsed_duplicates": 4},
            {"due_jobs": 5, "completed": 2, "failed": 1, "retried": 2, "superseded": 3, "downloaded_files": 12, "skipped_files": 7},
        )
        self.assertEqual(
            message,
            "Cycle summary - notifications: found=3 queued=2 (malformed=0, inactive=1, skiplist=0, errors=0, duplicates_collapsed=4) "
            "| queue: due=5 completed=2 failed=1 retried=2 superseded=3 files(downloaded=12, skipped=7)",
        )
        (event,) = lifecycle_logger.list_lifecycle_events(event_type="CYCLE_SUMMARY")
        self.assertEqual(event["message"], message)

    def test_an_empty_cycle_summary_is_all_zeros(self) -> None:
        self.assertIn("found=0 queued=0", lifecycle_logger.log_cycle_summary())
        self.assertIn("due=0 completed=0", lifecycle_logger.log_cycle_summary(None, None))

    def test_limit_warnings_can_be_found_and_cleared_per_repository(self) -> None:
        lifecycle_logger.log_warning("LIMIT", "Folder limit warning: O/App currently has 12 folder(s) in 'X' (limit=10, used space=1 GB).")
        lifecycle_logger.log_warning("LIMIT", "Folder limit warning: o/other currently has 11 folder(s) in 'Y' (limit=10, used space=1 GB).")
        lifecycle_logger.log_warning("MAPPING", "something else")
        self.assertEqual(lifecycle_logger.repos_with_limit_warnings(), {"o/app", "o/other"})
        self.assertEqual(lifecycle_logger.clear_limit_warnings("o/app"), 1)  # matched case-insensitively
        self.assertEqual(lifecycle_logger.repos_with_limit_warnings(), {"o/other"})
        self.assertEqual(len(lifecycle_logger.list_lifecycle_events(event_type="WARNING", limit=None)), 2)  # the others stay


class DoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._temp_dir.cleanup)
        self.root = self._temp_dir.name
        self.config_path = os.path.join(self.root, "config.json")
        self.mapping_path = os.path.join(self.root, "mapping.json")
        self.env = {"GMAIL_USER": "me@example.invalid", "GMAIL_APP_PASSWORD": "secret", "GITHUB_PAT": "token"}
        for target, name, value in (
            (doctor_checks, "_config_file_path", lambda: self.config_path),
            (doctor_checks, "_mapping_file_path", lambda: self.mapping_path),
            (mapping_manager, "_mapping_file_path", lambda: self.mapping_path),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, path: str, payload) -> None:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(payload if isinstance(payload, str) else json.dumps(payload))

    def run_doctor(self, platform="win32", env=None):
        environment = {k: v for k, v in (env if env is not None else self.env).items()}
        with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(doctor_checks.sys, "platform", platform), \
                contextlib.redirect_stdout(io.StringIO()):
            for name in ("GMAIL_USER", "GMAIL_APP_PASSWORD", "GITHUB_PAT"):
                if name not in environment:
                    os.environ.pop(name, None)
            return doctor_checks.run_doctor()

    def test_a_healthy_setup_is_ok(self) -> None:
        self.write(self.config_path, {"paths": {"default_download_dir": "C:\\Downloads"}})
        self.write(self.mapping_path, {"repositories": [{"repository": "o/app", "destination": self.root}]})
        report = self.run_doctor()
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["platform"], "win32")

    def test_missing_credentials_are_errors_and_a_missing_token_only_a_warning(self) -> None:
        self.write(self.config_path, {})
        self.write(self.mapping_path, {"repositories": []})
        report = self.run_doctor(env={})
        self.assertFalse(report["ok"])
        self.assertTrue(any("GMAIL_USER" in e for e in report["errors"]))
        self.assertTrue(any("GMAIL_APP_PASSWORD" in e for e in report["errors"]))
        self.assertTrue(any("GITHUB_PAT" in w for w in report["warnings"]))
        self.assertFalse(any("GITHUB_PAT" in e for e in report["errors"]))

    def test_missing_files_are_warnings_not_errors(self) -> None:
        report = self.run_doctor()
        self.assertTrue(report["ok"], report["errors"])
        self.assertTrue(any("config.json is missing" in w for w in report["warnings"]))
        self.assertTrue(any("mapping.json is missing" in w for w in report["warnings"]))

    def test_broken_files_are_errors(self) -> None:
        self.write(self.config_path, "{broken")
        self.write(self.mapping_path, "[1, 2]")
        report = self.run_doctor()
        self.assertFalse(report["ok"])
        joined = " ".join(report["errors"])
        self.assertIn("config.json could not be parsed", joined)
        self.assertIn("mapping.json root must be an object", joined)

    def test_mapping_schema_problems_are_reported(self) -> None:
        self.write(self.config_path, {})
        self.write(self.mapping_path, {"repositories": [{"repository": "o/app", "limit": "ten"}]})
        report = self.run_doctor()
        self.assertFalse(report["ok"])
        self.assertTrue(any(e.startswith("mapping schema:") and ".limit" in e for e in report["errors"]))

    def test_a_vanished_destination_is_a_warning(self) -> None:
        self.write(self.config_path, {})
        gone = os.path.join(self.root, "gone")
        self.write(self.mapping_path, {"repositories": [{"repository": "o/app", "destination": gone}]})
        report = self.run_doctor()
        self.assertTrue(report["ok"], report["errors"])
        self.assertTrue(any("destination missing for 'o/app'" in w for w in report["warnings"]))

    def test_paths_from_the_wrong_operating_system_are_flagged(self) -> None:
        self.write(self.config_path, {"paths": {"default_download_dir": "/home/me/dl"}})
        self.write(self.mapping_path, {"repositories": [{"repository": "o/app", "destination": "D:\\x"}]})
        windows = self.run_doctor(platform="win32")
        linux = self.run_doctor(platform="linux")
        self.assertTrue(any("POSIX-style on Windows" in w and "default_download_dir" in w for w in windows["warnings"]))
        self.assertTrue(any("Windows-style on linux" in w and "o/app" in w for w in linux["warnings"]))

    def test_platform_names_map_to_the_families_the_style_check_knows(self) -> None:
        # Regression: sys.platform is "win32", which was compared with "windows", so Windows paths were never checked.
        family = doctor_checks._platform_family
        self.assertEqual([family(p) for p in ("win32", "cygwin", "linux", "linux2", "darwin", "freebsd14")],
                         ["windows", "windows", "linux", "linux", "darwin", "freebsd14"])

    @unittest.skipUnless(os.name == "nt", "drive-root shorthand only exists on Windows")
    def test_a_bare_drive_letter_in_config_is_fine_because_the_app_reads_it_as_the_drive_root(self) -> None:
        self.write(self.config_path, {"paths": {"default_download_dir": "D:"}})
        self.write(self.mapping_path, {"repositories": []})
        report = self.run_doctor(platform="win32")
        self.assertFalse([w for w in report["warnings"] if "default_download_dir" in w], report["warnings"])

    def test_a_bare_drive_letter_as_a_mapping_destination_is_flagged(self) -> None:
        self.write(self.config_path, {})
        self.write(self.mapping_path, {"repositories": [{"repository": "o/app", "destination": "K:"}]})
        report = self.run_doctor(platform="win32")
        self.assertTrue(any("drive-relative" in w and "o/app" in w for w in report["warnings"]), report["warnings"])

    def test_style_rules_directly(self) -> None:
        warn = doctor_checks._warn_path_style_mismatch
        self.assertIsNone(warn("D:\\Games", "windows"))
        self.assertIsNone(warn("/srv/games", "linux"))
        self.assertIsNone(warn("", "windows"))
        self.assertIsNone(warn(None, "windows"))
        self.assertIn("drive-relative", warn("D:", "windows") or "")
        self.assertIn("drive-relative", warn("D:games", "windows") or "")
        self.assertIsNone(warn("D:/games", "windows"))
        self.assertIn("Windows-style", warn("\\\\server\\share", "darwin") or "")


if __name__ == "__main__":
    unittest.main()
