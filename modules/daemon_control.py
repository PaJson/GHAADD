"""Control channel for a running daemon: pause/resume, forced polls, log switch, stop.

The state lives in the single-row ``daemon_control`` table of ``state.db``
(accessed through ``db_manager``). The GUI and CLI write it; the polling loop
only reads it (the one exception is clearing a stale ``paused`` flag at
startup). The daemon publishes its own state in ``ghaadd.daemon.status.json``
instead, so each piece of state has a single owner.

``poll_now_request`` is the epoch time of the latest forced-poll request. The
daemon acts when it *differs* from the last value it handled (not when it is
later), so a clock stepping backwards cannot swallow a request.

``log_override`` switches terminal log mirroring on or off while the daemon
runs: None follows ``terminal_log.enabled`` in config.json, True/False force it.
It is a session setting, cleared when a daemon starts.

``stop_request`` works like ``poll_now_request``: the daemon stops (gracefully:
the running job finishes first) when it *differs* from the value the daemon saw
at startup, so a request left over from an earlier run never stops a new one.

``check_folders_request`` also works that way: the daemon runs its destination/folder-limit check
(inside ``ControlWatcher.wait()``, paused or not) when it differs from the value it saw at startup
or last handled.
"""

import sqlite3
import time
from contextlib import closing
from typing import Callable, Literal, NotRequired, Optional, TypedDict

from modules.db_manager import (
    get_daemon_check_folders_request,
    get_daemon_control,
    get_daemon_stop_request,
    open_database,
    set_daemon_check_folders_request,
    set_daemon_log_override,
    set_daemon_paused,
    set_daemon_poll_now_request,
    set_daemon_stop_request,
)

DEFAULT_TICK_SECONDS = 1.0

WaitResult = Literal["elapsed", "forced", "stop"]


class ControlState(TypedDict):
    paused: bool
    poll_now_request: Optional[float]
    log_override: Optional[bool]
    stop_request: NotRequired[Optional[float]]  # absent in states built by older callers/tests
    check_folders_request: NotRequired[Optional[float]]


def _default_state() -> ControlState:
    return {
        "paused": False, "poll_now_request": None, "log_override": None, "stop_request": None,
        "check_folders_request": None,
    }


def read_control_state(connection: sqlite3.Connection) -> ControlState:
    """Return the current control state (defaults until a row exists)."""
    paused, poll_now_request, log_override = get_daemon_control(connection)
    return {
        "paused": paused,
        "poll_now_request": poll_now_request,
        "log_override": log_override,
        "stop_request": get_daemon_stop_request(connection),
        "check_folders_request": get_daemon_check_folders_request(connection),
    }


def get_control_state() -> ControlState:
    """Read the current control state with a short-lived connection (for the GUI)."""
    with closing(open_database()) as connection:
        return read_control_state(connection)


def set_paused(paused: bool) -> None:
    """Pause or resume polling in the running daemon."""
    with closing(open_database()) as connection:
        set_daemon_paused(connection, paused)


def set_log_override(override: Optional[bool]) -> None:
    """Force terminal logging on/off in the running daemon (None = follow config.json)."""
    with closing(open_database()) as connection:
        set_daemon_log_override(connection, override)


def request_stop() -> float:
    """Ask the running daemon to stop after its current job; returns the request stamp."""
    with closing(open_database()) as connection:
        return set_daemon_stop_request(connection, time.time())


def request_check_folders() -> float:
    """Ask the running daemon to check destinations and folder limits now; returns the request stamp."""
    with closing(open_database()) as connection:
        return set_daemon_check_folders_request(connection, time.time())


def request_poll_now() -> float:
    """Ask the running daemon to poll as soon as possible; returns the request stamp."""
    with closing(open_database()) as connection:
        return set_daemon_poll_now_request(connection, time.time())


def reset_paused_on_startup(connection: sqlite3.Connection) -> None:
    """Start unpaused: a pause left behind by a previous run must not silently stall a new one."""
    if read_control_state(connection)["paused"]:
        set_daemon_paused(connection, False)


def reset_log_override_on_startup(connection: sqlite3.Connection) -> None:
    """Start from config.json: a log switch left behind by a previous run must not stick."""
    if read_control_state(connection)["log_override"] is not None:
        set_daemon_log_override(connection, None)


