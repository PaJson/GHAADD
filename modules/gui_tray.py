"""System tray support for the GUI: what to notify about, the icon's state and its drawing.

Everything except `TrayIcon` is toolkit-independent and testable without a display or the optional
tray packages. The tray needs `pystray` and `Pillow` (requirements-optional.txt); without them
`tray_available()` is False and the GUI behaves exactly as before.

`TrayIcon` runs pystray in its own thread. Its menu callbacks only put an action name in a queue; the
GUI polls `TrayIcon.pending_actions()` from its Tk loop, so Tk is never touched from the tray thread.
"""

from __future__ import annotations

import base64
import os
import queue
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Sequence

STATE_RUNNING = "running"
STATE_PAUSED = "paused"
STATE_STOPPED = "stopped"

STATE_COLORS = {
    STATE_RUNNING: "#2e8b57",  # green
    STATE_PAUSED: "#d99a00",  # amber
    STATE_STOPPED: "#7a7a7a",  # grey
}
ATTENTION_COLOR = "#d32f2f"  # the red dot: something unread needs a look

ACTION_SHOW = "show"
ACTION_TOGGLE = "toggle"  # show the window if it is hidden, hide it if it is open
ACTION_START = "start"
ACTION_POLL = "poll"
ACTION_SINGLE = "single"
ACTION_PAUSE = "pause"
ACTION_QUIT = "quit"

MAX_NOTIFICATION_NAMES = 3  # repositories listed by name in one notification


@dataclass(frozen=True)
class Notice:
    """A tray notification: a title and a message."""
    title: str
    message: str


def tray_available() -> bool:
    """True when the optional tray packages (pystray + Pillow) can be imported."""
    try:
        import PIL.Image  # noqa: F401
        import pystray  # noqa: F401
    except Exception:  # ImportError, or a missing system library on Linux
        return False
    return True


# ----- what to tell the user -----


def _dynamic(check: Callable[[Any], bool]) -> Any:
    """pystray evaluates a callable `visible=` / `enabled=` each time the menu opens; its type stubs only say bool."""
    return check


def _names(repos: Iterable[str]) -> str:
    """Join up to MAX_NOTIFICATION_NAMES repository names, with "(+N more)" for the rest."""
    unique = sorted({repo for repo in repos if repo})
    shown = ", ".join(unique[:MAX_NOTIFICATION_NAMES])
    extra = len(unique) - MAX_NOTIFICATION_NAMES
    return f"{shown} (+{extra} more)" if extra > 0 and shown else shown


def warnings_notice(rows: Sequence[Any]) -> Optional[Notice]:
    """Build one notice for new warning rows (anything with .repo and .message): the text for one, a count for several."""
    if not rows:
        return None
    if len(rows) == 1:
        row = rows[0]
        return Notice("GHAADD warning", f"{row.repo}: {row.message}" if row.repo else str(row.message))
    names = _names(row.repo for row in rows)
    return Notice("GHAADD warnings", f"{len(rows)} new warnings" + (f": {names}" if names else "") + ".")


def failed_jobs_notice(jobs: Sequence[dict[str, Any]]) -> Optional[Notice]:
    """Build the notice for jobs that failed (one named job, or a count); None when there are none."""
    if not jobs:
        return None
    if len(jobs) == 1:
        job = jobs[0]
        tag = f" {job['tag']}" if job.get("tag") else ""
        return Notice("GHAADD download failed", f"{job.get('repo', '')}{tag} could not be downloaded.")
    return Notice("GHAADD downloads failed", f"{len(jobs)} jobs failed: {_names(job.get('repo', '') for job in jobs)}.")


def unmapped_notice(repos: Sequence[str]) -> Optional[Notice]:
    """Build the notice for repositories that have no destination yet; None when there are none."""
    if not repos:
        return None
    if len(repos) == 1:
        return Notice("GHAADD: new repository", f"{repos[0]} has no destination yet. Set it up in the Mappings tab.")
    return Notice("GHAADD: new repositories", f"{len(repos)} repositories have no destination yet: {_names(repos)}.")


