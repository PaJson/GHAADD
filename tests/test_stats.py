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
from datetime import date, datetime, timedelta
from typing import Any
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
        arguments: dict[str, Any] = dict(entries=ENTRIES, jobs=jobs_fixture(), cycles=[NOW - 5 * HOUR, NOW - 2 * DAY, NOW - 100 * DAY],
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

    def test_top_none_keeps_every_row_and_totals_count_them(self) -> None:
        report = self.build(top=None)
        self.assertEqual(len(report["busiest_repositories"]), 3)
        self.assertEqual(len(report["biggest_repositories"]), 4)
        self.assertEqual(len(report["biggest_releases"]), 5)
        self.assertEqual(report["totals"], {"busiest_repositories": 3, "biggest_repositories": 4, "biggest_releases": 5})
        self.assertEqual(self.build(top=1)["totals"], report["totals"])  # the totals do not depend on the cut

    def test_limited_cuts_a_full_report_without_touching_it(self) -> None:
        full = self.build(top=None)
        cut = stats.limited(full, busiest=1, biggest=2)
        self.assertEqual(len(cut["busiest_repositories"]), 1)
        self.assertEqual(len(cut["biggest_repositories"]), 2)
        self.assertEqual(len(cut["biggest_releases"]), 2)
        self.assertEqual(len(full["biggest_releases"]), 5)
        everything = stats.limited(full, busiest=None, biggest=None)
        self.assertEqual(everything["biggest_releases"], full["biggest_releases"])

    def test_titles_say_all_only_when_more_than_the_default_top_is_listed_in_full(self) -> None:
        small = self.build(top=None)
        self.assertEqual(stats.list_titles(small)[2], "Top 5 biggest single releases")
        many = {name: [{}] * 30 for name in ("busiest_repositories", "biggest_repositories", "biggest_releases")}
        report = {**many, "totals": {name: 30 for name in many}}
        self.assertTrue(stats.list_titles(report)[0].startswith("All 30 busiest"))
        cut = stats.limited(report, busiest=10, biggest=25)
        cut["totals"] = report["totals"]
        titles = stats.list_titles(cut)
        self.assertTrue(titles[0].startswith("Top 10 busiest"))
        self.assertTrue(titles[1].startswith("Top 25 biggest repositories"))

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


class DateRangeTests(unittest.TestCase):
    def test_parse_date_reads_iso_dates_and_empty_means_none(self) -> None:
        self.assertEqual(stats.parse_date(" 2026-09-30 "), date(2026, 9, 30))
        self.assertIsNone(stats.parse_date("  "))

    def test_parse_date_explains_a_bad_date(self) -> None:
        for text in ("30/09/2026", "2026-13-01", "yesterday"):
            with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
                stats.parse_date(text)

    def test_the_last_day_is_included(self) -> None:
        window = stats.date_range(date(2026, 10, 1), date(2026, 10, 5))
        assert window is not None and window.start is not None and window.end is not None
        self.assertEqual(datetime.fromtimestamp(window.start), datetime(2026, 10, 1))
        self.assertEqual(datetime.fromtimestamp(window.end), datetime(2026, 10, 6))
        self.assertEqual(window.label, "2026-10-01 to 2026-10-05")

    def test_open_ends_and_no_window(self) -> None:
        self.assertIsNone(stats.date_range(None, None))
        only_first = stats.date_range(date(2026, 10, 1), None)
        assert only_first is not None
        self.assertIsNone(only_first.end)
        self.assertEqual(only_first.label, "2026-10-01 to now")
        only_last = stats.date_range(None, date(2026, 10, 1))
        assert only_last is not None
        self.assertIsNone(only_last.start)
        self.assertEqual(only_last.label, "the beginning to 2026-10-01")

    def test_a_reversed_window_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "after"):
            stats.date_range(date(2026, 10, 5), date(2026, 10, 1))

    def test_presets_count_today_as_the_first_day(self) -> None:
        self.assertIsNone(stats.preset_range(None, date(2026, 10, 8)))
        week = stats.preset_range(7, date(2026, 10, 8))
        assert week is not None
        self.assertEqual(week.label, "2026-10-02 to 2026-10-08")
        today = stats.preset_range(1, date(2026, 10, 8))
        assert today is not None
        self.assertEqual(today.label, "2026-10-08 to 2026-10-08")


