"""Reading and writing the credentials in `.env` (GMAIL_USER, GMAIL_APP_PASSWORD, GITHUB_PAT) for the GUI.

The daemon and the CLI read `.env` with python-dotenv at start-up; this module is the one place that writes it, so a
user never has to edit the file by hand. Writes keep every other line and comment of the file, are made under a file
lock and replace the file in one step (a crash cannot leave half a file), and on Linux/macOS the file is made
readable by its owner only. The secrets are never logged and never copied anywhere else; `credentials_fingerprint`
returns only a hash, which is how the GUI notices that a running daemon still has the old login.

Toolkit-independent. Tests: tests/test_env_manager.py.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Mapping, Optional

from dotenv import dotenv_values
from filelock import FileLock, Timeout

GMAIL_USER = "GMAIL_USER"
GMAIL_APP_PASSWORD = "GMAIL_APP_PASSWORD"
GITHUB_PAT = "GITHUB_PAT"
ENV_KEYS = (GMAIL_USER, GMAIL_APP_PASSWORD, GITHUB_PAT)
REQUIRED_KEYS = (GMAIL_USER, GMAIL_APP_PASSWORD)

_LOCK_TIMEOUT_SECONDS = 10.0
_REPLACE_ATTEMPTS = 5
_PLAIN_VALUE = re.compile(r"[A-Za-z0-9_.@+/:=%-]+")


# (label, address): where the user gets or checks what this window asks for (the GUI's "Internet" menu).
HELP_LINKS: tuple[tuple[str, str], ...] = (
    ("Gmail: create an app password", "https://myaccount.google.com/apppasswords"),
    ("Gmail: 2-step verification (needed for app passwords)", "https://myaccount.google.com/signinoptions/two-step-verification"),
    ("Gmail: turn IMAP access on", "https://mail.google.com/mail/u/0/#settings/fwdandpop"),
    ("Gmail: filters (move the GitHub mails to a folder)", "https://mail.google.com/mail/u/0/#settings/filters"),
    ("GitHub: create or manage tokens (classic)", "https://github.com/settings/tokens"),
    ("GitHub: notification emails settings", "https://github.com/settings/notifications"),
)


class EnvLockTimeout(RuntimeError):
    """The .env lock could not be taken in time (another GHAADD process is writing it)."""


def get_env_path() -> str:
    """The `.env` beside main.py (the file python-dotenv finds from main.py and from modules/)."""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")


def read_values() -> dict[str, str]:
    """The three credentials as the file holds them ("" for a missing key or a missing file)."""
    path = get_env_path()
    found = dotenv_values(path) if os.path.exists(path) else {}
    return {key: str(found.get(key) or "") for key in ENV_KEYS}


def normalize_password(password: str) -> str:
    """Google shows an app password as four groups ("abcd efgh ijkl mnop"); the spaces are not part of it."""
    return re.sub(r"\s+", "", password or "")


def validate(user: str, password: str, token: str) -> list[str]:
    """What is wrong with these values (empty list = fine). The token is optional, the Gmail login is not."""
    problems = []
    user = (user or "").strip()
    if not user:
        problems.append("Enter your Gmail address.")
    elif "@" not in user or re.search(r"\s", user):
        problems.append("The Gmail address should look like name@gmail.com.")
    if not normalize_password(password):
        problems.append("Enter your Gmail app password.")
    token = (token or "").strip()
    if token and re.search(r"\s", token):
        problems.append("The GitHub token must not contain spaces or line breaks.")
    return problems


def mask(value: str) -> str:
    """Bullets for display ("" stays empty)."""
    return "•" * min(len(value), 12) if value else ""


def summary() -> str:
    """One line for the Settings window: which login is set (never the secrets themselves)."""
    values = read_values()
    user = values[GMAIL_USER]
    gmail = f"Gmail: {user}" if user else "Gmail: not set"
    if user and not values[GMAIL_APP_PASSWORD]:
        gmail += " (no app password)"
    return f"{gmail}   ·   GitHub token: {'set' if values[GITHUB_PAT] else 'not set'}"


def credentials_fingerprint() -> str:
    """A short hash of the three credentials (the secrets themselves never leave this module)."""
    payload = json.dumps(read_values(), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _format_value(value: str) -> str:
    if _PLAIN_VALUE.fullmatch(value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _key_pattern(key: str) -> re.Pattern[str]:
    return re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=")


def _rewrite(content: str, changes: Mapping[str, Optional[str]]) -> str:
    """`content` with the changed keys replaced in place, removed (empty value) or appended; all else is kept."""
    newline = "\r\n" if "\r\n" in content else "\n"
    lines = content.splitlines()
    for key, value in changes.items():
        pattern = _key_pattern(key)
        wanted = None if value is None or value == "" else f"{key}={_format_value(value)}"
        result: list[str] = []
        placed = False
        for line in lines:
            if pattern.match(line):
                if wanted is not None and not placed:
                    result.append(wanted)
                    placed = True
                continue  # a later duplicate would override the new value, so it goes
            result.append(line)
        if wanted is not None and not placed:
            result.append(wanted)
        lines = result
    return newline.join(lines) + (newline if lines else "")


def update_values(changes: Mapping[str, Optional[str]]) -> bool:
    """Set (or, with None / "", remove) credentials in `.env`; True when the file changed.

    Only the given keys are touched. The running process's environment follows, so the GUI's own Doctor sees the
    new login at once. Raises EnvLockTimeout when another process holds the lock, OSError when the file cannot be
    written.
    """
    unknown = set(changes) - set(ENV_KEYS)
    if unknown:
        raise ValueError(f"Not a credential: {', '.join(sorted(unknown))}")
    path = get_env_path()
    lock = FileLock(path + ".lock", timeout=_LOCK_TIMEOUT_SECONDS)
    try:
        lock.acquire()
    except Timeout as exc:
        raise EnvLockTimeout("Another GHAADD window or process is writing .env; try again in a moment.") from exc
    try:
        original = ""
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8", newline="") as handle:
                original = handle.read()
        updated = _rewrite(original, changes)
        changed = updated != original
        if changed:
            _write_atomically(path, updated)
        for key, value in changes.items():
            if value:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)
        return changed
    finally:
        lock.release()


def _write_atomically(path: str, text: str) -> None:
    temporary = f"{path}.{os.getpid()}.tmp"
    try:
        with open(temporary, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        if os.name != "nt":
            os.chmod(temporary, 0o600)  # secrets: owner only
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:  # Windows: a virus scanner or editor briefly holds the file
                if attempt == _REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(0.1)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
