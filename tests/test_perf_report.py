"""Tests for the read-only performance report (--perf-report).

Run from the project root: python -m unittest discover -s tests -t .
"""
import json
import os
import tempfile
import unittest
from contextlib import closing
from unittest import mock

from modules import cli_commands, config_manager, db_manager, mapping_manager, perf_report


class PerfReportTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._temp_dir.cleanup)
        folder = self._temp_dir.name
        self.db_path = os.path.join(folder, "state.db")
        self.config_path = os.path.join(folder, "config.json")
        self.mapping_path = os.path.join(folder, "mapping.json")
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump({"paths": {"default_download_dir": folder}}, handle)
        with open(self.mapping_path, "w", encoding="utf-8") as handle:
            json.dump({"repositories": [{"repository": "o/app", "destination": "K:\\Apps", "folder": "App"}]}, handle)
        for target, name, value in (
            (db_manager, "get_state_db_path", lambda: self.db_path),
            (config_manager, "_config_file_path", lambda: self.config_path),
            (mapping_manager, "_mapping_file_path", lambda: self.mapping_path),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        with closing(db_manager.open_database()) as connection:
            db_manager.enqueue_job(connection, "o/app", "v1")
            db_manager.insert_lifecycle_event(connection, "WARNING", "w", category="API")
            db_manager.insert_lifecycle_event(connection, "COMPLETED_MOVE", "c", repo="o/app", tag="v1")


class StorageStatsTests(PerfReportTestCase):
    def test_counts_tables_statuses_and_indexes(self) -> None:
        with closing(db_manager.open_database()) as connection:
            stats = db_manager.get_storage_stats(connection)

        self.assertEqual(stats["tables"]["job_queue"], 1)
        self.assertEqual(stats["tables"]["lifecycle_events"], 2)
        self.assertEqual(stats["jobs_by_status"], {"PENDING": 1})
        self.assertEqual(stats["events_by_type"], {"COMPLETED_MOVE": 1, "WARNING": 1})
        self.assertEqual(stats["jobs_last_7_days"], 1)
        self.assertGreater(stats["page_count"], 0)
        self.assertIn("lifecycle_events.idx_lifecycle_events_event_type", stats["indexes"])


class CollectTests(PerfReportTestCase):
    def collect(self) -> dict:
        return perf_report.collect(repeat=2)

    def test_report_has_every_section_and_serializes_to_json(self) -> None:
        report = self.collect()

        for key in ("version", "sizes", "storage", "growth", "timings", "notes", "daemon_running"):
            self.assertIn(key, report)
        names = {timing["name"] for timing in report["timings"]}
        for expected in ("load config.json", "queue summary per repo", "Mappings table rows (changed database)",
                         "status tabs (unchanged database)", "status tabs (database changed, nothing new)",
                         "GUI daemon snapshot (every 1 s tick)"):
            self.assertIn(expected, names)
        self.assertTrue(all("median_ms" in t for t in report["timings"]), [t for t in report["timings"] if "error" in t])
        json.dumps(report)  # --json must work

    def test_text_report_is_readable(self) -> None:
        text = perf_report.format_report(self.collect())

        for heading in ("Data", "Growth", "Timings", "Notes"):
            self.assertIn(heading, text)
        self.assertIn("job_queue", text)
        self.assertIn("ms", text)

    def test_one_failing_probe_does_not_hide_the_others(self) -> None:
        with mock.patch.object(mapping_manager, "load_mapping", side_effect=RuntimeError("boom")):
            report = self.collect()

        failed = [t for t in report["timings"] if "error" in t]
        self.assertTrue(any("boom" in t["error"] for t in failed))
        self.assertTrue(any("median_ms" in t for t in report["timings"]))  # the rest still ran
        self.assertTrue(any("could not be measured" in note for note in report["notes"]))

    def test_notes_point_out_the_missing_repo_index(self) -> None:
        self.assertTrue(any("no index on repo" in note for note in self.collect()["notes"]))

    def test_slow_steps_are_flagged(self) -> None:
        report = {
            "timings": [{"group": "g", "name": "slow thing", "median_ms": 120.0}, {"group": "g", "name": "fast", "median_ms": 1.0}],
            "storage": {"tables": {"job_queue": 5}, "indexes": ["job_queue.idx_repo"], "freelist_pages": 0},
        }
        notes = perf_report._notes(report)
        self.assertTrue(any("slow thing" in note and "120" in note for note in notes))
        self.assertFalse(any("'fast'" in note for note in notes))
        self.assertFalse(any("no index on repo" in note for note in notes))  # an index exists in this fake

    def test_the_report_changes_nothing_on_disk(self) -> None:
        before = {path: (os.path.getsize(path), os.path.getmtime(path)) for path in (self.config_path, self.mapping_path)}
        self.collect()
        after = {path: (os.path.getsize(path), os.path.getmtime(path)) for path in before}
        self.assertEqual(before, after)
        with open(self.config_path, encoding="utf-8") as handle:
            self.assertNotIn("gui", json.load(handle))  # no read marks were saved by the measured status tabs


class CommandLineTests(unittest.TestCase):
    def test_flag_is_parsed_and_dispatched(self) -> None:
        args = cli_commands.parse_cli_args(["--perf-report"], "test")
        self.assertTrue(args.perf_report)
        with mock.patch.object(perf_report, "collect", return_value={"x": 1}) as collect, \
                mock.patch.object(perf_report, "format_report", return_value="REPORT") as formatted, \
                mock.patch("builtins.print") as printed:
            self.assertTrue(cli_commands.handle_cli_command(args, lambda: None))
        collect.assert_called_once()
        formatted.assert_called_once_with({"x": 1})
        printed.assert_called_once_with("REPORT")

    def test_json_flag_prints_json(self) -> None:
        args = cli_commands.parse_cli_args(["--perf-report", "--json"], "test")
        with mock.patch.object(perf_report, "collect", return_value={"x": 1}), mock.patch("builtins.print") as printed:
            cli_commands.handle_cli_command(args, lambda: None)
        self.assertEqual(json.loads(printed.call_args.args[0]), {"x": 1})


if __name__ == "__main__":
    unittest.main()
