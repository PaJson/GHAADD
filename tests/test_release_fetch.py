"""Fetching one release by its GitHub link: the URL parser, the queueing and the --fetch-release option.

GitHub is faked (release data and commit); state.db and mapping.json live in a temporary directory, so nothing real
is touched. Run from the project root: python -m unittest discover -s tests -t .
"""
import contextlib
import io
import time
import unittest
from typing import Any
from unittest import mock

from modules import cli_commands, daemon_lock, mapping_manager, release_fetch
from tests.test_cli_dispatch import DispatchTestCase
from tests.test_queue_worker_flow import REPO, WorkerTestCase

IDLE: Any = {"running": False, "queue_progress": None, "current_job": None}


class ParseTests(unittest.TestCase):
    def test_a_release_link_gives_repo_and_tag(self) -> None:
        for url in (
            "https://github.com/official-stockfish/Stockfish/releases/tag/sf_17.1",
            "  http://www.github.com/official-stockfish/Stockfish/releases/tag/sf_17.1/  ",
            "github.com/official-stockfish/Stockfish/releases/tag/sf_17.1?foo=1#top",
        ):
            self.assertEqual(
                release_fetch.parse_release_url(url), ("official-stockfish/Stockfish", "sf_17.1"), url
            )

    def test_a_tag_may_hold_slashes_and_percent_codes(self) -> None:
        ref = release_fetch.parse_release_url("https://github.com/o/r/releases/tag/release/1.0%2Bbeta")
        self.assertEqual(ref, ("o/r", "release/1.0+beta"))

    def test_links_that_are_not_one_release_are_refused(self) -> None:
        for url in (
            "",
            "https://github.com/official-stockfish/Stockfish/releases",
            "https://github.com/official-stockfish/Stockfish/releases/latest",
            "https://github.com/official-stockfish/Stockfish",
            "https://github.com/official-stockfish/Stockfish/releases/tag/",
            "https://github.com/official-stockfish",
            "https://example.com/o/r/releases/tag/v1",
            "https://github.com.evil.example/o/r/releases/tag/v1",
            "ftp://github.com/o/r/releases/tag/v1",
            "https://github.com/o r/x/releases/tag/v1",
        ):
            with self.assertRaises(release_fetch.ReleaseUrlError, msg=url):
                release_fetch.parse_release_url(url)

    def test_the_messages_name_the_problem(self) -> None:
        with self.assertRaises(release_fetch.ReleaseUrlError) as caught:
            release_fetch.parse_release_url("https://github.com/o/r/releases")
        self.assertIn("/releases/tag/", str(caught.exception))


