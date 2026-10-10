"""Tests for ghaadd_viewer.py: what the store believes (live / lost / stopped), what the server accepts, the page's safety.

The store runs on a fake clock; the HTTP tests talk to a real server on a free local port.
Run from the project root: python -m unittest discover -s tests -t .
"""
import contextlib
import http.client
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from typing import Any, Optional
from unittest import mock

# The viewer is a standalone file in viewer/ (it is not part of the modules package).
VIEWER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "viewer")
sys.path.insert(0, VIEWER_DIR)

import ghaadd_viewer  # noqa: E402
from ghaadd_viewer import Store  # noqa: E402

TOKEN = "viewer-token-" + "y" * 16


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def data(**status: Any) -> dict[str, Any]:
    return {
        "status": {
            "paused": False, "idle": False, "next_poll_at": 5300.0, "started_at": 1000.0, "progress": "",
            "queue": "Queue: nothing pending", "pending": 0, "due": 0, **status,
        },
        "repos": [{
            "repo": "cli/cli", "folder": "cli", "status": "Waiting", "tag": "v1", "last_check": "-", "step": "1 / 8",
            "next_check": "-", "files": "3", "limit": "1 / 10", "limit_warning": True, "destination": r"D:\secret",
        }],
        "tabs": {
            "warnings": [{"time": "t", "repo": "cli/cli", "kind": "API", "message": "m", "extra": "dropped"}],
            "completed": [], "limits": [], "unmapped": [{"repo": "a/b", "folder": "b", "time": "t"}],
        },
    }


def message(kind: str = "snapshot", name: str = "home-pc", sent_at: float = 5000.0, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"schema": 1, "type": kind, "name": name, "version": "2.5", "sent_at": sent_at}
    if kind == "snapshot":
        result["data"] = data()
    result.update(extra)
    return result


