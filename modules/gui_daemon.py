"""Toolkit-independent daemon status and control for the GUI's control bar.

`read_snapshot()` gathers what the daemon publishes (lock probe, status file,
control table) and `build_view()` turns it into the texts and enabled/disabled
states of the widgets. The `do_*` functions are the button actions; each
returns an error message, or None on success. Nothing here imports Tk.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Callable, Optional

from modules import config_manager, daemon_control, daemon_launcher, daemon_lock
from modules.file_cache import StatCache

CONTROL_READ_INTERVAL_SECONDS = 3.0  # how often the control table in state.db is read
CONTROL_FRESH_SECONDS = 2.0  # a control read this recent outranks the (lagging) status file
INTENT_SECONDS = 2.5  # after the GUI itself switched the log, show what was asked for while the daemon catches up
STARTING_TIMEOUT_SECONDS = 20.0  # "Starting..." is dropped if no daemon appears by then
STOPPING_HINT_SECONDS = 45.0  # after this, suggest the daemon may be an older version
STOPPING_TIMEOUT_SECONDS = 120.0  # after this, Stop is offered again

DOT_RUNNING = "running"
DOT_PAUSED = "paused"
DOT_STOPPED = "stopped"


@dataclass(frozen=True)
class DaemonSnapshot:
    running: bool = False
    pid: Optional[int] = None
    paused: bool = False
    next_poll_at: Optional[float] = None
    current_repo: Optional[str] = None
    log_on: bool = False  # effective terminal-log state: the override, else terminal_log.enabled
    restart_needed: bool = False  # config.json settings differ from the ones the daemon started with
    polling_idle: bool = False  # polling.enabled was off when it started: it polls only on Poll now


@dataclass(frozen=True)
class ControlBarView:
    dot: str
    status_text: str
    countdown_text: str
    start_enabled: bool
    stop_enabled: bool
    pause_text: str
    pause_enabled: bool
    poll_enabled: bool
    log_enabled: bool
    log_checked: bool
    restart_visible: bool = False
    restart_enabled: bool = False
    check_enabled: bool = False


def _compute_settings() -> tuple[Optional[str], Optional[bool]]:
    """(fingerprint, terminal_log.enabled) of the current config.json; (None, None) while it is unreadable."""
    try:
        config = config_manager.read_config_strict()
    except config_manager.ConfigUnreadableError:
        return None, None  # e.g. mid-edit: say "unknown" instead of comparing defaults
    try:
        return (
            config_manager.get_config_fingerprint(config),
            bool(config_manager.get_terminal_log_settings(config)["enabled"]),
        )
    except Exception:
        return None, None


class SnapshotReader:
    """Reads the daemon's published state cheaply enough to call every second.

    - config.json is re-read only when its size/mtime changed (StatCache).
    - The control table (pause, log override) lives in state.db, so it is read
      only every `control_interval` seconds, plus on demand right after the GUI
      itself changed something (`force_control=True`). In between, pause comes
      from the status file, which the daemon refreshes within about a second.
    """

    def __init__(
        self,
        control_interval: float = CONTROL_READ_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._control_interval = control_interval
        self._settings: StatCache[tuple[Optional[str], Optional[bool]]] = StatCache(
            lambda: [config_manager._config_file_path()], _compute_settings, clock=clock
        )
        self._control: Optional[daemon_control.ControlState] = None
        self._control_at: Optional[float] = None
        self._control_pid: Optional[int] = None
        self._intent_until = 0.0

    def read(self, force_control: bool = False) -> DaemonSnapshot:
        status = daemon_lock.get_daemon_status()
        fingerprint, config_log_enabled = self._settings.get()
        if not status["running"]:
            self._control = self._control_at = self._control_pid = None
            return DaemonSnapshot(log_on=bool(config_log_enabled))

        now = self._clock()
        if force_control:
            self._intent_until = now + INTENT_SECONDS
        due = (
            force_control
            or self._control_at is None
            or now - self._control_at >= self._control_interval
            or status["pid"] != self._control_pid  # a different daemon than the one we last asked
        )
        if due:
            try:
                self._control = daemon_control.get_control_state()
                self._control_at = now
                self._control_pid = status["pid"]
            except sqlite3.Error:
                pass  # busy database: keep what we had and use the status file

        control = self._control
        fresh = control is not None and self._control_at is not None and now - self._control_at < CONTROL_FRESH_SECONDS
        # Right after a control read the table is the newest truth (the status file lags ~1 s behind
        # a pause/resume); later the status file is newer than a few-seconds-old control read.
        paused = control["paused"] if fresh and control is not None else bool(status["paused"])
        log_override = control["log_override"] if control is not None else None
        intended_log = bool(config_log_enabled) if log_override is None else log_override
        # The daemon says whether the log file is really open (a failed open leaves it off). Right after
        # the GUI changed the switch, show what was asked for; the daemon reports back within a second.
        reported_log = status.get("log_active")
        log_on = intended_log if reported_log is None or now < self._intent_until else reported_log

        # Only a daemon that published its fingerprint can be compared (an older one cannot), and an
        # unreadable config.json is "unknown", never "changed".
        published = status.get("config_fingerprint")
        restart_needed = published is not None and fingerprint is not None and published != fingerprint

        current_job = status.get("current_job")
        return DaemonSnapshot(
            running=True,
            pid=status["pid"],
            paused=paused,
            next_poll_at=status["next_poll_at"],
            current_repo=current_job.get("repo") if isinstance(current_job, dict) else None,
            log_on=log_on,
            restart_needed=restart_needed,
            polling_idle=bool(status.get("polling_idle")),
        )


def read_snapshot() -> DaemonSnapshot:
    """One-off read with a fresh reader (nothing cached); the GUI keeps a SnapshotReader instead."""
    return SnapshotReader().read(force_control=True)


def format_countdown(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def build_view(
    snapshot: DaemonSnapshot,
    now: float,
    starting_since: Optional[float] = None,
    stopping_since: Optional[float] = None,
) -> ControlBarView:
    """Texts and enabled states for the control bar.

    starting_since / stopping_since are the times the GUI's Start / Stop were
    clicked (None when not pending); they only matter until the daemon appears
    or disappears, or their timeout passes.
    """
    starting = (
        starting_since is not None
        and not snapshot.running
        and now - starting_since < STARTING_TIMEOUT_SECONDS
    )
    stopping = (
        stopping_since is not None
        and snapshot.running
        and now - stopping_since < STOPPING_TIMEOUT_SECONDS
    )

    if not snapshot.running:
        return ControlBarView(
            dot=DOT_STOPPED,
            status_text="Daemon starting…" if starting else "Daemon not running",
            countdown_text="",
            start_enabled=not starting,
            stop_enabled=False,
            pause_text="Pause",
            pause_enabled=False,
            poll_enabled=False,
            log_enabled=False,
            log_checked=snapshot.log_on,
        )

    pid_text = f" (PID {snapshot.pid})" if snapshot.pid else ""
    if stopping:
        waited = now - (stopping_since or now)
        hint = (
            " – not stopping? It may be an older version; end it from its terminal once."
            if waited >= STOPPING_HINT_SECONDS
            else ""
        )
        status_text = f"Daemon stopping{pid_text}…{hint}"
    elif snapshot.paused:
        status_text = f"Daemon paused{pid_text}"
    else:
        status_text = f"Daemon running{pid_text}"

    if snapshot.paused:
        countdown = "Polling paused"
    elif snapshot.current_repo:
        countdown = f"Processing {snapshot.current_repo}"
    elif snapshot.polling_idle:
        countdown = "Polling is off: use Poll now"
    elif snapshot.next_poll_at is None:
        countdown = "Polling…"
    elif snapshot.next_poll_at - now <= 0:
        countdown = "Polling…"
    else:
        countdown = f"Next poll in {format_countdown(snapshot.next_poll_at - now)}"

    return ControlBarView(
        dot=DOT_PAUSED if snapshot.paused else DOT_RUNNING,
        status_text=status_text,
        countdown_text="" if stopping else countdown,
        start_enabled=False,
        stop_enabled=not stopping,
        pause_text="Resume" if snapshot.paused else "Pause",
        pause_enabled=not stopping,
        poll_enabled=not stopping,  # Poll now also works while paused: one poll, then it stays paused
        check_enabled=not stopping,  # a folder check is allowed while paused: it only reads folders
        log_enabled=not stopping,
        log_checked=snapshot.log_on,
        restart_visible=snapshot.restart_needed,
        restart_enabled=not stopping,
    )


def _guarded(action: Callable[[], object]) -> Optional[str]:
    """Run a control write; return a readable error instead of raising (a busy db is transient)."""
    try:
        action()
    except sqlite3.Error as exc:
        return f"Could not reach the control channel ({exc}); try again."
    except OSError as exc:
        return str(exc)
    return None


def do_set_paused(paused: bool) -> Optional[str]:
    return _guarded(lambda: daemon_control.set_paused(paused))


def do_poll_now() -> Optional[str]:
    return _guarded(daemon_control.request_poll_now)


def do_single_poll() -> Optional[str]:
    return _guarded(daemon_control.request_single_poll)


def do_check_folders() -> Optional[str]:
    return _guarded(daemon_control.request_check_folders)


def do_set_log(on: bool) -> Optional[str]:
    return _guarded(lambda: daemon_control.set_log_override(on))


def do_stop() -> Optional[str]:
    return _guarded(daemon_control.request_stop)


def do_start() -> Optional[str]:
    """Launch a detached daemon unless one is already running."""
    if daemon_lock.is_daemon_running():
        return "A daemon is already running."
    return _guarded(daemon_launcher.start_daemon)
