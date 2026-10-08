"""Backups: the zip contents, retention, the schedule, the config getter, the form check and the CLI command.

Run from the project root: python -m unittest discover -s tests -t .
"""
import io
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from datetime import datetime
from unittest import mock

from modules import backup_manager, cli_commands, config_manager, db_manager, gui_forms, warning_types


def _settings(directory: str, **overrides) -> config_manager.BackupSettings:
    settings: config_manager.BackupSettings = {
        "enabled": True, "every_hours": 24, "keep_files": 3, "directory": directory, "include_env": False,
    }
    settings.update(overrides)  # type: ignore[typeddict-item]
    return settings


def _fake_snapshot(path: str) -> None:
    with open(path, "wb") as handle:
        handle.write(b"fake database")


class BackupTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = self._temp.name
        self.backups = os.path.join(self.root, "backups")
        for name, text in (("config.json", "{}"), ("mapping.json", "{}"), (".env", "SECRET=1")):
            with open(os.path.join(self.root, name), "w", encoding="utf-8") as handle:
                handle.write(text)
        for target, name in (
            (config_manager, "get_config_path"), (backup_manager.mapping_manager, "_mapping_file_path"),
            (backup_manager.env_manager, "get_env_path"),
        ):
            file_name = {"get_config_path": "config.json", "_mapping_file_path": "mapping.json", "get_env_path": ".env"}[name]
            patcher = mock.patch.object(target, name, lambda file_name=file_name: os.path.join(self.root, file_name))
            patcher.start()
            self.addCleanup(patcher.stop)
        dry = mock.patch.object(backup_manager, "is_dry_run", lambda: False)
        dry.start()
        self.addCleanup(dry.stop)

    def make(self, when: datetime, **overrides) -> backup_manager.BackupResult:
        return backup_manager.create_backup(_settings(self.backups, **overrides), now=when, snapshot=_fake_snapshot)