class FetchTests(WorkerTestCase):
    """Runs on the worker tests' fake world: mapping.json and state.db in a temp folder, two re-check steps."""

    def setUp(self) -> None:
        super().setUp()
        self.release: Any = {
            "tag_name": "v1", "prerelease": False, "html_url": f"https://github.com/{REPO}/releases/tag/v1",
        }
        patches = (
            (release_fetch.asset_downloader, "get_release_data", lambda repo, tag: self.release),
            (release_fetch.daemon_lock, "get_daemon_status", lambda: IDLE),
            (mapping_manager, "log_warning", lambda kind, message: self.calls["warnings"].append((kind, message))),
        )
        for target, name, value in patches:
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def fetch(self, url: str = f"https://github.com/{REPO}/releases/tag/v1", **kwargs: Any) -> Any:
        return self.quietly(release_fetch.fetch_release, url, connection=self.connection, **kwargs)

    def test_a_new_repository_is_added_and_the_release_queued(self) -> None:
        outcome = self.fetch()
        self.assertTrue(outcome.mapping_created)
        self.assertIsNotNone(mapping_manager.get_repository_mapping(REPO))
        self.assertIn("UNMAPPED", self.warning_kinds())
        row = self.row(outcome.job_id)
        self.assertEqual((row["repo"], row["tag"], row["release_type"], row["status"]), (REPO, "v1", "Release", "PENDING"))
        self.assertEqual(row["expected_commit"], self.commit)
        self.assertEqual(row["attempt_count"], 2)  # the waiting re-checks are skipped: the next run is the last
        self.assertLessEqual(row["next_check_time"], time.time())

    def test_an_existing_repository_is_not_touched(self) -> None:
        self.write_mapping({"repository": REPO, "folder": "app", "destination": "", "active": True})
        before = mapping_manager.get_repository_mapping(REPO)
        outcome = self.fetch()
        self.assertFalse(outcome.mapping_created)
        self.assertEqual(mapping_manager.get_repository_mapping(REPO), before)
        self.assertEqual(self.warning_kinds(), [])

    def test_the_skiplist_and_the_active_switch_are_ignored(self) -> None:
        self.write_mapping({"repository": REPO, "folder": "app", "destination": "", "active": False, "skiplist": ["Release"]})
        outcome = self.fetch()
        self.assertEqual(self.row(outcome.job_id)["status"], "PENDING")

    def test_the_usual_rechecks_can_be_kept(self) -> None:
        outcome = self.fetch(finish_early=False)
        self.assertEqual(self.row(outcome.job_id)["attempt_count"], 0)
        self.assertFalse(outcome.finish_early)

    def test_a_prerelease_is_queued_as_one_and_github_spells_the_repository(self) -> None:
        self.release = {"tag_name": "v1", "prerelease": True, "html_url": "https://github.com/O/App/releases/tag/v1"}
        outcome = self.fetch(f"https://github.com/{REPO}/releases/tag/v1")
        self.assertEqual((outcome.repo, outcome.release_type), ("O/App", "Pre-release"))
        self.assertEqual(self.row(outcome.job_id)["release_type"], "Pre-release")

    def test_a_pending_job_of_the_same_release_is_replaced(self) -> None:
        old = self.job(tag="v1")
        outcome = self.fetch()
        self.assertEqual(self.row(old)["status"], "SUPERSEDED")
        self.assertEqual(self.pending_ids(), [outcome.job_id])

    def test_a_missing_release_queues_nothing_and_adds_nothing(self) -> None:
        self.release = None
        with self.assertRaises(release_fetch.ReleaseFetchError) as caught:
            self.fetch()
        self.assertIn("was not found on GitHub", str(caught.exception))
        self.assertEqual(self.pending_ids(), [])
        self.assertIsNone(mapping_manager.get_repository_mapping(REPO))

    def test_it_waits_while_the_daemon_is_in_a_cycle(self) -> None:
        busy: Any = {"running": True, "queue_progress": {"repo": "x/y"}, "current_job": None}
        with mock.patch.object(daemon_lock, "get_daemon_status", lambda: busy):
            with self.assertRaises(release_fetch.ReleaseFetchError) as caught:
                self.fetch()
        self.assertIn("busy with x/y", str(caught.exception))
        self.assertEqual(self.pending_ids(), [])

    def test_the_outcome_text_says_what_to_do_next(self) -> None:
        text = release_fetch.outcome_text(self.fetch())
        self.assertIn("queued as job #", text)
        self.assertIn("without a destination", text)


class CliFetchTests(DispatchTestCase):
    def outcome(self, job_id: Any = 5) -> Any:
        return release_fetch.FetchOutcome("o/app", "v1", "Release", job_id, False, True)

    def test_a_fetch_queues_and_asks_a_running_daemon_to_poll(self) -> None:
        self.patch("fetch_release", self.recorder("fetch", self.outcome()))
        self.patch("request_poll_now", self.recorder("poll"))
        handled, out, _ = self.run_cli("--fetch-release", "https://github.com/o/app/releases/tag/v1")
        self.assertTrue(handled)
        self.assertEqual(self.labels(), ["fetch", "poll"])
        self.assertEqual(self.calls[0][2], {"finish_early": True})
        self.assertIn("queued as job #5", out)

    def test_without_a_daemon_nothing_is_requested(self) -> None:
        self.daemon_running = False
        self.patch("fetch_release", self.recorder("fetch", self.outcome()))
        self.patch("request_poll_now", self.recorder("poll"))
        _, out, _ = self.run_cli("--fetch-release", "github.com/o/app/releases/tag/v1")
        self.assertEqual(self.labels(), ["fetch"])
        self.assertIn("No daemon is running", out)

    def test_keep_rechecks_is_passed_on_and_needs_the_command(self) -> None:
        self.patch("fetch_release", self.recorder("fetch", self.outcome()))
        self.patch("request_poll_now", self.recorder("poll"))
        self.run_cli("--fetch-release", "github.com/o/app/releases/tag/v1", "--fetch-keep-rechecks")
        self.assertEqual(self.calls[0][2], {"finish_early": False})
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli_commands.parse_cli_args(["--fetch-keep-rechecks"], "test")  # refused without --fetch-release

    def test_an_error_is_printed_to_stderr(self) -> None:
        def refuse(url: str, finish_early: bool = True) -> Any:
            raise release_fetch.ReleaseUrlError("not a release link")

        self.patch("fetch_release", refuse)
        handled, out, err = self.run_cli("--fetch-release", "nonsense")
        self.assertTrue(handled)
        self.assertEqual(out, "")
        self.assertIn("Fetch error: not a release link", err)


if __name__ == "__main__":
    unittest.main()
