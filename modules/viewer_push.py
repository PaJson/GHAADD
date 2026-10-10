"""Push read-only snapshots of what the GUI shows to the standalone web viewer (ghaadd_viewer.py).

The daemon only ever connects OUT (to ``viewer.url``), so it opens no port. `SnapshotBuilder` turns the data the
GUI loads (status, Mappings rows, the four status lists) into one JSON-ready dict; `ViewerPusher` sends it from a
background thread when it changed, plus a small heartbeat, and says goodbye on a clean stop. Nothing here can be
controlled from the other side, and no path leaves the machine: the destination column is left out and absolute
paths in message texts become <path>. A failing viewer never disturbs polling: short timeout, backoff, one message.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from typing import Any, Callable, Optional, Protocol

import requests

from modules import daemon_lock, gui_data
from modules.app_info import __version__
from modules.config_manager import ViewerSettings
from modules.gui_daemon import parse_progress, progress_summary

SCHEMA = 1  # version of the message format; the viewer refuses a newer one it cannot read
HEARTBEAT_SECONDS = 15.0  # a quiet daemon still says hello this often (the viewer calls it lost after ~3 missed)
TICK_SECONDS = 2.0  # how often the thread looks for changes
REQUEST_TIMEOUT_SECONDS = 5.0
GOODBYE_TIMEOUT_SECONDS = 2.0
BACKOFF_FIRST_SECONDS = 5.0
BACKOFF_MAX_SECONDS = 60.0
SNAPSHOT_PATH = "/api/snapshot"

# An absolute path in free text: a drive letter or UNC share on Windows, or a slash that does not follow a word
# character (so "owner/repo" and "12 / 15" stay as they are). Folder names contain spaces and brackets ("SONY - PS3",
# "SameBoy (LIJI32)"), so a path runs on to the next comma, semicolon, closing square bracket or quote, never just to
# the next space: it may swallow a few words of prose after it, but it never leaves half a path behind.
_PATH_TAIL = r"[^,;\]\"'<>|\r\n]*"
_PATH_PATTERN = re.compile(
    r"(?<!\w)[A-Za-z]:[\\/]" + _PATH_TAIL + r"|\\\\(?=[^\s\\])" + _PATH_TAIL + r"|(?<![\w.~-])/(?=[^\s/])" + _PATH_TAIL
)

# post(url, message, token, timeout) -> the viewer's answer as a dict; raises PushError when it cannot be delivered
Poster = Callable[[str, dict[str, Any], str, float], dict[str, Any]]


class PushError(Exception):
    """A message could not be delivered (the text is safe to print: it never contains the token)."""


def _say(text: str) -> None:
    """Print a line for the console without ever raising.

    A console that cannot show a character (an old code page) or has been closed must not be able to stop the sender.
    """
    try:
        print(text, flush=True)
    except (UnicodeError, OSError, ValueError):
        try:
            print(text.encode("ascii", "replace").decode("ascii"), flush=True)
        except (OSError, ValueError):
            pass  # nowhere to write: the status file still carries the state


def redact_paths(text: str) -> str:
    """Replace every absolute path in a message with <path>."""
    return _PATH_PATTERN.sub("<path>", text or "")


class _NullSeenStore:
    """A read-mark store that remembers nothing: pushing must not write the GUI's read marks into config.json."""

    def load(self) -> dict[str, int]:
        """Nothing is remembered."""
        return {}

    def save(self, seen: dict[str, int]) -> None:
        """Nothing is stored."""


class SnapshotSource(Protocol):
    """Anything that can produce a snapshot (the real builder, or a fake in the tests)."""

    def build(self) -> dict[str, Any]: ...