class BuildRangeTests(unittest.TestCase):
    def build(self, window, **overrides):
        arguments: dict[str, Any] = dict(entries=ENTRIES, jobs=jobs_fixture(), cycles=[], sizes=SIZES, folder_counts={},
                                         now=NOW, top=None, date_range=window)
        arguments.update(overrides)
        return stats.build(**arguments)

    def test_without_a_window_nothing_changes(self) -> None:
        report = self.build(None)
        self.assertIsNone(report["range"])
        self.assertEqual(len(report["daily"]), stats.DAILY_DAYS)
        self.assertEqual([row["key"] for row in report["periods"]], [key for key, _label, _days in stats.PERIODS])

    def test_busiest_counts_only_jobs_in_the_window(self) -> None:
        report = self.build(stats.date_range(date(2026, 10, 1), date(2026, 10, 5)))
        self.assertEqual([(row["repo"], row["jobs"]) for row in report["busiest_repositories"]],
                         [("o/quiet", 1), ("o/busy", 1)])
        self.assertEqual(report["totals"]["busiest_repositories"], 2)

    def test_biggest_uses_the_day_of_a_releases_first_job_and_drops_purged_ones(self) -> None:
        report = self.build(stats.date_range(date(2026, 10, 1), date(2026, 10, 5)))
        self.assertEqual([(row["repo"], row["tag"]) for row in report["biggest_releases"]], [("o/quiet", "v1")])
        self.assertEqual([row["repo"] for row in report["biggest_repositories"]], ["o/quiet"])  # o/gone has no job at all

    def test_a_reused_tag_counts_for_the_day_of_its_newest_file(self) -> None:
        # "latest" is reused by many jobs; asset_state holds only the newest build, so today's window must find it.
        jobs = [("o/roll", "latest", "COMPLETED", NOW - 30 * DAY), ("o/roll", "latest", "COMPLETED", NOW - 2 * HOUR)]
        sizes = {"o/roll|latest": {"bytes": 2 * GB, "files": 3, "newest": int(NOW - 2 * HOUR)}}
        today = stats.date_range(date(2026, 10, 8), date(2026, 10, 8))
        report = self.build(today, jobs=jobs, sizes=sizes)
        self.assertEqual([(row["repo"], row["bytes"]) for row in report["busiest_repositories"]], [("o/roll", 2 * GB)])
        self.assertEqual(report["periods"][-1]["bytes"], 2 * GB)
        older = self.build(stats.date_range(date(2026, 9, 1), date(2026, 9, 15)), jobs=jobs, sizes=sizes)
        self.assertEqual(older["periods"][-1]["bytes"], 0)  # no longer dated by the first job of the tag

    def test_measured_jobs_add_up_per_day_even_when_the_tag_is_reused(self) -> None:
        # Three jobs on one reused tag, two today and one a month ago; asset_state only knows the newest build.
        jobs = [("o/roll", "latest", "COMPLETED", NOW - 30 * DAY), ("o/roll", "latest", "COMPLETED", NOW - 5 * HOUR),
                ("o/roll", "latest", "COMPLETED", NOW - 2 * HOUR)]
        sizes = {"o/roll|latest": {"bytes": 1 * GB, "files": 3, "newest": int(NOW - 2 * HOUR)}}
        measured = [("o/roll", "latest", NOW - 30 * DAY, 1 * GB, 3), ("o/roll", "latest", NOW - 5 * HOUR, 2 * GB, 3),
                    ("o/roll", "latest", NOW - 2 * HOUR, 1 * GB, 3)]
        today = stats.date_range(date(2026, 10, 8), date(2026, 10, 8))
        report = self.build(today, jobs=jobs, sizes=sizes, job_sizes=measured)
        self.assertEqual(report["periods"][-1]["bytes"], 3 * GB)  # both of today's folders, not the single asset_state build
        self.assertEqual([(row["repo"], row["bytes"]) for row in report["busiest_repositories"]], [("o/roll", 3 * GB)])
        self.assertEqual([(row["tag"], row["bytes"]) for row in report["biggest_releases"]],
                         [("latest", 2 * GB), ("latest", 1 * GB)])  # each build is a row, with the plain tag
        lifetime = next(row for row in report["periods"] if row["key"] == "lifetime")
        self.assertEqual(lifetime["bytes"], 4 * GB)  # asset_state is not added on top of the measured jobs

    def test_a_release_without_measured_jobs_keeps_the_asset_state_estimate(self) -> None:
        jobs = [("o/a", "v1", "COMPLETED", NOW - 3 * HOUR), ("o/b", "v1", "COMPLETED", NOW - 2 * HOUR)]
        sizes = {"o/a|v1": {"bytes": 5, "files": 1}, "o/b|v1": {"bytes": 7, "files": 1}}
        report = self.build(None, jobs=jobs, sizes=sizes, job_sizes=[("o/b", "v1", NOW - 2 * HOUR, 70, 2)])
        lifetime = next(row for row in report["periods"] if row["key"] == "lifetime")
        self.assertEqual((lifetime["bytes"], lifetime["files"]), (75, 3))

    def test_the_activity_table_gets_a_row_for_the_window(self) -> None:
        report = self.build(stats.date_range(date(2026, 10, 1), date(2026, 10, 5)))
        row = report["periods"][-1]
        self.assertEqual((row["key"], row["label"]), ("range", "2026-10-01 to 2026-10-05"))
        self.assertEqual((row["jobs"], row["completed"], row["superseded"], row["bytes"], row["files"]), (2, 1, 1, 100, 1))
        self.assertEqual(report["periods"][0]["key"], "day")  # the fixed rows stay

    def test_the_daily_series_follows_the_window(self) -> None:
        report = self.build(stats.date_range(date(2026, 10, 1), date(2026, 10, 5)))
        self.assertEqual([day["date"] for day in report["daily"]],
                         ["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05"])
        self.assertEqual([day["jobs"] for day in report["daily"]], [0, 1, 0, 0, 1])
        self.assertEqual(stats.daily_title(report), "Jobs per day, 2026-10-01 to 2026-10-05, 5 days")

    def test_an_open_start_begins_at_the_first_job_and_the_end_stops_at_today(self) -> None:
        report = self.build(stats.date_range(None, date(2027, 1, 1)))
        self.assertEqual(report["daily"][0]["date"], datetime.fromtimestamp(NOW - 400 * DAY).date().isoformat())
        self.assertEqual(report["daily"][-1]["date"], "2026-10-08")
        self.assertEqual(sum(day["jobs"] for day in report["daily"]), 7)

    def test_a_window_without_jobs_is_empty_not_an_error(self) -> None:
        report = self.build(stats.date_range(date(2020, 1, 1), date(2020, 1, 3)))
        self.assertEqual(report["busiest_repositories"], [])
        self.assertEqual(report["biggest_releases"], [])
        self.assertEqual(report["periods"][-1]["jobs"], 0)

    def test_a_window_in_the_future_has_no_days(self) -> None:
        report = self.build(stats.date_range(date(2027, 1, 1), date(2027, 1, 3)))
        self.assertEqual(report["daily"], [])

    def test_mapping_and_reliability_ignore_the_window(self) -> None:
        everything, windowed = self.build(None), self.build(stats.date_range(date(2026, 10, 1), date(2026, 10, 5)))
        self.assertEqual(everything["reliability"], windowed["reliability"])
        self.assertEqual(everything["mapping"], windowed["mapping"])

    def test_the_text_report_names_the_window(self) -> None:
        report = self.build(stats.date_range(date(2026, 10, 1), date(2026, 10, 5)))
        report["storage"] = {}
        self.assertIn("Jobs per day, 2026-10-01 to 2026-10-05, 5 days", stats.format_report(report))


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

    def test_release_sizes_carry_the_newest_file_time(self) -> None:
        with closing(db_manager.open_database()) as connection:
            for item, mtime in (("a", 100.0), ("b", 300.0)):
                connection.execute(
                    "INSERT INTO asset_state (release_key, item_key, file_path, size, local_size, local_mtime) "
                    "VALUES ('o/r|t', ?, 'p', 10, 10, ?)", (item, mtime))
            connection.commit()
            self.assertEqual(db_manager.get_release_sizes(connection), {"o/r|t": {"bytes": 20, "files": 2, "newest": 300}})

    def test_purging_jobs_keeps_their_measured_sizes(self) -> None:
        with closing(db_manager.open_database()) as connection:
            first = db_manager.enqueue_job(connection, "o/app", "v1")
            second = db_manager.enqueue_job(connection, "o/app", "v2")
            for job_id, size, files in ((first, 100, 2), (second, 50, 1)):
                db_manager.add_job_folder_size(connection, job_id, size, files)
                db_manager.mark_job_completed(connection, job_id)  # PENDING jobs are never purged
            before = db_manager.get_job_folder_sizes(connection)
            self.assertEqual(db_manager.purge_job_queue_rows(connection, oldest_count=1), 1)
            self.assertEqual(db_manager.purge_job_queue_rows(connection, status="COMPLETED"), 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM job_queue").fetchone()[0], 0)
            self.assertEqual(db_manager.get_job_folder_sizes(connection), before)  # nothing double counted, nothing lost
        report = stats.collect()
        self.assertEqual(report["periods"][-1]["bytes"], 150)

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

    def test_stats_top_all_and_a_number(self) -> None:
        self.assertEqual(cli_commands.parse_cli_args(["--stats", "--stats-top", "all"], "test").stats_top, 0)
        self.assertEqual(cli_commands.parse_cli_args(["--stats", "--stats-top", "25"], "test").stats_top, 25)
        self.assertIsNone(cli_commands.parse_cli_args(["--stats"], "test").stats_top)
        for bad in ("0", "-3", "lots"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli_commands.parse_cli_args(["--stats", "--stats-top", bad], "test")

    def test_stats_top_changes_how_many_rows_are_listed(self) -> None:
        with mock.patch.object(stats, "collect", wraps=stats.collect) as collect:
            self.run_cli("--stats")
            self.run_cli("--stats", "--stats-top", "3")
            self.run_cli("--stats", "--stats-top", "all")
        self.assertEqual([call.kwargs["top"] for call in collect.call_args_list], [stats.TOP_COUNT, 3, None])

    def test_stats_dates_make_a_window_and_are_checked(self) -> None:
        with mock.patch.object(stats, "collect", wraps=stats.collect) as collect:
            report = json.loads(self.run_cli("--stats", "--json", "--stats-from", "2026-10-01", "--stats-to", "2026-10-05"))
        self.assertEqual(report["range"], "2026-10-01 to 2026-10-05")
        self.assertEqual(collect.call_args.kwargs["date_range"].label, "2026-10-01 to 2026-10-05")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli_commands.parse_cli_args(["--stats", "--stats-from", "01.10.2026"], "test")

    def test_stats_window_with_the_first_day_after_the_last_is_refused(self) -> None:
        parsed = cli_commands.parse_cli_args(["--stats", "--stats-from", "2026-10-05", "--stats-to", "2026-10-01"], "test")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            cli_commands.handle_cli_command(parsed, lambda: None)
        self.assertIn("after the last date", out.getvalue())

    def test_stats_options_need_stats(self) -> None:
        for option in (["--stats-top", "5"], ["--stats-from", "2026-10-01"], ["--stats-to", "2026-10-01"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli_commands.parse_cli_args(option, "test")

    def test_json_is_accepted_with_stats_and_rejected_without_a_command(self) -> None:
        cli_commands.parse_cli_args(["--stats", "--json"], "test")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli_commands.parse_cli_args(["--json"], "test")


if __name__ == "__main__":
    unittest.main()


class BucketDailyTests(unittest.TestCase):
    def series(self, count):
        start = date(2025, 1, 1)
        return [{"date": str(start + timedelta(days=i)), "jobs": 1} for i in range(count)]

    def test_short_series_stays_daily(self):
        unit, buckets = stats.bucket_daily(self.series(30), 100)
        self.assertEqual((unit, len(buckets)), ("day", 30))

    def test_long_series_becomes_weeks_that_keep_the_total(self):
        unit, buckets = stats.bucket_daily(self.series(400), 100)
        self.assertEqual(unit, "week")
        self.assertEqual(sum(b["jobs"] for b in buckets), 400)
        self.assertEqual(buckets[0]["days"], 5)  # 2025-01-01 is a Wednesday: the first week is cut off

    def test_very_long_series_becomes_months(self):
        unit, buckets = stats.bucket_daily(self.series(1500), 100)
        self.assertEqual(unit, "month")
        self.assertEqual(sum(b["days"] for b in buckets), 1500)