class ControlWatcher:
    """Waits out a polling interval while honouring pause and forced-poll requests."""

    def __init__(
        self,
        *,
        connection: Optional[sqlite3.Connection] = None,
        enabled: bool = True,
        read_state: Optional[Callable[[], ControlState]] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        on_log_override: Optional[Callable[[Optional[bool]], None]] = None,
        on_check_folders: Optional[Callable[[], None]] = None,
    ) -> None:
        if not enabled:
            # Disabled (dry-run): never react to the real daemon's control state.
            read_state = _default_state
        elif read_state is None:
            read_state = self._make_database_reader(connection) if connection is not None else _default_state
        self._read_state = read_state
        self._clock = clock
        self._sleep = sleep
        self._tick_seconds = tick_seconds
        # Fires with the new log_override whenever it changes; None means "follow config".
        self._on_log_override = on_log_override
        self._on_check_folders = on_check_folders
        self._last_log_override: Optional[bool] = None
        # True once a checkpoint() during work saw a pause (cleared by begin_cycle()).
        self.cycle_interrupted = False
        # Whatever is already in the file at startup counts as handled.
        initial_state = self._read_state()
        self.last_handled_request: Optional[float] = initial_state["poll_now_request"]
        self._initial_stop_request: Optional[float] = initial_state.get("stop_request")
        self._last_check_request: Optional[float] = initial_state.get("check_folders_request")
        # True once a stop request newer than startup was seen; the daemon then winds down.
        self.stop_requested = False

    def begin_cycle(self) -> None:
        """Mark the start of a poll cycle, clearing any earlier interruption."""
        self.cycle_interrupted = False
        self._sync_log_override(self._read_state())

    def _check_stop(self, state: ControlState) -> bool:
        if state.get("stop_request") != self._initial_stop_request:
            self.stop_requested = True
        return self.stop_requested

    def _run_requested_folder_check(self, state: ControlState) -> None:
        """Run the folder check once per new request (it does not touch the countdown or the pause)."""
        request = state.get("check_folders_request")
        if request != self._last_check_request:
            self._last_check_request = request
            if self._on_check_folders is not None:
                self._on_check_folders()

    def _sync_log_override(self, state: ControlState) -> None:
        override = state.get("log_override")
        if override != self._last_log_override:
            self._last_log_override = override
            if self._on_log_override is not None:
                self._on_log_override(override)

    def checkpoint(self) -> bool:
        """Return True if work should stop now because polling is paused or a stop was requested.

        Meant to be passed as should_pause to the ingest/queue loops, which call
        it at safe boundaries (between emails and between jobs).
        """
        state = self._read_state()
        self._sync_log_override(state)
        if self._check_stop(state) or state["paused"]:
            self.cycle_interrupted = True
            return True
        return False

    @staticmethod
    def _make_database_reader(connection: sqlite3.Connection) -> Callable[[], ControlState]:
        last_known = _default_state()

        def read() -> ControlState:
            nonlocal last_known
            try:
                last_known = read_control_state(connection)
            except sqlite3.Error:
                pass  # e.g. "database is locked": keep the last known state this tick
            return last_known

        return read

    def wait(
        self,
        seconds: float,
        on_change: Optional[Callable[[bool, Optional[float]], None]] = None,
    ) -> WaitResult:
        """Block until the interval has run down or a poll is forced.

        The countdown freezes while paused. A forced poll requested during a
        pause is not acted on; it stays pending and fires on resume.
        on_change(paused, next_poll_at) fires at the start and whenever the
        paused state flips; next_poll_at is epoch seconds, or None while paused.
        A stop request ends the wait at once ("stop"), paused or not.
        """
        remaining = float(seconds)
        last_tick = self._clock()
        was_paused = False
        reported_paused: Optional[bool] = None

        while True:
            state = self._read_state()
            self._sync_log_override(state)
            if self._check_stop(state):
                return "stop"
            self._run_requested_folder_check(state)
            paused = state["paused"]
            now = self._clock()
            elapsed = now - last_tick
            last_tick = now

            # Skip the tick in which a pause ends: the paused stretch isn't counted.
            if not paused and not was_paused:
                remaining -= elapsed
            was_paused = paused

            if on_change is not None and paused != reported_paused:
                reported_paused = paused
                on_change(paused, None if paused else time.time() + max(remaining, 0.0))

            if not paused:
                request = state["poll_now_request"]
                if request is not None and request != self.last_handled_request:
                    self.last_handled_request = request
                    return "forced"
                if remaining <= 0:
                    return "elapsed"
                self._sleep(min(self._tick_seconds, remaining))
            else:
                self._sleep(self._tick_seconds)