def limit_notice(repos: Sequence[str]) -> Optional[Notice]:
    """Build the notice for repositories whose folder is over its limit; None when there are none."""
    if not repos:
        return None
    if len(repos) == 1:
        return Notice("GHAADD: folder over its limit", f"{repos[0]} has more release folders than its limit allows.")
    return Notice("GHAADD: folders over their limit", f"{len(repos)} repositories are over their limit: {_names(repos)}.")


def daemon_stopped_notice() -> Notice:
    """Build the notice shown when the daemon stopped without Stop/Restart from the GUI."""
    return Notice("GHAADD daemon stopped", "The polling daemon is no longer running. Nothing is downloaded until it is started.")


class NewItemTracker:
    """Reports the items that are new since the last call; the first call only records what is already there."""

    def __init__(self) -> None:
        """Start with no baseline: the first look only records what exists, so there is no notice flood at startup."""
        self._known: Optional[set[str]] = None

    def new(self, items: Iterable[str]) -> list[str]:
        """Return the items not seen in the previous call (empty on the very first call, which sets the baseline)."""
        current = set(items)
        if self._known is None:
            self._known = current
            return []
        fresh = sorted(current - self._known)
        self._known = current
        return fresh


# ----- icon state -----


def icon_state(running: bool, paused: bool) -> str:
    """Pick the icon state: stopped, paused or running."""
    if not running:
        return STATE_STOPPED
    return STATE_PAUSED if paused else STATE_RUNNING


def tooltip_text(app_name: str, state: str, unread_warnings: int, unmapped: int, countdown: str = "") -> str:
    """The hover text of the tray icon, e.g. "GHAADD: running (next poll in 4:20), 2 warnings"."""
    first = {STATE_RUNNING: "running", STATE_PAUSED: "paused", STATE_STOPPED: "daemon not running"}[state]
    if state == STATE_RUNNING and countdown:
        first += f" (next poll in {countdown})"
    parts = [f"{app_name}: {first}"]
    if unread_warnings:
        parts.append(f"{unread_warnings} unread warning{'s' if unread_warnings != 1 else ''}")
    if unmapped:
        parts.append(f"{unmapped} unmapped")
    return ", ".join(parts)


def needs_attention(unread_warnings: int, unmapped: int, failed_unseen: int = 0) -> bool:
    """True when the icon should show its attention dot (unread warnings, unmapped or failed jobs)."""
    return unread_warnings > 0 or unmapped > 0 or failed_unseen > 0


