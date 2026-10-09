"""Tests for modules/viewer_push.py: path redaction, the snapshot, and when the pusher sends what.

Nothing here touches the network, the real state.db or a real clock: the data sources, the poster and the clock
are fakes. Run from the project root: python -m unittest discover -s tests -t .
"""
import contextlib
import io
import json
import unittest
from typing import Any, Optional
from unittest import mock

import requests

from modules import config_manager, gui_data, viewer_push
from modules.config_manager import ViewerSettings
from modules.gui_data import QueueCounts, RepoTable
from modules.repo_overview import RepoRow
from modules.status_tabs import StatusRow, UnmappedRow
from modules.viewer_push import PushError, SnapshotBuilder, ViewerPusher, redact_paths

TOKEN = "t0ken-" + "x" * 20
SETTINGS: ViewerSettings = {"enabled": True, "url": "http://viewer:8888", "token": TOKEN, "name": "home-pc"}


def _settings(**changes: str) -> ViewerSettings:
    """The test settings with some text values replaced."""
    return {
        "enabled": True, "url": changes.get("url", SETTINGS["url"]), "token": changes.get("token", SETTINGS["token"]),
        "name": changes.get("name", SETTINGS["name"]),
    }


class RedactPathsTests(unittest.TestCase):
    def test_real_completed_messages_lose_the_whole_path_spaces_and_brackets_included(self) -> None:
        # These are the shapes found in a real state.db: the path ends at the comma, whatever the folder names hold.
        cases = {
            r"Completed [a/b v1 (abc)] moved to [L:\Gaming\Emulators\SONY - PS3 - RPCS3\@GitHub\Release\2026-10-09_17-14, 0.0.43, build-1, a1d3583]":
                "Completed [a/b v1 (abc)] moved to [<path>, 0.0.43, build-1, a1d3583]",
            r"Completed [x/y v1 (98da)] moved to [D:\GHAADD\Complete\SameBoy (LIJI32)\Release\2026-10-09_16-59, SameBoy v1.0.4, v1.0.4, 98da08c]":
                "Completed [x/y v1 (98da)] moved to [<path>, SameBoy v1.0.4, v1.0.4, 98da08c]",
            r"Completed [x/y nightly (1)] moved to [K:\Apps\Subtitle Edit\@GitHub\Pre-release\2026-10-09_10-00, Subtitle Edit v5, nightly, 1]":
                "Completed [x/y nightly (1)] moved to [<path>, Subtitle Edit v5, nightly, 1]",
        }
        for text, expected in cases.items():
            self.assertEqual(redact_paths(text), expected, text)

    def test_quoted_paths_in_error_texts_are_replaced(self) -> None:
        text = "Could not move staging folder to Complete: [WinError 5] Access is denied: 'D:\\\\GHAADD\\\\Processing\\\\My Tool\\\\x'"
        self.assertEqual(redact_paths(text), "Could not move staging folder to Complete: [WinError 5] Access is denied: '<path>'")

    def test_unc_and_posix_paths_with_spaces_are_replaced(self) -> None:
        cases = {
            r"Share \\nas\media\Game Tools\x, next": "Share <path>, next",
            "Cannot write /mnt/data/SONY - PS4/Release/2026-10-09, tag": "Cannot write <path>, tag",
            "Moved to d:/Users/me/GHAADD/Complete/x.zip": "Moved to <path>",
            "Two paths: C:\\a\\b, and /srv/x/y;": "Two paths: <path>, and <path>;",
        }
        for text, expected in cases.items():
            self.assertEqual(redact_paths(text), expected, text)

    def test_no_part_of_a_path_is_left_behind(self) -> None:
        text = r"Moved to [L:\Gaming\Secret Games\SONY - PS3\@GitHub\Release\2026, tag] and /home/me/My Files/x"
        redacted = redact_paths(text)
        for leftover in ("Gaming", "Secret", "SONY", "@GitHub", "home", "My Files"):
            self.assertNotIn(leftover, redacted)

    def test_repository_names_and_plain_text_stay(self) -> None:
        for text in (
            "cli/cli v2.30.0 downloaded", "owner/repo-name: 12 / 15 folders", "released 3/4 files", "and/or v1.0/rc",
            "Repository 'TASEmulators/fceux' was added without a configured destination", "tag:v1",
            "",
        ):
            self.assertEqual(redact_paths(text), text)


