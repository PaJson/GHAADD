"""--queue-status / --queue-report: option checking, the numbers in the report, the text and CSV output.

Runs against a throw-away database; the real state.db is never opened. Run from the project root:
python -m unittest discover -s tests -t .
"""
import contextlib
import csv
import io
import json
import os
import tempfile
import time
import unittest
from typing import Any
from unittest import mock

from modules import db_manager, mapping_manager, queue_reports


class OptionTests(unittest.TestCase):
    build = staticmethod(queue_reports.build_queue_status_options)

    def test_defaults(self) -> None:
        options = self.build()
        self.assertEqual((options["limit"], options["hours"], options["date"], options["repo_filter"], options["status"]), (10, None, None, None, None))
        self.assertEqual((options["as_json"], options["report"], options["report_only"], options["report_csv_path"]), (False, False, False, None))

    def test_all_and_a_zero_limit_remove_the_limit(self) -> None:
        self.assertIsNone(self.build(queue_all=True)["limit"])
        self.assertIsNone(self.build(queue_limit=0)["limit"])
        self.assertEqual(self.build(queue_limit=25)["limit"], 25)

    def test_report_only_implies_report(self) -> None:
        options = self.build(queue_report_only=True)
        self.assertTrue(options["report"] and options["report_only"])

    def test_filters_are_normalised(self) -> None:
        options = self.build(queue_repo_filter="  Owner ", queue_status_filter=" failed ", queue_hours=1.5, )
        self.assertEqual((options["repo_filter"], options["status"], options["hours"]), ("Owner", "FAILED", 1.5))
        self.assertEqual(self.build(queue_date="2026-10-06")["date"], "2026-10-06")

    def test_bad_values_are_rejected_with_a_clear_message(self) -> None:
        cases: tuple[tuple[dict[str, Any], str], ...] = (
            ({"queue_limit": -1}, "--queue-limit"),
            ({"queue_hours": 0}, "--queue-hours"),
            ({"queue_hours": -2}, "--queue-hours"),
            ({"queue_date": "06-10-2026"}, "--queue-date"),
            ({"queue_date": "2026-13-40"}, "--queue-date"),
            ({"queue_hours": 2, "queue_date": "2026-10-06"}, "only one of"),
            ({"queue_repo_filter": "   "}, "--queue-repo-filter"),
        )
        for kwargs, fragment in cases:
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError) as raised:
                    self.build(**kwargs)
                self.assertIn(fragment, str(raised.exception))

    def test_a_csv_option_turns_the_report_on_and_names_the_file(self) -> None:
        explicit = self.build(queue_report_csv="out.csv")
        self.assertEqual((explicit["report"], explicit["report_csv_path"]), (True, "out.csv"))
        default = self.build(queue_report_csv="  ", queue_status_filter="failed", queue_date="2026-10-06")
        self.assertRegex(str(default["report_csv_path"]), r"^queue-report-date-2026-10-06-failed-\d{8}-\d{6}\.csv$")
        hours = self.build(queue_report_csv="", queue_hours=1.5)
        self.assertRegex(str(hours["report_csv_path"]), r"^queue-report-last-1p5h-\d{8}-\d{6}\.csv$")
        self.assertRegex(str(self.build(queue_report_csv="")["report_csv_path"]), r"^queue-report-all-\d{8}-\d{6}\.csv$")


class ScopeTests(unittest.TestCase):
    scope = staticmethod(queue_reports._build_queue_scope)

    def test_no_filters_means_no_where_clause(self) -> None:
        self.assertEqual(self.scope(), ("", []))

    def test_hours_look_back_from_now(self) -> None:
        clause, params = self.scope(hours=2, now_timestamp=10_000.0)
        self.assertEqual((clause, params), ("WHERE created_at >= ?", [10_000.0 - 7200.0]))

    def test_a_date_covers_exactly_that_day(self) -> None:
        clause, params = self.scope(date_value="2026-10-06")
        self.assertEqual(clause, "WHERE created_at >= ? AND created_at < ?")
        self.assertTrue(23 * 3600 <= float(params[1]) - float(params[0]) <= 25 * 3600, params)  # one day (23-25 h around a clock change)

    def test_repo_status_and_table_alias(self) -> None:
        clause, params = self.scope(repo_filter="Owner", status_filter="FAILED", table_alias="j")
        self.assertEqual(clause, "WHERE LOWER(j.repo) LIKE ? AND j.status = ?")
        self.assertEqual(params, ["%owner%", "FAILED"])


class ReportTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._temp_dir.cleanup)
        self.root = self._temp_dir.name
        db_path = os.path.join(self.root, "state.db")
        self.mapping_path = os.path.join(self.root, "mapping.json")  # absent = every repository on the default setting
        for target, name, value in (
            (db_manager, "get_state_db_path", lambda: db_path),
            (mapping_manager, "_mapping_file_path", lambda: self.mapping_path),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def seed(self) -> None:
        """6 jobs: 1 success, 1 skip, 1 failed, 1 superseded (finalized), 2 pending (one due, one in the future)."""
        with contextlib.closing(db_manager.open_database()) as c:
            done = db_manager.enqueue_job(c, "o/app", "v1", "Release", 0, "aaa")
            db_manager.mark_job_completed(c, done, 3, 3, 1, 4)
            failed = db_manager.enqueue_job(c, "o/app", "v2", "Release", 0, "bbb")
            db_manager.mark_job_failed(c, failed, 6, "bbb", 0, 0, 0, "FAILED")
            skipped = db_manager.enqueue_job(c, "x/other", "v1", "Pre-release", 0, "ccc")
            db_manager.mark_job_completed(c, skipped, 3, 0, 2, 2, "SKIP")
            db_manager.save_job_skip_details(c, skipped, 3, [{"item_key": "k", "file_name": "a.zip", "reason": "already downloaded"}])
            superseded = db_manager.enqueue_job(c, "o/app", "v3", "Release", 0, "ddd")
            db_manager.mark_job_superseded(c, superseded, 0, "ddd", 0, 0, 0, "SUPERSEDED_COMMIT_CHANGED_FINALIZED")
            db_manager.enqueue_job(c, "o/app", "v4", "Release", 0, "eee")
            db_manager.enqueue_job(c, "o/app", "v5", "Release", time.time() + 99999, "fff")

    def status(self, **kwargs) -> dict:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            queue_reports.print_queue_status(as_json=True, **kwargs)
        return json.loads(buffer.getvalue())

    def text(self, **kwargs) -> str:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            queue_reports.print_queue_status(**kwargs)
        return buffer.getvalue()


class StatusTests(ReportTestCase):
    def test_counts_and_the_next_pending_job(self) -> None:
        self.seed()
        data = self.status()
        self.assertEqual(data["total_jobs"], 6)
        self.assertEqual(data["status_counts"], {"COMPLETED": 2, "FAILED": 1, "PENDING": 2, "SUPERSEDED": 1})
        self.assertEqual(data["pending_due_now"], 1)  # the other pending job is scheduled for the future
        self.assertEqual((data["next_pending_job"]["repo"], data["next_pending_job"]["tag"]), ("o/app", "v4"))
        self.assertIsNone(data["report"])  # only on request

    def test_the_job_list_is_newest_first_and_respects_the_limit(self) -> None:
        self.seed()
        self.assertEqual([j["tag"] for j in self.status(limit=3)["recent_jobs"]], ["v5", "v4", "v3"])
        self.assertEqual(len(self.status(limit=None)["recent_jobs"]), 6)
        self.assertEqual(self.status(limit=None)["filters"]["limit"], "all")

    def test_filters_narrow_the_counts(self) -> None:
        self.seed()
        self.assertEqual(self.status(repo_filter="OTHER")["total_jobs"], 1)
        failed = self.status(status_filter="FAILED")
        self.assertEqual((failed["total_jobs"], [j["tag"] for j in failed["recent_jobs"]]), (1, ["v2"]))
        self.assertEqual(self.status(hours=1)["total_jobs"], 6)
        self.assertEqual(self.status(date_value="2001-01-01")["total_jobs"], 0)

    def test_skipped_files_show_up_with_their_reason(self) -> None:
        self.seed()
        job = next(j for j in self.status(limit=None)["recent_jobs"] if j["repo"] == "x/other")
        self.assertEqual(job["skip_detail_count"], 1)
        self.assertEqual(job["skipped_items_preview"][0]["reason"], "already downloaded")

    def seed_three_releases(self) -> tuple[int, int, int]:
        """nightly (4 files), nightly again (6 files), then v9 (8 files)."""
        with contextlib.closing(db_manager.open_database()) as c:
            first = db_manager.enqueue_job(c, "o/app", "nightly", "Release", 0)
            db_manager.mark_job_completed(c, first, 3, 3, 1, 4)
            second = db_manager.enqueue_job(c, "o/app", "nightly", "Release", 0)
            db_manager.mark_job_completed(c, second, 3, 5, 1, 6)
            third = db_manager.enqueue_job(c, "o/app", "v9", "Release", 0)
            db_manager.mark_job_completed(c, third, 3, 7, 1, 8)
        return first, second, third

    def jobs_by_id(self) -> dict:
        return {j["id"]: j for j in self.status()["recent_jobs"]}

    def use_sanity_mode(self, mode) -> None:
        entry = {"repository": "o/app"}
        if mode is not None:
            entry["sanity_check"] = mode
        with open(self.mapping_path, "w", encoding="utf-8") as handle:
            json.dump({"repositories": [entry]}, handle)

    def test_by_default_a_job_is_compared_with_the_previous_release_whatever_its_tag(self) -> None:
        first, second, third = self.seed_three_releases()
        jobs = self.jobs_by_id()
        self.assertIsNone(jobs[first]["previous_success_tag"])  # nothing before the first one
        self.assertEqual((jobs[second]["previous_success_tag"], jobs[second]["file_count_delta_vs_previous_success"]), ("nightly", 2))
        self.assertEqual((jobs[third]["previous_success_tag"], jobs[third]["previous_success_total_items"]), ("nightly", 6))
        self.assertEqual(jobs[third]["file_count_delta_vs_previous_success"], 2)

    def test_same_tag_mode_only_compares_runs_of_the_same_tag(self) -> None:
        first, second, third = self.seed_three_releases()
        self.use_sanity_mode("same_tag")
        jobs = self.jobs_by_id()
        self.assertEqual((jobs[second]["previous_success_tag"], jobs[second]["previous_success_total_items"]), ("nightly", 4))
        self.assertIsNone(jobs[third]["previous_success_tag"])  # v9 has no earlier run of its own tag

    def test_off_mode_shows_no_comparison(self) -> None:
        self.seed_three_releases()
        self.use_sanity_mode("off")
        self.assertTrue(all(j["previous_success_tag"] is None for j in self.jobs_by_id().values()))

    def test_other_repositories_keep_their_own_setting(self) -> None:
        with contextlib.closing(db_manager.open_database()) as c:
            for repo, files in (("o/app", 4), ("o/app", 6), ("x/other", 4), ("x/other", 6)):
                job = db_manager.enqueue_job(c, repo, f"t{files}-{repo}", "Release", 0)
                db_manager.mark_job_completed(c, job, 3, files, 0, files)
        with open(self.mapping_path, "w", encoding="utf-8") as handle:
            json.dump({"repositories": [{"repository": "O/App", "sanity_check": "off"}, {"repository": "x/other"}]}, handle)
        by_repo = {}
        for job in self.status()["recent_jobs"]:
            by_repo.setdefault(job["repo"], []).append(job["previous_success_tag"])
        self.assertEqual(by_repo["o/app"], [None, None])  # switched off (repository names match whatever the capitalisation)
        self.assertEqual(sorted(by_repo["x/other"], key=str), [None, "t4-x/other"])

    def test_a_job_is_never_compared_with_a_release_that_finished_after_it(self) -> None:
        first, _second, _third = self.seed_three_releases()
        self.assertIsNone(self.jobs_by_id()[first]["previous_success_tag"])


class ReportTests(ReportTestCase):
    def test_the_numbers(self) -> None:
        self.seed()
        report = self.status(report=True)["report"]
        self.assertEqual(report["window_total_jobs"], 6)
        self.assertEqual((report["terminal_jobs"], report["success_jobs"], report["skip_jobs"], report["failed_jobs"]), (3, 1, 1, 1))
        self.assertEqual((report["success_rate_percent"], report["hard_failure_rate_percent"]), (66.67, 33.33))  # a skip counts as success
        self.assertEqual(report["supersede_finalized_jobs"], 1)
        self.assertEqual([r["repo"] for r in report["top_failed_repos"]], ["o/app"])
        self.assertEqual({r["repo"]: r["terminal_success_rate_percent"] for r in report["top_successful_repos"]}, {"o/app": 50.0, "x/other": 100.0})
        self.assertEqual(report["skip_reasons"], [{"reason": "already downloaded", "count": 1}])
        self.assertEqual(report["top_skipped_items"], [{"item_label": "a.zip", "skip_count": 1}])
        self.assertEqual((report["oldest_purgeable_job"]["tag"], report["newest_purgeable_job"]["tag"]), ("v1", "v3"))
        self.assertEqual([e["would_purge_count"] for e in report["purge_age_preview"]], [0, 0, 0, 0, 0])  # everything is new

    def test_an_empty_queue_has_no_rates(self) -> None:
        report = self.status(report=True)["report"]
        self.assertEqual((report["terminal_jobs"], report["success_rate_percent"], report["hard_failure_rate_percent"]), (0, None, None))
        self.assertIsNone(report["oldest_purgeable_job"])

    def test_old_jobs_show_up_in_the_purge_preview(self) -> None:
        self.seed()
        with contextlib.closing(db_manager.open_database()) as c:
            c.execute("UPDATE job_queue SET completed_at = ?, updated_at = ?, created_at = ? WHERE tag = 'v1' AND repo = 'o/app'",
                      (time.time() - 40 * 86400,) * 3)
            c.commit()
        preview = {e["age_days"]: e["would_purge_count"] for e in self.status(report=True)["report"]["purge_age_preview"]}
        self.assertEqual((preview[7], preview[30], preview[90]), (1, 1, 0))


class TextOutputTests(ReportTestCase):
    def test_an_empty_queue_says_so(self) -> None:
        text = self.text()
        self.assertIn("Total jobs: 0", text)
        self.assertIn("No queue rows found.", text)

    def test_the_summary_lines(self) -> None:
        self.seed()
        text = self.text(limit=2)
        for fragment in ("Queue status", "Total jobs: 6", "- COMPLETED: 2", "- PENDING: 2", "- PENDING due now: 1",
                         "Next pending job: #5 o/app v4", "Jobs shown (newest first, limit=2):"):
            self.assertIn(fragment, text)
        self.assertNotIn("Queue report", text)

    def test_a_measured_job_shows_what_it_brought_in_beside_its_last_check(self) -> None:
        self.seed()
        with contextlib.closing(db_manager.open_database()) as c:
            job = c.execute("SELECT id FROM job_queue WHERE repo = 'o/app' AND tag = 'v1'").fetchone()["id"]
            db_manager.add_job_folder_size(c, job, 400 * 1024 * 1024, 13)
        data = self.status(limit=None)["recent_jobs"]
        self.assertEqual([(j["folder_bytes"], j["folder_files"]) for j in data if j["tag"] == "v1" and j["repo"] == "o/app"],
                         [(400 * 1024 * 1024, 13)])
        self.assertIsNone(next(j for j in data if j["tag"] == "v2")["folder_bytes"])
        lines = [line for line in self.text(limit=None).splitlines() if line.startswith("- #")]
        measured = next(line for line in lines if "o/app v1 " in line)
        self.assertIn("last check: downloaded=", measured)
        self.assertIn("brought_in=13 files / 400.0 MB, ", measured)
        self.assertNotIn("brought_in", next(line for line in lines if "o/app v2 " in line))

    def test_active_filters_are_listed(self) -> None:
        self.seed()
        self.assertIn("Filters: repo~app, status=FAILED", self.text(repo_filter="app", status_filter="FAILED"))

    def test_the_report_section(self) -> None:
        self.seed()
        text = self.text(report=True, report_only=True)
        for fragment in ("Queue report", "Terminal outcomes: success=1, skip=1, failed=1", "Success rate: 66.67%",
                         "Top failed repos:", "- o/app: 1", "- --purge-age 7: 0 job(s)", "- already downloaded: 1"):
            self.assertIn(fragment, text)
        self.assertNotIn("Jobs shown", text)  # report-only hides the job list

    def test_the_csv_file_has_the_report_sections(self) -> None:
        self.seed()
        path = os.path.join(self.root, "report.csv")
        text = self.text(report=True, report_csv_path=path)
        self.assertIn(f"Report CSV written: {path}", text)
        with open(path, newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        summary = {r["metric"]: r["value"] for r in rows if r["section"] == "summary"}
        self.assertEqual((summary["terminal_jobs"], summary["success_rate_percent"]), ("3", "66.67"))
        self.assertEqual({r["metric"]: r["value"] for r in rows if r["section"] == "status_breakdown"}["PENDING"], "2")
        self.assertEqual([r["repo"] for r in rows if r["section"] == "top_failed_repos"], ["o/app"])
        self.assertEqual([r["metric"] for r in rows if r["section"] == "skip_reasons"], ["already downloaded"])

    def test_an_unwritable_csv_path_is_reported_not_raised(self) -> None:
        self.seed()
        bad = os.path.join(self.root, "missing-folder", "report.csv")
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            self.text(report=True, report_csv_path=bad)
        self.assertIn("Failed to write report CSV", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
