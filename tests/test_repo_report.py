"""modules.repo_report: the `--repo-overview` table and JSON built from the GUI's Mappings rows.

Run from the project root: python -m unittest discover -s tests -t .
"""
import unittest
from typing import Any

from modules import gui_data, repo_overview, repo_report


def make_table() -> gui_data.RepoTable:
    """Two repositories: one finalized with a stored count, one that has never finished a release."""
    entries: list[dict[str, Any]] = [
        {"repository": "owner/app", "folder": "App", "destination": "X:\\Apps", "limit": 10,
         "last_finalized": "2026-10-01_10-00", "last_finalized_file_count": 17},
        {"repository": "owner/tool", "limit": 5},
    ]
    rows = repo_overview.build_rows(entries, {}, 0.0, lambda entry: 5)
    return gui_data.RepoTable(rows=rows, entries={e["repository"]: e for e in entries})


class BuildOverviewTests(unittest.TestCase):
    def test_a_repository_shows_its_stored_file_count_and_stamp(self) -> None:
        by_name = {row["repository"]: row for row in repo_report.build_overview(make_table())}
        self.assertEqual(by_name["owner/app"]["last_finalized_file_count"], 17)
        self.assertEqual(by_name["owner/app"]["last_finalized"], "2026-10-01_10-00")

    def test_an_unknown_count_is_none_in_the_data_and_a_dash_in_the_text(self) -> None:
        report = {"repositories": repo_report.build_overview(make_table()), "db_error": None}
        tool = next(row for row in report["repositories"] if row["repository"] == "owner/tool")
        self.assertIsNone(tool["last_finalized_file_count"])
        self.assertEqual(tool["last_finalized"], "-")
        line = next(line for line in repo_report.format_report(report).splitlines() if line.startswith("owner/tool"))
        self.assertTrue(line.endswith("-"), line)


class FormatReportTests(unittest.TestCase):
    def report(self) -> dict[str, Any]:
        return {"repositories": repo_report.build_overview(make_table()), "db_error": None}

    def test_the_table_has_a_heading_a_rule_and_a_row_per_repository(self) -> None:
        lines = repo_report.format_report(self.report()).splitlines()
        self.assertTrue(lines[0].startswith("Repository"))
        self.assertIn("Finalized files", lines[0])
        self.assertEqual(set(lines[1].replace(" ", "")), {"-"})
        self.assertEqual(lines[-1], "2 repositories.")

    def test_columns_line_up(self) -> None:
        lines = repo_report.format_report(self.report()).splitlines()
        status_column = lines[0].index("Status")
        self.assertEqual(lines[1][status_column - 2:status_column], "  ")  # the rule is cut at the same place
        self.assertEqual(lines[1][status_column], "-")
        for row in lines[2:4]:
            self.assertEqual(row[status_column - 2:status_column], "  ")  # the repository column ends before it

    def test_the_filter_keeps_matching_repositories_only(self) -> None:
        text = repo_report.format_report(self.report(), "TOOL")
        self.assertIn("owner/tool", text)
        self.assertNotIn("owner/app", text)
        self.assertTrue(text.endswith("1 repository."))

    def test_nothing_to_show_is_said_plainly(self) -> None:
        self.assertEqual(repo_report.format_report({"repositories": [], "db_error": None}), "No repositories to show.")
        self.assertEqual(repo_report.format_report(self.report(), "zzz"), "No repositories to show.")

    def test_a_database_problem_is_reported_above_the_table(self) -> None:
        report = self.report()
        report["db_error"] = "state.db unavailable: locked"
        self.assertTrue(repo_report.format_report(report).startswith("Warning: state.db unavailable: locked"))


if __name__ == "__main__":
    unittest.main()
