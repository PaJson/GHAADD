"""Toolkit-independent model for the GUI's status tabs (Warnings, Completed, Folder limits, Unmapped).

The tabs are *queries over structured data*, never parsed log text: the event tabs
read `lifecycle_events` (filtered by event type/category), the Unmapped tab lists
`mapping.json` entries that have no destination yet.

Unread counters: a tab title shows "Warnings (3)" while it holds events newer than
the tab's last-seen event id, and plain "Warnings" when everything is read. The
last-seen ids are GUI-side state (kept in config.json under gui.status_tabs by
`gui_data`), never written to state.db or mapping.json. The first time a tab is
seen, everything that already exists counts as read, so a fresh GUI does not open
with "Completed (1230)".

Nothing here touches Tk, SQLite or files: the data comes in through injected
callables, which keeps it testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol

DEFAULT_ROW_LIMIT = 500

KIND_EVENTS = "events"
KIND_UNMAPPED = "unmapped"

# What the "Type" column shows for an event row.
TYPE_CATEGORY = "category"
TYPE_TAG = "tag"
TYPE_FIXED = "fixed"


@dataclass(frozen=True)
class TabDef:
    key: str
    title: str
    kind: str = KIND_EVENTS
    event_types: tuple[str, ...] = ()
    categories: Optional[tuple[str, ...]] = None  # None = any category
    exclude_categories: tuple[str, ...] = ()
    counter: bool = True  # show an unread count in the title
    type_source: str = TYPE_CATEGORY
    type_text: str = ""  # used when type_source == TYPE_FIXED


# Adding a tab is adding a definition here.
TAB_DEFS: tuple[TabDef, ...] = (
    TabDef(
        key="warnings",
        title="Warnings",
        event_types=("WARNING", "PARTIAL_MOVE"),
        exclude_categories=("LIMIT",),  # those have their own tab
    ),
    TabDef(key="completed", title="Completed", event_types=("COMPLETED_MOVE",), type_source=TYPE_TAG),
    TabDef(
        key="limits",
        title="Folder limits",
        event_types=("WARNING",),
        categories=("LIMIT",),
        counter=False,  # repeats while a folder stays over its limit: a list to consult, not an inbox
        type_source=TYPE_FIXED,
        type_text="Limit",
    ),
    TabDef(key="unmapped", title="Unmapped", kind=KIND_UNMAPPED),
)


@dataclass(frozen=True)
class StatusRow:
    id: int
    time: str
    repo: str
    kind: str
    message: str


@dataclass(frozen=True)
class UnmappedRow:
    repo: str
    foldername: str
    first_seen: str


class SeenStore(Protocol):
    """Where the per-tab last-seen event ids are remembered (config.json in the GUI)."""

    def load(self) -> dict[str, int]: ...

    def save(self, seen: dict[str, int]) -> None: ...


# fetch(tab, after_id, limit) -> raw event dicts, newest first, only those with id > after_id
FetchEvents = Callable[[TabDef, Optional[int], int], list[Mapping[str, Any]]]

_REPO_IN_TEXT = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def format_title(title: str, unread: int) -> str:
    """"Warnings (3)" while there is something unread, plain "Warnings" otherwise."""
    return f"{title} ({unread})" if unread > 0 else title


def format_event_time(epoch: Any) -> str:
    try:
        return datetime.fromtimestamp(float(epoch)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def find_repo(message: str, known: Mapping[str, str]) -> str:
    """The mapped repository a message talks about ("" if none): warnings carry it only in their text.

    `known` maps lower-cased owner/repo to its canonical spelling.
    """
    for candidate in _REPO_IN_TEXT.findall(message or ""):
        canonical = known.get(candidate.strip(".").lower())
        if canonical:
            return canonical
    return ""


def unmapped_rows(entries: Iterable[Mapping[str, Any]]) -> list[UnmappedRow]:
    """Mapping entries without a destination (the daemon adds these when an unknown repo notifies)."""
    rows = []
    for entry in entries:
        name = str(entry.get("name") or "").strip()
        if not name or str(entry.get("destination") or "").strip():
            continue
        stamp = str(entry.get("last_notification_seen") or "")
        rows.append(
            UnmappedRow(
                repo=name,
                foldername=str(entry.get("foldername") or "").strip(),
                first_seen=_format_stamp(stamp),
            )
        )
    rows.sort(key=lambda row: row.repo.casefold())
    rows.sort(key=lambda row: row.first_seen, reverse=True)  # newest first, ties by name
    return rows


def _format_stamp(stamp: str) -> str:
    """mapping.json stamps look like 2026-10-06_04-47; show 2026-10-06 04:47."""
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})", stamp)
    return f"{match.group(1)} {match.group(2)}:{match.group(3)}" if match else stamp


@dataclass
class _TabState:
    rows: list[StatusRow] = field(default_factory=list)  # newest first
    newest_id: Optional[int] = None


class StatusTabsModel:
    """Rows, unread counts and read marks for the event tabs."""

    def __init__(
        self,
        defs: Iterable[TabDef],
        fetch: FetchEvents,
        fetch_max_id: Callable[[], int],
        store: SeenStore,
        known_repos: Callable[[], Mapping[str, str]] = lambda: {},
        limit: int = DEFAULT_ROW_LIMIT,
    ) -> None:
        self._defs = {tab.key: tab for tab in defs}
        self._fetch = fetch
        self._fetch_max_id = fetch_max_id
        self._store = store
        self._known_repos = known_repos
        self._limit = limit
        self._states = {key: _TabState() for key, tab in self._defs.items() if tab.kind == KIND_EVENTS}
        self._seen: dict[str, int] = {}
        self._started = False

    @property
    def event_tab_keys(self) -> list[str]:
        return list(self._states)

    def refresh(self) -> set[str]:
        """Pull events that arrived since the last call; return the keys of tabs whose rows changed."""
        if not self._started:
            self._start()
        known = self._known_repos()
        changed: set[str] = set()
        for key, state in self._states.items():
            raw = self._fetch(self._defs[key], state.newest_id, self._limit)
            if not raw:
                continue
            new_rows = [self._to_row(self._defs[key], event, known) for event in raw]
            state.rows = (new_rows + state.rows)[: self._limit]
            state.newest_id = new_rows[0].id
            changed.add(key)
        return changed

    def rows(self, key: str) -> list[StatusRow]:
        return list(self._states[key].rows)

    def seen_id(self, key: str) -> int:
        return self._seen.get(key, 0)

    def unread(self, key: str) -> int:
        if not self._defs[key].counter:
            return 0
        seen = self.seen_id(key)
        return sum(1 for row in self._states[key].rows if row.id > seen)

    def title(self, key: str) -> str:
        return format_title(self._defs[key].title, self.unread(key))

    def clear(self, key: str) -> None:
        """Forget the rows of a tab whose events were deleted (newer events still arrive normally)."""
        self._states[key].rows = []

    def mark_read(self, key: str) -> bool:
        """Mark everything currently in the tab as read; True when the read mark moved."""
        rows = self._states[key].rows
        if not rows or rows[0].id <= self.seen_id(key):
            return False
        self._seen[key] = rows[0].id
        self._store.save(dict(self._seen))
        return True

    # ----- internals -----

    def _start(self) -> None:
        """Load the saved read marks; tabs seen for the first time start with everything read."""
        self._started = True
        self._seen = {key: int(value) for key, value in self._store.load().items() if isinstance(value, int)}
        newest = self._fetch_max_id()
        dirty = False
        for key in self._states:
            if key not in self._seen or self._seen[key] > newest:  # new tab, or the event table was reset
                self._seen[key] = newest
                dirty = True
        if dirty:
            self._store.save(dict(self._seen))

    @staticmethod
    def _to_row(tab: TabDef, event: Mapping[str, Any], known: Mapping[str, str]) -> StatusRow:
        message = str(event.get("message") or "")
        if tab.type_source == TYPE_TAG:
            kind = str(event.get("tag") or "")
        elif tab.type_source == TYPE_FIXED:
            kind = tab.type_text
        else:
            kind = str(event.get("category") or str(event.get("event_type") or "").replace("_", " ").capitalize())
        repo = str(event.get("repo") or "") or find_repo(message, known)
        return StatusRow(
            id=int(event["id"]),
            time=format_event_time(event.get("created_at")),
            repo=repo,
            kind=kind,
            message=message,
        )