class CreateBackupTests(BackupTestCase):
    def test_zip_holds_database_settings_and_manifest_but_not_env(self) -> None:
        result = self.make(datetime(2026, 10, 8, 12, 0, 0))
        self.assertTrue(result.ok, result.message)
        assert result.path is not None
        self.assertEqual(os.path.basename(result.path), "ghaadd_backup_20261008_120000.zip")
        with zipfile.ZipFile(result.path) as archive:
            self.assertEqual(sorted(archive.namelist()), ["config.json", "manifest.json", "mapping.json", "state.db"])
            self.assertEqual(archive.read("state.db"), b"fake database")
            self.assertEqual(json.loads(archive.read("manifest.json"))["files"], ["state.db", "config.json", "mapping.json"])

    def test_env_is_included_only_when_asked(self) -> None:
        result = self.make(datetime(2026, 10, 8, 12, 0, 0), include_env=True)
        assert result.path is not None
        with zipfile.ZipFile(result.path) as archive:
            self.assertIn(".env", archive.namelist())

    def test_no_temporary_files_are_left_and_same_second_gets_a_counter(self) -> None:
        when = datetime(2026, 10, 8, 12, 0, 0)
        self.make(when)
        self.make(when)
        names = sorted(os.listdir(self.backups))
        self.assertEqual(names, ["ghaadd_backup_20261008_120000.zip", "ghaadd_backup_20261008_120000_2.zip"])

    def test_old_backups_beyond_keep_files_are_removed(self) -> None:
        for hour in range(5):
            self.make(datetime(2026, 10, 8, hour, 0, 0), keep_files=3)
        names = [info.name for info in backup_manager.list_backups(self.backups)]
        self.assertEqual(names, [f"ghaadd_backup_20261008_0{hour}0000.zip" for hour in (4, 3, 2)])

    def test_keep_files_zero_keeps_everything(self) -> None:
        for hour in range(4):
            self.make(datetime(2026, 10, 8, hour, 0, 0), keep_files=0)
        self.assertEqual(len(backup_manager.list_backups(self.backups)), 4)

    def test_failure_is_reported_and_leaves_nothing_behind(self) -> None:
        def broken(_path: str) -> None:
            raise sqlite3.OperationalError("disk full")

        result = backup_manager.create_backup(_settings(self.backups), snapshot=broken)
        self.assertFalse(result.ok)
        self.assertIn("disk full", result.message)
        self.assertEqual(os.listdir(self.backups), [])

    def test_dry_run_writes_nothing(self) -> None:
        with mock.patch.object(backup_manager, "is_dry_run", lambda: True):
            result = backup_manager.create_backup(_settings(self.backups), snapshot=_fake_snapshot)
        self.assertTrue(result.ok)
        self.assertFalse(os.path.exists(self.backups))

    def test_real_database_snapshot_is_a_valid_database(self) -> None:
        database = os.path.join(self.root, "state.db")
        with mock.patch.object(db_manager, "get_state_db_path", lambda: database):
            connection = db_manager.open_database()
            connection.close()
            result = backup_manager.create_backup(_settings(self.backups))
        self.assertTrue(result.ok, result.message)
        assert result.path is not None
        with zipfile.ZipFile(result.path) as archive:
            copy = os.path.join(self.root, "copy.db")
            with open(copy, "wb") as handle:
                handle.write(archive.read("state.db"))
        check = sqlite3.connect(copy)
        try:
            tables = {row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            check.close()
        self.assertIn("job_queue", tables)


class ScheduleTests(BackupTestCase):
    def test_not_due_when_switched_off(self) -> None:
        self.assertFalse(backup_manager.is_backup_due(_settings(self.backups, enabled=False)))

    def test_due_when_there_is_no_backup_yet(self) -> None:
        self.assertTrue(backup_manager.is_backup_due(_settings(self.backups)))

    def test_due_only_after_the_interval(self) -> None:
        self.make(datetime(2026, 10, 8, 12, 0, 0))
        base = datetime(2026, 10, 8, 12, 0, 0).timestamp()
        settings = _settings(self.backups, every_hours=24)
        self.assertFalse(backup_manager.is_backup_due(settings, now=base + 23 * 3600))
        self.assertTrue(backup_manager.is_backup_due(settings, now=base + 24 * 3600))

    def test_run_scheduled_backup_does_nothing_when_not_due(self) -> None:
        with mock.patch.object(config_manager, "get_backup_settings", lambda *_: _settings(self.backups, enabled=False)):
            self.assertIsNone(backup_manager.run_scheduled_backup())

    def test_run_scheduled_backup_writes_when_due(self) -> None:
        with mock.patch.object(config_manager, "get_backup_settings", lambda *_: _settings(self.backups)), \
                mock.patch.object(db_manager, "backup_database", _fake_snapshot):
            result = backup_manager.run_scheduled_backup()
        assert result is not None
        self.assertTrue(result.ok, result.message)
        self.assertEqual(len(backup_manager.list_backups(self.backups)), 1)

    def test_list_backups_ignores_other_files_and_a_missing_folder(self) -> None:
        self.assertEqual(backup_manager.list_backups(os.path.join(self.root, "nope")), [])
        os.makedirs(self.backups)
        with open(os.path.join(self.backups, "notes.txt"), "w", encoding="utf-8") as handle:
            handle.write("x")
        self.assertEqual(backup_manager.list_backups(self.backups), [])


class SettingsAndFormTests(unittest.TestCase):
    def test_defaults_are_on_daily_keep_fourteen_and_a_backups_folder(self) -> None:
        settings = config_manager.get_backup_settings({})
        self.assertTrue(settings["enabled"])
        self.assertEqual((settings["every_hours"], settings["keep_files"], settings["include_env"]), (24, 14, False))
        self.assertEqual(os.path.basename(settings["directory"]), "backups")

    def test_values_are_read_and_clamped(self) -> None:
        settings = config_manager.get_backup_settings(
            {"backup": {"enabled": True, "every_hours": 0, "keep_files": -5, "directory": "/x/y", "include_env": True}}
        )
        self.assertEqual((settings["enabled"], settings["every_hours"], settings["keep_files"]), (True, 1, 0))
        self.assertEqual((settings["directory"], settings["include_env"]), ("/x/y", True))

    def test_form_produces_dotted_config_keys(self) -> None:
        result = gui_forms.build_backup_changes(
            {"enabled": True, "every_hours": "12", "keep_files": "5", "directory": " D:/b ", "include_env": False}
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.changes, {
            "backup.enabled": True, "backup.every_hours": 12, "backup.keep_files": 5,
            "backup.directory": "D:/b", "backup.include_env": False,
        })

    def test_form_rejects_bad_numbers(self) -> None:
        result = gui_forms.build_backup_changes({"every_hours": "0", "keep_files": "x"})
        self.assertFalse(result.ok)
        self.assertEqual(len(result.errors), 2)

    def test_backup_is_a_known_warning_type(self) -> None:
        self.assertIn("BACKUP", warning_types.KNOWN_TYPES)


class DaemonHookTests(unittest.TestCase):
    def check(self, result):
        import main  # imported here: it reads .env at import time

        warnings: list[tuple[str, str]] = []
        out = io.StringIO()
        with mock.patch.object(main, "run_scheduled_backup", lambda: result), \
                mock.patch.object(main, "is_dry_run", lambda: False), \
                mock.patch.object(main, "log_warning", lambda kind, message: warnings.append((kind, message))), \
                redirect_stdout(out):
            main.run_backup_check()
        return out.getvalue(), warnings

    def test_nothing_due_prints_nothing(self) -> None:
        self.assertEqual(self.check(None), ("", []))

    def test_success_is_announced(self) -> None:
        text, warnings = self.check(backup_manager.BackupResult(True, "Backup written: x.zip", "x.zip"))
        self.assertIn("Backup written: x.zip", text)
        self.assertEqual(warnings, [])

    def test_failure_is_recorded_as_a_backup_warning(self) -> None:
        _, warnings = self.check(backup_manager.BackupResult(False, "The backup failed: disk full"))
        self.assertEqual(warnings, [("BACKUP", "The backup failed: disk full")])

    def test_an_exception_never_escapes(self) -> None:
        import main

        def boom():
            raise RuntimeError("x")

        with mock.patch.object(main, "run_scheduled_backup", boom), mock.patch.object(main, "is_dry_run", lambda: False), \
                redirect_stdout(io.StringIO()):
            main.run_backup_check()


class CliTests(unittest.TestCase):
    def run_cli(self, result: backup_manager.BackupResult) -> tuple[bool, str]:
        parsed = cli_commands.parse_cli_args(["--backup"], "test")
        out = io.StringIO()
        with mock.patch.object(backup_manager, "create_backup", lambda: result), redirect_stdout(out):
            handled = cli_commands.handle_cli_command(parsed, lambda: None)
        return handled, out.getvalue()

    def test_backup_command_prints_the_result(self) -> None:
        handled, text = self.run_cli(backup_manager.BackupResult(True, "Backup written: x.zip", "x.zip"))
        self.assertTrue(handled)
        self.assertIn("Backup written: x.zip", text)

    def test_failed_backup_exits_with_an_error(self) -> None:
        with self.assertRaises(SystemExit):
            self.run_cli(backup_manager.BackupResult(False, "The backup failed: disk full"))


if __name__ == "__main__":
    unittest.main()