class SnapshotBuilder:
    """Builds the snapshot dict from the same sources as the GUI; meant for one thread (the pusher's)."""

    def __init__(self, status_reader: Callable[[], Any] = daemon_lock.read_published_status) -> None:
        """Create the builder; `status_reader` returns the daemon status dict (injectable for tests)."""
        self._status_reader = status_reader
        self._feed = gui_data.StatusFeed(store=_NullSeenStore())

    def build(self) -> dict[str, Any]:
        """Return {"status": ..., "repos": [...], "tabs": {...}}: the Mappings rows and the status lists, path-free."""
        status = self._status_reader()
        progress = progress_summary(parse_progress(status.get("queue_progress")))
        counts = gui_data.load_queue_counts()
        table = gui_data.load_repo_table()
        self._feed.refresh()
        model = self._feed.model
        tabs: dict[str, list[dict[str, Any]]] = {
            key: [
                {"time": row.time, "repo": row.repo, "kind": row.kind, "message": redact_paths(row.message)}
                for row in model.rows(key)
            ]
            for key in model.event_tab_keys
        }
        tabs["unmapped"] = [
            {"repo": row.repo, "folder": row.folder, "time": row.first_seen} for row in self._feed.unmapped()
        ]
        return {
            "status": {
                "paused": bool(status.get("paused")),
                "idle": bool(status.get("polling_idle")),
                "next_poll_at": status.get("next_poll_at"),
                "started_at": status.get("started_at"),
                "progress": progress,
                "queue": gui_data.queue_text(counts),
                "pending": counts.pending,
                "due": counts.due,
            },
            "repos": [
                {
                    "repo": row.repo, "folder": row.folder, "status": row.status, "tag": row.tag,
                    "last_check": row.last_check, "step": row.step, "next_check": row.next_check,
                    "files": row.files, "limit": row.limit, "limit_warning": row.limit_warning,
                }
                for row in table.rows
            ],
            "tabs": tabs,
        }


def post_message(url: str, message: dict[str, Any], token: str, timeout: float) -> dict[str, Any]:
    """Send one message to the viewer; return its JSON answer, or raise PushError with a plain reason."""
    try:
        response = requests.post(
            url + SNAPSHOT_PATH, json=message, headers={"Authorization": f"Bearer {token}"}, timeout=timeout
        )
    except requests.RequestException as exc:
        raise PushError(f"cannot reach the viewer ({exc.__class__.__name__})") from None
    if response.status_code == 401:
        raise PushError("the viewer rejected the token")
    if response.status_code == 403:
        raise PushError("the viewer does not accept this token for this daemon name")
    if response.status_code != 200:
        raise PushError(f"the viewer answered HTTP {response.status_code}")
    try:
        answer = response.json()
    except ValueError:
        raise PushError("the viewer's answer was not understood") from None
    return answer if isinstance(answer, dict) else {}


