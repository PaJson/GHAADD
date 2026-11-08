"""The data behind the Mappings editor's "Fetch again" button: the failed tag of a row and the link built from it.

Only toolkit-independent code is covered; the button and the dialog are thin. Run from the project root:
python -m unittest discover -s tests -t .
"""
import unittest
from typing import Any

from modules import release_fetch, repo_overview

NOW = 1_000_000.0
REPO = "finalburneo/FBNeo"


def rows_for(summary: dict[str, Any] | None) -> list[repo_overview.RepoRow]:
    """Build the rows of one mapped repository whose job summary is `summary`."""
    summaries = {REPO.lower(): summary} if summary is not None else {}
    return repo_overview.build_rows([{"repository": REPO, "folder": "FBNeo"}], summaries, NOW, lambda entry: 5)


def summary(**fields: Any) -> dict[str, Any]:
    """A job summary like db_manager.get_repo_job_summaries returns, with the given fields changed."""
    base: dict[str, Any] = {
        "latest_tag": "latest", "latest_status": "FAILED", "latest_release_type": "Release",
        "latest_updated_at": NOW - 60, "last_activity": NOW - 60, "pending_next_check": None, "pending_count": 0,
    }
    base.update(fields)
    return base


class FailedTagTests(unittest.TestCase):
    def test_a_failed_latest_job_gives_the_tag_to_fetch_again(self) -> None:
        row = rows_for(summary())[0]
        self.assertEqual((row.status, row.failed_tag), (repo_overview.STATUS_FAILED, "latest"))

    def test_other_states_have_no_failed_tag(self) -> None:
        for fields in (
            {"latest_status": "COMPLETED"},
            {"pending_next_check": NOW + 600, "pending_count": 1},  # a newer job is waiting: that one decides
        ):
            with self.subTest(fields=fields):
                self.assertEqual(rows_for(summary(**fields))[0].failed_tag, "")
        self.assertEqual(rows_for(None)[0].failed_tag, "")


class ReleaseUrlTests(unittest.TestCase):
    def test_the_link_is_read_back_by_the_fetch_parser(self) -> None:
        for tag in ("latest", "v1.0", "release/2026-11", "build 5%", "weird#tag?x"):
            with self.subTest(tag=tag):
                ref = release_fetch.parse_release_url(release_fetch.release_url(REPO, tag))
                self.assertEqual((ref.repo, ref.tag), (REPO, tag))

    def test_the_link_is_a_plain_github_release_page(self) -> None:
        self.assertEqual(
            release_fetch.release_url(REPO, "latest"), "https://github.com/finalburneo/FBNeo/releases/tag/latest")


if __name__ == "__main__":
    unittest.main()
