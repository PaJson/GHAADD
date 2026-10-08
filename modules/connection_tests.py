"""The "Test" buttons of the Gmail & GitHub window: try a login without saving anything.

`check_gmail` logs in to Gmail over IMAP, lists the folders (labels) and checks that the chosen folder exists;
`check_github_token` asks GitHub's rate-limit endpoint, which needs no permissions. Both return a result object with
a plain-language message instead of raising, and take the network client as a parameter so the tests use fakes.
Passwords never appear in a message. Toolkit-independent. Tests: tests/test_connection_tests.py.
"""
from __future__ import annotations

import socket
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import requests

GMAIL_HOST = "imap.gmail.com"
GITHUB_RATE_LIMIT_URL = "https://api.github.com/rate_limit"
TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class ConnectionResult:
    """Outcome of a connection test: ok, a plain message, and (Gmail only) the folder list and folder check."""
    ok: bool
    message: str
    folders: list[str] = field(default_factory=list)  # Gmail only: the selectable folders, sorted
    folder_found: Optional[bool] = None  # Gmail only: None when no folder was asked about


def _scrub(text: str, secret: str) -> str:
    """Replace the secret in a message with *** so a password never reaches the screen."""
    return text.replace(secret, "***") if secret else text


def _default_client(host: str) -> Any:
    """Open the IMAP client; imported here so this module imports without the network package."""
    from imapclient import IMAPClient  # imported here: the module stays importable without the network package

    return IMAPClient(host, use_uid=True, timeout=TIMEOUT_SECONDS)


def _selectable(entries: Any) -> list[str]:
    """Return the sorted names of real folders, leaving out containers such as "[Gmail]"."""
    names = []
    for flags, _delimiter, name in entries:
        if any(bytes(flag).lower() == b"\\noselect" for flag in flags):
            continue  # a container such as "[Gmail]", not a folder mail can be in
        names.append(str(name))
    return sorted(set(names), key=str.casefold)


def check_gmail(
    user: str,
    password: str,
    folder: str = "",
    client_factory: Callable[[str], Any] = _default_client,
) -> ConnectionResult:
    """Log in, list the folders and (when `folder` is given) check that it exists and count its unread mails."""
    if not user or not password:
        return ConnectionResult(False, "Enter the Gmail address and the app password first.")
    try:
        with client_factory(GMAIL_HOST) as server:
            server.login(user, password)
            folders = _selectable(server.list_folders())
            if not folder:
                return ConnectionResult(True, f"Logged in. {len(folders)} folders found.", folders)
            if folder not in folders:
                return ConnectionResult(
                    True,
                    f"Logged in, but there is no folder named \"{folder}\". Pick one from the list, or create that "
                    "label in Gmail (and tell Gmail to move the GitHub mails there with a filter).",
                    folders,
                    False,
                )
            server.select_folder(folder, readonly=True)
            unread = len(server.search("UNSEEN"))
            return ConnectionResult(
                True, f"Logged in. Folder \"{folder}\" found, {unread} unread mail(s).", folders, True
            )
    except (socket.timeout, TimeoutError):
        return ConnectionResult(False, f"{GMAIL_HOST} did not answer in {TIMEOUT_SECONDS} seconds. Check the internet connection.")
    except OSError as exc:  # no network, DNS, refused, TLS
        return ConnectionResult(False, f"Could not reach {GMAIL_HOST}: {_scrub(str(exc), password)}")
    except Exception as exc:  # imaplib/imapclient errors: a wrong login is the usual one
        text = _scrub(str(exc), password)
        lowered = text.lower()
        if any(word in lowered for word in ("authenticationfailed", "invalid credentials", "login", "password")):
            return ConnectionResult(
                False,
                "Gmail rejected the login. Use an app password (it needs 2-step verification on your Google account), "
                "not your normal password, and check that IMAP is enabled in Gmail's settings. "
                f"Gmail said: {text}",
            )
        return ConnectionResult(False, f"Gmail test failed: {text}")


def check_github_token(
    token: str,
    getter: Callable[..., Any] = requests.get,
) -> ConnectionResult:
    """Ask GitHub whether the token is accepted (an empty token is fine: it only means the low anonymous limit)."""
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = getter(GITHUB_RATE_LIMIT_URL, headers=headers, timeout=TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        return ConnectionResult(False, f"Could not reach GitHub: {_scrub(str(exc), token)}")
    if response.status_code == 401:
        return ConnectionResult(False, "GitHub rejected the token (expired, revoked or mistyped).")
    if response.status_code != 200:
        return ConnectionResult(False, f"GitHub answered with status {response.status_code}.")
    try:
        core = response.json()["resources"]["core"]
        limit, remaining = int(core["limit"]), int(core["remaining"])
    except (ValueError, KeyError, TypeError):
        return ConnectionResult(True, "GitHub answered, but the rate limit could not be read.")
    if not token:
        return ConnectionResult(True, f"No token: GitHub allows {limit} requests per hour without one ({remaining} left).")
    return ConnectionResult(True, f"Token accepted: {remaining} of {limit} requests per hour left.")
