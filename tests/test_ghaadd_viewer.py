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
import threading
import unittest
from typing import Any, Optional

# The viewer is a standalone file in viewer/ (it is not part of the modules package).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "viewer"))

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
    def setUp(self) -> None:
        self.store = Store(lost_after=45.0)
        self.server = ghaadd_viewer.make_server("127.0.0.1", 0, self.store, [TOKEN])
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

    def test_the_compose_file_demands_a_token(self) -> None:
        self.assertIn("GHAADD_VIEWER_TOKENS: ${GHAADD_VIEWER_TOKENS:?", self.read("docker-compose.yml"))


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

    def test_it_refuses_to_start_without_a_usable_token(self) -> None:
        for argv in ([], ["--token", "short"]):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(ghaadd_viewer.main(argv), 2, argv)
            self.assertTrue(err.getvalue())

    def test_a_port_that_is_taken_is_reported(self) -> None:
        taken = ghaadd_viewer.make_server("127.0.0.1", 0, Store(), [TOKEN])
        self.addCleanup(taken.server_close)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            status = ghaadd_viewer.main(["--token", TOKEN, "--host", "127.0.0.1", "--port", str(taken.server_address[1])])
        self.assertEqual(status, 1)
        self.assertIn("Cannot listen", err.getvalue())


if __name__ == "__main__":
    unittest.main()