class ViewerSettingsTests(unittest.TestCase):
    def test_defaults_are_off_and_empty(self) -> None:
        settings = config_manager.get_viewer_settings({})
        self.assertFalse(settings["enabled"])
        self.assertEqual((settings["url"], settings["token"]), ("", ""))
        self.assertTrue(settings["name"])  # the computer's name, never empty

    def test_values_are_trimmed_and_the_trailing_slash_dropped(self) -> None:
        settings = config_manager.get_viewer_settings(
            {"viewer": {"enabled": True, "url": " http://192.168.0.100:8888/ ", "token": " abc ", "name": " home-pc "}}
        )
        self.assertEqual(
            settings, {"enabled": True, "url": "http://192.168.0.100:8888", "token": "abc", "name": "home-pc"}
        )

    def test_wrong_types_fall_back(self) -> None:
        settings = config_manager.get_viewer_settings({"viewer": {"enabled": "maybe", "url": 5, "token": None, "name": 3}})
        self.assertFalse(settings["enabled"])
        self.assertEqual((settings["url"], settings["token"]), ("", ""))

    def test_the_fingerprint_notices_a_changed_viewer_address(self) -> None:
        with mock.patch.object(config_manager.env_manager, "credentials_fingerprint", lambda: "x"):
            first = config_manager.get_config_fingerprint({"viewer": {"url": "http://a:1"}})
            second = config_manager.get_config_fingerprint({"viewer": {"url": "http://b:1"}})
        self.assertNotEqual(first, second)


def _repo_row(**changes: Any) -> RepoRow:
    values: dict[str, Any] = dict(
        repo="cli/cli", folder="cli", destination=r"D:\GitHub\cli", status="Waiting", tag="(R) v2.30.0",
        last_check="2026-10-09 10:00:00", step="2 / 8", next_check="2026-10-09 12:00:00", files="5", limit="3 / 10",
        last_activity=1.0,
    )
    values.update(changes)
    return RepoRow(**values)


class FakeModel:
    event_tab_keys = ["warnings", "completed", "limits"]

    def rows(self, key: str) -> list[StatusRow]:
        if key == "warnings":
            return [StatusRow(7, "2026-10-09 10:01:00", "cli/cli", "API", r"Could not write D:\GitHub\cli\x.zip for cli/cli")]
        if key == "completed":
            return [StatusRow(
                8, "2026-10-09 10:02:00", "cli/cli", "(R) v2.30.0",
                r"Completed [cli/cli v2.30.0 (d60206a)] moved to [L:\Gaming\Tools\Achievement Watcher (AW)\@GitHub\Release\2026-10-09_16-59, CLI v2.30.0, v2.30.0, d60206a]",
            )]
        return []


class FakeFeed:
    def __init__(self, store: Any = None) -> None:
        self.model = FakeModel()
        self.refreshed = 0

    def refresh(self) -> set[str]:
        self.refreshed += 1
        return set()

    def unmapped(self) -> list[UnmappedRow]:
        return [UnmappedRow("new/tool", "tool", "2026-10-08 09:00")]


def _status() -> dict[str, Any]:
    return {
        "paused": False, "polling_idle": False, "next_poll_at": 5000.0, "started_at": 1000.0,
        "queue_progress": {"index": 2, "total": 9, "repo": "cli/cli", "tag": "v2.31.0", "release_type": "Release"},
    }


