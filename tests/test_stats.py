"""The statistics behind `--stats` and the GUI's Stats window (modules/stats.py).

The figures are built from plain data (`stats.build`), so the arithmetic is checked with a small hand-made history;
`collect` and the CLI command are checked once against a temporary state.db and mapping.json.
Run from the project root: python -m unittest discover -s tests -t .
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from unittest import mock

from modules import cli_commands, config_manager, db_manager, mapping_manager, stats

NOW = datetime(2026, 10, 8, 12, 0, 0).timestamp()
HOUR, DAY = 3600, 86400
GB = 1024 ** 3


def jobs_fixture():
    """(repo, tag, status, created_at): oldest first, like db_manager.get_job_history."""
    return [
        ("o/old", "v1", "COMPLETED", NOW - 400 * DAY),
        ("o/busy", "v1", "COMPLETED", NOW - 40 * DAY),
        ("o/busy", "v2", "SUPERSEDED", NOW - 6 * DAY),
        ("o/busy", "v3", "COMPLETED", NOW - 2 * HOUR),
        ("o/busy", "v4", "FAILED", NOW - 1 * HOUR),
        ("O/Busy", "v5", "PENDING", NOW - 30 * 60),   # same repository, different spelling
        ("o/quiet", "v1", "COMPLETED", NOW - 3 * DAY),
    ]


SIZES = {
    "o/old|v1": {"bytes": 5 * GB, "files": 3},
    "o/busy|v1": {"bytes": 1 * GB, "files": 2},
    "o/busy|v3": {"bytes": 2 * GB, "files": 2},
    "o/quiet|v1": {"bytes": 100, "files": 1},
    "o/gone|v9": {"bytes": 7 * GB, "files": 4},   # its jobs were purged: counts for lifetime and rankings only
}

ENTRIES = [
    {"repository": "o/busy", "destination": "X:\\a", "limit": 2, "skiplist": ["Pre-release"], "recheck_intervals": [5]},
    {"repository": "o/quiet", "destination": "X:\\b", "shared_destination": True},
    {"repository": "o/old", "destination": "", "active": False},
    {"repository": "o/never", "destination": "X:\\c"},
]


class FormattingTests(unittest.TestCase):
    def test_bytes_use_binary_units(self) -> None:
        self.assertEqual(stats.format_bytes(0), "0 B")
        self.assertEqual(stats.format_bytes(1023), "1,023 B")
        self.assertEqual(stats.format_bytes(1536), "1.5 KB")
        self.assertEqual(stats.format_bytes(5 * GB), "5.0 GB")
        self.assertEqual(stats.format_bytes(2048 * GB), "2.0 TB")
        self.assertEqual(stats.format_bytes(None), "n/a")

    def test_sparkline_scales_to_the_largest_value(self) -> None:
        line = stats.sparkline([0, 5, 10])
        self.assertEqual(len(line), 3)
        self.assertEqual(line[0], stats.SPARK_BLOCKS[0])
        self.assertEqual(line[-1], stats.SPARK_BLOCKS[-1])

    def test_sparkline_of_nothing_is_flat(self) -> None:
        self.assertEqual(stats.sparkline([0, 0, 0]), stats.SPARK_BLOCKS[0] * 3)
        self.assertEqual(stats.sparkline([]), "")


class MappingStatsTests(unittest.TestCase):
    def test_counts(self) -> None:
        counts = {"o/busy": {"folder_count": 3}, "o/quiet": {"folder_count": 1}}
        result = stats.mapping_stats(ENTRIES, counts)
        self.assertEqual(result, {
            "total": 4, "active": 3, "inactive": 1, "without_destination": 1, "with_skiplist": 1,
            "with_own_recheck": 1, "shared_destination": 1, "over_limit": 1,
        })

    def test_an_empty_mapping(self) -> None:
        result = stats.mapping_stats([], {})
        self.assertEqual((result["total"], result["active"], result["over_limit"]), (0, 0, 0))

    def test_a_folder_count_at_the_limit_is_not_over_it(self) -> None:
        result = stats.mapping_stats([{"repository": "O/Busy", "limit": 3}], {"o/busy": {"folder_count": 3}})
        self.assertEqual(result["over_limit"], 0)


class BuildTests(unittest.TestCase):
    def build(self, **overrides):
        arguments = dict(entries=ENTRIES, jobs=jobs_fixture(), cycles=[NOW - 5 * HOUR, NOW - 2 * DAY, NOW - 100 * DAY],
                         sizes=SIZES, folder_counts={}, now=NOW)
        arguments.update(overrides)
        return stats.build(**arguments)

    def period(self, report, key):
        return next(row for row in report["periods"] if row["key"] == key)

    def test_jobs_per_period(self) -> None:
        report = self.build()
        self.assertEqual([self.period(report, key)["jobs"] for key in ("day", "week", "month", "year", "lifetime")],
                         [3, 5, 5, 6, 7])

    def test_statuses_inside_a_period(self) -> None:
        week = self.period(self.build(), "week")
        self.assertEqual((week["completed"], week["failed"], week["superseded"], week["pending"]), (2, 1, 1, 1))

    def test_polling_cycles_per_period(self) -> None:
        report = self.build()
        self.assertEqual([self.period(report, key)["cycles"] for key in ("day", "week", "year", "lifetime")], [1, 2, 3, 3])

    def test_data_is_attributed_to_the_period_of_the_releases_first_job(self) -> None:
        report = self.build()
        self.assertEqual(self.period(report, "day")["bytes"], 2 * GB)            # o/busy v3
        self.assertEqual(self.period(report, "month")["bytes"], 2 * GB + 100)    # + o/quiet v1
        self.assertEqual(self.period(report, "year")["bytes"], 3 * GB + 100)     # + o/busy v1
        self.assertEqual(self.period(report, "lifetime")["bytes"], 15 * GB + 100)  # also the release without jobs
        self.assertEqual(self.period(report, "lifetime")["files"], 12)

    def test_jobs_per_day_average_uses_the_shorter_of_period_and_history(self) -> None:
        report = self.build(jobs=jobs_fixture()[1:])  # history now starts 40 days ago
        self.assertEqual(self.period(report, "year")["jobs_per_day"], round(6 / 40, 1))
        self.assertEqual(self.period(report, "week")["jobs_per_day"], round(5 / 7, 1))

    def test_reliability(self) -> None:
        reliability = self.build()["reliability"]
        self.assertEqual((reliability["completed"], reliability["failed"], reliability["superseded"],
                          reliability["pending"]), (4, 1, 1, 1))
        self.assertEqual(reliability["success_percent"], 80.0)

    def test_no_finished_jobs_gives_no_percentage(self) -> None:
        jobs = [("o/a", "v1", "PENDING", NOW - 10)]
        self.assertIsNone(self.build(jobs=jobs)["reliability"]["success_percent"])

    def test_the_daily_series_covers_thirty_days_oldest_first_with_zeros(self) -> None:
        daily = self.build()["daily"]
        self.assertEqual(len(daily), stats.DAILY_DAYS)
        self.assertEqual(daily[-1]["date"], "2026-10-08")
        self.assertEqual(daily[-1]["jobs"], 3)  # v3, v4 and the pending v5 (today = 2026-10-08)
        self.assertEqual(sum(day["jobs"] for day in daily), 5)
        self.assertEqual(daily[0]["jobs"], 0)

    def test_busiest_repositories_group_spellings_and_rank_by_jobs(self) -> None:
        rows = self.build()["busiest_repositories"]
        self.assertEqual([row["repo"] for row in rows], ["o/busy", "o/quiet", "o/old"])  # tie on one job: newest first
        busy = rows[0]
        self.assertEqual((busy["jobs"], busy["jobs_30d"], busy["completed"], busy["failed"]), (5, 4, 2, 1))
        self.assertEqual(busy["bytes"], 3 * GB)
        self.assertEqual(busy["last_job"], "2026-10-08 11:30")

    def test_the_top_list_is_limited(self) -> None:
        self.assertEqual(len(self.build(top=2)["busiest_repositories"]), 2)
        self.assertEqual(len(self.build(top=1)["biggest_releases"]), 1)

    def test_biggest_repositories_rank_by_total_size(self) -> None:
        rows = self.build()["biggest_repositories"]
        self.assertEqual([row["repo"] for row in rows], ["o/gone", "o/old", "o/busy", "o/quiet"])
        busy = rows[2]
        self.assertEqual((busy["bytes"], busy["releases"], busy["files"], busy["largest"]), (3 * GB, 2, 4, 2 * GB))
        self.assertEqual(busy["average_release_bytes"], 3 * GB // 2)

    def test_biggest_releases(self) -> None:
        rows = self.build()["biggest_releases"]
        self.assertEqual([(row["repo"], row["tag"]) for row in rows[:2]], [("o/gone", "v9"), ("o/old", "v1")])
        self.assertEqual(rows[0]["bytes"], 7 * GB)

    def test_tracked_totals(self) -> None:
        self.assertEqual(self.build()["tracked"], {"bytes": 15 * GB + 100, "files": 12, "releases": 5})

    def test_history(self) -> None:
        history = self.build()["history"]
        self.assertEqual(history["jobs"], 7)
        self.assertEqual(history["first_job"], datetime.fromtimestamp(NOW - 400 * DAY).strftime("%Y-%m-%d %H:%M"))
        self.assertEqual(history["days"], 401)
        self.assertEqual(history["busiest_day"], {"date": "2026-10-08", "jobs": 3})

    def test_mapping_figures_that_need_the_history(self) -> None:
        mapping = self.build()["mapping"]
        # active entries: busy, quiet, never. busy and quiet had jobs in the last 30 days, never did not.
        self.assertEqual(mapping["quiet_30_days"], 1)
        self.assertEqual(mapping["seen_in_jobs_not_mapped"], 0)
        self.assertEqual(self.build(entries=ENTRIES[:1])["mapping"]["seen_in_jobs_not_mapped"], 2)

    def test_an_empty_installation_gives_a_complete_report(self) -> None:
        report = self.build(entries=[], jobs=[], cycles=[], sizes={})
        self.assertEqual(report["history"]["first_job"], None)
        self.assertEqual(report["history"]["busiest_day"], None)
        self.assertEqual([row["jobs"] for row in report["periods"]], [0] * 5)
        self.assertEqual(report["busiest_repositories"], [])
        text = stats.format_report(report)
        self.assertIn("No jobs recorded yet.", text)
        self.assertIn("0 mapped", text)


class TextReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.report = stats.build(ENTRIES, jobs_fixture(), [NOW - HOUR], SIZES, {}, NOW)
        self.text = stats.format_report(self.report)

    def test_every_section_is_there(self) -> None:
        for heading in ("Repositories", "History", "Activity", "Jobs per day, last 30 days", "Top 3 busiest repositories",
                        "Top 4 biggest repositories", "Top 5 biggest single releases"):
            self.assertIn(heading, self.text)

    def test_the_figures_are_readable(self) -> None:
        self.assertIn("4 mapped: 3 active, 1 inactive, 1 without a destination", self.text)
        self.assertIn("Last 24 hours", self.text)
        self.assertIn("o/gone v9", self.text)
        self.assertIn("7.0 GB", self.text)

    def test_the_rows_of_every_table_match_the_report(self) -> None:
        self.assertEqual(len(stats.period_rows(self.report)), 5)
        self.assertEqual(len(stats.busiest_rows(self.report)[0]), len(stats.BUSIEST_HEADERS))
        self.assertEqual(len(stats.biggest_rows(self.report)[0]), len(stats.BIGGEST_HEADERS))
        self.assertEqual(len(stats.release_rows(self.report)[0]), len(stats.RELEASE_HEADERS))
        self.assertEqual(len(stats.period_rows(self.report)[0]), len(stats.PERIOD_HEADERS))

    def test_the_report_can_be_turned_into_json(self) -> None:
        self.assertEqual(json.loads(json.dumps(self.report))["version"], self.report["version"])


class CollectTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._temp_dir.cleanup)
        folder = self._temp_dir.name
        self.db_path = os.path.join(folder, "state.db")
        mapping_path = os.path.join(folder, "mapping.json")
        config_path = os.path.join(folder, "config.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump({"paths": {"default_download_dir": folder}}, handle)
        with open(mapping_path, "w", encoding="utf-8") as handle:
            json.dump({"repositories": [
                {"repository": "o/app", "destination": "K:\\Apps"},
                {"repository": "o/other", "destination": "K:\\Other", "active": False},
            ]}, handle)
        for target, name, value in (
            (db_manager, "get_state_db_path", lambda: self.db_path),
            (config_manager, "_config_file_path", lambda: config_path),
            (mapping_manager, "_mapping_file_path", lambda: mapping_path),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class CollectTests(CollectTestCase):
    def test_an_empty_database_gives_a_report_without_errors(self) -> None:
        report = stats.collect()
        self.assertEqual(report["mapping"]["total"], 2)
        self.assertEqual(report["mapping"]["active"], 1)
        self.assertEqual(report["history"]["jobs"], 0)
        self.assertEqual(report["storage"]["rows"]["job_queue"], 0)
        self.assertIsNotNone(report["storage"]["state_db_bytes"])
        self.assertIn("No jobs recorded yet.", stats.format_report(report))

    def test_it_reads_the_real_tables(self) -> None:
        with closing(db_manager.open_database()) as connection:
            db_manager.enqueue_job(connection, "o/app", "v1")
            db_manager.insert_lifecycle_event(connection, "CYCLE_SUMMARY", "cycle")
            db_manager.save_state_entry(connection, "o/app|v1", "asset:1", "a.zip", "x/a.zip", None, 2048, None, None, self._temp_dir.name)
        report = stats.collect()
        self.assertEqual(report["history"]["jobs"], 1)
        self.assertEqual(report["periods"][-1]["cycles"], 1)
        self.assertEqual(report["tracked"]["files"], 1)
        self.assertEqual(report["busiest_repositories"][0]["repo"], "o/app")
        self.assertEqual(report["biggest_repositories"][0]["repo"], "o/app")
        self.assertEqual(report["storage"]["rows"]["asset_state"], 1)

    def test_it_never_writes_to_the_mapping_file(self) -> None:
        path = mapping_manager._mapping_file_path()
        before = os.path.getmtime(path)
        stats.collect()
        self.assertEqual(os.path.getmtime(path), before)


class CliTests(CollectTestCase):
    def run_cli(self, *args: str) -> str:
        parsed = cli_commands.parse_cli_args(list(args), "test")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            handled = cli_commands.handle_cli_command(parsed, lambda: None)
        self.assertTrue(handled)
        return out.getvalue()

    def test_stats_prints_the_text_report(self) -> None:
        text = self.run_cli("--stats")
        self.assertIn("statistics", text)
        self.assertIn("2 mapped: 1 active, 1 inactive", text)

    def test_stats_json_is_pure_json(self) -> None:
        report = json.loads(self.run_cli("--stats", "--json"))
        self.assertEqual(report["mapping"]["total"], 2)

    def test_json_is_accepted_with_stats_and_rejected_without_a_command(self) -> None:
        cli_commands.parse_cli_args(["--stats", "--json"], "test")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli_commands.parse_cli_args(["--json"], "test")


if __name__ == "__main__":
    unittest.main()