def quiet(store: Store, payload: Any) -> dict[str, Any]:
    """Receive a message without the viewer's console lines in the test output."""
    with contextlib.redirect_stdout(io.StringIO()):
        return store.receive(payload)


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.store = Store(lost_after=45.0, clock=self.clock)

    def only(self) -> dict[str, Any]:
        daemons = self.store.view()["daemons"]
        self.assertEqual(len(daemons), 1)
        return daemons[0]

    def test_nothing_is_shown_before_a_daemon_has_sent_data(self) -> None:
        self.assertEqual(self.store.view()["daemons"], [])

    def test_a_snapshot_makes_the_daemon_live_with_its_data(self) -> None:
        self.assertEqual(quiet(self.store, message()), {"ok": True, "need_snapshot": False})
        daemon = self.only()
        self.assertEqual((daemon["name"], daemon["state"], daemon["version"]), ("home-pc", "live", "2.5"))
        self.assertEqual(daemon["data"]["repos"][0]["repo"], "cli/cli")
        self.assertEqual(daemon["data"]["tabs"]["unmapped"][0]["repo"], "a/b")

    def test_unknown_fields_are_dropped_on_the_way_in(self) -> None:
        quiet(self.store, message())
        daemon = self.only()
        self.assertNotIn("destination", daemon["data"]["repos"][0])
        self.assertNotIn("extra", daemon["data"]["tabs"]["warnings"][0])
        self.assertIs(daemon["data"]["repos"][0]["limit_warning"], True)

    def test_the_countdown_uses_the_daemons_own_clock_and_runs_down(self) -> None:
        quiet(self.store, message(sent_at=5000.0))  # next poll at 5300 on the daemon's clock: 300 s later
        self.assertEqual(self.only()["data"]["status"]["next_poll_in"], 300.0)
        self.clock.now += 20
        self.assertEqual(self.only()["data"]["status"]["next_poll_in"], 280.0)

    def test_no_countdown_while_paused_or_idle(self) -> None:
        for flag in ("paused", "idle"):
            store = Store(clock=self.clock)
            payload = message()
            payload["data"] = data(**{flag: True})
            quiet(store, payload)
            self.assertIsNone(store.view()["daemons"][0]["data"]["status"]["next_poll_in"], flag)

    def test_heartbeats_keep_it_live_and_silence_makes_it_lost_without_old_data(self) -> None:
        quiet(self.store, message())
        for _ in range(4):
            self.clock.now += 30
            self.assertEqual(quiet(self.store, message("heartbeat")), {"ok": True, "need_snapshot": False})
            self.assertEqual(self.only()["state"], "live")
        self.clock.now += 46
        daemon = self.only()
        self.assertEqual(daemon["state"], "lost")
        self.assertNotIn("data", daemon)  # the old numbers are not shown
        self.assertEqual(daemon["age"], 46.0)

    def test_a_heartbeat_after_a_gap_does_not_revive_old_data_but_asks_for_a_snapshot(self) -> None:
        quiet(self.store, message())
        self.clock.now += 100
        self.assertEqual(quiet(self.store, message("heartbeat")), {"ok": True, "need_snapshot": True})
        self.assertEqual(self.only()["state"], "lost")
        quiet(self.store, message())
        self.assertEqual(self.only()["state"], "live")

    def test_goodbye_means_stopped_with_no_data_until_it_sends_again(self) -> None:
        quiet(self.store, message())
        self.clock.now += 5
        quiet(self.store, message("goodbye"))
        daemon = self.only()
        self.assertEqual(daemon["state"], "stopped")
        self.assertNotIn("data", daemon)
        self.clock.now += 3600
        self.assertEqual(self.only()["state"], "stopped")  # stays "stopped", it does not decay into "lost"
        self.assertEqual(quiet(self.store, message("heartbeat")), {"ok": True, "need_snapshot": True})
        quiet(self.store, message())
        self.assertEqual(self.only()["state"], "live")

    def test_a_heartbeat_or_goodbye_from_an_unknown_daemon_creates_nothing(self) -> None:
        self.assertEqual(quiet(self.store, message("heartbeat")), {"ok": True, "need_snapshot": True})
        self.assertEqual(quiet(self.store, message("goodbye")), {"ok": True, "need_snapshot": False})
        self.assertEqual(self.store.view()["daemons"], [])

    def test_a_ping_is_answered_but_changes_nothing(self) -> None:
        ping = {"schema": 1, "type": "ping"}
        self.assertEqual(quiet(self.store, ping), {"ok": True, "need_snapshot": False})
        self.assertEqual(self.store.view()["daemons"], [])  # it makes no daemon known
        quiet(self.store, message())
        self.clock.now += 100
        quiet(self.store, {**ping, "name": "home-pc"})  # even with a known daemon's name: it must not look alive again
        self.assertEqual(self.only()["state"], "lost")
        with self.assertRaises(ValueError):
            quiet(self.store, {"schema": 2, "type": "ping"})

    def test_several_daemons_are_kept_apart(self) -> None:
        quiet(self.store, message(name="home-pc"))
        quiet(self.store, message(name="nas"))
        self.clock.now += 50
        quiet(self.store, message(name="nas"))
        states = {daemon["name"]: daemon["state"] for daemon in self.store.view()["daemons"]}
        self.assertEqual(states, {"home-pc": "lost", "nas": "live"})

    def test_bad_messages_are_refused(self) -> None:
        bad: list[Any] = [
            "text", [], {}, message(name="  "), message(kind="unknown"),
            {**message(), "schema": 2}, {**message(), "schema": None},
            {**message(), "data": {"status": {}}}, {**message(), "data": {"tabs": {}}},
            {**message(), "data": {**data(), "repos": "nope"}},
        ]
        for payload in bad:
            with self.assertRaises(ValueError, msg=str(payload)[:80]):
                quiet(self.store, payload)
        self.assertEqual(self.store.view()["daemons"], [])

    def test_the_name_loses_control_characters_and_is_limited(self) -> None:
        quiet(self.store, message(name="ho\x00me\n-pc" + "z" * 100))
        name = self.only()["name"]
        self.assertTrue(name.startswith("home-pc"))
        self.assertEqual(len(name), 64)

    def test_the_number_of_daemons_is_limited(self) -> None:
        for number in range(ghaadd_viewer.MAX_DAEMONS):
            quiet(self.store, message(name=f"d{number}"))
        with self.assertRaisesRegex(ValueError, "too many"):
            quiet(self.store, message(name="one-too-many"))
        quiet(self.store, message(name="d0"))  # a known daemon can still send

    def test_texts_and_row_counts_are_capped(self) -> None:
        payload = message()
        payload["data"]["tabs"]["warnings"] = [{"time": "t", "repo": "r", "kind": "k", "message": "x" * 10_000}] * (ghaadd_viewer.MAX_ROWS + 50)
        quiet(self.store, payload)
        rows = self.only()["data"]["tabs"]["warnings"]
        self.assertEqual(len(rows), ghaadd_viewer.MAX_ROWS)
        self.assertEqual(len(rows[0]["message"]), ghaadd_viewer.MAX_TEXT)

    def test_odd_numbers_in_the_status_are_ignored(self) -> None:
        payload = message()
        payload["data"] = data(next_poll_at=float("inf"), started_at="soon", pending=True, due="3")
        quiet(self.store, payload)
        status = self.only()["data"]["status"]
        self.assertIsNone(status["next_poll_at"])
        self.assertIsNone(status["started_at"])
        self.assertEqual((status["pending"], status["due"]), (0, 0))
        self.assertIsNone(status["next_poll_in"])


