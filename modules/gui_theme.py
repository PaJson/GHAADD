"""Light and dark color palettes for the GUI, without any toolkit code.

`main_gui.MainWindow.apply_dark_mode` applies them; the choice lives in config.json as `gui.dark_mode`.
"""

from typing import Dict

# Every key exists in both palettes: a color is swapped for the other palette's color of the same key.
LIGHT: Dict[str, str] = {
    "window": "#f0f0f0",  # window and frame background
    "field": "#ffffff",  # entries, text areas, canvases
    "text": "#000000",
    "muted": "#666666",
    "error": "#b3261e",
    "warning": "#9a6700",
    "ok": "#2e7d32",
    "stripe": "#f2f5f9",  # every second table row
    "header_bg": "#dde5ef",
    "header_fg": "#1f2d3d",
    "header_active": "#cbd7e6",
    "header_pressed": "#bccbdf",
    "selected": "#0078d4",
    "detail": "#fbfbfb",  # the detail pane under the tables
    "border": "#c8c8c8",
    "axis": "#999999",
    "button": "#e1e1e1",
    "button_active": "#cfe4f7",
    "disabled": "#a0a0a0",
}

DARK: Dict[str, str] = {
    "window": "#2b2d30",
    "field": "#1e1f22",
    "text": "#dfe1e5",
    "muted": "#9da0a8",
    "error": "#ff7b72",
    "warning": "#e3b341",
    "ok": "#56d364",
    "stripe": "#26282b",
    "header_bg": "#3a3d41",
    "header_fg": "#e6e8eb",
    "header_active": "#4a4e53",
    "header_pressed": "#55595f",
    "selected": "#2f6fb5",
    "detail": "#232427",
    "border": "#4a4d52",
    "axis": "#7b7e84",
    "button": "#3c3f43",
    "button_active": "#4a4e53",
    "disabled": "#6b6e74",
}


def palette(dark: bool) -> Dict[str, str]:
    """Return the dark or the light palette."""
    return DARK if dark else LIGHT


def color_swaps(dark: bool) -> Dict[str, str]:
    """Return {other palette's color: this palette's color}, so existing widgets can be recolored in place."""
    new, old = palette(dark), palette(not dark)
    return {old[key].lower(): new[key] for key in new if old[key].lower() != new[key].lower()}


def button_text(dark: bool) -> str:
    """Return the label of the toggle button: it names the mode a click switches to."""
    return "☀ Light" if dark else "\U0001f319 Dark"
