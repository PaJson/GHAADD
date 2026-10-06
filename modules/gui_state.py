"""Remembered GUI window state (size, position, maximized), stored in config.json.

Lives under the "gui.window" section so it can be edited or deleted by hand to
reset the window. The daemon ignores the section. Toolkit-independent: the
window passes in the screen size and applies the result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from modules import config_manager

SECTION = "gui.window"
MIN_VISIBLE_PIXELS = 100  # how much of the window must stay on screen when restoring a position

# Tk reports absolute positions as "+x+y" (a position left of/above the screen as "+-x"). The
# "-x" form means "from the right/bottom edge" and is never returned by geometry(), so it is rejected.
_GEOMETRY_PATTERN = re.compile(r"^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$")


@dataclass(frozen=True)
class WindowState:
    width: int
    height: int
    x: Optional[int] = None
    y: Optional[int] = None
    maximized: bool = False


def parse_geometry(geometry: str) -> Optional[WindowState]:
    """Parse a Tk geometry() string like "1280x720+40+-8" (None when it does not match)."""
    match = _GEOMETRY_PATTERN.match(geometry.strip())
    if not match:
        return None
    width, height = int(match.group(1)), int(match.group(2))
    x, y = int(match.group(3)), int(match.group(4))
    return WindowState(width, height, x, y)


def _as_int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def load_window_state(config: Optional[Mapping[str, Any]] = None) -> Optional[WindowState]:
    """Return the saved window state, or None when nothing usable is stored."""
    config = config if config is not None else config_manager.load_config()
    gui = config.get("gui")
    window = gui.get("window") if isinstance(gui, dict) else None
    if not isinstance(window, dict):
        return None
    width, height = _as_int(window.get("width")), _as_int(window.get("height"))
    if width is None or height is None or width <= 0 or height <= 0:
        return None
    return WindowState(
        width=width,
        height=height,
        x=_as_int(window.get("x")),
        y=_as_int(window.get("y")),
        maximized=window.get("maximized") is True,
    )


def save_window_state(state: WindowState) -> bool:
    """Write the window state to config.json; True when the file changed."""
    changes: dict[str, Any] = {
        f"{SECTION}.width": state.width,
        f"{SECTION}.height": state.height,
        f"{SECTION}.maximized": state.maximized,
    }
    if state.x is not None and state.y is not None:
        changes[f"{SECTION}.x"] = state.x
        changes[f"{SECTION}.y"] = state.y
    return config_manager.set_config_values(changes)


def fit_to_screen(
    state: WindowState,
    screen_x: int,
    screen_y: int,
    screen_width: int,
    screen_height: int,
    min_width: int,
    min_height: int,
) -> WindowState:
    """Make a remembered state safe to apply: not larger than the screen, not off-screen.

    (screen_x, screen_y, screen_width, screen_height) is the whole virtual desktop, so a saved
    position on a monitor that is gone now is dropped and the OS places the window instead.
    """
    width = max(min_width, min(state.width, screen_width))
    height = max(min_height, min(state.height, screen_height))
    x, y = state.x, state.y
    if x is not None and y is not None:
        visible_horizontally = screen_x - width + MIN_VISIBLE_PIXELS <= x <= screen_x + screen_width - MIN_VISIBLE_PIXELS
        visible_vertically = screen_y <= y <= screen_y + screen_height - MIN_VISIBLE_PIXELS
        if not (visible_horizontally and visible_vertically):
            x = y = None
    return WindowState(width, height, x, y, state.maximized)


def to_geometry(state: WindowState) -> str:
    """Tk geometry string for a state ("WxH" or "WxH+X+Y")."""
    if state.x is None or state.y is None:
        return f"{state.width}x{state.height}"
    return f"{state.width}x{state.height}+{state.x}+{state.y}"
