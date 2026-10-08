"""cli_commands.handle_cli_command: which command runs, what it prints and what it refuses.

The commands are called with arguments parsed from a real command line, but everything they would act on
(daemon control, database, autostart, shortcuts, doctor, ...) is replaced by a recorder, so nothing real is touched.
Run from the project root: python -m unittest discover -s tests -t .
"""
import contextlib
import io
import json
import sqlite3
import unittest
from types import SimpleNamespace
from unittest import mock

from modules import cli_commands


class DispatchTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[tuple] = []
        self.dry_run = False
        self.daemon_running = True
        self.smoke_ran = False

    def patch(self, name: str, value) -> None:
        patcher = mock.patch.object(cli_commands, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def recorder(self, label: str, result=None):
        def record(*args, **kwargs):
            self.calls.append((label, args, kwargs))
            return result
        return record

    def run_cli(self, *args: str) -> tuple[bool, str, str]:
        parsed = cli_commands.parse_cli_args(list(args), "test")
        out, err = io.StringIO(), io.StringIO()
        self.patch("is_dry_run", lambda: self.dry_run)
        self.patch("is_daemon_running", lambda: self.daemon_running)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            handled = cli_commands.handle_cli_command(parsed, lambda: setattr(self, "smoke_ran", True))
        return handled, out.getvalue(), err.getvalue()

    def labels(self) -> list[str]:
        return [call[0] for call in self.calls]


class NoCommandTests(DispatchTestCase):
    def test_plain_run_options_are_not_one_shot_commands(self) -> None:
        for args in ((), ("--poll",), ("--once",), ("--single",), ("--drain-queue",)):
            handled, out, err = self.run_cli(*args)
            self.assertFalse(handled, args)
            self.assertEqual((out, err), ("", ""))

    def test_smoke_test(self) -> None:
        handled, _, _ = self.run_cli("--smoke-test")
        self.assertTrue(handled)
        self.assertTrue(self.smoke_ran)


class AutostartAndShortcutTests(DispatchTestCase):
    def test_install_autostart_passes_mode_and_trigger_and_prints_the_message(self) -> None:
        with mock.patch("modules.autostart.install_autostart", self.recorder(
                "install", SimpleNamespace(ok=True, message="Installed."))):
            handled, out, err = self.run_cli("--install-autostart", "--autostart-mode", "task", "--task-trigger", "manual")
        self.assertTrue(handled)
        self.assertEqual(self.calls[0][2], {"mode": "task", "trigger": "manual"})
        self.assertEqual((out, err), ("Installed.\n", ""))

    def test_a_failed_install_goes_to_stderr(self) -> None:
        with mock.patch("modules.autostart.install_autostart", self.recorder(
                "install", SimpleNamespace(ok=False, message="No luck."))):
            _, out, err = self.run_cli("--install-autostart")
        self.assertEqual((out, err), ("", "No luck.\n"))
        self.assertEqual(self.calls[0][2], {"mode": "auto", "trigger": "logon"})

    def test_uninstall_autostart(self) -> None:
        with mock.patch("modules.autostart.uninstall_autostart", self.recorder(
                "uninstall", SimpleNamespace(ok=True, message="Removed."))):
            _, out, _ = self.run_cli("--uninstall-autostart")
        self.assertEqual(out, "Removed.\n")

    def test_autostart_status(self) -> None:
        with mock.patch("modules.autostart.autostart_status", lambda: SimpleNamespace(detail="not installed")):
            _, out, _ = self.run_cli("--autostart-status")
        self.assertEqual(out, "Autostart: not installed\n")

    def test_create_shortcuts_with_options(self) -> None:
        with mock.patch("modules.shortcuts.create_shortcuts", self.recorder(
                "create", SimpleNamespace(ok=True, message="Created."))):
            _, out, _ = self.run_cli("--create-shortcuts", "--shortcut-dir", "X", "--shortcut-minimized")
        args, kwargs = self.calls[0][1], self.calls[0][2]
        self.assertEqual(args, ("X",))
        self.assertIn("--minimized", kwargs["options"])
        self.assertNotIn("--start-daemon", kwargs["options"])
        self.assertEqual(out, "Created.\n")

    def test_remove_shortcuts(self) -> None:
        with mock.patch("modules.shortcuts.remove_shortcuts", self.recorder(
                "remove", SimpleNamespace(ok=False, message="Nothing to remove."))):
            _, out, err = self.run_cli("--remove-shortcuts")
        self.assertEqual((out, err), ("", "Nothing to remove.\n"))


class DetachedDaemonTests(DispatchTestCase):
    def test_it_does_not_start_a_second_daemon(self) -> None:
        with mock.patch("modules.daemon_launcher.start_daemon", self.recorder("start", 1)):
            _, out, _ = self.run_cli("--daemon-detached")
        self.assertIn("already running", out)
        self.assertEqual(self.calls, [])

    def test_it_starts_one_when_none_runs(self) -> None:
        self.daemon_running = False
        with mock.patch("modules.daemon_launcher.start_daemon", self.recorder("start", 4242)):
            _, out, _ = self.run_cli("--daemon-detached")
        self.assertIn("PID 4242", out)

    def test_a_launch_failure_is_reported(self) -> None:
        self.daemon_running = False

        def broken():
            raise OSError("no python")

        with mock.patch("modules.daemon_launcher.start_daemon", broken):
            handled, _, err = self.run_cli("--daemon-detached")
        self.assertTrue(handled)
        self.assertIn("Could not start the daemon: no python", err)


class ReportCommandTests(DispatchTestCase):
    def test_perf_report_as_text_and_as_json(self) -> None:
        with mock.patch("modules.perf_report.collect", lambda: {"a": 1}), \
                mock.patch("modules.perf_report.format_report", lambda report: "TEXT REPORT"):
            _, text, _ = self.run_cli("--perf-report")
            _, as_json, _ = self.run_cli("--perf-report", "--json")
        self.assertEqual(text, "TEXT REPORT\n")
        self.assertEqual(json.loads(as_json), {"a": 1})

    def test_doctor_text_lists_errors_warnings_and_checks(self) -> None:
        self.patch("run_doctor", lambda: {
            "ok": False, "platform": "TestOS", "errors": ["E1"], "warnings": ["W1"], "checks": ["C1"]})
        _, out, _ = self.run_cli("--doctor")
        for expected in ("blocking issues", "Platform: TestOS", "- E1", "- W1", "- C1"):
            self.assertIn(expected, out)

    def test_doctor_passed_and_json(self) -> None:
        report = {"ok": True, "platform": "TestOS", "errors": [], "warnings": [], "checks": []}
        self.patch("run_doctor", lambda: report)
        _, out, _ = self.run_cli("--doctor")
        self.assertIn("Doctor checks passed.", out)
        self.assertNotIn("Errors:", out)
        _, as_json, _ = self.run_cli("--doctor", "--json")
        self.assertEqual(json.loads(as_json), report)

    def test_mapping_validate(self) -> None:
        self.patch("validate_mapping_schema", lambda: {"ok": False, "errors": ["bad"], "warnings": ["hmm"]})
        _, out, _ = self.run_cli("--mapping-validate")
        self.assertIn("Mapping validation failed.", out)
        self.assertIn("- bad", out)
        self.assertIn("- hmm", out)
        _, as_json, _ = self.run_cli("--mapping-validate", "--json")
        self.assertFalse(json.loads(as_json)["ok"])

    def test_mapping_validate_passed(self) -> None:
        self.patch("validate_mapping_schema", lambda: {"ok": True, "errors": [], "warnings": []})
        _, out, _ = self.run_cli("--mapping-validate")
        self.assertEqual(out, "Mapping validation passed.\n")


class DaemonControlTests(DispatchTestCase):
    def setUp(self) -> None:
        super().setUp()
        for name in ("set_paused", "set_log_override", "request_poll_now", "request_single_poll",
                     "request_check_folders", "request_stop"):
            self.patch(name, self.recorder(name))

    def test_each_option_writes_its_request(self) -> None:
        expected = {
            "--pause": ("set_paused", (True,)),
            "--resume": ("set_paused", (False,)),
            "--log-on": ("set_log_override", (True,)),
            "--log-off": ("set_log_override", (False,)),
            "--poll-now": ("request_poll_now", ()),
            "--poll-one": ("request_single_poll", ()),
            "--check-folders": ("request_check_folders", ()),
            "--stop": ("request_stop", ()),
        }
        for option, (label, args) in expected.items():
            self.calls.clear()
            handled, out, err = self.run_cli(option)
            self.assertTrue(handled, option)
            self.assertEqual([(c[0], c[1]) for c in self.calls], [(label, args)], option)
            self.assertTrue(out.strip(), option)
            self.assertEqual(err, "")

    def test_nothing_is_written_when_no_daemon_runs(self) -> None:
        self.daemon_running = False
        handled, _, err = self.run_cli("--pause")
        self.assertTrue(handled)
        self.assertIn("No GHAADD daemon is running", err)
        self.assertEqual(self.calls, [])

    def test_contradicting_options_are_refused(self) -> None:
        _, _, err = self.run_cli("--pause", "--resume")
        self.assertIn("--pause and --resume cannot be combined", err)
        _, _, err = self.run_cli("--log-on", "--log-off")
        self.assertIn("--log-on and --log-off cannot be combined", err)
        self.assertEqual(self.calls, [])

    def test_a_locked_database_is_reported_instead_of_crashing(self) -> None:
        def locked(*args):
            raise sqlite3.OperationalError("database is locked")

        self.patch("request_poll_now", locked)
        handled, _, err = self.run_cli("--poll-now")
        self.assertTrue(handled)
        self.assertIn("database is locked", err)


class JobCommandTests(DispatchTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.patch("open_database", lambda: contextlib.nullcontext(object()))

    def test_run_pending_reports_processed_skipped_and_missing(self) -> None:
        self.patch("process_selected_pending_jobs", lambda conn, token, ids: (len(ids) - 2, [7], [9]))
        _, out, _ = self.run_cli("--run-pending", "9", "7", "7", "3")
        self.assertIn("Processed 1 job(s)", out)
        self.assertIn("Skipped (not pending):\n- #7", out)
        self.assertIn("Skipped (not found):\n- #9", out)

    def test_run_pending_rejects_ids_below_one(self) -> None:
        self.patch("process_selected_pending_jobs", self.recorder("run"))
        _, _, err = self.run_cli("--run-pending", "0", "3")
        self.assertIn("invalid job ID", err)
        self.assertEqual(self.calls, [])

    def test_removing_pending_jobs_reports_removed_skipped_and_unknown_ones(self) -> None:
        removed = [{"id": 1, "repo": "o/a", "tag": "v1", "release_type": None, "expected_commit": None}]
        everything = removed + [
            {"id": 2, "status": "COMPLETED", "repo": "o/b", "tag": "v2", "release_type": "Pre-release"}]
        self.patch("supersede_pending_jobs_by_ids", lambda conn, ids: removed)
        self.patch("get_jobs_by_ids", lambda conn, ids: everything)
        _, out, _ = self.run_cli("--queue-remove-pending-ids", "1", "2", "5")
        self.assertIn("Removed 1 pending job(s)", out)
        self.assertIn("- #1 o/a v1 (Release, expected_commit=unknown)", out)
        self.assertIn("- #2 status=COMPLETED o/b v2 (Pre-release)", out)
        self.assertIn("Skipped (not found): 5", out)

    def test_removing_pending_jobs_rejects_ids_below_one(self) -> None:
        _, _, err = self.run_cli("--queue-remove-pending-ids", "-4")
        self.assertIn("invalid job ID", err)


class QueueStatusTests(DispatchTestCase):
    def test_options_are_passed_on(self) -> None:
        self.patch("print_queue_status", self.recorder("status"))
        handled, _, _ = self.run_cli("--queue-status", "--queue-limit", "5", "--queue-repo-filter", "x", "--json")
        self.assertTrue(handled)
        kwargs = self.calls[0][2]
        self.assertTrue(kwargs["as_json"])
        self.assertEqual(kwargs["limit"], 5)
        self.assertEqual(kwargs["repo_filter"], "x")

    def test_a_bad_option_value_is_reported_and_nothing_is_printed(self) -> None:
        self.patch("print_queue_status", self.recorder("status"))
        _, _, err = self.run_cli("--queue-status", "--queue-limit", "-1")
        self.assertIn("Queue status option error", err)
        self.assertEqual(self.calls, [])


class PurgeTests(DispatchTestCase):
    def test_purge_state_deletes(self) -> None:
        self.patch("purge_state_database", lambda: True)
        _, out, _ = self.run_cli("--purge-state")
        self.assertEqual(out, "Deleted local state database: state.db\n")

    def test_purge_state_with_nothing_to_delete(self) -> None:
        self.patch("purge_state_database", lambda: False)
        _, out, _ = self.run_cli("--purge-state")
        self.assertIn("No local state database found", out)

    def test_purge_state_dry_run_deletes_nothing(self) -> None:
        self.dry_run = True
        self.patch("purge_state_database", self.recorder("purge"))
        for exists, wanted in ((True, "Would delete local state database"), (False, "No local state database")):
            self.patch("get_state_db_path", lambda: __file__ if exists else "/definitely/not/here.db")
            _, out, _ = self.run_cli("--purge-state")
            self.assertIn(wanted, out)
        self.assertEqual(self.calls, [])

    def test_purge_events_needs_an_age(self) -> None:
        self.patch("purge_lifecycle_events", self.recorder("purge", 0))
        _, _, err = self.run_cli("--purge")
        self.assertIn("--purge-age is required", err)
        self.assertEqual(self.calls, [])

    def test_purge_events_rejects_a_negative_age(self) -> None:
        self.patch("purge_lifecycle_events", self.recorder("purge", 0))
        _, _, err = self.run_cli("--purge", "--purge-age", "-1")
        self.assertIn("must be >= 0", err)

    def test_purge_events_rejects_purge_oldest(self) -> None:
        self.patch("purge_lifecycle_events", self.recorder("purge", 0))
        _, _, err = self.run_cli("--purge", "--purge-age", "1", "--purge-oldest", "3")
        self.assertIn("only supported with --purge-jobs", err)
        self.assertEqual(self.calls, [])

    def test_purge_events_reports_the_count_and_the_filter(self) -> None:
        self.patch("purge_lifecycle_events", self.recorder("purge", 12))
        _, out, _ = self.run_cli("--purge", "--purge-age", "30", "--purge-type", "WARNING", "--purge-repository", "x")
        self.assertIn("Purged 12 lifecycle event(s)", out)
        self.assertIn("type=WARNING", out)
        self.assertIn("age>=30d", out)
        self.assertEqual(self.calls[0][2]["min_age_days"], 30)
        self.assertFalse(self.calls[0][2]["dry_run"])

    def test_purge_events_dry_run_wording(self) -> None:
        self.dry_run = True
        self.patch("purge_lifecycle_events", self.recorder("purge", 3))
        _, out, _ = self.run_cli("--purge", "--purge-age", "0")
        self.assertIn("[DRY-RUN] Would purge 3", out)
        self.assertTrue(self.calls[0][2]["dry_run"])

    def test_purge_jobs_option_errors(self) -> None:
        self.patch("open_database", lambda: contextlib.nullcontext(object()))
        self.patch("purge_job_queue_rows", self.recorder("purge", 0))
        cases = (
            (("--purge-jobs",), "--purge-age or --purge-oldest is required"),
            (("--purge-jobs", "--purge-age", "1", "--purge-oldest", "2"), "only one of"),
            (("--purge-jobs", "--purge-age", "-1"), "--purge-age must be >= 0"),
            (("--purge-jobs", "--purge-oldest", "0"), "positive integer"),
        )
        for args, message in cases:
            _, _, err = self.run_cli(*args)
            self.assertIn(message, err, args)
        self.assertEqual(self.calls, [])

    def test_purge_jobs_by_age_and_by_count(self) -> None:
        self.patch("open_database", lambda: contextlib.nullcontext(object()))
        self.patch("purge_job_queue_rows", self.recorder("purge", 5))
        _, out, _ = self.run_cli("--purge-jobs", "--purge-age", "10", "--purge-status", "FAILED")
        self.assertIn("Purged 5 job_queue row(s)", out)
        self.assertIn("status=FAILED", out)
        self.assertEqual(self.calls[0][2]["min_age_days"], 10)
        self.assertIsNone(self.calls[0][2]["oldest_count"])
        _, out, _ = self.run_cli("--purge-jobs", "--purge-oldest", "4")
        self.assertIn("oldest 4", out)
        self.assertIn("PENDING never touched", out)
        self.assertEqual(self.calls[1][2]["oldest_count"], 4)


class MoveAndLifecycleLogTests(DispatchTestCase):
    def test_move_complete_to_destination(self) -> None:
        self.patch("move_complete_folders_to_mapped_destinations", lambda: {"moved": 2})
        _, out, _ = self.run_cli("--move-complete-to-destination")
        self.assertEqual(out, "")
        _, out, _ = self.run_cli("--move-complete-to-destination", "--json")
        self.assertEqual(json.loads(out), {"moved": 2})

    def events(self):
        return [
            {"created_at_readable": "2026-10-08 10:00", "category": "COMPLETE", "event_type": "X", "message": "done"},
            {"created_at_readable": "2026-10-08 11:00", "category": None, "event_type": "WARNING", "message": "hmm"},
        ]

    def test_the_default_is_twenty_events_printed_with_category_or_type(self) -> None:
        self.patch("list_lifecycle_events", self.recorder("list", self.events()))
        _, out, _ = self.run_cli("--lifecycle-log")
        self.assertEqual(self.calls[0][2]["limit"], 20)
        self.assertIn("[2026-10-08 10:00] COMPLETE: done", out)
        self.assertIn("[2026-10-08 11:00] WARNING: hmm", out)

    def test_limit_zero_means_all_and_filters_are_passed_on(self) -> None:
        self.patch("list_lifecycle_events", self.recorder("list", []))
        _, out, _ = self.run_cli("--lifecycle-log", "--lifecycle-limit", "0", "--lifecycle-type", "WARNING",
                                 "--lifecycle-repo-filter", "x")
        self.assertEqual(self.calls[0][2], {"limit": None, "event_type": "WARNING", "repo_filter": "x"})
        self.assertEqual(out, "No lifecycle events found.\n")

    def test_a_negative_limit_is_refused(self) -> None:
        self.patch("list_lifecycle_events", self.recorder("list", []))
        _, _, err = self.run_cli("--lifecycle-log", "--lifecycle-limit", "-3")
        self.assertIn("invalid --lifecycle-limit", err)
        self.assertEqual(self.calls, [])

    def test_json_output(self) -> None:
        self.patch("list_lifecycle_events", self.recorder("list", self.events()))
        _, out, _ = self.run_cli("--lifecycle-log", "--json")
        self.assertEqual(len(json.loads(out)), 2)


if __name__ == "__main__":
    unittest.main()