def draw_icon(state: str, attention: bool, size: int = 64, base_image_path: Optional[str] = None) -> Any:
    """A tray image: the app icon when its file exists, else a coloured disc with a download arrow.

    The state colour is a ring around the picture, and a red dot in the corner means "look at me".
    Needs Pillow (imported here so the rest of the module works without it).
    """
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    color = STATE_COLORS.get(state, STATE_COLORS[STATE_STOPPED])
    drawn_base = False
    if base_image_path and os.path.isfile(base_image_path):
        try:
            base = Image.open(base_image_path).convert("RGBA").resize((size - 12, size - 12))
            image.paste(base, (6, 6), base)
            drawn_base = True
        except Exception:  # a broken icon file falls back to the drawn one
            drawn_base = False
    if drawn_base:
        draw.ellipse((1, 1, size - 2, size - 2), outline=color, width=max(3, size // 14))
    else:
        draw.ellipse((2, 2, size - 3, size - 3), fill=color)
        middle, top, bottom, half = size // 2, size * 0.22, size * 0.72, size * 0.2
        line = max(4, size // 9)
        draw.line((middle, top, middle, bottom - half * 0.6), fill="white", width=line)
        draw.polygon([(middle - half, bottom - half * 1.1), (middle + half, bottom - half * 1.1), (middle, bottom)], fill="white")
        draw.line((size * 0.28, size * 0.82, size * 0.72, size * 0.82), fill="white", width=line // 2 + 1)
    if attention:
        radius = size * 0.2
        centre = (size - radius - 2, radius + 2)
        draw.ellipse((centre[0] - radius, centre[1] - radius, centre[0] + radius, centre[1] + radius), fill=ATTENTION_COLOR, outline="white", width=2)
    return image


# ----- how Windows names the app -----

WINDOWS_ID_KEY = r"Software\Classes\AppUserModelId"


def register_windows_identity(app_id: str, display_name: str, icon_path: str, registry: Any = None) -> bool:
    """Tell Windows what to call the app in notifications and their settings (otherwise "Python").

    Writes the per-user registry key AppUserModelId/<app_id> (no admin rights) with a display name and icon, only
    when they differ. Does nothing and returns False off Windows or when the registry cannot be written.
    """
    if registry is None:
        try:
            import winreg as registry  # type: ignore[no-redef]
        except ImportError:
            return False
    values = {"DisplayName": display_name}
    if icon_path and os.path.isfile(icon_path):
        values["IconUri"] = icon_path
    try:
        with registry.CreateKeyEx(registry.HKEY_CURRENT_USER, f"{WINDOWS_ID_KEY}\\{app_id}", 0, registry.KEY_READ | registry.KEY_WRITE) as key:
            for name, value in values.items():
                try:
                    current, _kind = registry.QueryValueEx(key, name)
                except OSError:
                    current = None
                if current != value:
                    registry.SetValueEx(key, name, 0, registry.REG_SZ, value)
    except OSError:
        return False
    return True


# ----- Windows toast notifications (named after the app, not after python) -----

_CREATE_NO_WINDOW = 0x08000000


def _ps_quote(text: str) -> str:
    """Quote text as a single-quoted PowerShell string."""
    return "'" + text.replace("'", "''") + "'"


def windows_toast_script(app_id: str, title: str, message: str) -> str:
    """PowerShell (5.1) that shows a toast under `app_id`; Windows lists it by the name registered for that id."""
    return "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null",
            "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null",
            "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument",
            "$xml.LoadXml('<toast><visual><binding template=\"ToastGeneric\"><text/><text/></binding></visual></toast>')",
            "$texts = $xml.GetElementsByTagName('text')",
            f"$texts.Item(0).AppendChild($xml.CreateTextNode({_ps_quote(title)})) | Out-Null",
            f"$texts.Item(1).AppendChild($xml.CreateTextNode({_ps_quote(message)})) | Out-Null",
            "$toast = New-Object Windows.UI.Notifications.ToastNotification $xml",
            f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier({_ps_quote(app_id)}).Show($toast)",
        ]
    )


def send_windows_toast(app_id: str, notice: "Notice", runner: Any = None) -> bool:
    """Show a toast; True when PowerShell reported success. Never raises."""
    encoded = base64.b64encode(windows_toast_script(app_id, notice.title, notice.message).encode("utf-16-le")).decode("ascii")
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
    try:
        if runner is not None:
            return runner(command).returncode == 0
        flags = _CREATE_NO_WINDOW if sys.platform == "win32" else 0
        return subprocess.run(command, capture_output=True, timeout=30, creationflags=flags, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# ----- the menu follows the real state -----


@dataclass(frozen=True)
class MenuState:
    """What the tray menu offers right now."""

    window_visible: bool
    daemon_running: bool
    daemon_paused: bool

    @property
    def can_start(self) -> bool:
        """Start daemon is offered only while no daemon runs."""
        return not self.daemon_running

    @property
    def can_poll(self) -> bool:
        """Poll now is offered while a daemon runs."""
        return self.daemon_running  # also while paused: it polls once and stays paused

    @property
    def can_pause(self) -> bool:
        """Pause/Resume is offered while a daemon runs."""
        return self.daemon_running  # also while paused: the entry then reads "Resume polling"


def toggle_label(app_name: str, window_visible: bool) -> str:
    """Return the first tray menu entry: "Hide <app>" while the window is visible, else "Show <app>"."""
    return f"Hide {app_name}" if window_visible else f"Show {app_name}"


# ----- the pystray adapter -----


class TrayIcon:
    """The tray icon itself. Create it, call `start()`, then poll `pending_actions()` from the GUI loop."""

    def __init__(
        self,
        app_name: str,
        paused_label: Callable[[], str],
        base_image_path: Optional[str] = None,
        toast_app_id: Optional[str] = None,
    ) -> None:
        """Remember the app name, the label callback for Pause/Resume and the optional icon image and toast id."""
        self._toast_app_id = toast_app_id  # Windows: show notifications as toasts under this app id
        self._app_name = app_name
        self._paused_label = paused_label
        self._base_image_path = base_image_path
        self._actions: "queue.SimpleQueue[str]" = queue.SimpleQueue()
        self._icon: Any = None
        self._shown: tuple[str, bool, str] = ("", False, "")  # (state, attention, tooltip) last given to the system
        # Written by the GUI thread, read by the menu callbacks (tray thread): plain values, never Tk objects.
        self._menu_state = MenuState(window_visible=True, daemon_running=False, daemon_paused=False)

    def start(self) -> bool:
        """Show the icon. False when the system has no usable tray (the GUI then works without it)."""
        try:
            import pystray

            menu = pystray.Menu(
                pystray.MenuItem(
                    lambda _item: toggle_label(self._app_name, self._menu_state.window_visible),
                    lambda *_: self._actions.put(ACTION_TOGGLE),
                    default=True,
                ),
                pystray.MenuItem(
                    "Start daemon", lambda *_: self._actions.put(ACTION_START), visible=_dynamic(lambda _item: self._menu_state.can_start)
                ),
                pystray.MenuItem(
                    "Poll now", lambda *_: self._actions.put(ACTION_POLL), enabled=_dynamic(lambda _item: self._menu_state.can_poll)
                ),
                pystray.MenuItem(
                    "Poll one item", lambda *_: self._actions.put(ACTION_SINGLE), enabled=_dynamic(lambda _item: self._menu_state.can_poll)
                ),
                pystray.MenuItem(
                    lambda _item: self._paused_label(),
                    lambda *_: self._actions.put(ACTION_PAUSE),
                    enabled=_dynamic(lambda _item: self._menu_state.can_pause),
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(f"Quit {self._app_name} window", lambda *_: self._actions.put(ACTION_QUIT)),
            )
            self._icon = pystray.Icon(
                "ghaadd", draw_icon(STATE_STOPPED, False, base_image_path=self._base_image_path), self._app_name, menu
            )
            self._icon.run_detached()
        except Exception:
            self._icon = None
            return False
        return True

    @property
    def active(self) -> bool:
        """True once the tray icon is running."""
        return self._icon is not None

    def pending_actions(self) -> list[str]:
        """Return and clear the menu actions queued by the tray thread (the GUI thread then handles them)."""
        actions: list[str] = []
        while True:
            try:
                actions.append(self._actions.get_nowait())
            except queue.Empty:
                return actions

    def set_menu_state(self, state: MenuState) -> None:
        """Tell the menu what is true now (cheap; the system menu is only refreshed when something differs)."""
        if self._icon is None or state == self._menu_state:
            return
        self._menu_state = state
        try:
            self._icon.update_menu()
        except Exception:
            pass

    def update(self, state: str, attention: bool, tooltip: str) -> None:
        """Change the picture and hover text; the system is only touched when something differs."""
        if self._icon is None or (state, attention, tooltip) == self._shown:
            return
        try:
            if (state, attention) != self._shown[:2]:
                self._icon.icon = draw_icon(state, attention, base_image_path=self._base_image_path)
            self._icon.title = tooltip
            self._icon.update_menu()
        except Exception:
            return
        self._shown = (state, attention, tooltip)

    def notify(self, notice: Notice) -> None:
        """Show a notice: a Windows toast when possible (in a thread), else a tray balloon."""
        if self._icon is None:
            return
        if self._toast_app_id and sys.platform == "win32":
            # PowerShell takes a second or so: do it in a thread, and fall back to the tray balloon if it fails
            threading.Thread(target=self._toast_or_balloon, args=(notice,), daemon=True).start()
            return
        self._balloon(notice)

    def _toast_or_balloon(self, notice: Notice) -> None:
        """Try the Windows toast and fall back to the tray balloon if it fails."""
        if not send_windows_toast(self._toast_app_id or "", notice):
            self._balloon(notice)

    def _balloon(self, notice: Notice) -> None:
        """Show the notice as a tray balloon; some Linux trays cannot, which is ignored."""
        icon = self._icon
        if icon is None:
            return
        try:
            icon.notify(notice.message, notice.title)
        except Exception:  # not every Linux tray can show balloons
            pass

    def stop(self) -> None:
        """Remove the tray icon (safe to call twice)."""
        icon, self._icon = self._icon, None
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass
