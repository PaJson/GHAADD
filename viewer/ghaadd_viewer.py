#!/usr/bin/env python3
"""GHAADD web viewer: a passive page that shows what a GHAADD daemon pushes to it (standalone, standard library only).

The daemon connects OUT to this viewer (``viewer.url`` and ``viewer.token`` in its config.json) and sends read-only
snapshots of what its GUI shows; this program keeps only the latest one per daemon, in memory, and serves a page
that refreshes by itself. It cannot control or even reach the daemon, and it shows no stale data: a daemon that
stops sending (or says goodbye) is shown as lost (or stopped), never with its old numbers.

    python ghaadd_viewer.py --token SECRET [--port 8888] [--host 0.0.0.0] [--lost-after 45]

Tokens: a token is either plain (any daemon may use it) or ``name=token`` (only the daemon with that name may). They can
be given with --token, in GHAADD_VIEWER_TOKENS (separated by comma, semicolon or new line) and in the file
``config/.env`` beside this script: the only file it reads, re-read when it changes, so add or remove a token there and
the viewer follows, no restart. (The Docker setup mounts its config folder at /app/config.) Other settings:
GHAADD_VIEWER_PORT, GHAADD_VIEWER_HOST, GHAADD_VIEWER_LOST_AFTER (that is how the Docker setup passes them).
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterable, Optional, Union

VIEWER_VERSION = "1.1"
SCHEMA = 1  # the daemon's message format this viewer understands
DEFAULT_PORT = 8888
DEFAULT_HOST = "0.0.0.0"
DEFAULT_LOST_AFTER_SECONDS = 45.0  # about three missed 15 s heartbeats
MIN_TOKEN_LENGTH = 16
MAX_BODY_BYTES = 5 * 1024 * 1024
MAX_DAEMONS = 20
MAX_REJECTED = 20  # distinct rejected senders remembered for the page
REJECTED_SHOWN_SECONDS = 3600.0  # a rejected sender that has not tried again for this long is forgotten
MAX_ROWS = 5000
MAX_TEXT = 2000
TOKENS_KEY = "GHAADD_VIEWER_TOKENS"
TOKEN_FILE_CHECK_SECONDS = 2.0  # how often the token file is looked at (a change is picked up within this time)
ANY_NAME = "*"  # a plain token is the same as "*=token"
# The icon files in the assets folder beside this script, by the address they are served on. Fixed names only:
# nothing a request says is ever used as a file name.
ICONS = {"/favicon.ico": ("ghaadd.ico", "image/x-icon"), "/icon.png": ("ghaadd.png", "image/png")}
ICON_CACHE_SECONDS = 86400

EVENT_TABS = ("warnings", "completed", "limits")
REPO_TEXT_FIELDS = ("repo", "folder", "status", "tag", "last_check", "step", "next_check", "files", "limit")
EVENT_FIELDS = ("time", "repo", "kind", "message")
UNMAPPED_FIELDS = ("repo", "folder", "time")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


def _text(value: Any) -> str:
    """Return a value as a short plain string (None becomes empty): whatever arrives is never trusted to be small."""
    return "" if value is None else str(value)[:MAX_TEXT]


def _number(value: Any) -> Optional[float]:
    """Return a finite number, or None when the value is missing or not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value == value and abs(value) != float("inf") else None


def _rows(value: Any, fields: tuple[str, ...]) -> list[dict[str, Any]]:
    """Keep only the known fields of the first MAX_ROWS rows of a list; anything else in it is dropped."""
    if not isinstance(value, list):
        raise ValueError("a list of rows was expected")
    return [{field: _text(row.get(field)) for field in fields} for row in value[:MAX_ROWS] if isinstance(row, dict)]


def clean_data(raw: Any) -> dict[str, Any]:
    """Validate a snapshot's data and rebuild it from the known fields only; raises ValueError when it is unusable."""
    if not isinstance(raw, dict) or not isinstance(raw.get("status"), dict) or not isinstance(raw.get("tabs"), dict):
        raise ValueError("the snapshot has no status or tabs")
    status = raw["status"]
    if not isinstance(raw.get("repos"), list):
        raise ValueError("a list of repositories was expected")
    repos: list[dict[str, Any]] = []
    for row in raw["repos"][:MAX_ROWS]:
        if isinstance(row, dict):
            repo: dict[str, Any] = {field: _text(row.get(field)) for field in REPO_TEXT_FIELDS}
            repo["limit_warning"] = row.get("limit_warning") is True
            repos.append(repo)
    tabs = {key: _rows(raw["tabs"].get(key, []), EVENT_FIELDS) for key in EVENT_TABS}
    tabs["unmapped"] = _rows(raw["tabs"].get("unmapped", []), UNMAPPED_FIELDS)
    return {
        "status": {
            "paused": status.get("paused") is True,
            "idle": status.get("idle") is True,
            "next_poll_at": _number(status.get("next_poll_at")),
            "started_at": _number(status.get("started_at")),
            "progress": _text(status.get("progress")),
            "queue": _text(status.get("queue")),
            "pending": int(_number(status.get("pending")) or 0),
            "due": int(_number(status.get("due")) or 0),
        },
        "repos": repos,
        "tabs": tabs,
    }


