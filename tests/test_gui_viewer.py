"""Tests for the Viewer window's logic: form validation (gui_forms), status texts and the connection test (gui_viewer).

No Tk and no network: the post function and the clock are fakes. Run from the project root:
python -m unittest discover -s tests -t .
"""
import unittest
from typing import Any

from modules import gui_forms, gui_viewer
from modules.viewer_push import PushError

TOKEN = "a-good-token-" + "x" * 12


def form(**changes: Any) -> dict[str, Any]:
    values: dict[str, Any] = {"enabled": True, "url": "http://192.168.0.100:8888", "token": TOKEN, "name": ""}
    values.update(changes)
    return values


class ViewerUrlTests(unittest.TestCase):
    def test_plain_http_and_https_addresses_are_accepted_and_tidied(self) -> None:
        cases = {
            "http://192.168.0.100:8888": "http://192.168.0.100:8888",
            "  http://viewer.lan:8888/  ": "http://viewer.lan:8888",
            "https://viewer.example.org": "https://viewer.example.org",
            "http://nas": "http://nas",
            "http://[::1]:8888": "http://[::1]:8888",
        }
        for text, expected in cases.items():
            self.assertEqual(gui_forms.normalize_viewer_url(text), expected, text)

    def test_anything_else_is_refused(self) -> None:
        for text in (
            "", "   ", "192.168.0.100:8888", "ftp://nas", "http://", "http:// nas", "http://nas:notaport",
            "http://nas:99999", "http://nas:0", "http://nas/some/path", "http://nas?x=1", "http://nas#top",
            "http://user:secret@nas:8888", "javascript:alert(1)",
        ):
            self.assertIsNone(gui_forms.normalize_viewer_url(text), text)


class BuildViewerChangesTests(unittest.TestCase):
    def test_a_good_form_gives_the_config_keys(self) -> None:
        result = gui_forms.build_viewer_changes(form(url=" http://nas:8888/ ", name=" home-pc "))
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(
            result.changes,
            {"viewer.enabled": True, "viewer.url": "http://nas:8888", "viewer.token": TOKEN, "viewer.name": "home-pc"},
        )

    def test_the_keys_are_the_ones_the_daemon_reads(self) -> None:
        from modules import config_manager

        changes = gui_forms.build_viewer_changes(form(name="pc")).changes
        config: dict[str, Any] = {"viewer": {key.split(".", 1)[1]: value for key, value in changes.items()}}
        self.assertEqual(
            config_manager.get_viewer_settings(config),
            {"enabled": True, "url": "http://192.168.0.100:8888", "token": TOKEN, "name": "pc"},
        )

    def test_sending_needs_an_address_and_a_token(self) -> None:
        for changes in ({"url": ""}, {"token": ""}, {"url": "", "token": ""}):
            result = gui_forms.build_viewer_changes(form(**changes))
            self.assertFalse(result.ok, changes)
            self.assertIn("address and a token", result.errors[0])

    def test_an_unused_viewer_can_be_left_half_filled_or_empty(self) -> None:
        for changes in ({"enabled": False, "url": "", "token": ""}, {"enabled": False, "token": ""}, {"enabled": False, "url": ""}):
            result = gui_forms.build_viewer_changes(form(**changes))
            self.assertTrue(result.ok, (changes, result.errors))
            self.assertIs(result.changes["viewer.enabled"], False)

    def test_a_bad_address_or_token_is_refused_even_when_not_enabled(self) -> None:
        self.assertIn("address must look like", gui_forms.build_viewer_changes(form(enabled=False, url="nas:8888")).errors[0])
        short = gui_forms.build_viewer_changes(form(enabled=False, token="short"))
        self.assertIn("at least 16", short.errors[0])
        self.assertFalse(gui_forms.build_viewer_changes(form(token="has a space inside it ok")).ok)

    def test_the_name_is_limited(self) -> None:
        self.assertTrue(gui_forms.build_viewer_changes(form(name="x" * 64)).ok)
        self.assertFalse(gui_forms.build_viewer_changes(form(name="x" * 65)).ok)
        self.assertFalse(gui_forms.build_viewer_changes(form(name="bad\x00name")).ok)


