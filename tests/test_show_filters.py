"""The Mappings tab's Show drop-down: Has skiplist, Custom recheck and Shared folder (plus the older choices)."""
import unittest
from typing import Any

from modules import repo_overview


def rows_for(*entries: dict[str, Any]) -> dict[str, repo_overview.RepoRow]:
    """Build the table rows for these mapping entries (no jobs), keyed by repository."""
    return {row.repo: row for row in repo_overview.build_rows(list(entries), {}, 0.0, lambda entry: 5)}


def shown(rows: dict[str, repo_overview.RepoRow], choice: str) -> list[str]:
    """The repositories a Show choice lets through, sorted."""
    return sorted(repo for repo, row in rows.items() if repo_overview.show_matches(row, choice))


class ShowFilterTests(unittest.TestCase):
    def test_the_new_choices_are_in_the_drop_down(self) -> None:
        for choice in ("Has skiplist", "Custom recheck", "Shared folder"):
            self.assertIn(choice, repo_overview.SHOW_FILTERS)
        self.assertEqual(repo_overview.SHOW_FILTERS[0], "All")  # the editor resets to the first choice

    def test_has_skiplist_needs_at_least_one_real_entry(self) -> None:
        rows = rows_for(
            {"repository": "o/none"},
            {"repository": "o/empty", "skiplist": []},
            {"repository": "o/blank", "skiplist": ["", "  "]},
            {"repository": "o/pre", "skiplist": ["Pre-release"]},
        )
        self.assertEqual(shown(rows, "Has skiplist"), ["o/pre"])

    def test_custom_recheck_means_an_own_usable_minute_list(self) -> None:
        rows = rows_for(
            {"repository": "o/global"},
            {"repository": "o/empty", "recheck_intervals": []},
            {"repository": "o/junk", "recheck_intervals": [0, -5, "x", True]},
            {"repository": "o/own", "recheck_intervals": [5, 60]},
            {"repository": "o/text", "recheck_intervals": ["15"]},
        )
        self.assertEqual(shown(rows, "Custom recheck"), ["o/own", "o/text"])

    def test_shared_folder_is_only_a_real_true(self) -> None:
        rows = rows_for(
            {"repository": "o/a", "shared_destination": True},
            {"repository": "o/b", "shared_destination": False},
            {"repository": "o/c", "shared_destination": "true"},
            {"repository": "o/d"},
        )
        self.assertEqual(shown(rows, "Shared folder"), ["o/a"])

    def test_the_older_choices_still_work(self) -> None:
        rows = rows_for({"repository": "o/on"}, {"repository": "o/off", "active": False})
        self.assertEqual(shown(rows, "All"), ["o/off", "o/on"])
        self.assertEqual(shown(rows, "Active"), ["o/on"])
        self.assertEqual(shown(rows, "Inactive"), ["o/off"])
        self.assertEqual(shown(rows, "Has pending"), [])

    def test_an_unknown_choice_shows_everything(self) -> None:
        rows = rows_for({"repository": "o/a"})
        self.assertEqual(shown(rows, "Something else"), ["o/a"])


if __name__ == "__main__":
    unittest.main()