class ViewerPusher:
    """Sends snapshots to the viewer: when the data changed, otherwise a heartbeat; start()/stop() are repeatable."""

    def __init__(
        self,
        settings: ViewerSettings,
        builder: Optional[SnapshotSource] = None,
        post: Poster = post_message,
        clock: Callable[[], float] = time.monotonic,
        on_status: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> None:
        """Keep the settings and collaborators; nothing is sent until start() (or push_once() in tests)."""
        self.settings = settings
        self._builder = builder
        self._post = post
        self._clock = clock
        self._on_status = on_status
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_digest: Optional[str] = None
        self._last_sent: Optional[float] = None
        self._need_snapshot = True  # the viewer knows nothing about us yet (or may have restarted)
        self._failures = 0
        self._retry_at = 0.0
        self.last_ok: Optional[float] = None  # epoch time of the last delivered message
        self.error = ""  # why the last attempt failed ("" = fine)

    @property
    def active(self) -> bool:
        """Return True while the sender thread runs."""
        return self._thread is not None and self._thread.is_alive()

    def not_ready_reason(self) -> str:
        """Why pushing cannot start ("" when it can): the address and the token are both required."""
        url = self.settings["url"]
        if not url.startswith(("http://", "https://")):
            return "set viewer.url in config.json (for example http://192.168.0.100:8888)"
        if not self.settings["token"]:
            return "set viewer.token in config.json (python main.py --new-viewer-token makes one)"
        return ""

    def start(self) -> bool:
        """Start the sender thread; False when the settings are incomplete (see not_ready_reason)."""
        if self.active:
            return True
        reason = self.not_ready_reason()
        if reason:
            self.error = reason
            self._publish()
            return False
        self._stop_event.clear()
        self._need_snapshot = True
        self._failures = 0
        self._retry_at = 0.0
        self.error = ""
        self._thread = threading.Thread(target=self._run, name="ghaadd-viewer-push", daemon=True)
        self._thread.start()
        self._publish()
        return True

    def stop(self, goodbye: bool = True) -> None:
        """Stop the thread; with `goodbye` the viewer is told once that this daemon is stopping (best effort)."""
        thread, self._thread = self._thread, None
        if thread is None:
            return
        self._stop_event.set()
        thread.join(timeout=REQUEST_TIMEOUT_SECONDS + 1)
        if goodbye:
            try:
                self._post(self.settings["url"], self._message("goodbye"), self.settings["token"], GOODBYE_TIMEOUT_SECONDS)
            except PushError:
                pass  # the viewer will notice the silence instead
        self._publish()

    def apply_override(self, override: Optional[bool], configured: bool) -> bool:
        """Follow a live switch (True/False force, None = the config value); return whether it is pushing now."""
        want = configured if override is None else override
        if want and not self.active:
            if self.start():
                _say(
                    f"📡 Viewer push switched on ({self.settings['url']})" + (" (config default)." if override is None else ".")
                )
            else:
                _say(f"📡 Viewer push cannot start: {self.error}.")
        elif not want and self.active:
            _say("📡 Viewer push switched off" + (" (config default)." if override is None else "."))
            self.stop()
        return self.active

    def push_once(self) -> None:
        """One look: send a snapshot when the data changed (or the viewer asked), else a heartbeat when one is due."""
        now = self._clock()
        if now < self._retry_at:
            return
        if self._builder is None:
            self._builder = SnapshotBuilder()
        try:
            snapshot = self._builder.build()
        except Exception as exc:  # a database hiccup must not kill the thread; the next tick tries again
            self._note_error(f"could not read the data ({exc.__class__.__name__}: {exc})")
            return
        digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        if self._need_snapshot or digest != self._last_digest:
            message = self._message("snapshot", snapshot)
        elif self._last_sent is None or now - self._last_sent >= HEARTBEAT_SECONDS:
            message = self._message("heartbeat")
        else:
            return
        try:
            answer = self._post(self.settings["url"], message, self.settings["token"], REQUEST_TIMEOUT_SECONDS)
        except PushError as exc:
            self._failures += 1
            self._retry_at = now + min(BACKOFF_MAX_SECONDS, BACKOFF_FIRST_SECONDS * 2 ** (self._failures - 1))
            self._need_snapshot = True  # a viewer that was down has lost what we told it
            self._note_error(str(exc))
            return
        self._last_sent = now
        if message["type"] == "snapshot":
            self._last_digest = digest
        self._need_snapshot = bool(answer.get("need_snapshot"))
        if self.error:
            _say("📡 Viewer push: the viewer is reachable again.")
        self._failures = 0
        self.error = ""
        self.last_ok = time.time()
        self._publish()

    def _message(self, kind: str, data: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Wrap a payload as a message: format version, kind, who we are and when it was sent."""
        message: dict[str, Any] = {
            "schema": SCHEMA, "type": kind, "name": self.settings["name"], "version": __version__, "sent_at": time.time(),
        }
        if data is not None:
            message["data"] = data
        return message

    def _note_error(self, text: str) -> None:
        """Remember why pushing fails; the console hears about a new reason once, not on every attempt."""
        if text != self.error:
            _say(f"📡 Viewer push: {text}. Trying again shortly; polling is not affected.")
        self.error = text
        self._publish()

    def _publish(self) -> None:
        """Tell the status file how pushing is doing (for the GUI and for diagnosis)."""
        if self._on_status is not None:
            self._on_status({"active": self.active, "last_ok": self.last_ok, "error": self.error})

    def _run(self) -> None:
        """The thread: look for changes every few seconds until stopped, whatever goes wrong."""
        while not self._stop_event.is_set():
            try:
                self.push_once()
            except Exception as exc:  # the sender must never end by accident; it only reports
                try:
                    self._note_error(f"unexpected problem ({exc.__class__.__name__}: {exc})")
                except Exception:  # not even a failing report may end it
                    pass
            self._stop_event.wait(TICK_SECONDS)