@dataclass(frozen=True)
class TokenEntry:
    """One accepted token and the daemon name it is for (lower-cased; ANY_NAME = every name)."""
    name: str
    token: bytes


def split_items(text: str) -> list[str]:
    """Split a list of tokens at commas, semicolons and line breaks; empty items are dropped."""
    return [item.strip() for item in re.split(r"[,;\r\n]+", text) if item.strip()]


def parse_entries(items: Iterable[str]) -> tuple[list[TokenEntry], list[str]]:
    """Turn "token" / "name=token" items into entries; the second result lists what was wrong (never a token's text)."""
    entries: list[TokenEntry] = []
    problems: list[str] = []
    for item in items:
        name, separator, token = item.strip().partition("=")
        if not separator:
            name, token = ANY_NAME, item.strip()
        name, token = name.strip(), token.strip()
        if not name or re.search(r"\s", name):
            problems.append("an entry has no usable name before the '=' (use name=token, or just the token)")
        elif len(token) < MIN_TOKEN_LENGTH or re.search(r"\s", token):
            who = "a token" if name == ANY_NAME else f"the token for '{name}'"
            problems.append(f"{who} must be at least {MIN_TOKEN_LENGTH} characters without spaces")
        else:
            entries.append(TokenEntry(name.casefold() if name != ANY_NAME else ANY_NAME, token.encode("utf-8")))
    return entries, problems


def read_env_value(text: str, key: str) -> Optional[str]:
    """Return the value of `key` in the text of a .env file (the last assignment wins), or None when it is not there.

    Understands comments, an "export " prefix, quotes (also around a value that continues over several lines) and
    a trailing " # comment" after an unquoted value.
    """
    value: Optional[str] = None
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        index += 1
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, separator, rest = line.partition("=")
        if not separator or line.startswith("#") or name.strip() != key:
            continue
        rest = rest.strip()
        if rest[:1] in ("'", '"'):
            quote, body = rest[0], rest[1:]
            while quote not in body and index < len(lines):  # a quoted value may continue on the next lines
                body += "\n" + lines[index]
                index += 1
            end = body.find(quote)
            value = body if end < 0 else body[:end]
        else:
            comment = rest.find(" #")
            value = (rest if comment < 0 else rest[:comment]).strip()
    return value


class TokenBook:
    """The accepted tokens: those given at start plus those in a file that is re-read when it changes (thread-safe).

    A token can be tied to one daemon name; checks never stop at the first match, so timing says nothing.
    """

    def __init__(
        self, static: Iterable[str] = (), env_file: Optional[str] = None, clock: Callable[[], float] = time.monotonic
    ) -> None:
        """Parse the start-up items and, when there is a file, read it once; `clock` is injectable for the tests."""
        self._clock = clock
        self._lock = threading.Lock()
        self._static, self._static_problems = parse_entries(static)
        self._file_problems: list[str] = []
        self._file_entries: list[TokenEntry] = []
        self._env_file = env_file
        self._signature: Optional[tuple[int, int, int]] = None
        self._checked_at = float("-inf")
        self._file_warned = False
        if env_file:
            self._reload(first=True)

    def _read_file(self) -> Optional[list[TokenEntry]]:
        """The entries in the file (empty when it has no token setting), or None when it cannot be read right now."""
        assert self._env_file is not None
        try:
            with open(self._env_file, encoding="utf-8") as handle:
                text = handle.read()
        except (OSError, UnicodeDecodeError):
            return None
        entries, problems = parse_entries(split_items(read_env_value(text, TOKENS_KEY) or ""))
        for problem in problems:
            print(f"Token file {self._env_file}: {problem}.", flush=True)
        self._file_problems = problems  # only the latest reading counts: a fixed file has no problems any more
        return entries

    @property
    def problems(self) -> list[str]:
        """What is wrong with the tokens given at start and with the file as last read (never the tokens' text)."""
        return self._static_problems + self._file_problems

    def _reload(self, first: bool = False) -> None:
        """Read the file when it is new or changed; a file that cannot be read keeps the tokens from before."""
        assert self._env_file is not None
        try:
            stat = os.stat(self._env_file)
        except OSError:
            if first or self._signature is None:
                return  # there never was a file: the tokens given at start are all there is
            if not self._file_warned:
                self._file_warned = True
                print(f"Token file {self._env_file} cannot be read: keeping the tokens known so far.", flush=True)
            return
        signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        if signature == self._signature:
            return
        entries = self._read_file()
        if entries is None:
            return
        self._signature = signature
        self._file_warned = False
        before = {(entry.name, entry.token) for entry in self._file_entries}
        after = {(entry.name, entry.token) for entry in entries}
        self._file_entries = entries
        if not first:
            print(
                f"Token file re-read: {len(after)} token(s), {len(after - before)} added, {len(before - after)} removed.",
                flush=True,
            )

    def _refresh(self) -> None:
        """Look at the file now and then (not on every request)."""
        if not self._env_file:
            return
        now = self._clock()
        with self._lock:
            if now - self._checked_at < TOKEN_FILE_CHECK_SECONDS:
                return
            self._checked_at = now
            self._reload()

    def entries(self) -> list[TokenEntry]:
        """Every token accepted right now."""
        self._refresh()
        with self._lock:
            return self._static + self._file_entries

    def check(self, token: str, name: str = "") -> str:
        """Return "ok", "wrong_name" (a known token, but for another daemon) or "unknown".

        Without a name only the token itself can be judged, so a token tied to some name counts as fine.
        """
        given = token.encode("utf-8")
        wanted = name.strip().casefold()
        matched = allowed = False
        for entry in self.entries():
            if hmac.compare_digest(given, entry.token):
                matched = True
                if not wanted or entry.name in (ANY_NAME, wanted):
                    allowed = True
        return "ok" if allowed else "wrong_name" if matched else "unknown"

    def summary(self) -> str:
        """One line for the start-up message: how many tokens, how many tied to a name (never the tokens)."""
        entries = self.entries()
        tied = sum(1 for entry in entries if entry.name != ANY_NAME)
        file_note = f"; the file {self._env_file} is re-read when it changes" if self._env_file else ""
        return f"{len(entries)} token(s), {tied} tied to a daemon name{file_note}"