class ServerTestCase(unittest.TestCase):
    def tokens(self) -> Any:
        """What the server under test accepts (a subclass can tie tokens to names)."""
        return [TOKEN]

    def setUp(self) -> None:
        self.store = Store(lost_after=45.0)
        self.book = ghaadd_viewer.TokenBook(self.tokens())
        self.server = ghaadd_viewer.make_server("127.0.0.1", 0, self.store, self.book)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(
        self, method: str, path: str, body: Optional[bytes] = None, token: Optional[str] = TOKEN,
        headers: Optional[dict[str, str]] = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(connection.close)
        sent = dict(headers or {})
        if token is not None:
            sent["Authorization"] = f"Bearer {token}"
        connection.request(method, path, body=body, headers=sent)
        response = connection.getresponse()
        return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()

    def post(self, payload: Any, token: Optional[str] = TOKEN) -> tuple[int, dict[str, Any]]:
        with contextlib.redirect_stdout(io.StringIO()):
            status, _, body = self.request("POST", "/api/snapshot", json.dumps(payload).encode(), token)
        return status, json.loads(body)


class ServerTests(ServerTestCase):
    def test_the_page_the_health_check_and_the_view_are_served_to_everyone(self) -> None:
        status, headers, body = self.request("GET", "/", token=None)
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["content-type"])
        self.assertIn(b"GHAADD", body)
        self.assertIn("default-src 'none'", headers["content-security-policy"])
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(self.request("GET", "/healthz", token=None)[2], b"ok")
        status, _, body = self.request("GET", "/api/view", token=None)
        self.assertEqual((status, json.loads(body)["daemons"]), (200, []))

    def test_a_snapshot_with_the_token_shows_up_in_the_view(self) -> None:
        self.assertEqual(self.post(message()), (200, {"ok": True, "need_snapshot": False}))
        daemons = json.loads(self.request("GET", "/api/view", token=None)[2])["daemons"]
        self.assertEqual([(d["name"], d["state"]) for d in daemons], [("home-pc", "live")])

    def test_without_or_with_a_wrong_token_nothing_is_accepted(self) -> None:
        for token in (None, "", "wrong", TOKEN[:-1], TOKEN + "x"):
            status, answer = self.post(message(), token=token)
            self.assertEqual(status, 401, token)
            self.assertFalse(answer["ok"])
        with contextlib.redirect_stdout(io.StringIO()):
            status, _, _ = self.request("POST", "/api/snapshot", b"{}", token=None, headers={"Authorization": f"Basic {TOKEN}"})
        self.assertEqual(status, 401)
        self.assertEqual(self.store.view()["daemons"], [])

    def test_a_rejection_always_reaches_the_sender_even_with_a_big_body(self) -> None:
        # The server used to answer 401 and close without reading the body; on Windows the connection was then reset
        # and the sender saw "connection aborted" instead of the answer, now and then. Many tries make that visible.
        big = {**message(), "padding": "x" * 300_000}
        for _ in range(40):
            status, answer = self.post(big, token="wrong-token")
            self.assertEqual(status, 401)
            self.assertFalse(answer["ok"])
        for method, path in (("PUT", "/api/view"), ("POST", "/nothing"), ("DELETE", "/api/snapshot")):
            for _ in range(10):
                with contextlib.redirect_stdout(io.StringIO()):
                    status, _, _ = self.request(method, path, b"y" * 200_000)
                self.assertIn(status, (404, 405), (method, path))

    def test_a_second_token_works_too(self) -> None:
        other = "second-token-" + "z" * 16
        server = ghaadd_viewer.make_server("127.0.0.1", 0, Store(), [TOKEN, other])
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        self.addCleanup(connection.close)
        with contextlib.redirect_stdout(io.StringIO()):
            connection.request("POST", "/api/snapshot", json.dumps(message()).encode(), {"Authorization": f"Bearer {other}"})
            self.assertEqual(connection.getresponse().status, 200)

    def test_a_ping_needs_the_token_too(self) -> None:
        self.assertEqual(self.post({"schema": 1, "type": "ping"}), (200, {"ok": True, "need_snapshot": False}))
        self.assertEqual(self.post({"schema": 1, "type": "ping"}, token="wrong")[0], 401)

    def test_a_schema_it_does_not_know_is_a_clear_400(self) -> None:
        status, answer = self.post({**message(), "schema": 99})
        self.assertEqual(status, 400)
        self.assertIn("unsupported message format", answer["error"])

    def test_broken_bodies_are_refused(self) -> None:
        for body in (b"not json", b"\xff\xfe", b"[1, 2]", b""):
            with contextlib.redirect_stdout(io.StringIO()):
                status, _, _ = self.request("POST", "/api/snapshot", body)
            self.assertEqual(status, 400, body)

    def test_a_huge_body_is_refused_before_it_is_read(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(connection.close)
        connection.putrequest("POST", "/api/snapshot")
        connection.putheader("Authorization", f"Bearer {TOKEN}")
        connection.putheader("Content-Length", str(ghaadd_viewer.MAX_BODY_BYTES + 1))
        connection.endheaders()
        self.assertEqual(connection.getresponse().status, 413)

    def test_a_missing_length_is_refused(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(connection.close)
        connection.putrequest("POST", "/api/snapshot")
        connection.putheader("Authorization", f"Bearer {TOKEN}")
        connection.endheaders()
        self.assertEqual(connection.getresponse().status, 411)

    def test_nothing_else_can_be_done(self) -> None:
        for method in ("PUT", "DELETE", "PATCH"):
            self.assertEqual(self.request(method, "/api/view")[0], 405, method)
        self.assertEqual(self.request("POST", "/api/view", b"{}")[0], 404)
        self.assertEqual(self.request("GET", "/api/snapshot", token=None)[0], 404)
        self.assertEqual(self.request("GET", "/nothing", token=None)[0], 404)
        self.assertEqual(self.request("GET", "/../../etc/passwd", token=None)[0], 404)
        self.assertEqual(self.store.view()["daemons"], [])

    def test_there_is_no_way_to_control_the_daemon_from_here(self) -> None:
        # The answers to a daemon only ever say "ok" and whether a full snapshot is wanted.
        status, answer = self.post(message())
        self.assertEqual(set(answer), {"ok", "need_snapshot"})


class PageSafetyTests(unittest.TestCase):
    def test_the_page_never_builds_html_from_received_text(self) -> None:
        # Everything that comes from the network is shown with textContent; these would let it become markup.
        for risky in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(risky, ghaadd_viewer.PAGE, risky)

    def test_the_page_names_the_connection_problems(self) -> None:
        for text in ("no data received", "daemon stopped", "Cannot reach the viewer", "No daemon has connected yet"):
            self.assertIn(text, ghaadd_viewer.PAGE)


TOKEN_A = "token-for-home-pc-" + "a" * 10
TOKEN_B = "token-for-the-nas-" + "b" * 10
TOKEN_ANY = "token-for-anyone-" + "c" * 10
UNKNOWN = "not-a-known-token-at-all"


class TokenParsingTests(unittest.TestCase):
    def test_items_are_split_at_commas_semicolons_and_line_breaks(self) -> None:
        self.assertEqual(ghaadd_viewer.split_items("a,b;c\nd\r\n e ;; ,f"), ["a", "b", "c", "d", "e", "f"])
        self.assertEqual(ghaadd_viewer.split_items(" , ;\n"), [])

    def test_a_plain_token_is_for_any_name_and_name_equals_token_is_tied_to_one(self) -> None:
        entries, problems = ghaadd_viewer.parse_entries([TOKEN_ANY, f"Home-PC={TOKEN_A}", f"*={TOKEN_B}"])
        self.assertEqual(problems, [])
        self.assertEqual([(e.name, e.token.decode()) for e in entries], [("*", TOKEN_ANY), ("home-pc", TOKEN_A), ("*", TOKEN_B)])

    def test_problems_are_described_without_ever_showing_a_token(self) -> None:
        entries, problems = ghaadd_viewer.parse_entries(
            ["short", "nas=tiny", "=" + TOKEN_A, f"two words={TOKEN_A}", "has space in it but long enough"]
        )
        self.assertEqual(entries, [])
        self.assertEqual(len(problems), 5)
        for problem in problems:
            self.assertNotIn(TOKEN_A, problem)
            self.assertNotIn("tiny", problem)
        self.assertIn("'nas'", problems[1])

    def test_the_env_file_reader_understands_the_usual_forms(self) -> None:
        key = ghaadd_viewer.TOKENS_KEY
        cases = {
            f"{key}=abc": "abc",
            f"  {key} = abc  ": "abc",
            f"export {key}=abc": "abc",
            f'{key}="a=1; b=2"': "a=1; b=2",
            f"{key}='x y'": "x y",
            f"# comment\nOTHER=1\n{key}=abc # trailing": "abc",
            f"{key}=old\n{key}=new": "new",  # the last assignment wins, as in any .env
            f'{key}="first\nsecond\nthird"\nOTHER=1': "first\nsecond\nthird",  # a quoted value over several lines
            f"#{key}=commented": None,
            f"X{key}=no\n{key}X=no": None,
            "": None,
        }
        for text, expected in cases.items():
            self.assertEqual(ghaadd_viewer.read_env_value(text, key), expected, text)

    def test_tokens_from_the_command_line_and_the_environment_are_merged_without_duplicates(self) -> None:
        self.assertEqual(
            ghaadd_viewer.collect_tokens([f"{TOKEN_A};{TOKEN_B}", TOKEN_A], f"{TOKEN_B}\nnas={TOKEN_ANY}"),
            [TOKEN_A, TOKEN_B, f"nas={TOKEN_ANY}"],
        )


class TokenBookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = os.path.join(folder.name, ".env")
        self.stamp = 1_700_000_000_000_000_000

    def write(self, text: str) -> None:
        """Write the file with a new modification time, so a change is always visible to the signature check."""
        with open(self.path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        self.stamp += 10_000_000_000
        os.utime(self.path, ns=(self.stamp, self.stamp))

    def line(self, value: str) -> str:
        return f"{ghaadd_viewer.TOKENS_KEY}={value}\n"

    def book(self, *static: str) -> ghaadd_viewer.TokenBook:
        with contextlib.redirect_stdout(io.StringIO()):
            return ghaadd_viewer.TokenBook(static, env_file=self.path, clock=self.clock)

    def check(self, book: ghaadd_viewer.TokenBook, token: str, name: str = "") -> str:
        with contextlib.redirect_stdout(io.StringIO()):
            return book.check(token, name)

    def test_ok_unknown_and_wrong_name(self) -> None:
        book = ghaadd_viewer.TokenBook([f"home-pc={TOKEN_A}", TOKEN_ANY])
        self.assertEqual(book.check(TOKEN_A, "home-pc"), "ok")
        self.assertEqual(book.check(TOKEN_A, "HOME-PC"), "ok")  # names are not case sensitive
        self.assertEqual(book.check(TOKEN_A, "nas"), "wrong_name")  # a known token, but it belongs to another daemon
        self.assertEqual(book.check(TOKEN_ANY, "nas"), "ok")  # a plain token works for any name
        self.assertEqual(book.check(TOKEN_B, "nas"), "unknown")
        self.assertEqual(book.check("", "nas"), "unknown")
        self.assertEqual(book.check(TOKEN_A, ""), "ok")  # with no name given only the token can be judged (a ping)

    def test_a_token_in_two_entries_is_ok_when_either_fits(self) -> None:
        book = ghaadd_viewer.TokenBook([f"a={TOKEN_A}", f"b={TOKEN_A}"])
        self.assertEqual([book.check(TOKEN_A, n) for n in ("a", "b", "c")], ["ok", "ok", "wrong_name"])

    def test_a_file_that_is_not_there_is_just_no_extra_tokens(self) -> None:
        book = self.book(TOKEN_A)
        self.assertEqual(self.check(book, TOKEN_A), "ok")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.clock.now += 100
            self.assertEqual(book.check(TOKEN_A), "ok")
        self.assertEqual(out.getvalue(), "")  # and no warning: the file never existed

    def test_tokens_are_taken_from_the_file_at_start(self) -> None:
        self.write(self.line(f"home-pc={TOKEN_A}; {TOKEN_B}"))
        book = self.book()
        self.assertEqual((self.check(book, TOKEN_A, "home-pc"), self.check(book, TOKEN_B, "x")), ("ok", "ok"))
        self.assertEqual(book.problems, [])
        self.assertIn("2 token(s), 1 tied to a daemon name", book.summary())

    def test_a_token_added_to_the_file_is_accepted_without_a_restart_and_a_removed_one_is_not(self) -> None:
        self.write(self.line(TOKEN_A))
        book = self.book()
        self.assertEqual(self.check(book, TOKEN_B), "unknown")
        self.write(self.line(f"{TOKEN_A};nas={TOKEN_B}"))
        self.assertEqual(self.check(book, TOKEN_B, "nas"), "unknown")  # the file is looked at only every couple of seconds
        self.clock.now += ghaadd_viewer.TOKEN_FILE_CHECK_SECONDS
        self.assertEqual(self.check(book, TOKEN_B, "nas"), "ok")
        self.write(self.line(f"nas={TOKEN_B}"))  # the first token is revoked
        self.clock.now += ghaadd_viewer.TOKEN_FILE_CHECK_SECONDS
        self.assertEqual((self.check(book, TOKEN_A), self.check(book, TOKEN_B, "nas")), ("unknown", "ok"))

    def test_the_changes_are_reported_on_the_console_without_the_tokens(self) -> None:
        self.write(self.line(TOKEN_A))
        book = self.book()
        self.write(self.line(TOKEN_B))
        self.clock.now += 5
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            book.check(TOKEN_A)
        self.assertIn("1 added, 1 removed", out.getvalue())
        self.assertNotIn(TOKEN_A, out.getvalue())
        self.assertNotIn(TOKEN_B, out.getvalue())

    def test_a_file_replaced_by_a_new_one_is_followed(self) -> None:
        # What an editor that saves safely does: a new file is renamed over the old one.
        self.write(self.line(TOKEN_A))
        book = self.book()
        replacement = self.path + ".new"
        with open(replacement, "w", encoding="utf-8") as handle:
            handle.write(self.line(TOKEN_B))
        os.replace(replacement, self.path)
        self.clock.now += 5
        self.assertEqual((self.check(book, TOKEN_A), self.check(book, TOKEN_B)), ("unknown", "ok"))

    def test_an_emptied_token_line_revokes_every_file_token_but_not_the_ones_given_at_start(self) -> None:
        self.write(self.line(TOKEN_A))
        book = self.book(TOKEN_ANY)
        self.write("OTHER=1\n")
        self.clock.now += 5
        self.assertEqual((self.check(book, TOKEN_A), self.check(book, TOKEN_ANY)), ("unknown", "ok"))

    def test_a_file_that_disappears_keeps_the_tokens_known_so_far(self) -> None:
        self.write(self.line(TOKEN_A))
        book = self.book()
        os.remove(self.path)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.clock.now += 5
            self.assertEqual(book.check(TOKEN_A), "ok")
            self.clock.now += 5
            self.assertEqual(book.check(TOKEN_A), "ok")
        self.assertEqual(out.getvalue().count("cannot be read"), 1)  # said once, not at every look
        self.write(self.line(TOKEN_B))  # and when it comes back it is followed again
        self.clock.now += 5
        self.assertEqual((self.check(book, TOKEN_A), self.check(book, TOKEN_B)), ("unknown", "ok"))

    def test_bad_entries_in_the_file_are_reported_and_skipped_without_losing_the_good_ones(self) -> None:
        self.write(self.line(f"{TOKEN_A};short;nas={TOKEN_B}"))
        book = self.book()
        self.assertEqual(len(book.problems), 1)
        self.assertNotIn("short", book.problems[0])
        self.assertEqual((self.check(book, TOKEN_A), self.check(book, TOKEN_B, "nas")), ("ok", "ok"))
        self.write(self.line(TOKEN_A))  # fixed: the problem is gone
        self.clock.now += 5
        self.check(book, TOKEN_A)
        self.assertEqual(book.problems, [])

    def test_start_up_problems_come_from_the_given_tokens_too(self) -> None:
        book = ghaadd_viewer.TokenBook(["short", TOKEN_A])
        self.assertEqual(len(book.problems), 1)
        self.assertEqual(book.check(TOKEN_A), "ok")


class RejectedListTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.store = Store(lost_after=45.0, clock=self.clock)

    def note(self, name: str = "nas", address: str = "10.0.0.5", reason: str = "token not accepted") -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.store.note_rejection(name, address, reason)
        return out.getvalue()

    def test_a_rejected_sender_is_listed_with_how_often_and_how_long_ago(self) -> None:
        self.note()
        self.clock.now += 7
        self.note()
        self.clock.now += 3
        (item,) = self.store.view()["rejected"]
        self.assertEqual(
            (item["name"], item["address"], item["reason"], item["count"], item["age"]),
            ("nas", "10.0.0.5", "token not accepted", 2, 3.0),
        )

    def test_the_console_is_told_once_per_sender_and_reason(self) -> None:
        self.assertIn("Rejected 'nas' from 10.0.0.5: token not accepted", self.note())
        self.assertEqual(self.note(), "")
        self.assertIn("not allowed for the name", self.note(reason="this token is not allowed for the name 'nas'"))

    def test_the_newest_comes_first_and_the_list_is_capped(self) -> None:
        for number in range(ghaadd_viewer.MAX_REJECTED + 5):
            self.clock.now += 1
            self.note(name=f"d{number}")
        names = [item["name"] for item in self.store.view()["rejected"]]
        self.assertEqual(len(names), ghaadd_viewer.MAX_REJECTED)
        self.assertEqual(names[0], f"d{ghaadd_viewer.MAX_REJECTED + 4}")
        self.assertNotIn("d0", names)  # the one that had been quiet longest was forgotten

    def test_a_sender_that_stopped_trying_is_forgotten_after_an_hour(self) -> None:
        self.note()
        self.clock.now += ghaadd_viewer.REJECTED_SHOWN_SECONDS - 1
        self.assertEqual(len(self.store.view()["rejected"]), 1)
        self.clock.now += 2
        self.assertEqual(self.store.view()["rejected"], [])

    def test_a_daemon_that_gets_through_is_no_longer_listed_as_rejected(self) -> None:
        self.note(name="home-pc")
        self.note(name="nas")
        quiet(self.store, message(name="home-pc"))
        self.assertEqual([item["name"] for item in self.store.view()["rejected"]], ["nas"])

    def test_a_ping_does_not_clear_anything(self) -> None:
        self.note(name="nas")
        quiet(self.store, {"schema": 1, "type": "ping", "name": "nas"})
        self.assertEqual(len(self.store.view()["rejected"]), 1)

    def test_what_a_sender_claims_is_cleaned_before_it_is_shown(self) -> None:
        self.note(name="ev\x00il\nname" + "z" * 100, address="1.2.3.4\x07")
        (item,) = self.store.view()["rejected"]
        self.assertTrue(item["name"].startswith("evilname"))
        self.assertEqual(len(item["name"]), 64)
        self.assertEqual(item["address"], "1.2.3.4")
        self.note(name="   ")
        self.assertIn("(no name)", [i["name"] for i in self.store.view()["rejected"]])

    def test_a_rejected_sender_is_never_a_daemon(self) -> None:
        self.note(name="nas")
        self.assertEqual(self.store.view()["daemons"], [])


class NamedTokenServerTests(ServerTestCase):
    def tokens(self) -> Any:
        return [f"home-pc={TOKEN_A}", f"nas={TOKEN_B}", TOKEN_ANY]

    def rejected(self) -> list[dict[str, Any]]:
        return json.loads(self.request("GET", "/api/view", token=None)[2])["rejected"]

    def test_each_token_works_for_its_own_daemon_and_the_plain_one_for_all(self) -> None:
        self.assertEqual(self.post(message(name="home-pc"), token=TOKEN_A)[0], 200)
        self.assertEqual(self.post(message(name="nas"), token=TOKEN_B)[0], 200)
        self.assertEqual(self.post(message(name="laptop"), token=TOKEN_ANY)[0], 200)
        self.assertEqual(sorted(d["name"] for d in self.store.view()["daemons"]), ["home-pc", "laptop", "nas"])
        self.assertEqual(self.rejected(), [])

    def test_a_token_for_another_name_is_refused_with_403_and_listed(self) -> None:
        status, answer = self.post(message(name="nas"), token=TOKEN_A)  # home-pc's token, claiming to be the nas
        self.assertEqual(status, 403)
        self.assertFalse(answer["ok"])
        self.assertEqual(self.store.view()["daemons"], [])  # and the real nas cannot be overwritten
        (item,) = self.rejected()
        self.assertEqual((item["name"], item["address"], item["count"]), ("nas", "127.0.0.1", 1))
        self.assertIn("not allowed for the name 'nas'", item["reason"])

    def test_an_unknown_token_is_refused_with_401_and_listed_under_the_name_it_claimed(self) -> None:
        for _ in range(3):
            self.assertEqual(self.post(message(name="nas"), token=UNKNOWN)[0], 401)
        (item,) = self.rejected()
        self.assertEqual((item["name"], item["reason"], item["count"]), ("nas", "token not accepted", 3))

    def test_a_removed_token_shows_up_as_rejected(self) -> None:
        self.assertEqual(self.post(message(name="nas"), token=TOKEN_B)[0], 200)
        # What a changed token file does: the entry is gone from the list the server checks.
        self.book._static = [entry for entry in self.book._static if entry.token.decode() != TOKEN_B]
        self.assertEqual(self.post(message("heartbeat", name="nas"), token=TOKEN_B)[0], 401)
        self.assertEqual([item["name"] for item in self.rejected()], ["nas"])

    def test_a_daemon_that_is_accepted_again_leaves_the_rejected_list(self) -> None:
        self.post(message(name="nas"), token=UNKNOWN)
        self.assertEqual(len(self.rejected()), 1)
        self.post(message(name="nas"), token=TOKEN_B)
        self.assertEqual(self.rejected(), [])

    def test_a_ping_checks_the_token_against_the_name_when_one_is_sent(self) -> None:
        self.assertEqual(self.post({"schema": 1, "type": "ping", "name": "nas"}, token=TOKEN_B)[0], 200)
        self.assertEqual(self.post({"schema": 1, "type": "ping", "name": "home-pc"}, token=TOKEN_B)[0], 403)
        self.assertEqual(self.post({"schema": 1, "type": "ping"}, token=TOKEN_B)[0], 200)  # no name: only the token is judged
        self.assertEqual(self.store.view()["daemons"], [])

    def test_the_token_is_judged_before_the_body(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.request("POST", "/api/snapshot", b"not json", token=UNKNOWN)[0], 401)
            self.assertEqual(self.request("POST", "/api/snapshot", b"not json", token=TOKEN_B)[0], 400)
            self.assertEqual(self.request("POST", "/api/snapshot", b"[1]", token=TOKEN_ANY)[0], 400)

    def test_the_page_has_a_place_for_the_rejected_senders(self) -> None:
        self.assertIn(b"Rejected connections", self.request("GET", "/", token=None)[2])


class IconTests(ServerTestCase):
    def icon_bytes(self, name: str) -> bytes:
        with open(os.path.join(VIEWER_DIR, "assets", name), "rb") as handle:
            return handle.read()

    def test_the_favicon_and_the_png_are_served_with_the_right_type_and_may_be_cached(self) -> None:
        for path, name, content_type in (("/favicon.ico", "ghaadd.ico", "image/x-icon"), ("/icon.png", "ghaadd.png", "image/png")):
            status, headers, body = self.request("GET", path, token=None)
            self.assertEqual(status, 200, path)
            self.assertEqual(headers["content-type"], content_type)
            self.assertEqual(body, self.icon_bytes(name))
            self.assertEqual(headers["cache-control"], f"public, max-age={ghaadd_viewer.ICON_CACHE_SECONDS}")
            self.assertEqual(headers["x-content-type-options"], "nosniff")

    def test_head_gives_the_headers_without_the_body(self) -> None:
        status, headers, body = self.request("HEAD", "/favicon.ico", token=None)
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(headers["content-length"], str(len(self.icon_bytes("ghaadd.ico"))))

    def test_everything_else_is_still_never_cached(self) -> None:
        for path in ("/", "/api/view", "/healthz"):
            self.assertEqual(self.request("GET", path, token=None)[1]["cache-control"], "no-store", path)

    def test_the_page_may_load_its_own_images_and_nothing_else(self) -> None:
        policy = self.request("GET", "/", token=None)[1]["content-security-policy"]
        self.assertIn("img-src 'self'", policy)
        self.assertIn("default-src 'none'", policy)

    def test_the_page_links_the_icons(self) -> None:
        page = self.request("GET", "/", token=None)[2].decode("utf-8")
        for link in ('href="/favicon.ico"', 'href="/icon.png"'):
            self.assertIn(link, page)

    def test_without_the_assets_folder_there_is_just_no_icon_and_the_viewer_goes_on(self) -> None:
        with tempfile.TemporaryDirectory() as empty, mock.patch.object(ghaadd_viewer, "assets_dir", lambda: empty):
            self.assertEqual(self.request("GET", "/favicon.ico", token=None)[0], 404)
            self.assertEqual(self.request("GET", "/icon.png", token=None)[0], 404)
            self.assertEqual(self.request("GET", "/healthz", token=None)[2], b"ok")

    def test_only_the_two_icons_can_be_fetched_no_other_file(self) -> None:
        for path in (
            "/assets/ghaadd.ico", "/assets/", "/config/.env", "/config/.env.example", "/ghaadd_viewer.py",
            "/favicon.ico/../config/.env", "/../config/.env", "/icon.png/x", "/FAVICON.ICO",
        ):
            self.assertEqual(self.request("GET", path, token=None)[0], 404, path)


class ViewerFolderTests(unittest.TestCase):
    """viewer/ must stay copyable to another machine and must never put the app's own files into the image."""

    folder = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "viewer")

    def read(self, name: str) -> str:
        with open(os.path.join(self.folder, name), encoding="utf-8") as handle:
            return handle.read()

    def test_the_viewer_imports_nothing_from_the_app(self) -> None:
        import ast

        imported = set()
        for node in ast.walk(ast.parse(self.read("ghaadd_viewer.py"))):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn("modules", imported)
        self.assertNotIn("requests", imported)  # standard library only: nothing to pip install on the server
        self.assertLessEqual(imported - set(sys.stdlib_module_names), set())

    def test_the_image_gets_only_the_viewer(self) -> None:
        dockerfile = self.read("Dockerfile")
        self.assertEqual([line for line in dockerfile.splitlines() if line.startswith(("COPY", "ADD"))],
                         ["COPY ghaadd_viewer.py /app/ghaadd_viewer.py"])
        ignore = [line for line in self.read(".dockerignore").splitlines() if line and not line.startswith("#")]
        self.assertEqual(ignore, ["*", "!ghaadd_viewer.py"])  # everything else is excluded from the build context
        self.assertIn("USER viewer", dockerfile)  # not root
        self.assertIn("build: .", self.read("docker-compose.yml"))  # this folder is the context, not the repo root

    def test_the_compose_file_mounts_the_token_folder_where_the_viewer_looks_read_only(self) -> None:
        compose = self.read("docker-compose.yml")
        # The whole folder, not the single file: an editor that saves by replacing the file would be missed by a
        # single-file mount (it follows the file's identity). /app/config is where the script looks by itself.
        self.assertIn("./config:/app/config:ro", compose)
        self.assertNotIn("ENV_FILE", compose)  # there is no way to point the viewer elsewhere, so none is set
        self.assertNotIn(".env:/", compose)
        self.assertEqual(os.path.dirname(ghaadd_viewer.default_env_file()), os.path.join(os.path.dirname(os.path.abspath(ghaadd_viewer.__file__)), "config"))

    def test_the_compose_file_builds_the_local_image_and_says_to_use_build(self) -> None:
        compose = self.read("docker-compose.yml")
        self.assertIn("    build: .", compose)
        self.assertIn("    image: ghaadd-viewer:latest", compose)
        # Without --build, a name that is not on the computer yet is first looked for on Docker Hub (a harmless
        # "pull access denied" before the build); the file says how to avoid it, and so do the READMEs.
        self.assertIn("docker compose up -d --build", compose)
        self.assertIn("docker compose up -d --build", self.read("README.md"))

    def test_the_compose_file_mounts_the_icons_read_only_too(self) -> None:
        self.assertIn("./assets:/app/assets:ro", self.read("docker-compose.yml"))

    def test_the_icons_are_copies_of_the_ones_of_the_app(self) -> None:
        # The viewer folder must be copyable alone, so it holds its own copy: this fails when the app's icon changes
        # and the copy was forgotten (copy assets/ghaadd.ico and assets/ghaadd.png into viewer/assets/ again).
        root = os.path.dirname(VIEWER_DIR)
        for name in ("ghaadd.ico", "ghaadd.png"):
            with open(os.path.join(root, "assets", name), "rb") as original, open(os.path.join(VIEWER_DIR, "assets", name), "rb") as copy:
                self.assertEqual(copy.read(), original.read(), name)

    def test_the_example_token_file_is_there_and_holds_no_usable_token(self) -> None:
        text = self.read(os.path.join("config", ".env.example"))
        self.assertIn(ghaadd_viewer.TOKENS_KEY, text)
        value = ghaadd_viewer.read_env_value(text, ghaadd_viewer.TOKENS_KEY) or ""
        entries, _ = ghaadd_viewer.parse_entries(ghaadd_viewer.split_items(value))
        self.assertEqual(entries, [])  # copying it unchanged must never give anybody access


class StartUpTests(unittest.TestCase):
    def test_tokens_come_from_the_command_line_and_the_environment_without_duplicates(self) -> None:
        args = ghaadd_viewer.parse_args(
            ["--token", " a" * 1 + "b" * 20, "--token", "c" * 20],
            {"GHAADD_VIEWER_TOKENS": "c" * 20 + ", " + "d" * 20 + ",,", "GHAADD_VIEWER_PORT": "9000"},
        )
        self.assertEqual(args.tokens, ["a" + "b" * 20, "c" * 20, "d" * 20])
        self.assertEqual((args.port, args.host, args.lost_after), (9000, "0.0.0.0", 45.0))

    def test_defaults(self) -> None:
        args = ghaadd_viewer.parse_args([], {})
        self.assertEqual((args.port, args.host, args.lost_after, args.tokens), (8888, "0.0.0.0", 45.0, []))

    def test_the_one_token_file_is_config_env_beside_the_script_wherever_it_is_started_from(self) -> None:
        here = os.path.dirname(os.path.abspath(ghaadd_viewer.__file__))
        self.assertEqual(ghaadd_viewer.default_env_file(), os.path.join(here, "config", ".env"))

    def test_there_is_no_way_to_point_the_viewer_at_another_token_file(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            ghaadd_viewer.parse_args(["--env-file", "x.env"], {})
        self.assertIn("--env-file", err.getvalue())  # argparse: unrecognized argument
        args = ghaadd_viewer.parse_args([], {"GHAADD_VIEWER_ENV_FILE": "/elsewhere/.env"})
        self.assertFalse(hasattr(args, "env_file"))  # and the variable of that name is simply not read

    def run_main(self, env_file: str, *argv: str) -> tuple[int, str]:
        """Run main() with the token file pointed at one of our own, so a real config/.env cannot interfere."""
        err = io.StringIO()
        with mock.patch.object(ghaadd_viewer, "default_env_file", lambda: env_file),                 contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            return ghaadd_viewer.main(list(argv)), err.getvalue()

    def test_it_refuses_to_start_without_a_usable_token(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            none = os.path.join(folder, "no-such.env")
            status, text = self.run_main(none)
            self.assertEqual(status, 2)
            self.assertIn("No token given", text)
            self.assertIn(none, text)  # it says which file it looked in
            status, text = self.run_main(none, "--token", "short-secret")
            self.assertEqual(status, 2)
            self.assertIn("Token problem", text)
            self.assertNotIn("short-secret", text)  # never the token itself

    def test_the_placeholders_of_the_example_file_stop_the_viewer_from_starting(self) -> None:
        status, text = self.run_main(os.path.join(VIEWER_DIR, "config", ".env.example"))
        self.assertEqual(status, 2)
        self.assertIn("Token problem", text)
        self.assertIn("'home-pc'", text)

    def test_a_good_token_file_alone_is_enough_to_start(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, ".env")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(f"{ghaadd_viewer.TOKENS_KEY}=nas={TOKEN_B}\n")
            taken = ghaadd_viewer.make_server("127.0.0.1", 0, Store(), [TOKEN])  # a taken port: main stops after the token check
            self.addCleanup(taken.server_close)
            status, text = self.run_main(path, "--host", "127.0.0.1", "--port", str(taken.server_address[1]))
            self.assertEqual(status, 1)  # it got as far as opening the port, so the tokens were accepted
            self.assertIn("Cannot listen", text)

    def test_a_port_that_is_taken_is_reported(self) -> None:
        taken = ghaadd_viewer.make_server("127.0.0.1", 0, Store(), [TOKEN])
        self.addCleanup(taken.server_close)
        with tempfile.TemporaryDirectory() as folder:
            status, text = self.run_main(
                os.path.join(folder, "none.env"), "--token", TOKEN, "--host", "127.0.0.1", "--port", str(taken.server_address[1]),
            )
        self.assertEqual(status, 1)
        self.assertIn("Cannot listen", text)


if __name__ == "__main__":
    unittest.main()
