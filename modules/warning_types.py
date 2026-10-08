"""The kinds of warning the app records, and which of them may pop up a notification.

Every warning is stored with a type (`lifecycle_logger.log_warning(type, message)`); the Warnings tab shows it in
its Type column. The GUI's tray notifications can be switched off per type (`gui.silenced_warning_types` in
config.json): the Warnings tab and its unread counter always show everything. Toolkit-independent.
"""

from __future__ import annotations

from typing import Iterable

# (type as stored/shown, what it means). New types a future version adds appear in the Settings checklist once
# they are listed here; until then they notify like any other type.
WARNING_TYPES: tuple[tuple[str, str], ...] = (
    ("API", "Could not resolve a release's current commit from GitHub (often a release that was replaced or removed)."),
    ("SANITY_CHECK", "A finished release has a different number of files than the previous one."),
    ("SKIPPED", "A notification matched the repository's skiplist and was not queued."),
    ("MOVE", "A staging folder could not be moved to the Complete or Partial folder."),
    ("SUPERSEDE_MOVE", "An incomplete, superseded staging folder could not be moved."),
    ("SUPERSEDE_FINALIZE", "A release that a newer one replaced was finalized with the files it already had."),
    ("PREMATURE_FINALIZE", "A release disappeared from GitHub before its re-checks finished, so it was finalized as it was."),
    ("FOLDER_RENAMED", "A release's staging folder changed between attempts (title edited upstream); the old folder was left behind."),
    ("FOLDER_RENAMED_MOVE", "...and the old staging folder could not be moved to the Partial folder."),
    ("FOLDER_RENAMED_MOVED", "...and the old staging folder was moved to the Partial folder."),
    ("DESTINATION", "A repository's destination is missing or unusable, so the default folder was used."),
    ("MAPPING", "A mapped destination folder no longer exists, or mapping.json was upgraded to the 2.0 key names."),
    ("MAILBOX", "Emails could not be marked as read or moved to the Trash."),
    ("LIMIT", "A repository has more release folders than its limit (this also has its own notification)."),
    ("PARTIAL_MOVE", "A superseded release was moved to the Partial folder (shown as \"Partial move\" in the Warnings tab)."),
)

# Silent by default: API is routine when a release is replaced, and LIMIT already has a notification of its own.
DEFAULT_SILENCED: tuple[str, ...] = ("API", "LIMIT")

KNOWN_TYPES: tuple[str, ...] = tuple(code for code, _meaning in WARNING_TYPES)


def type_of(kind: object) -> str:
    """The type code of a Warnings-tab row's Type text ("API", "Partial move", "LIMIT (+3 earlier)" ...)."""
    text = str(kind or "").split(" (", 1)[0]
    return text.strip().upper().replace(" ", "_")


def wants_notice(kind: object, silenced: Iterable[str]) -> bool:
    """True when a new row of this type may produce a notification."""
    return type_of(kind) not in {str(code).strip().upper() for code in silenced}


def count_notifying(kinds: Iterable[object], silenced: Iterable[str]) -> int:
    """How many of these Warnings-tab rows (given by their Type text) are of a type that notifies."""
    muted = {str(code).strip().upper() for code in silenced}
    return sum(1 for kind in kinds if type_of(kind) not in muted)


def describe(code: str) -> str:
    """Return the plain-language meaning of a warning type code, or "" when it is unknown."""
    for known, meaning in WARNING_TYPES:
        if known == code:
            return meaning
    return ""


def summary(silenced: Iterable[str]) -> str:
    """"12 of 14 warning types notify" for the Settings window."""
    muted = {str(code).strip().upper() for code in silenced}
    notifying = sum(1 for code in KNOWN_TYPES if code not in muted)
    return f"{notifying} of {len(KNOWN_TYPES)} warning types notify"