class SnapshotBuilderTests(unittest.TestCase):
    def build(self) -> dict[str, Any]:
        rows = [_repo_row(), _repo_row(repo="other/tool", limit_warning=True)]
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(gui_data, "StatusFeed", FakeFeed))
            stack.enter_context(mock.patch.object(gui_data, "load_repo_table", lambda: RepoTable(rows=rows)))
            stack.enter_context(mock.patch.object(gui_data, "load_queue_counts", lambda: QueueCounts(pending=4, due=1)))
            return SnapshotBuilder(status_reader=_status).build()

    def test_the_snapshot_has_what_the_gui_shows(self) -> None:
        snapshot = self.build()
        self.assertEqual(snapshot["status"]["pending"], 4)
        self.assertEqual(snapshot["status"]["due"], 1)
        self.assertEqual(snapshot["status"]["next_poll_at"], 5000.0)
        self.assertIn("Processing 2 of 9: cli/cli", snapshot["status"]["progress"])
        self.assertEqual([row["repo"] for row in snapshot["repos"]], ["cli/cli", "other/tool"])
        self.assertTrue(snapshot["repos"][1]["limit_warning"])
        self.assertEqual(set(snapshot["tabs"]), {"warnings", "completed", "limits", "unmapped"})
        self.assertEqual(snapshot["tabs"]["unmapped"], [{"repo": "new/tool", "folder": "tool", "time": "2026-10-08 09:00"}])

    def test_no_path_leaves_the_machine(self) -> None:
        snapshot = self.build()
        text = json.dumps(snapshot)
        for secret in ("GitHub", "Gaming", "Achievement", "@GitHub", "destination"):
            self.assertNotIn(secret, text)
        self.assertEqual(snapshot["tabs"]["warnings"][0]["message"], "Could not write <path>")
        self.assertEqual(
            snapshot["tabs"]["completed"][0]["message"],
            "Completed [cli/cli v2.30.0 (d60206a)] moved to [<path>, CLI v2.30.0, v2.30.0, d60206a]",
        )
        self.assertNotIn("destination", snapshot["repos"][0])


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeBuilder:
    def __init__(self) -> None:
        self.snapshot: dict[str, Any] = {"status": {"pending": 1}, "repos": [], "tabs": {}}
        self.error: Optional[Exception] = None

    def build(self) -> dict[str, Any]:
        if self.error is not None:
            raise self.error
        return json.loads(json.dumps(self.snapshot))


class PusherTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.builder = FakeBuilder()
        self.sent: list[dict[str, Any]] = []
        self.answers: list[Any] = []  # what the next posts answer: a dict, or an exception to raise
        self.statuses: list[dict[str, Any]] = []
        self.pusher = ViewerPusher(
            SETTINGS, builder=self.builder, post=self.post, clock=self.clock, on_status=self.statuses.append,
        )

    def post(self, url: str, message: dict[str, Any], token: str, timeout: float) -> dict[str, Any]:
        self.assertEqual((url, token), (SETTINGS["url"], TOKEN))
        self.sent.append(message)
        answer = self.answers.pop(0) if self.answers else {"ok": True}
        if isinstance(answer, Exception):
            raise answer
        return answer

    def push(self) -> str:
        """One look; returns what the console was told."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.pusher.push_once()
        return out.getvalue()

    def kinds(self) -> list[str]:
        return [message["type"] for message in self.sent]


class PushOnceTests(PusherTestCase):
    def test_the_first_look_sends_a_snapshot_with_who_and_when(self) -> None:
        self.push()
        self.assertEqual(self.kinds(), ["snapshot"])
        message = self.sent[0]
        self.assertEqual((message["schema"], message["name"]), (viewer_push.SCHEMA, "home-pc"))
        self.assertEqual(message["data"], self.builder.snapshot)
        self.assertIn("sent_at", message)
        self.assertNotIn(TOKEN, json.dumps(message))  # the token travels in the header only

    def test_unchanged_data_sends_only_a_heartbeat_when_one_is_due(self) -> None:
        self.push()
        self.clock.now += 5
        self.push()
        self.assertEqual(self.kinds(), ["snapshot"])  # nothing changed, not yet time
        self.clock.now += viewer_push.HEARTBEAT_SECONDS
        self.push()
        self.assertEqual(self.kinds(), ["snapshot", "heartbeat"])
        self.assertNotIn("data", self.sent[1])

    def test_changed_data_sends_a_new_snapshot_at_once(self) -> None:
        self.push()
        self.builder.snapshot["status"]["pending"] = 2
        self.clock.now += 1
        self.push()
        self.assertEqual(self.kinds(), ["snapshot", "snapshot"])

    def test_a_viewer_that_asks_for_a_snapshot_gets_one_next_look(self) -> None:
        self.push()
        self.clock.now += viewer_push.HEARTBEAT_SECONDS
        self.answers.append({"ok": True, "need_snapshot": True})
        self.push()  # heartbeat, answered "send everything again"
        self.clock.now += 1
        self.push()
        self.assertEqual(self.kinds(), ["snapshot", "heartbeat", "snapshot"])

    def test_a_failure_backs_off_doubling_up_to_a_limit_and_is_reported_once(self) -> None:
        self.answers += [PushError("cannot reach the viewer (ConnectionError)")] * 12
        first = self.push()
        self.assertIn("cannot reach the viewer", first)
        self.assertIn("polling is not affected", first)
        # The retry time is the thing under test, so the test reads it from the pusher.
        delays = [self.pusher._retry_at - self.clock.now]
        for _ in range(7):
            sent_before = len(self.sent)
            self.clock.now = self.pusher._retry_at - 0.5  # just before the retry: still backing off
            self.assertEqual(self.push(), "")
            self.assertEqual(len(self.sent), sent_before)
            self.clock.now = self.pusher._retry_at
            self.assertEqual(self.push(), "")  # tried again, same reason: not said again on the console
            self.assertEqual(len(self.sent), sent_before + 1)
            delays.append(self.pusher._retry_at - self.clock.now)
        self.assertEqual(delays, [5, 10, 20, 40, 60, 60, 60, 60])
        self.assertEqual(self.pusher.error, "cannot reach the viewer (ConnectionError)")

    def test_recovery_is_announced_and_everything_is_sent_again(self) -> None:
        self.answers.append(PushError("cannot reach the viewer (ConnectionError)"))
        self.push()
        self.clock.now += viewer_push.BACKOFF_MAX_SECONDS
        text = self.push()
        self.assertIn("reachable again", text)
        self.assertEqual(self.kinds(), ["snapshot", "snapshot"])
        self.assertEqual(self.pusher.error, "")
        self.assertIsNotNone(self.pusher.last_ok)
        self.assertEqual(self.statuses[-1]["error"], "")
        self.assertEqual(self.statuses[-1]["last_ok"], self.pusher.last_ok)

    def test_a_data_problem_is_reported_and_retried_without_crashing(self) -> None:
        self.builder.error = RuntimeError("database is locked")
        text = self.push()
        self.assertIn("could not read the data", text)
        self.assertEqual(self.sent, [])
        self.builder.error = None
        self.clock.now += 1
        self.push()
        self.assertEqual(self.kinds(), ["snapshot"])


class LifecycleTests(PusherTestCase):
    def test_it_will_not_start_without_an_address_or_a_token(self) -> None:
        for changes, expected in (({"url": ""}, "viewer.url"), ({"url": "ftp://x"}, "viewer.url"), ({"token": ""}, "viewer.token")):
            pusher = ViewerPusher(_settings(**changes), builder=self.builder, post=self.post)
            self.assertIn(expected, pusher.not_ready_reason())
            self.assertFalse(pusher.start())
            self.assertFalse(pusher.active)
        self.assertEqual(self.sent, [])

    def test_the_live_switch_starts_and_stops_and_says_goodbye(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertTrue(self.pusher.apply_override(True, configured=False))
            self.assertTrue(self.pusher.active)
            self.assertTrue(self.pusher.apply_override(True, configured=False))  # already on: nothing changes
            self.assertFalse(self.pusher.apply_override(False, configured=True))
        self.assertFalse(self.pusher.active)
        self.assertEqual(self.kinds()[-1], "goodbye")
        self.assertEqual(self.kinds().count("goodbye"), 1)
        self.assertIn("switched on", out.getvalue())
        self.assertIn("switched off", out.getvalue())

    def test_no_override_follows_the_config_value(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(self.pusher.apply_override(None, configured=False))
            self.assertTrue(self.pusher.apply_override(None, configured=True))
            self.assertFalse(self.pusher.apply_override(None, configured=False))
        self.assertEqual(self.kinds()[-1], "goodbye")

    def test_a_lost_goodbye_is_not_an_error(self) -> None:
        def post(url: str, message: dict[str, Any], token: str, timeout: float) -> dict[str, Any]:
            if message["type"] == "goodbye":
                raise PushError("cannot reach the viewer (ConnectionError)")
            return {"ok": True}

        pusher = ViewerPusher(SETTINGS, builder=self.builder, post=post, clock=self.clock)
        with contextlib.redirect_stdout(io.StringIO()):
            pusher.apply_override(True, configured=False)
            pusher.stop()
        self.assertFalse(pusher.active)

    def test_stopping_something_that_never_ran_is_a_no_op(self) -> None:
        self.pusher.stop()
        self.assertEqual(self.sent, [])

    def test_starting_without_settings_says_why(self) -> None:
        pusher = ViewerPusher(_settings(url=""), builder=self.builder, post=self.post)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertFalse(pusher.apply_override(True, configured=False))
        self.assertIn("cannot start", out.getvalue())
        self.assertIn("viewer.url", out.getvalue())


class FakeResponse:
    def __init__(self, status: int, body: Any = None, bad_json: bool = False) -> None:
        self.status_code = status
        self._body = body
        self._bad_json = bad_json

    def json(self) -> Any:
        if self._bad_json:
            raise ValueError("no json")
        return self._body


class PostMessageTests(unittest.TestCase):
    def post(self, response: Any) -> dict[str, Any]:
        with mock.patch.object(viewer_push.requests, "post", return_value=response) as posted:
            result = viewer_push.post_message("http://viewer:8888", {"type": "heartbeat"}, TOKEN, 3.0)
        args, kwargs = posted.call_args
        self.assertEqual(args[0], "http://viewer:8888/api/snapshot")
        self.assertEqual(kwargs["headers"], {"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(kwargs["timeout"], 3.0)
        return result

    def test_a_good_answer_is_returned(self) -> None:
        self.assertEqual(self.post(FakeResponse(200, {"ok": True, "need_snapshot": True})), {"ok": True, "need_snapshot": True})

    def test_a_rejected_token_is_said_plainly(self) -> None:
        for status in (401, 403):
            with self.assertRaisesRegex(PushError, "rejected the token"):
                self.post(FakeResponse(status))

    def test_other_failures_become_push_errors_without_the_token(self) -> None:
        with self.assertRaisesRegex(PushError, "HTTP 500"):
            self.post(FakeResponse(500))
        with self.assertRaisesRegex(PushError, "not understood"):
            self.post(FakeResponse(200, bad_json=True))
        with mock.patch.object(viewer_push.requests, "post", side_effect=requests.ConnectionError(f"boom {TOKEN}")):
            with self.assertRaises(PushError) as caught:
                viewer_push.post_message("http://viewer:8888", {}, TOKEN, 3.0)
        self.assertIn("cannot reach the viewer", str(caught.exception))
        self.assertNotIn(TOKEN, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