class _Daemon:
    """What the viewer remembers about one daemon: its latest snapshot and when it last made itself heard."""

    def __init__(self, version: str) -> None:
        """Start empty: no data, never heard."""
        self.version = version
        self.data: Optional[dict[str, Any]] = None
        self.sent_at: Optional[float] = None  # the daemon's own clock when it sent the snapshot
        self.snapshot_at = 0.0  # this viewer's clock when it arrived
        self.last_seen = 0.0
        self.stopped = False


class Store:
    """Latest snapshot per daemon name, with the rules for what is live, lost or stopped (thread-safe)."""

    def __init__(self, lost_after: float = DEFAULT_LOST_AFTER_SECONDS, clock: Callable[[], float] = time.monotonic) -> None:
        """Set up an empty store; `clock` is injectable so tests need no real time."""
        self.lost_after = lost_after
        self._clock = clock
        self._lock = threading.Lock()
        self._daemons: dict[str, _Daemon] = {}
        # Senders whose token was refused, by (claimed name, address): shown on the page, never mixed with real daemons.
        self._rejected: dict[tuple[str, str], dict[str, Any]] = {}

    def note_rejection(self, name: str, address: str, reason: str) -> None:
        """Remember that `name` at `address` was refused (the name is only what the sender claimed)."""
        name = _CONTROL_CHARACTERS.sub("", name).strip()[:64] or "(no name)"
        address = _CONTROL_CHARACTERS.sub("", address)[:64]
        now = self._clock()
        key = (name, address)
        with self._lock:
            record = self._rejected.get(key)
            if record is None or record["reason"] != reason:  # new, or refused for a different reason: say so once
                print(f"Rejected '{name}' from {address}: {reason}.", flush=True)
                if key not in self._rejected and len(self._rejected) >= MAX_REJECTED:
                    quietest = min(self._rejected, key=lambda other: self._rejected[other]["last"])
                    del self._rejected[quietest]
                record = {"name": name, "address": address, "reason": reason, "count": 0, "last": now}
                self._rejected[key] = record
            record["count"] += 1
            record["last"] = now

    def receive(self, message: Any) -> dict[str, Any]:
        """Take one message (snapshot, heartbeat or goodbye); return the answer for the daemon, or raise ValueError."""
        if not isinstance(message, dict):
            raise ValueError("the message is not an object")
        schema = message.get("schema")
        if schema != SCHEMA:
            raise ValueError(f"unsupported message format {schema!r} (this viewer reads {SCHEMA})")
        kind = message.get("type")
        if kind == "ping":  # "is the viewer there and is my token right?": answered, but nothing is remembered or touched
            return {"ok": True, "need_snapshot": False}
        name = _CONTROL_CHARACTERS.sub("", _text(message.get("name"))).strip()[:64]
        if kind not in ("snapshot", "heartbeat", "goodbye") or not name:
            raise ValueError("unknown message type or missing name")
        version = _CONTROL_CHARACTERS.sub("", _text(message.get("version")))[:32]
        data = clean_data(message.get("data")) if kind == "snapshot" else None
        now = self._clock()
        with self._lock:
            # A daemon that gets through again is no longer a rejected one (a fixed or restored token).
            for key in [key for key in self._rejected if key[0] == name]:
                del self._rejected[key]
            daemon = self._daemons.get(name)
            if kind == "snapshot":
                if daemon is None:
                    if len(self._daemons) >= MAX_DAEMONS:
                        raise ValueError("too many daemons")
                    daemon = self._daemons[name] = _Daemon(version)
                    print(f"Daemon '{name}' is sending data (GHAADD {version or '?'}).", flush=True)
                daemon.version = version or daemon.version
                daemon.data = data
                daemon.sent_at = _number(message.get("sent_at"))
                daemon.snapshot_at = daemon.last_seen = now
                daemon.stopped = False
                return {"ok": True, "need_snapshot": False}
            if daemon is None:  # only a snapshot makes a daemon known: ask for one, or ignore the goodbye
                return {"ok": True, "need_snapshot": kind == "heartbeat"}
            if kind == "goodbye":
                daemon.data, daemon.stopped, daemon.last_seen = None, True, now
                print(f"Daemon '{name}' has stopped.", flush=True)
                return {"ok": True, "need_snapshot": False}
            # A heartbeat only proves the daemon is alive: if what we hold is gone or too old to trust, ask again.
            if daemon.data is None or daemon.stopped or now - daemon.last_seen > self.lost_after:
                return {"ok": True, "need_snapshot": True}
            daemon.last_seen = now
            return {"ok": True, "need_snapshot": False}

    def view(self) -> dict[str, Any]:
        """Return what the page shows: every daemon with its state, and its data only while it is live."""
        now = self._clock()
        entries = []
        with self._lock:
            for name in sorted(self._daemons):
                daemon = self._daemons[name]
                age = now - daemon.last_seen
                state = "stopped" if daemon.stopped else "lost" if age > self.lost_after or daemon.data is None else "live"
                entry: dict[str, Any] = {"name": name, "version": daemon.version, "state": state, "age": round(age, 1)}
                if state == "live" and daemon.data is not None:
                    data = {**daemon.data, "status": dict(daemon.data["status"])}  # only the status gets a new field
                    status = data["status"]
                    # The daemon's clock and ours may differ, so the countdown is measured on the daemon's own clock
                    # (its next poll minus when it sent the snapshot) and run down by the time since it arrived.
                    next_poll_at, sent_at = status["next_poll_at"], daemon.sent_at
                    waiting = not status["paused"] and not status["idle"]
                    if waiting and next_poll_at is not None and sent_at is not None:
                        status["next_poll_in"] = max(0.0, next_poll_at - sent_at - (now - daemon.snapshot_at))
                    else:
                        status["next_poll_in"] = None
                    entry["data"] = data
                entries.append(entry)
        rejected = self.rejected()
        return {"viewer_version": VIEWER_VERSION, "lost_after": self.lost_after, "daemons": entries, "rejected": rejected}

    def rejected(self) -> list[dict[str, Any]]:
        """The senders refused lately, newest first; one that has not tried again for an hour is forgotten."""
        now = self._clock()
        with self._lock:
            for key in [key for key, record in self._rejected.items() if now - record["last"] > REJECTED_SHOWN_SECONDS]:
                del self._rejected[key]
            records = sorted(self._rejected.values(), key=lambda record: record["last"], reverse=True)
            return [
                {"name": r["name"], "address": r["address"], "reason": r["reason"], "count": r["count"], "age": round(now - r["last"], 1)}
                for r in records
            ]