class DescribePushTests(unittest.TestCase):
    NOW = 10_000.0

    def view(self, **status: Any) -> gui_viewer.PushView:
        return gui_viewer.describe_push(status, self.NOW)

    def test_no_daemon_means_nothing_to_switch(self) -> None:
        view = self.view(running=False)
        self.assertIn("No daemon is running", view.text)
        self.assertEqual((view.start_enabled, view.stop_enabled, view.problem), (False, False, False))

    def test_sending_shows_when_it_last_got_through(self) -> None:
        view = self.view(running=True, viewer_push={"active": True, "last_ok": self.NOW - 12, "error": ""})
        self.assertEqual(view.text, "Sending to the viewer. Last delivered 12 s ago.")
        self.assertEqual((view.start_enabled, view.stop_enabled, view.problem), (False, True, False))

    def test_sending_but_failing_is_a_problem_and_can_be_stopped(self) -> None:
        view = self.view(running=True, viewer_push={"active": True, "last_ok": None, "error": "the viewer rejected the token"})
        self.assertIn("rejected the token", view.text)
        self.assertIn("Nothing delivered yet", view.text)
        self.assertEqual((view.start_enabled, view.stop_enabled, view.problem), (False, True, True))

    def test_not_sending_can_be_started_and_explains_why_it_could_not(self) -> None:
        plain = self.view(running=True, viewer_push=None)
        self.assertEqual((plain.start_enabled, plain.stop_enabled, plain.problem), (True, False, False))
        failed = self.view(running=True, viewer_push={"active": False, "last_ok": None, "error": "set viewer.url in config.json"})
        self.assertIn("Not sending: set viewer.url", failed.text)
        self.assertEqual((failed.start_enabled, failed.stop_enabled, failed.problem), (True, False, True))

    def test_odd_status_data_does_not_break_it(self) -> None:
        self.assertFalse(self.view(running=True, viewer_push="nonsense").stop_enabled)
        self.assertIn("Nothing delivered yet", self.view(running=True, viewer_push={"active": True, "last_ok": "soon"}).text)

    def test_ago_is_short_and_readable(self) -> None:
        self.assertEqual(
            [gui_viewer.format_ago(s) for s in (-5, 0, 59, 89, 90, 600, 5400, 7500)],
            ["0 s", "0 s", "59 s", "89 s", "1 min", "10 min", "1 h 30 min", "2 h 5 min"],
        )


class CheckViewerTests(unittest.TestCase):
    def check(self, url: str = "http://nas:8888", token: str = TOKEN, answer: Any = None) -> Any:
        calls: list[tuple] = []

        def post(address: str, message: dict[str, Any], given: str, timeout: float) -> dict[str, Any]:
            calls.append((address, message, given))
            if isinstance(answer, Exception):
                raise answer
            return {"ok": True}

        result = gui_viewer.check_viewer(url, token, post=post)
        return result, calls

    def test_a_good_answer_is_a_success_and_only_a_ping_is_sent(self) -> None:
        result, calls = self.check(url=" http://nas:8888/ ")
        self.assertTrue(result.ok)
        self.assertEqual(calls, [("http://nas:8888", {"schema": 1, "type": "ping"}, TOKEN)])  # nothing that could look like a daemon

    def test_problems_are_said_plainly(self) -> None:
        result, _ = self.check(answer=PushError("the viewer rejected the token"))
        self.assertEqual((result.ok, result.message), (False, "The viewer rejected the token."))
        result, _ = self.check(answer=PushError("cannot reach the viewer (ConnectionError)"))
        self.assertFalse(result.ok)
        self.assertTrue(result.message.startswith("Cannot reach the viewer"))

    def test_nothing_is_sent_without_an_address_and_a_token(self) -> None:
        for url, token in (("", TOKEN), ("nas:8888", TOKEN), ("http://nas:8888", "  ")):
            result, calls = self.check(url=url, token=token)
            self.assertFalse(result.ok, (url, token))
            self.assertEqual(calls, [])


class TokenTests(unittest.TestCase):
    def test_a_generated_token_is_long_enough_for_the_viewer_and_the_form(self) -> None:
        token = gui_viewer.generate_token()
        self.assertGreaterEqual(len(token), gui_forms.MIN_VIEWER_TOKEN_LENGTH)
        self.assertTrue(gui_forms.build_viewer_changes(form(token=token)).ok)
        self.assertNotEqual(token, gui_viewer.generate_token())

    def test_the_default_name_is_never_empty(self) -> None:
        self.assertTrue(gui_viewer.computer_name())


if __name__ == "__main__":
    unittest.main()
