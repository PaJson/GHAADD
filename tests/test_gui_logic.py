"""Tests for the toolkit-independent GUI logic: config writes, form validation, overview rows.

Run from the project root: python -m unittest discover -s tests -t .
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from modules import config_manager, db_manager, dry_run_mode, gui_forms, repo_overview


class ConfigWriteTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.config_path = os.path.join(self._temp_dir.name, "config.json")
        patcher = mock.patch.object(config_manager, "_config_file_path", lambda: self.config_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(dry_run_mode.set_dry_run, False)

    def write(self, payload) -> None:
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    def read(self):
        with open(self.config_path, encoding="utf-8") as handle:
            return json.load(handle)


class UpdateConfigTests(ConfigWriteTestCase):
    def test_sets_nested_values_and_keeps_others(self) -> None:
        self.write({"polling": {"interval_seconds": 300, "enabled": True}, "mailbox": {"folder": "X"}})

        changed = config_manager.set_config_values(
            {"polling.interval_seconds": 600, "processing.recheck_intervals_minutes": [5, 15]}
        )

        self.assertTrue(changed)
        data = self.read()
        self.assertEqual(data["polling"], {"interval_seconds": 600, "enabled": True})
        self.assertEqual(data["processing"]["recheck_intervals_minutes"], [5, 15])
        self.assertEqual(data["mailbox"], {"folder": "X"})

    def test_number_arrays_stay_on_one_line(self) -> None:
        config_manager.set_config_values({"processing.recheck_intervals_minutes": [5, 15, 30]})

        with open(self.config_path, encoding="utf-8") as handle:
            self.assertIn('"recheck_intervals_minutes": [5, 15, 30]', handle.read())

    def test_unchanged_values_do_not_rewrite_file(self) -> None:
        self.write({"polling": {"interval_seconds": 300}})
        with open(self.config_path, "rb") as handle:
            before = handle.read()

        changed = config_manager.set_config_values({"polling.interval_seconds": 300})

        self.assertFalse(changed)
        with open(self.config_path, "rb") as handle:
            self.assertEqual(handle.read(), before)

    def test_missing_file_is_created(self) -> None:
        config_manager.set_config_values({"terminal_log.enabled": True})
        self.assertEqual(self.read(), {"terminal_log": {"enabled": True}})

    def test_unreadable_file_is_never_overwritten(self) -> None:
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.write("{ not json")

        with self.assertRaises(config_manager.ConfigUnreadableError):
            config_manager.set_config_values({"polling.interval_seconds": 60})

        with open(self.config_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "{ not json")

    def test_uses_lf_line_endings_and_leaves_no_temp_files(self) -> None:
        config_manager.set_config_values({"a.b": 1})

        with open(self.config_path, "rb") as handle:
            self.assertNotIn(b"\r", handle.read())
        self.assertEqual([n for n in os.listdir(self._temp_dir.name) if n.endswith(".tmp")], [])

    def test_lock_timeout_raises_clear_error(self) -> None:
        with mock.patch.object(config_manager, "_CONFIG_LOCK_TIMEOUT_SECONDS", 0.2):
            with config_manager.FileLock(f"{self.config_path}.lock"):
                with self.assertRaises(config_manager.ConfigLockTimeout):
                    config_manager.update_config(lambda config: None)

    def test_dry_run_writes_nothing(self) -> None:
        dry_run_mode.set_dry_run(True)
        config_manager.set_config_values({"polling.interval_seconds": 60})
        self.assertFalse(os.path.exists(self.config_path))


class RepoFormTests(unittest.TestCase):
    def valid_form(self, **overrides):
        form = {
            "destination": tempfile.gettempdir(),
            "foldername": " App ",
            "subfolder": "@GitHub",
            "limit": "10",
            "recheck": "60, 240, 60",
            "release_folders": "Release, release, Pre-release",
            "skiplist": "beta , rc",
            "paused": True,
        }
        form.update(overrides)
        return form

    def test_valid_form_builds_normalized_changes(self) -> None:
        result = gui_forms.build_repo_changes(self.valid_form())

        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.warnings, [])
        self.assertEqual(result.changes["foldername"], "App")
        self.assertEqual(result.changes["limit"], 10)
        self.assertEqual(result.changes["recheck_intervals_minutes"], [60, 240])
        self.assertEqual(result.changes["limit_release_type_folders"], ["Release", "Pre-release"])
        self.assertEqual(result.changes["skiplist"], ["beta", "rc"])
        self.assertIs(result.changes["paused"], True)
        self.assertNotIn("name", result.changes)
        self.assertNotIn("last_finalized", result.changes)

    def test_empty_recheck_means_default(self) -> None:
        result = gui_forms.build_repo_changes(self.valid_form(recheck=""))
        self.assertTrue(result.ok)
        self.assertEqual(result.changes["recheck_intervals_minutes"], [])

    def test_bad_values_are_reported(self) -> None:
        result = gui_forms.build_repo_changes(
            self.valid_form(destination=" ", limit="ten", recheck="5, x, -3")
        )

        self.assertFalse(result.ok)
        joined = " ".join(result.errors)
        self.assertIn("Destination", joined)
        self.assertIn("Limit", joined)
        self.assertIn("'x'", joined)
        self.assertIn("'-3'", joined)

    def test_missing_destination_folder_is_only_a_warning(self) -> None:
        missing = os.path.join(tempfile.gettempdir(), "ghaadd-does-not-exist-xyz")
        result = gui_forms.build_repo_changes(self.valid_form(destination=missing))

        self.assertTrue(result.ok)
        self.assertEqual(len(result.warnings), 1)

    def test_new_repo_needs_owner_slash_repo_and_destination(self) -> None:
        self.assertTrue(gui_forms.build_new_repo("owner/repo", tempfile.gettempdir()).ok)
        for bad in ("repo", "a/b/c", "a/ b", "/b", ""):
            with self.subTest(name=bad):
                self.assertFalse(gui_forms.build_new_repo(bad, tempfile.gettempdir()).ok)
        self.assertFalse(gui_forms.build_new_repo("owner/repo", "").ok)


class SettingsFormTests(unittest.TestCase):
    def valid_form(self, **overrides):
        form = {
            "recheck": "5, 15, 30",
            "max_emails": "0",
            "dest_check": "10",
            "interval": "300",
            "jitter_min": "5",
            "jitter_max": "30",
            "download_dir": tempfile.gettempdir(),
            "log_enabled": True,
            "log_max_mb": "10",
            "log_keep": "30",
        }
        form.update(overrides)
        return form

    def test_valid_form_maps_to_dotted_config_keys(self) -> None:
        result = gui_forms.build_settings_changes(self.valid_form())

        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.changes["processing.recheck_intervals_minutes"], [5, 15, 30])
        self.assertEqual(result.changes["polling.interval_seconds"], 300)
        self.assertIs(result.changes["terminal_log.enabled"], True)
        self.assertEqual(set(result.changes), set(gui_forms.SETTINGS_KEYS.values()))

    def test_invalid_values_are_reported(self) -> None:
        result = gui_forms.build_settings_changes(
            self.valid_form(recheck="", interval="5", jitter_min="40", log_keep="x", download_dir="")
        )

        self.assertFalse(result.ok)
        joined = " ".join(result.errors)
        for fragment in ("Default recheck", "Polling interval", "Jitter min", "Keep log files", "download dir"):
            self.assertIn(fragment, joined)


class OverviewTests(unittest.TestCase):
    NOW = 1_800_000_000.0

    def summary(self, **overrides):
        base = {
            "last_activity": self.NOW - 100,
            "latest_tag": "v1",
            "latest_status": "COMPLETED",
            "latest_updated_at": self.NOW - 100,
            "latest_downloaded": 3,
            "latest_skipped": 5,
            "latest_total": 8,
            "pending_next_check": None,
            "pending_attempts": 0,
        }
        base.update(overrides)
        return base

    def test_status_rules(self) -> None:
        derive = repo_overview.derive_status
        self.assertEqual(derive(True, self.summary(pending_next_check=self.NOW - 1), self.NOW), "Paused")
        self.assertEqual(derive(False, self.summary(pending_next_check=self.NOW - 1), self.NOW), "Queued")
        self.assertEqual(derive(False, self.summary(pending_next_check=self.NOW + 60), self.NOW), "Waiting")
        self.assertEqual(derive(False, self.summary(latest_status="FAILED"), self.NOW), "Failed")
        self.assertEqual(derive(False, self.summary(), self.NOW), "Idle")
        self.assertEqual(derive(False, None, self.NOW), "Idle")
        self.assertEqual(derive(True, self.summary(), self.NOW, running=True), "Running")
        self.assertEqual(derive(False, None, self.NOW, running=True), "Running")

    def test_rows_order_by_recent_activity_then_folder_name(self) -> None:
        entries = [
            {"name": "o/never-b", "foldername": "B never"},
            {"name": "o/old", "foldername": "Old"},
            {"name": "o/new", "foldername": "New"},
            {"name": "o/never-a", "foldername": "a never"},
        ]
        summaries = {
            "o/old": self.summary(last_activity=self.NOW - 500),
            "o/new": self.summary(last_activity=self.NOW - 5),
        }

        rows = repo_overview.build_rows(entries, summaries, self.NOW, lambda entry: 5)

        self.assertEqual([row.repo for row in rows], ["o/new", "o/old", "o/never-a", "o/never-b"])

    def test_running_repo_is_matched_case_insensitively(self) -> None:
        entries = [{"name": "Owner/Repo", "foldername": "A"}, {"name": "o/other", "foldername": "B"}]

        rows = repo_overview.build_rows(entries, {}, self.NOW, lambda entry: 5, running_repo="owner/repo")

        self.assertEqual({row.repo: row.status for row in rows}, {"Owner/Repo": "Running", "o/other": "Idle"})

    def test_row_values_for_a_waiting_repo(self) -> None:
        entries = [{"name": "Owner/Repo", "foldername": "", "destination": "K:\\Apps", "limit": 10}]
        summaries = {"owner/repo": self.summary(pending_next_check=self.NOW + 600, pending_attempts=2)}

        (row,) = repo_overview.build_rows(entries, summaries, self.NOW, lambda entry: 5)

        self.assertEqual(row.foldername, "Owner/Repo")  # falls back to the repo name
        self.assertEqual((row.status, row.step, row.tag, row.files, row.limit), ("Waiting", "2 / 5", "v1", "8", "10"))
        self.assertEqual(row.files, "8")
        self.assertNotEqual(row.next_check, "-")

    def test_repo_without_jobs_shows_dashes(self) -> None:
        (row,) = repo_overview.build_rows([{"name": "o/x", "foldername": "X"}], {}, self.NOW, lambda entry: 5)
        self.assertEqual((row.status, row.tag, row.last_check, row.step, row.next_check, row.files), ("Idle",) + ("-",) * 5)
        self.assertEqual(row.files, "-")

    def test_files_shows_missing_count_only_while_incomplete(self) -> None:
        format_files = repo_overview.format_files
        self.assertEqual(format_files(self.summary(latest_downloaded=17, latest_skipped=0, latest_total=17)), "17")
        self.assertEqual(format_files(self.summary(latest_downloaded=0, latest_skipped=17, latest_total=17)), "17")
        self.assertEqual(format_files(self.summary(latest_downloaded=3, latest_skipped=5, latest_total=16)), "8 / 16")
        self.assertEqual(format_files(self.summary(latest_total=0)), "-")
        self.assertEqual(format_files(None), "-")


class RepoJobSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        db_path = os.path.join(self._temp_dir.name, "state.db")
        patcher = mock.patch.object(db_manager, "get_state_db_path", lambda: db_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.connection = db_manager.open_database()
        self.addCleanup(self.connection.close)

    def add_job(self, repo, tag, status, next_check, attempts=0, updated=100.0, downloaded=0, skipped=0, total=0):
        self.connection.execute(
            "INSERT INTO job_queue (repo, tag, status, attempt_count, next_check_time, downloaded_count,"
            " skipped_count, total_items, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (repo, tag, status, attempts, next_check, downloaded, skipped, total, updated),
        )
        self.connection.commit()

    def test_summarizes_latest_pending_and_activity_per_repo(self) -> None:
        self.add_job("Owner/App", "v1", "COMPLETED", 10, updated=100, downloaded=4)
        self.add_job("Owner/App", "v2", "PENDING", 500, attempts=1, updated=200, downloaded=1, skipped=6, total=8)
        self.add_job("Owner/App", "v2", "PENDING", 300, attempts=2, updated=150, downloaded=1, skipped=6, total=8)
        self.add_job("Owner/App", "v3", "SUPERSEDED", 0, updated=900)
        self.add_job("other/failed", "v9", "FAILED", 5, updated=50)

        summaries = db_manager.get_repo_job_summaries(self.connection)

        app = summaries["owner/app"]
        self.assertEqual(app["latest_tag"], "v2")  # newest non-superseded job
        self.assertEqual(app["pending_next_check"], 300)  # earliest pending wins
        self.assertEqual(app["pending_attempts"], 2)
        self.assertEqual((app["latest_downloaded"], app["latest_skipped"], app["latest_total"]), (1, 6, 8))
        self.assertEqual(app["last_activity"], 900)
        failed = summaries["other/failed"]
        self.assertEqual((failed["latest_status"], failed["pending_next_check"]), ("FAILED", None))

    def test_empty_database_gives_empty_summary(self) -> None:
        self.assertEqual(db_manager.get_repo_job_summaries(self.connection), {})


if __name__ == "__main__":
    unittest.main()