def _make_handler(store: Store, book: TokenBook) -> type[BaseHTTPRequestHandler]:
    """Build the request handler class: the page and its data for everyone, snapshots only with a known token."""

    class Handler(BaseHTTPRequestHandler):
        """Answers GET / /healthz /api/view and POST /api/snapshot."""
        server_version = "GHAADD-viewer"
        timeout = 15  # seconds a client may stall before its connection is dropped

        def log_message(self, format: str, *args: Any) -> None:
            """Stay silent: the page asks every few seconds and would fill the log."""

        def _send(self, code: int, body: bytes, content_type: str, cache_seconds: int = 0) -> None:
            """Write a response with the headers every answer carries (only the icons may be cached)."""
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", f"public, max-age={cache_seconds}" if cache_seconds else "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self'",
            )
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, payload: Any, code: int = 200) -> None:
            """Answer with a JSON document."""
            self._send(code, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

        def _fail(self, code: int, text: str) -> None:
            """Answer with a JSON error and close the connection (a body we did not read must not be mistaken for a request)."""
            self.close_connection = True
            self._json({"ok": False, "error": text}, code)

        def do_HEAD(self) -> None:
            """Answer HEAD like GET, without the body."""
            self.do_GET()

        def do_GET(self) -> None:
            """Serve the page, the health check and the current view."""
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/healthz":
                self._send(200, b"ok", "text/plain; charset=utf-8")
            elif path in ICONS:
                file_name, content_type = ICONS[path]
                try:
                    with open(os.path.join(assets_dir(), file_name), "rb") as handle:
                        icon = handle.read()
                except OSError:  # no assets folder (for example not mounted): the page just has no icon
                    self._fail(404, "not found")
                else:
                    self._send(200, icon, content_type, ICON_CACHE_SECONDS)
            elif path == "/api/view":
                self._json(store.view())
            else:
                self._fail(404, "not found")

        def _token(self) -> str:
            """The bearer token of the request ("" when there is none)."""
            header = self.headers.get("Authorization", "")
            return header[len("Bearer "):].strip() if header.startswith("Bearer ") else ""

        def _read_body(self) -> Optional[bytes]:
            """Read the request body (at most MAX_BODY_BYTES); on a missing or oversized length answer and return None.

            The body is always read BEFORE any answer is sent: closing a connection with unread data resets it, and
            on Windows that can destroy the answer (a 401 then reached the daemon as "cannot reach the viewer").
            """
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._fail(411, "Content-Length is required")
                return None
            if length < 0 or length > MAX_BODY_BYTES:
                self._fail(413, "message too large")
                return None
            return self.rfile.read(length)

        def do_POST(self) -> None:
            """Accept one message from a daemon: read it, then check the address and the token, then the content."""
            body = self._read_body()
            if body is None:
                return
            if self.path.split("?", 1)[0] != "/api/snapshot":
                self._fail(404, "not found")
                return
            try:
                message: Any = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                message = None  # judged below: the token is checked first, a bad body is only the business of a known sender
            # The name is what the sender claims: it is only used to tie a token to a daemon and to tell who was refused.
            claimed = _text(message.get("name")) if isinstance(message, dict) else ""
            verdict = book.check(self._token(), claimed)
            if verdict != "ok":
                address = str(self.client_address[0])
                if verdict == "unknown":
                    store.note_rejection(claimed, address, "token not accepted")
                    self._fail(401, "token rejected")
                else:
                    store.note_rejection(claimed, address, f"this token is not allowed for the name '{claimed}'")
                    self._fail(403, "token not allowed for this name")
                return
            try:
                if message is None:
                    raise ValueError("the message is not valid JSON")
                answer = store.receive(message)
            except ValueError as exc:
                self._fail(400, str(exc))
                return
            self._json(answer)

        def _not_allowed(self) -> None:
            """Refuse every other method (a body they may have sent is read first, for the reason given above)."""
            if self.headers.get("Content-Length") and self._read_body() is None:
                return
            self._fail(405, "not allowed")

        do_PUT = do_DELETE = do_PATCH = _not_allowed

    return Handler


class _Server(ThreadingHTTPServer):
    """The HTTP server: request threads end with the process, and Windows does not let two servers share a port."""
    daemon_threads = True
    allow_reuse_address = os.name != "nt"  # on Windows SO_REUSEADDR would let a second server bind the same port


def make_server(host: str, port: int, store: Store, tokens: Union[TokenBook, Iterable[str]]) -> ThreadingHTTPServer:
    """Create (but do not start) the server; `tokens` is a TokenBook or plain items ("token" / "name=token").

    Raises OSError when the port cannot be opened.
    """
    book = tokens if isinstance(tokens, TokenBook) else TokenBook(tokens)
    return _Server((host, port), _make_handler(store, book))


def collect_tokens(given: list[str], environment: Optional[str]) -> list[str]:
    """Merge the --token values and the environment value (comma, semicolon or line separated) into one list."""
    items = [item for value in given for item in split_items(value)] + split_items(environment or "")
    return list(dict.fromkeys(items))


def assets_dir() -> str:
    """The folder with the icons: assets beside this script (mounted read-only in the Docker setup)."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


def default_env_file() -> str:
    """The one token file: config/.env beside this script (not the current folder, so it is found from anywhere)."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", ".env")


