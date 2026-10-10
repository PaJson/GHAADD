"""Toolkit-independent logic of the GUI's Viewer window: the daemon's push status as text, a token, a connection test.

The window itself (main_gui.ViewerDialog) only shows what these functions return. Nothing here imports Tk, and the
network call is a parameter, so the tests need neither.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any, Mapping

from modules import config_manager, gui_forms
from modules.connection_tests import ConnectionResult
from modules.viewer_push import SCHEMA, Poster, PushError, post_message

TOKEN_BYTES = 24  # about 32 characters: well above the viewer's minimum


@dataclass(frozen=True)
class PushView:
    """What the Viewer window shows about the running daemon's push: a line of text and the two buttons' state."""
    text: str
    problem: bool  # the text reports something wrong (shown in the error colour)
    start_enabled: bool
    stop_enabled: bool


def generate_token() -> str:
    """Return a new random token for the viewer and the daemon to share."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def format_ago(seconds: float) -> str:
    """"12 s", "3 min" or "2 h 5 min": how long ago something happened, short enough for a status line."""
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min"


def describe_push(status: Mapping[str, Any], now: float) -> PushView:
    """Turn the daemon status (daemon_lock.get_daemon_status()) into the line and button states of the window."""
    if not status.get("running"):
        return PushView("No daemon is running, so nothing is being sent.", False, False, False)
    push = status.get("viewer_push")
    push = push if isinstance(push, dict) else {}
    active = push.get("active") is True
    error = str(push.get("error") or "")
    last_ok = push.get("last_ok")
    delivered = (
        f"Last delivered {format_ago(now - float(last_ok))} ago." if isinstance(last_ok, (int, float)) else "Nothing delivered yet."
    )
    if active and error:
        return PushView(f"Sending, but the last attempt failed: {error}. {delivered}", True, False, True)
    if active:
        return PushView(f"Sending to the viewer. {delivered}", False, False, True)
    if error:
        return PushView(f"Not sending: {error}", True, True, False)
    return PushView("The daemon is not sending to the viewer.", False, True, False)


def check_viewer(url: str, token: str, name: str = "", post: Poster = post_message) -> ConnectionResult:
    """Ask the viewer whether it is there and accepts the token for this daemon name (a "ping": nothing is changed there).

    The name (the typed one, else the computer's name) matters because a token can be tied to one daemon name.
    """
    address = gui_forms.normalize_viewer_url(url)
    if address is None:
        return ConnectionResult(ok=False, message="Enter the viewer address first (like http://192.168.0.100:8888).")
    if not token.strip():
        return ConnectionResult(ok=False, message="Enter the token first (Generate makes one).")
    try:
        post(address, {"schema": SCHEMA, "type": "ping", "name": name.strip() or computer_name()}, token.strip(), 5.0)
    except PushError as exc:
        text = str(exc)
        return ConnectionResult(ok=False, message=text[:1].upper() + text[1:] + ".")
    return ConnectionResult(ok=True, message="The viewer answered and accepted the token.")


def computer_name() -> str:
    """The name the daemon uses when none is set (the same fallback as config_manager.get_viewer_settings)."""
    return config_manager.get_viewer_settings({})["name"]