def parse_args(argv: list[str], environ: Optional[dict[str, str]] = None) -> argparse.Namespace:
    """Read the command line; the environment supplies the defaults (that is how the Docker setup configures it)."""
    environ = dict(os.environ) if environ is None else environ
    parser = argparse.ArgumentParser(description="Passive web viewer for a GHAADD daemon (read-only, no controls).")
    parser.add_argument(
        "--token", action="append", default=[],
        help="A token a daemon may send data with: TOKEN (any daemon) or NAME=TOKEN (only that daemon). Repeatable.",
    )
    parser.add_argument("--port", type=int, default=int(environ.get("GHAADD_VIEWER_PORT") or DEFAULT_PORT))
    parser.add_argument("--host", default=environ.get("GHAADD_VIEWER_HOST") or DEFAULT_HOST, help="Address to listen on.")
    parser.add_argument(
        "--lost-after", type=float, default=float(environ.get("GHAADD_VIEWER_LOST_AFTER") or DEFAULT_LOST_AFTER_SECONDS),
        help="Seconds without data before a daemon is shown as lost (default 45).",
    )
    args = parser.parse_args(argv)
    args.tokens = collect_tokens(args.token, environ.get("GHAADD_VIEWER_TOKENS"))
    return args


def main(argv: Optional[list[str]] = None) -> int:
    """Start the viewer and serve until interrupted; returns the exit status."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    env_file = default_env_file()
    book = TokenBook(args.tokens, env_file=env_file)
    if book.problems:
        for problem in book.problems:
            print(f"Token problem: {problem} (python main.py --new-viewer-token makes a good one).", file=sys.stderr)
        return 2
    if not book.entries():
        print(
            f"No token given: use --token SECRET, {TOKENS_KEY}, or a {TOKENS_KEY}=... line in {env_file}. "
            "The daemon sends the same value as viewer.token.",
            file=sys.stderr,
        )
        return 2
    store = Store(lost_after=args.lost_after)
    try:
        server = make_server(args.host, args.port, store, book)
    except OSError as exc:
        print(f"Cannot listen on {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 1
    print(f"GHAADD viewer {VIEWER_VERSION} listening on {args.host}:{args.port} (read-only; daemons are 'lost' after {args.lost_after:g} s).", flush=True)
    print(f"Accepting {book.summary()}.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Viewer stopped.")
    finally:
        server.server_close()
    return 0


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GHAADD viewer</title>
<link rel="icon" href="favicon.ico" sizes="any">
<link rel="icon" type="image/png" href="icon.png">
<link rel="apple-touch-icon" href="icon.png">
<style>
:root {
  --bg: #f5f6f8; --panel: #ffffff; --text: #1b1f24; --muted: #5d6673; --line: #d9dde3;
  --accent: #2557c7; --ok: #1e8a4c; --warn: #b26a00; --bad: #c0362c; --warnbg: #fff4dc; --stripe: #f1f3f6;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14171b; --panel: #1d2127; --text: #e6e9ed; --muted: #98a2b0; --line: #343b45;
    --accent: #6b9bff; --ok: #4cc27f; --warn: #e0a243; --bad: #ff7b70; --warnbg: #3a2f17; --stripe: #232830;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
header { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 16px; padding: 12px 16px; background: var(--panel); border-bottom: 1px solid var(--line); }
h1 { margin: 0; font-size: 18px; }
.version { color: var(--muted); font-size: 12px; }
.spacer { flex: 1; }
.pill { padding: 2px 10px; border-radius: 999px; font-weight: 600; font-size: 12px; border: 1px solid currentColor; }
.pill.live { color: var(--ok); } .pill.paused { color: var(--warn); } .pill.lost, .pill.stopped, .pill.down { color: var(--bad); }
select, input { font: inherit; color: var(--text); background: var(--bg); border: 1px solid var(--line); border-radius: 6px; padding: 4px 8px; }
main { padding: 12px 16px 32px; max-width: 1500px; margin: 0 auto; }
nav { display: flex; flex-wrap: wrap; gap: 4px; margin: 4px 0 12px; align-items: center; }
#tabs { display: flex; flex-wrap: wrap; gap: 4px; }
nav button { font: inherit; color: var(--text); background: transparent; border: 1px solid transparent; border-bottom: 2px solid transparent; padding: 6px 12px; cursor: pointer; border-radius: 6px 6px 0 0; }
nav button[aria-selected="true"] { border-bottom-color: var(--accent); color: var(--accent); font-weight: 600; }
nav input { margin-left: auto; min-width: 160px; }
.banner { padding: 14px 16px; border-radius: 8px; border: 1px solid var(--bad); color: var(--bad); background: var(--panel); font-weight: 600; margin: 8px 0; }
.banner small { display: block; color: var(--muted); font-weight: 400; margin-top: 4px; }
.rejected { padding: 10px 16px; border-radius: 8px; border: 1px solid var(--warn); background: var(--warnbg); margin: 8px 0; }
.rejected h2 { margin: 0 0 4px; font-size: 13px; color: var(--warn); }
.rejected div { padding: 2px 0; }
.rejected small { color: var(--muted); }
.cards { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 12px; }
.cards.second { margin-top: 12px; }
@media (max-width: 1000px) { .cards { grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); } }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 12px 14px; }
.card h2 { margin: 0 0 6px; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }
.card .big { font-size: 20px; font-weight: 600; word-break: break-word; }
.tablewrap { overflow-x: auto; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { position: sticky; top: 0; background: var(--panel); color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .03em; white-space: nowrap; }
tbody tr:nth-child(even) { background: var(--stripe); }
tbody tr.warn { background: var(--warnbg); }
td.message { white-space: pre-wrap; word-break: break-word; min-width: 260px; }
td.nowrap { white-space: nowrap; }
.empty { padding: 24px; text-align: center; color: var(--muted); }
</style>
</head>
<body>
<header>
  <h1>GHAADD</h1><span class="version" id="version"></span>
  <select id="daemon" hidden></select>
  <span class="spacer"></span>
  <span class="pill down" id="pill">connecting…</span>
</header>
<main>
  <div id="rejected"></div>
  <div id="banner"></div>
  <nav><span id="tabs"></span><input id="filter" type="search" placeholder="Filter…" hidden></nav>
  <div id="content"></div>
</main>
<script>
"use strict";
const TABS = [
  ["overview", "Overview"], ["mappings", "Mappings"], ["warnings", "Warnings"],
  ["completed", "Completed"], ["limits", "Folder limits"], ["unmapped", "Unmapped"],
];
const COLUMNS = {
  mappings: [["repo", "Repository (owner/repo)"], ["folder", "Name (folder)"], ["status", "Status"], ["tag", "Tag"],
             ["last_check", "Last check"], ["step", "Recheck"], ["next_check", "Next check"], ["files", "Files"], ["limit", "Limit"]],
  warnings: [["time", "Time"], ["repo", "Repository (owner/repo)"], ["kind", "Type"], ["message", "Message"]],
  completed: [["time", "Time"], ["repo", "Repository (owner/repo)"], ["kind", "Tag"], ["message", "Message"]],
  limits: [["time", "Time"], ["repo", "Repository (owner/repo)"], ["kind", "Type"], ["message", "Message"]],
  unmapped: [["repo", "Repository (owner/repo)"], ["folder", "Name (folder)"], ["time", "First seen"]],
};
const $ = (id) => document.getElementById(id);
let view = null;          // the last answer of /api/view (null = the viewer itself cannot be reached)
let fetchedAt = 0;        // performance.now() when it arrived, for the countdown
let tab = (location.hash || "#overview").slice(1);
let chosen = "";          // daemon name picked in the selector
let filter = "";
let drawn = "";           // what is on screen, so an unchanged page is not rebuilt

if (!TABS.some((t) => t[0] === tab)) tab = "overview";

function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}

function duration(seconds) {
  seconds = Math.max(0, Math.round(seconds));
  const h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60), s = seconds % 60;
  const two = (n) => String(n).padStart(2, "0");
  return h ? h + ":" + two(m) + ":" + two(s) : two(m) + ":" + two(s);
}

function current() {
  if (!view || !view.daemons.length) return null;
  return view.daemons.find((d) => d.name === chosen) || view.daemons[0];
}

function setPill(text, cls) {
  $("pill").textContent = text;
  $("pill").className = "pill " + cls;
}

function banner(text, detail) {
  const box = $("banner");
  box.replaceChildren();
  if (!text) return;
  const node = el("div", text, "banner");
  if (detail) node.appendChild(el("small", detail));
  box.appendChild(node);
}

let navKey = "";

function renderRejected(list) {
  // Senders whose token was refused: shown apart from the daemons (what they claim to be is not trusted).
  const box = $("rejected");
  box.replaceChildren();
  if (!list || !list.length) return;
  const panel = el("div", null, "rejected");
  panel.appendChild(el("h2", "Rejected connections"));
  for (const item of list) {
    const line = el("div", item.name + " (" + item.address + "): " + item.reason + " ");
    line.appendChild(el("small", "— " + item.count + (item.count === 1 ? " attempt" : " attempts") + ", last " + duration(item.age) + " ago"));
    panel.appendChild(line);
  }
  box.appendChild(panel);
}

function buildNav(data) {
  // The tab buttons are rebuilt only when their text changes, and the filter box is never rebuilt:
  // otherwise it would lose the focus every refresh while someone types in it.
  const labels = TABS.map(([key, title]) => {
    const count = data ? (key === "mappings" ? data.repos.length : data.tabs[key] ? data.tabs[key].length : 0) : 0;
    return key !== "overview" && count ? title + " (" + count + ")" : title;
  });
  const wanted = JSON.stringify([tab, labels]);
  if (wanted !== navKey) {
    navKey = wanted;
    const tabs = $("tabs");
    tabs.replaceChildren();
    TABS.forEach(([key], index) => {
      const button = el("button", labels[index]);
      button.setAttribute("role", "tab");
      button.setAttribute("aria-selected", key === tab ? "true" : "false");
      button.onclick = () => { tab = key; location.hash = key; render(); };
      tabs.appendChild(button);
    });
  }
  $("filter").hidden = !data || tab === "overview";
}

function card(title, value) {
  const node = el("div", null, "card");
  node.appendChild(el("h2", title));
  const big = el("div", value, "big");
  node.appendChild(big);
  return [node, big];
}

function renderOverview(daemon, data) {
  const status = data.status;
  // Two rows on the same five-column grid: what the daemon is doing, then the sizes of the lists.
  const doing = el("div", null, "cards");
  const lists = el("div", null, "cards second");
  const [c1] = card("Daemon", daemon.name + (daemon.version ? " · GHAADD " + daemon.version : ""));
  if (status.started_at) c1.appendChild(el("div", "Started " + new Date(status.started_at * 1000).toLocaleString()));
  const polling = status.paused ? "Paused" : status.idle ? "Polling is off: only on Poll now" : "Polling";
  const [c2] = card("Polling", polling);
  if (status.next_poll_in !== null && status.next_poll_in !== undefined) {
    const next = el("div", null, null);
    next.id = "countdown";
    c2.appendChild(next);
  }
  doing.appendChild(c1);
  doing.appendChild(card("Repositories", String(data.repos.length))[0]);
  doing.appendChild(c2);
  doing.appendChild(card("Working on", status.progress || "Nothing right now")[0]);
  doing.appendChild(card("Queue", status.queue || "—")[0]);
  const counts = [
    ["Warnings", data.tabs.warnings.length], ["Completed", data.tabs.completed.length],
    ["Folder limits", data.tabs.limits.length], ["Unmapped", data.tabs.unmapped.length],
  ];
  for (const [title, value] of counts) lists.appendChild(card(title, String(value))[0]);
  $("content").replaceChildren(doing, lists);
  tickCountdown();
}

function renderTable(data) {
  const columns = COLUMNS[tab];
  const rows = tab === "mappings" ? data.repos : data.tabs[tab];
  const needle = filter.trim().toLowerCase();
  const shown = needle ? rows.filter((r) => columns.some(([key]) => String(r[key]).toLowerCase().includes(needle))) : rows;
  const wrap = el("div", null, "tablewrap");
  if (!shown.length) {
    wrap.appendChild(el("div", rows.length ? "Nothing matches the filter." : "Nothing to show.", "empty"));
    $("content").replaceChildren(wrap);
    return;
  }
  const table = el("table");
  const head = el("tr");
  for (const [, title] of columns) head.appendChild(el("th", title));
  table.appendChild(el("thead")).appendChild(head);
  const body = el("tbody");
  for (const row of shown) {
    const tr = el("tr", null, row.limit_warning ? "warn" : "");
    for (const [key] of columns) {
      let value = row[key];
      if (key === "limit" && row.limit_warning) value = "⚠ " + value;
      tr.appendChild(el("td", value, key === "message" ? "message" : "nowrap"));
    }
    body.appendChild(tr);
  }
  table.appendChild(body);
  wrap.appendChild(table);
  $("content").replaceChildren(wrap);
}

function renderContent(data) {
  if (!data) { $("content").replaceChildren(); drawn = ""; return; }
  const key = JSON.stringify([tab, filter, tab === "overview" ? data.status : tab === "mappings" ? data.repos : data.tabs[tab]]);
  if (key === drawn) return;
  drawn = key;
  const daemon = current();
  if (tab === "overview") renderOverview(daemon, data); else renderTable(data);
}

function tickCountdown() {
  const node = $("countdown");
  const daemon = current();
  if (!node || !daemon || !daemon.data) return;
  const left = daemon.data.status.next_poll_in - (performance.now() - fetchedAt) / 1000;
  node.textContent = "Next poll in " + duration(left);
}

function render() {
  const select = $("daemon");
  if (!view) {
    setPill("viewer unreachable", "down");
    banner("Cannot reach the viewer.", "This page cannot get data from the server it came from. Retrying…");
    select.hidden = true;
    renderRejected(null);
    buildNav(null); renderContent(null);
    return;
  }
  $("version").textContent = "viewer " + view.viewer_version;
  renderRejected(view.rejected);
  const names = view.daemons.map((d) => d.name).join("\n");
  if (select.dataset.names !== names) {
    select.dataset.names = names;
    select.replaceChildren(...view.daemons.map((d) => { const o = el("option", d.name); o.value = d.name; return o; }));
  }
  select.hidden = view.daemons.length < 2;
  const daemon = current();
  if (daemon) { chosen = daemon.name; select.value = chosen; }
  if (!daemon) {
    setPill("waiting", "down");
    banner("No daemon has connected yet.", "Set viewer.url and viewer.token in the daemon's config.json, then switch the push on (python main.py --push-on).");
    buildNav(null); renderContent(null);
    return;
  }
  if (daemon.state === "stopped") {
    setPill("daemon stopped", "stopped");
    banner("The daemon “" + daemon.name + "” has stopped.", "It said goodbye " + duration(daemon.age) + " ago. Nothing is shown until it sends data again.");
    buildNav(null); renderContent(null);
  } else if (daemon.state === "lost") {
    setPill("no data received", "lost");
    banner("No data received from “" + daemon.name + "”.", "Nothing arrived for " + duration(daemon.age) + " (the daemon, its computer or the network may be down). Old numbers are not shown.");
    buildNav(null); renderContent(null);
  } else {
    const status = daemon.data.status;
    if (status.paused) setPill("paused", "paused"); else setPill("live", "live");
    banner("");
    buildNav(daemon.data); renderContent(daemon.data);
  }
}

async function refresh() {
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 5000);
    const response = await fetch("api/view", { cache: "no-store", signal: controller.signal });
    clearTimeout(timer);
    if (!response.ok) throw new Error("HTTP " + response.status);
    view = await response.json();
    fetchedAt = performance.now();
  } catch (error) {
    view = null;
  }
  drawn = view ? drawn : "";
  render();
}

$("filter").oninput = (event) => { filter = event.target.value; drawn = ""; if (current() && current().data) renderContent(current().data); };
$("daemon").onchange = (event) => { chosen = event.target.value; drawn = ""; render(); };
window.addEventListener("hashchange", () => {
  const wanted = location.hash.slice(1);
  if (TABS.some((t) => t[0] === wanted)) { tab = wanted; render(); }
});
setInterval(tickCountdown, 1000);
setInterval(refresh, 3000);
refresh();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
