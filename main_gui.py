"""GHAADD GUI (Tkinter): monitor and control window for the polling daemon.

Tabs: Mappings (edit mapping.json), Terminal log, and the status tabs (Warnings, Completed, Folder limits,
Unmapped); dialogs: Settings, Gmail & GitHub, Doctor, Stats. The widgets here are a thin view: parsing,
validation, ordering and data loading live in the toolkit-independent modules (gui_forms, gui_data, ...).
"""

from __future__ import annotations

import argparse
import collections
import os
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import date, datetime
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Iterable, Literal, Optional

from modules import (
    autostart,
    backup_manager,
    config_manager,
    connection_tests,
    daemon_launcher,
    daemon_lock,
    env_manager,
    gui_daemon,
    gui_data,
    gui_doctor,
    gui_forms,
    gui_instance,
    gui_state,
    gui_theme,
    gui_tooltips,
    gui_tray,
    gui_viewer,
    log_tail,
    mapping_manager,
    shortcuts,
    stats,
    status_tabs,
    warning_types,
    work_folders,
)
from modules.app_info import APP_NAME, APP_USER_MODEL_ID, __version__

Anchor = Literal["nw", "n", "ne", "w", "center", "e", "sw", "s", "se"]

APP_TITLE = f"{APP_NAME} {__version__}"
# The app icon is looked up here: ghaadd.ico (preferred on Windows) or ghaadd.png.
ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
WINDOWS_APP_ID = APP_USER_MODEL_ID  # own taskbar identity, so the taskbar shows our icon instead of Python's
DAEMON_REFRESH_INTERVAL_MS = 1000
UNEXPECTED_EXIT_WINDOW_SECONDS = 120.0  # a daemon this GUI started that dies within this long is reported
DEFAULT_STATUS_TEXT = ""  # the footer is empty until a message is shown

COLOR_ERROR = "#b3261e"
COLOR_WARNING = "#9a6700"
COLOR_MUTED = "#666666"
COLOR_STRIPE = "#f2f5f9"  # every second table row
COLOR_HEADER_BG = "#dde5ef"  # table column headers
COLOR_HEADER_FG = "#1f2d3d"
COLOR_SELECTED = "#0078d4"
COLOR_OK = "#2e7d32"
COLOR_FIELD = "#ffffff"  # entries, text areas, canvases
COLOR_DETAIL = "#fbfbfb"  # the detail pane under the tables
COLOR_BORDER = "#c8c8c8"
COLOR_AXIS = "#999999"  # chart axes


def set_palette(dark: bool) -> None:
    """Point the COLOR_* constants at the light or dark palette (widgets built afterwards pick them up)."""
    global COLOR_ERROR, COLOR_WARNING, COLOR_MUTED, COLOR_STRIPE, COLOR_HEADER_BG, COLOR_HEADER_FG
    global COLOR_SELECTED, COLOR_OK, COLOR_FIELD, COLOR_DETAIL, COLOR_BORDER, COLOR_AXIS
    colors = gui_theme.palette(dark)
    COLOR_ERROR, COLOR_WARNING, COLOR_MUTED = colors["error"], colors["warning"], colors["muted"]
    COLOR_STRIPE, COLOR_HEADER_BG, COLOR_HEADER_FG = colors["stripe"], colors["header_bg"], colors["header_fg"]
    COLOR_SELECTED, COLOR_OK, COLOR_FIELD = colors["selected"], colors["ok"], colors["field"]
    COLOR_DETAIL, COLOR_BORDER, COLOR_AXIS = colors["detail"], colors["border"], colors["axis"]

# Row icon per status (first column). Inactive (active: false in mapping.json) wins over runtime state.
STATUS_ICONS = {
    "Running": "▶",
    "Queued": "◐",  # not an emoji (hourglass U+23F3 is missing from the fonts of many Linux systems)
    "Waiting": "◷",
    "Idle": "○",
    "Inactive": "⊘",
    "Failed": "✖",
}

STATUS_LEGEND = gui_tooltips.STATUS_LEGEND_TITLE + "\n" + "\n".join(
    f"{icon}  {name}: {gui_tooltips.STATUS_HINTS[name]}" for name, icon in STATUS_ICONS.items()
)


def pick_directory(parent: tk.Misc, variable: tk.Variable) -> None:
    """Let the user choose a folder and store it (normalized) in `variable`."""
    current = str(variable.get()).strip()
    chosen = filedialog.askdirectory(
        parent=parent, initialdir=current if os.path.isdir(current) else None, title="Select folder"
    )
    if chosen:
        variable.set(os.path.normpath(chosen))


def open_in_file_manager(path: str) -> None:
    """Open a folder in the system file manager, or a file in its default program (Explorer / Finder / xdg-open)."""
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def attach_tooltip(widget: tk.Misc, text: str) -> None:
    """Show `text` in a hover box while the pointer rests on `widget`."""
    tip = Tooltip(widget)
    widget.bind("<Enter>", lambda event: tip.schedule(text, event.x_root + 12, event.y_root + 18), add="+")
    widget.bind("<Leave>", lambda _event: tip.hide(), add="+")
    widget.bind("<ButtonPress>", lambda _event: tip.hide(), add="+")


def center_dialog(dialog: tk.Toplevel, parent: tk.Misc, focus: Optional[tk.Widget] = None) -> None:
    """Put a dialog in the middle of its parent window (or of the screen when the parent is not visible),
    bring it to the front and give it the keyboard focus (on `focus` when given)."""
    dialog.update_idletasks()
    border_x = border_y = 0  # window frame around the content (title bar, borders); geometry() positions the frame
    if parent.winfo_viewable():
        area = (parent.winfo_rootx(), parent.winfo_rooty(), parent.winfo_width(), parent.winfo_height())
        border_x, border_y = parent.winfo_rootx() - parent.winfo_x(), parent.winfo_rooty() - parent.winfo_y()
    else:
        area = (dialog.winfo_vrootx(), dialog.winfo_vrooty(), dialog.winfo_vrootwidth(), dialog.winfo_vrootheight())
    x, y = gui_state.center_over(
        *area, dialog.winfo_reqwidth(), dialog.winfo_reqheight(),
        dialog.winfo_vrootx(), dialog.winfo_vrooty(), dialog.winfo_vrootwidth(), dialog.winfo_vrootheight(),
    )
    dialog.geometry(f"+{x - border_x}+{y - border_y}")
    dialog.lift()
    dialog.focus_force()
    if focus is not None:
        focus.focus_set()


def reveal_window(window: tk.Toplevel, place: Callable[[], None]) -> None:
    """Show a window that was built withdrawn without anyone seeing it settle: map it off screen, lay it out, move it.

    A window's first appearance lands at the screen's corner and re-lays itself out in plain sight. So the first map
    happens beyond the right edge of the desktop, `place` then moves the finished window to where it belongs in one
    step. Transparency is not used: Windows 11 still draws the frame of a transparent window (seen as an empty
    outline in the corner). A window manager that refuses off-screen positions shows it where it clamps it.
    """
    window.geometry(f"+{window.winfo_vrootx() + window.winfo_vrootwidth() + 100}+{window.winfo_vrooty()}")
    window.deiconify()
    window.update()  # sizes, wrapping and the widgets' own Configure handling settle here, out of sight
    place()
    window.update()


def format_status_title(title: str, count: int) -> str:
    """"Unmapped (2)": the number of repositories that still need a destination (not an unread count)."""
    return status_tabs.format_title(title, count)


def describe_error(exc: BaseException) -> str:
    """One readable line for the errors a mapping/config save can raise."""
    if isinstance(exc, mapping_manager.MappingValidationError):
        return "; ".join(exc.errors)
    if isinstance(exc, KeyError):
        return "This repository is no longer in mapping.json."
    return str(exc) or exc.__class__.__name__


class Tooltip:
    """Small hover text window for a widget (shown after a short delay)."""

    DELAY_MS = 450
    WRAP_PIXELS = 640  # longer text wraps instead of running off the screen

    def __init__(self, widget: tk.Misc) -> None:
        """Attach a hover-tooltip helper to `widget`."""
        self._widget = widget
        self._window: Optional[tk.Toplevel] = None
        self._job: Optional[str] = None

    def schedule(self, text: str, x_root: int, y_root: int) -> None:
        """Show the tooltip after a short delay at the mouse position (restarting any pending one)."""
        self.hide()
        self._job = self._widget.after(self.DELAY_MS, lambda: self._show(text, x_root, y_root))

    def hide(self) -> None:
        """Cancel a pending tooltip and close a visible one."""
        if self._job is not None:
            self._widget.after_cancel(self._job)
            self._job = None
        if self._window is not None:
            self._window.destroy()
            self._window = None

    def _show(self, text: str, x_root: int, y_root: int) -> None:
        """Create the tooltip window with the wrapped text, kept on screen."""
        self._job = None
        window = tk.Toplevel(self._widget)
        window.wm_overrideredirect(True)
        tk.Label(
            window, text=text, justify="left", wraplength=self.WRAP_PIXELS, background="#ffffe1", foreground="#000000",
            relief="solid", borderwidth=1, padx=6, pady=3,
        ).pack()
        window.update_idletasks()
        widget = self._widget
        x, y = gui_state.place_popup(
            x_root, y_root, window.winfo_reqwidth(), window.winfo_reqheight(),
            widget.winfo_vrootx(), widget.winfo_vrooty(), widget.winfo_vrootwidth(), widget.winfo_vrootheight(),
        )
        window.wm_geometry(f"+{x}+{y}")
        self._window = window


class FilterEntry(ttk.Frame):
    """A filter field with a small clear (x) button to its right.

    The button is always visible: dark while the field holds text, greyed out while it is empty.
    Escape in the field clears it too. `width` is the field's width in characters.
    """

    def __init__(self, master: tk.Misc, textvariable: tk.StringVar, width: int = 20) -> None:
        """Build the entry with its clear (x) button, bound to `textvariable`."""
        super().__init__(master)
        self._variable = textvariable
        self.columnconfigure(0, weight=1)
        self.entry = ttk.Entry(self, textvariable=textvariable, width=width)
        self.entry.grid(row=0, column=0, sticky="ew")
        self.clear_button = ttk.Button(self, text="✕", width=3, command=self.clear)
        self.clear_button.grid(row=0, column=1, padx=(4, 0))
        attach_tooltip(self.clear_button, gui_tooltips.CONTROL_HELP["clear_filter"])
        self.entry.bind("<Escape>", lambda _event: self.clear())
        textvariable.trace_add("write", lambda *_: self._sync())
        self._sync()

    def clear(self) -> None:
        """Empty the field and put the cursor back in it."""
        self._variable.set("")
        self.entry.focus_set()

    def _sync(self) -> None:
        """Enable the clear button only while the field has text."""
        self.clear_button.state(["!disabled"] if str(self._variable.get()) else ["disabled"])


def fit_text(text: str, max_pixels: int, measure: Callable[[str], int]) -> str:
    """Return `text`, or its longest prefix plus an ellipsis that still fits `max_pixels`."""
    if measure(text) <= max_pixels:
        return text
    low, high = 0, len(text)
    while low < high:  # longest prefix whose "prefix\u2026" fits
        middle = (low + high + 1) // 2
        if measure(text[:middle].rstrip() + "\u2026") <= max_pixels:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + "\u2026"


class ControlBar(ttk.Frame):
    """Daemon status indicator and controls; MainWindow feeds it a gui_daemon.ControlBarView."""

    DOT_COLORS = {
        gui_daemon.DOT_RUNNING: "#2e9e4f",
        gui_daemon.DOT_PAUSED: "#d89a00",
        gui_daemon.DOT_STOPPED: "#8a8a8a",
    }

    def __init__(self, master: tk.Misc, status_parent: Optional[tk.Misc] = None) -> None:
        """Build the three button groups; the daemon status goes into `status_parent` (the footer) when given."""
        super().__init__(master, padding=(10, 8))

        # The daemon's status (dot, text, countdown) lives in the window's footer, at the right, when one is given.
        status_frame = ttk.Frame(status_parent if status_parent is not None else self)
        if status_parent is not None:
            status_frame.grid(row=0, column=1, sticky="e", padx=(8, 10))
        else:
            status_frame.grid(row=0, column=0, sticky="w")
        self.status_dot = tk.Label(status_frame, text="\u25cf", fg=self.DOT_COLORS[gui_daemon.DOT_STOPPED], font=("Segoe UI", 12))
        self.status_dot.grid(row=0, column=0, padx=(0, 4))
        self.status_label = ttk.Label(status_frame, text="Checking daemon\u2026")
        self.status_label.grid(row=0, column=1, sticky="w")
        self.countdown_label = ttk.Label(status_frame, text="", foreground=COLOR_MUTED)
        self.countdown_label.grid(row=0, column=2, padx=(16, 0), sticky="w")

        # The buttons sit in three titled groups, centred in the bar, titles centred too.
        self.columnconfigure(0, weight=1)
        groups = ttk.Frame(self)
        groups.grid(row=1, column=0)
        box_padding = (8, 2, 8, 6)
        daemon_box = ttk.LabelFrame(groups, text="Daemon", padding=box_padding, labelanchor="n")
        polling_box = ttk.LabelFrame(groups, text="Polling", padding=box_padding, labelanchor="n")
        folders_box = ttk.LabelFrame(groups, text="Folders", padding=box_padding, labelanchor="n")
        tools_box = ttk.LabelFrame(groups, text="Tools", padding=box_padding, labelanchor="n")
        self.folder_buttons = {key: ttk.Button(folders_box, text=label) for key, label in work_folders.FOLDER_BUTTONS}
        self.start_button = ttk.Button(daemon_box, text="Start")
        self.stop_button = ttk.Button(daemon_box, text="Stop")
        self.pause_button = ttk.Button(daemon_box, text="Pause")
        self.poll_button = ttk.Button(polling_box, text="Poll now")
        self.single_button = ttk.Button(polling_box, text="Poll one")
        self.check_button = ttk.Button(polling_box, text="Check folders")
        self.detailed_log = tk.BooleanVar(value=False)
        self.log_check = ttk.Checkbutton(tools_box, text="Terminal log", variable=self.detailed_log)
        self.restart_button = ttk.Button(daemon_box, text="\u21bb Restart")  # shown only while it is needed
        self.doctor_button = ttk.Button(tools_box, text="Doctor")
        self.stats_button = ttk.Button(tools_box, text="Stats")
        self.settings_button = ttk.Button(tools_box, text="Settings\u2026")
        bold = tkfont.nametofont("TkDefaultFont").copy()
        bold.configure(weight="bold")
        self._attention_font = bold  # keep a reference or Tk drops it
        ttk.Style(self).configure("Attention.TButton", foreground=COLOR_WARNING, font=bold)
        self._doctor_tip = Tooltip(self.doctor_button)
        self._doctor_tip_text = gui_tooltips.CONTROL_HELP["doctor"]
        self.doctor_button.bind("<Enter>", lambda event: self._doctor_tip.schedule(self._doctor_tip_text, event.x_root + 12, event.y_root + 18))
        self.doctor_button.bind("<Leave>", lambda _event: self._doctor_tip.hide())
        self.doctor_button.bind("<ButtonPress>", lambda _event: self._doctor_tip.hide())
        self._restart_tip = Tooltip(self.restart_button)
        self.restart_button.bind("<Enter>", self._show_restart_tip)
        self.restart_button.bind("<Leave>", lambda _event: self._restart_tip.hide())
        self.restart_button.bind("<ButtonPress>", lambda _event: self._restart_tip.hide())

        for box, widgets in (
            (daemon_box, (self.start_button, self.stop_button, self.pause_button, self.restart_button)),
            (polling_box, (self.poll_button, self.single_button, self.check_button)),
            (folders_box, tuple(self.folder_buttons.values())),
            (tools_box, (self.log_check, self.doctor_button, self.stats_button, self.settings_button)),
        ):
            box.pack(side="left", padx=(0, 10))
            for column, widget in enumerate(widgets):
                widget.grid(row=0, column=column, padx=(0, 6) if widget is not widgets[-1] else 0)
        self.log_check.grid_configure(padx=(0, 12))
        attach_tooltip(self.poll_button, gui_tooltips.CONTROL_HELP["poll_now"])
        attach_tooltip(self.single_button, gui_tooltips.CONTROL_HELP["poll_one"])
        attach_tooltip(self.check_button, gui_tooltips.CONTROL_HELP["check_folders"])
        attach_tooltip(self.stats_button, gui_tooltips.CONTROL_HELP["stats"])
        for key, button in self.folder_buttons.items():
            attach_tooltip(button, gui_tooltips.CONTROL_HELP[f"folder_{key}"])
        # The light/dark switch sits at the right edge of the header, clear of the centred groups.
        self.theme_button = ttk.Button(self, text=gui_theme.button_text(False), width=8)
        self.theme_button.place(relx=1.0, x=-2, y=0, anchor="ne")
        attach_tooltip(self.theme_button, gui_tooltips.CONTROL_HELP["dark_mode"])
        self.apply_view(gui_daemon.build_view(gui_daemon.DaemonSnapshot(), 0.0))

    def set_folder_counts(self, counts: dict[str, int]) -> None:
        """Show each folder's entry count in its button ("Complete (2)"); an empty folder shows just the name."""
        for key, label in work_folders.FOLDER_BUTTONS:
            text = work_folders.button_text(label, counts.get(key, 0))
            if str(self.folder_buttons[key].cget("text")) != text:
                self.folder_buttons[key].configure(text=text)

    def set_doctor_attention(self, reasons: list[str], highlight: bool) -> None:
        """Highlight the Doctor button (and say why in its tooltip) when something needed is missing."""
        if highlight:
            self.doctor_button.configure(text="\u26a0 Doctor", style="Attention.TButton")
            lines = [gui_tooltips.CONTROL_HELP["doctor_attention"], *[f"\u2022 {reason}" for reason in reasons]]
            self._doctor_tip_text = "\n".join(lines)
        else:
            self.doctor_button.configure(text="Doctor", style="TButton")
            self._doctor_tip_text = gui_tooltips.CONTROL_HELP["doctor"]

    def _show_restart_tip(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        """Show the Restart button's hover help."""
        self._restart_tip.schedule(gui_tooltips.CONTROL_HELP["restart"], event.x_root, event.y_root)

    def apply_view(self, view: gui_daemon.ControlBarView) -> None:
        """Show the given status text/colors and enable only the buttons that make sense."""
        self.status_dot.configure(fg=self.DOT_COLORS[view.dot])
        self.status_label.configure(text=view.status_text)
        self.countdown_label.configure(text=view.countdown_text)
        self.pause_button.configure(text=view.pause_text)
        for widget, enabled in (
            (self.start_button, view.start_enabled),
            (self.stop_button, view.stop_enabled),
            (self.pause_button, view.pause_enabled),
            (self.poll_button, view.poll_enabled),
            (self.single_button, view.poll_enabled),  # the same moments as Poll now
            (self.check_button, view.check_enabled),
            (self.log_check, view.log_enabled),
        ):
            widget.state(["!disabled"] if enabled else ["disabled"])
        self.detailed_log.set(view.log_checked)
        if view.restart_visible:
            self.restart_button.grid()  # restores the remembered grid position
            self.restart_button.state(["!disabled"] if view.restart_enabled else ["disabled"])
        else:
            self.restart_button.grid_remove()
            self._restart_tip.hide()


class AddRepositoryDialog(tk.Toplevel):
    """Ask for owner/repo and a destination, then add the entry to mapping.json."""

    def __init__(self, master: tk.Misc, on_added: Callable[[str], None]) -> None:
        """Build the dialog asking for a repository name (owner/repo) and its destination."""
        super().__init__(master)
        self.title("Add repository")
        self.resizable(False, False)
        self.transient(master)  # type: ignore[arg-type]
        self._on_added = on_added

        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)

        self.name_var = tk.StringVar()
        self.dest_var = tk.StringVar()

        ttk.Label(body, text="Repository (owner/repo)").grid(row=0, column=0, sticky="w")
        name_entry = ttk.Entry(body, textvariable=self.name_var, width=52)
        name_entry.grid(row=1, column=0, sticky="ew", pady=(0, 8))

        ttk.Label(body, text="Destination").grid(row=2, column=0, sticky="w")
        dest_row = ttk.Frame(body)
        dest_row.grid(row=3, column=0, sticky="ew")
        dest_row.columnconfigure(0, weight=1)
        ttk.Entry(dest_row, textvariable=self.dest_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(dest_row, text="Browse…", width=9, command=lambda: pick_directory(self, self.dest_var)).grid(
            row=0, column=1, padx=(6, 0)
        )

        self.message = ttk.Label(body, text="", foreground=COLOR_ERROR, wraplength=420)
        self.message.grid(row=4, column=0, sticky="w", pady=(10, 0))

        buttons = ttk.Frame(body)
        buttons.grid(row=5, column=0, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(buttons, text="Add", command=self._add).grid(row=0, column=1)

        self.bind("<Return>", lambda _event: self._add())
        self.bind("<Escape>", lambda _event: self.destroy())
        center_dialog(self, master, focus=name_entry)
        self.grab_set()

    def _add(self) -> None:
        """Validate the input, add the repository through mapping_manager and tell the caller."""
        result = gui_forms.build_new_repo(self.name_var.get(), self.dest_var.get())
        if not result.ok:
            self.message.configure(text=" ".join(result.errors))
            return
        repo = result.changes["repository"]
        try:
            mapping_manager.add_repository(repo, {"destination": result.changes["destination"]})
        except Exception as exc:  # validation (duplicate name/destination), lock timeout, I/O
            self.message.configure(text=describe_error(exc))
            return
        self.destroy()
        self._on_added(repo)


class MappingsTab(ttk.Frame):
    """Wide repository table on top, editor below (always fully visible)."""

    # (key, heading, width, anchor) for the table; keys match repo_overview.RepoRow fields.
    TABLE_COLUMNS: tuple[tuple[str, str, int, Anchor], ...] = (
        ("icon", "?", 40, "center"),  # status icon; the "?" header explains it on hover
        ("repo", "Repository (owner/repo)", 150, "w"),
        ("folder", "Name (folder)", 150, "w"),
        ("destination", "Destination", 150, "w"),
        ("tag", "Tag", 100, "w"),
        ("last_check", "Last check", 135, "w"),
        ("step", "Recheck", 65, "center"),
        ("next_check", "Next check", 135, "w"),
        ("files", "Files", 70, "center"),
        ("limit", "Limit", 75, "center"),
    )
    STRETCH_COLUMNS = ("repo", "folder", "destination", "tag")
    SHOW_FILTERS = ("All", "Active", "Inactive", "Has pending")
    # Editor layout: three columns of up to three rows; a row holds one or more fields side by side.
    # A field is (key, label, kind, weight): kinds name, entry, readonly, dest, spin, choice; weight is its share of the
    # row's width (a spin box keeps its natural width). The last column's third row holds Active and the buttons.
    FORM_LAYOUT = (
        (
            (("name", "Repository (owner/repo)", "name", 1),),
            (("folder", "Name (folder)", "entry", 3), ("subfolder", "Subfolder", "entry", 1)),
            (("destination", "Destination", "dest", 1),),
        ),
        (
            (("recheck", "Recheck (minutes)", "entry", 1), ("default_recheck", "Default recheck", "readonly", 1)),
            (("limit", "Limit", "spin", 0), ("release_folders", "Release types", "entry", 1)),
            (("sanity_check", "Sanity check (file count)", "choice", 1),),
        ),
        (
            (("skiplist", "Skiplist", "entry", 1),),
            (
                ("last_seen", "Last notification", "readonly", 3),
                ("last_finalized", "Last finalized", "readonly", 3),
                ("last_filecount", "Last file count", "readonly", 2),
            ),
            (),
        ),
    )
    # Form values the user can change (the rest is read-only display).
    EDITABLE_KEYS = (
        "folder", "destination", "subfolder", "release_folders", "limit", "recheck", "skiplist", "sanity_check",
        "shared_destination", "active",
    )
    CHECK_KEYS = ("active", "shared_destination")  # yes/no fields (a check box instead of text)

    def __init__(self, master: tk.Misc, set_status: Callable[[str], None]) -> None:
        """Build the Mappings tab: filter bar, table and the editor form."""
        super().__init__(master, padding=10)
        self._set_status = set_status
        self._table = gui_data.RepoTable()
        self._current_repo: Optional[str] = None  # repo shown in the editor
        self._loaded: dict[str, Any] = {}  # editable values as loaded, to detect changes
        self._loading = False  # True while the editor is being filled programmatically
        self._rendering = False  # True while the table is being updated programmatically
        self._rows_by_repo: dict[str, Any] = {}
        self._fit_job: Optional[str] = None
        self._shown: dict[str, tuple[list[str], tuple[str, ...]]] = {}  # iid -> (values, tags) currently in the tree
        self._column_widths: tuple[int, ...] = ()  # widths the current ellipsis fit was computed for
        self._measure_cache: dict[str, int] = {}
        self._tip_cell: Optional[tuple[str, str]] = None

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self._build_filter_bar()
        self._build_table()
        self._build_form()
        # Table takes all spare height; the editor keeps its natural height so it is never cut off.
        self.table_frame.grid(row=1, column=0, sticky="nsew")
        self.form_frame.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self._tooltip = Tooltip(self.tree)
        self._cell_font = self._tree_font()
        self._update_buttons()
        self.refresh()

    # ----- construction -----

    def _build_filter_bar(self) -> None:
        """Create the filter field and the Active/Inactive selector above the table."""
        bar = ttk.Frame(self)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        bar.columnconfigure(1, weight=1)
        ttk.Label(bar, text="Filter:").grid(row=0, column=0, padx=(0, 4))
        self.filter_var = tk.StringVar()
        FilterEntry(bar, self.filter_var).grid(row=0, column=1, sticky="ew")
        self.filter_var.trace_add("write", lambda *_: self._render_rows())
        self.show_var = tk.StringVar(value=self.SHOW_FILTERS[0])
        show_box = ttk.Combobox(bar, textvariable=self.show_var, state="readonly", width=12, values=self.SHOW_FILTERS)
        show_box.grid(row=0, column=2, padx=(8, 0))
        show_box.bind("<<ComboboxSelected>>", lambda _event: self._render_rows())
        self.add_button = ttk.Button(bar, text="Add repository…", command=self._add_repository)
        self.remove_button = ttk.Button(bar, text="Remove", command=self._remove_repository)
        self.add_button.grid(row=0, column=3, padx=(8, 6))
        self.remove_button.grid(row=0, column=4)

    def _build_table(self) -> None:
        """Create the repository table with its scrollbars, column headings and hover help."""
        self.table_frame = ttk.Frame(self)
        self.table_frame.columnconfigure(0, weight=1)
        self.table_frame.rowconfigure(0, weight=1)
        keys = [column[0] for column in self.TABLE_COLUMNS]
        self.tree = ttk.Treeview(self.table_frame, columns=keys, show="headings", selectmode="browse")
        fitted = self._fitted_widths()
        for key, title, width, anchor in self.TABLE_COLUMNS:
            self.tree.heading(key, text=title, anchor=anchor)
            self.tree.column(key, width=fitted.get(key, width), minwidth=30 if key == "icon" else 40, anchor=anchor, stretch=key in self.STRETCH_COLUMNS)
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(self.table_frame, orient="vertical", command=self.tree.yview)
        self._xscroll = ttk.Scrollbar(self.table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=self._set_xscroll)
        yscroll.grid(row=0, column=1, sticky="ns")
        self._xscroll.grid(row=1, column=0, sticky="ew")
        self.tree.tag_configure("odd", background=COLOR_STRIPE)
        self.tree.tag_configure("limit_warn", foreground=COLOR_WARNING)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        # Re-fit the "..." truncation after the user drags a column or the window is resized.
        self.tree.bind("<ButtonRelease-1>", lambda _event: self._schedule_refit())
        self.tree.bind("<Configure>", lambda _event: self._schedule_refit())
        self.tree.bind("<Motion>", self._on_motion)
        self.tree.bind("<Leave>", lambda _event: self._hide_tip())
        self.tree.bind("<ButtonPress>", lambda _event: self._hide_tip())
        self.tree.bind("<MouseWheel>", lambda _event: self._hide_tip())
        self.tree.bind("<Double-1>", self._on_double_click)

    def _on_double_click(self, event: tk.Event) -> None:
        """Double-clicking a row (any column) opens its folder, like the Open folder button."""
        repo = self.tree.identify_row(event.y)  # "" on the column headings and empty space
        if repo and repo == self._current_repo:  # not the current one = the unsaved-changes prompt was declined
            self._open_folder()

    def _build_form(self) -> None:
        """Create the editor form from FORM_LAYOUT, plus the message row and the Save/Revert/Add/Remove buttons."""
        self.form_frame = ttk.LabelFrame(self, text="Selected repository", padding=10)
        for column in range(len(self.FORM_LAYOUT)):
            self.form_frame.columnconfigure(column, weight=1, uniform="form")

        self.vars: dict[str, tk.Variable] = {
            key: tk.StringVar() for column in self.FORM_LAYOUT for row in column for key, _l, _k, _w in row
        }
        for key in self.CHECK_KEYS:
            self.vars[key] = tk.BooleanVar()
        for key in self.EDITABLE_KEYS:
            self.vars[key].trace_add("write", lambda *_: self._on_form_edited())

        self.form_widgets: list[ttk.Widget] = []
        row_count = max(len(rows) for rows in self.FORM_LAYOUT)
        for column, rows in enumerate(self.FORM_LAYOUT):
            pad = (0, 0) if column == len(self.FORM_LAYOUT) - 1 else (0, 14)
            for row, cells in enumerate(rows):
                if cells:
                    self._add_form_row(row * 2, column, pad, cells)

        # Validation message under the first two columns (only shown while there is one), and Active + the
        # buttons in the last column's last row.
        self.message_label = ttk.Label(self.form_frame, text="", foreground=COLOR_ERROR, wraplength=700)
        self.message_label.grid(row=row_count * 2, column=0, columnspan=2, sticky="w")
        self.message_label.grid_remove()
        actions = ttk.Frame(self.form_frame)
        actions.grid(row=row_count * 2 - 2, rowspan=2, column=len(self.FORM_LAYOUT) - 1, sticky="ew")  # the whole last row
        actions.columnconfigure(1, weight=1)  # the spare width goes between the check boxes and the buttons
        self.active_check = ttk.Checkbutton(actions, text="Active", variable=self.vars["active"])
        self.shared_check = ttk.Checkbutton(actions, text="Shared folder", variable=self.vars["shared_destination"])
        self.revert_button = ttk.Button(actions, text="Revert", command=self._revert)
        self.save_button = ttk.Button(actions, text="Save", command=self._save)
        self.open_button = ttk.Button(actions, text="Open folder", command=self._open_folder)
        self.active_check.grid(row=0, column=0, sticky="w")  # the two check boxes stack; the buttons sit beside them
        self.shared_check.grid(row=1, column=0, sticky="w")
        self.open_button.grid(row=0, column=2, rowspan=2, padx=(0, 6))
        self.revert_button.grid(row=0, column=3, rowspan=2, padx=(0, 6))
        self.save_button.grid(row=0, column=4, rowspan=2)
        self.form_widgets.extend([self.active_check, self.shared_check, self.open_button])
        attach_tooltip(self.active_check, gui_tooltips.FIELD_HELP["active"])
        attach_tooltip(self.shared_check, gui_tooltips.FIELD_HELP["shared_destination"])
        attach_tooltip(self.open_button, gui_tooltips.FIELD_HELP["open_folder"])

    def _add_form_row(
        self, grid_row: int, column: int, pad: tuple[int, int], cells: tuple[tuple[str, str, str, int], ...]
    ) -> None:
        """One editor row: its fields side by side, each a title above its input (two grid rows high)."""
        holder = ttk.Frame(self.form_frame)
        holder.grid(row=grid_row, rowspan=2, column=column, sticky="ew", padx=pad, pady=(0, 6))
        for index, (key, label, kind, weight) in enumerate(cells):
            gap = (0, 0) if index == 0 else (10, 0)
            if weight:
                holder.columnconfigure(index, weight=weight, uniform="cells")  # exact proportions, whatever the text
            title = ttk.Label(holder, text=label)
            title.grid(row=0, column=index, sticky="w", padx=gap)
            if key in gui_tooltips.FIELD_HELP:
                attach_tooltip(title, gui_tooltips.FIELD_HELP[key])
            if kind == "name":
                widget: tk.Misc = self._name_widget(holder)
            elif kind == "dest":
                widget = self._dest_widget(holder)
            elif kind == "spin":
                widget = ttk.Spinbox(holder, from_=0, to=999, width=6, textvariable=self.vars[key])
                self.form_widgets.append(widget)
            elif kind == "choice":
                widget = ttk.Combobox(
                    holder, textvariable=self.vars[key], state="readonly",
                    values=[text for _value, text in gui_forms.SANITY_CHOICES],
                )
                self.form_widgets.append(widget)
            else:
                widget = ttk.Entry(
                    holder, textvariable=self.vars[key], state="readonly" if kind == "readonly" else "normal"
                )
                if kind != "readonly":
                    self.form_widgets.append(widget)
            widget.grid(row=1, column=index, sticky="w" if kind == "spin" else "ew", padx=gap)

    def _name_widget(self, parent: tk.Misc) -> ttk.Frame:
        """The read-only repository name with a globe button that opens its GitHub page."""
        holder = ttk.Frame(parent)
        holder.columnconfigure(0, weight=1)
        ttk.Entry(holder, textvariable=self.vars["name"], state="readonly").grid(row=0, column=0, sticky="ew")
        self.github_button = ttk.Button(holder, text="🌐", width=3, command=self._open_github)
        self.github_button.grid(row=0, column=1, padx=(6, 0))
        attach_tooltip(self.github_button, gui_tooltips.FIELD_HELP["github"])
        self.form_widgets.append(self.github_button)
        return holder

    def _dest_widget(self, parent: tk.Misc) -> ttk.Frame:
        """The destination entry with its Browse button."""
        holder = ttk.Frame(parent)
        holder.columnconfigure(0, weight=1)
        entry = ttk.Entry(holder, textvariable=self.vars["destination"])
        entry.grid(row=0, column=0, sticky="ew")
        self.browse_button = ttk.Button(
            holder, text="Browse…", width=9, command=lambda: pick_directory(self, self.vars["destination"])
        )
        self.browse_button.grid(row=0, column=1, padx=(6, 0))
        self.form_widgets.extend([entry, self.browse_button])
        return holder

    def _open_github(self) -> None:
        """Open the selected repository's GitHub page in the default browser."""
        repo = self._current_repo
        url = gui_forms.github_url(repo) if repo else None
        if url is None:
            self._set_status("This repository name has no GitHub page to open.")
            return
        if webbrowser.open(url):
            self._set_status(f"Opened {url}")
        else:
            self._set_status(f"Could not open a browser for {url}")

    def _open_folder(self) -> None:
        """Open the repository's folder (destination/folder name/subfolder, or the longest part that exists)."""
        repo = self._current_repo
        if repo is None:
            return
        form = self._form_values()
        folder = gui_forms.resolve_open_folder(
            repo, str(form["destination"]), str(form["folder"]), str(form["subfolder"])
        )
        if folder is None:
            self._set_status("No existing folder to open: set a destination that exists first.")
            return
        try:
            open_in_file_manager(folder)
        except OSError as exc:
            self._set_status(f"Cannot open {folder}: {describe_error(exc)}")
            return
        self._set_status(f"Opened {folder}")

    def select_repo(self, repo: str) -> bool:
        """Show and select a repository in the table (clearing filters that would hide it)."""
        if repo not in self._table.entries:
            return False
        self.filter_var.set("")
        self.show_var.set(self.SHOW_FILTERS[0])
        self._render_rows()
        self.tree.selection_set(repo)
        self.tree.focus(repo)
        self.tree.see(repo)
        return True

    # ----- table -----

    def refresh(self) -> None:
        """Reload mapping.json + queue state; keep selection and unsaved edits."""
        try:
            self._table = gui_data.load_repo_table()
        except Exception as exc:
            self._set_status(f"Cannot load mappings: {describe_error(exc)}")
            return
        self.default_recheck_text = gui_forms.format_list(self._table.default_recheck)
        self.vars["default_recheck"].set(self.default_recheck_text)
        if self._table.db_error:
            self._set_status(self._table.db_error)
        self._render_rows()
        self._sync_form_after_refresh()

    def _set_xscroll(self, first: float | str, last: float | str) -> None:
        """Show the horizontal scrollbar only while the columns are wider than the table."""
        needed = float(first) > 0.0 or float(last) < 1.0
        if needed and not self._xscroll.winfo_ismapped():
            self._xscroll.grid()
        elif not needed and self._xscroll.winfo_ismapped():
            self._xscroll.grid_remove()
        self._xscroll.set(first, last)

    def _fitted_widths(self) -> dict[str, int]:
        """Widths for the columns whose text has a known shape: never below the listed width (tuned on Windows),
        wider where the system font is (Linux fonts are wider, so the timestamps would end in "...")."""
        font = self._tree_font()
        bold = tkfont.nametofont("TkDefaultFont").copy()
        bold.configure(weight="bold")
        stamp = font.measure("2026-10-07 00:00") + 47  # text plus the cell's side margins (Windows 135, Linux 174)
        heading = bold.measure("Recheck") + 17
        step = font.measure("5 / 5 (+99)") + 20  # "2 / 5 (+2)": the step plus the jobs that are waiting besides it
        wanted = {"last_check": stamp, "next_check": stamp, "step": max(heading, step)}
        listed = {key: width for key, _title, width, _anchor in self.TABLE_COLUMNS}
        return {key: max(listed[key], width) for key, width in wanted.items()}

    def _tree_font(self) -> tkfont.Font:
        """Return the font the table uses, for measuring text widths."""
        spec = ttk.Style(self).lookup("Treeview", "font") or "TkDefaultFont"
        try:
            return tkfont.Font(root=self, font=spec)
        except tk.TclError:
            return tkfont.nametofont("TkDefaultFont")

    @staticmethod
    def _cell_text(row: Any, key: str) -> str:
        """Return the text of one table cell (status icon, limit with warning mark, or the row value)."""
        if key == "icon":
            return STATUS_ICONS.get(row.status, "")
        if key == "limit" and row.limit_warning:
            return f"⚠ {row.limit}".rstrip()  # a folder-limit warning is on record
        return str(getattr(row, key))

    def _cell_display(self, row: Any, key: str) -> str:
        """Cell text, shortened with an ellipsis when it does not fit the column."""
        text = self._cell_text(row, key)
        if key == "icon":
            return text
        width = int(self.tree.column(key, "width")) - 14  # cell padding
        return fit_text(text, max(width, 20), self._measure)

    def _measure(self, text: str) -> int:
        """Pixel width of `text` (memoized: font.measure is a slow Tcl round trip)."""
        width = self._measure_cache.get(text)
        if width is None:
            if len(self._measure_cache) > 20000:
                self._measure_cache.clear()
            width = self._measure_cache[text] = self._cell_font.measure(text)
        return width

    def _current_widths(self) -> tuple[int, ...]:
        """Return the current width of every table column."""
        return tuple(int(self.tree.column(key, "width")) for key, *_ in self.TABLE_COLUMNS)

    def _schedule_refit(self) -> None:
        """Re-fit the ellipsis once resizing/dragging has paused (each event restarts the timer)."""
        if self._fit_job is not None:
            self.after_cancel(self._fit_job)
        self._fit_job = self.after(200, self._refit)

    def _refit(self) -> None:
        """Re-render the rows when the column widths changed (so cut-off text is redone)."""
        self._fit_job = None
        if self._current_widths() != self._column_widths:
            self._render_rows()

    def _on_motion(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        """Show hover help for the heading, status icon or cut-off cell under the mouse."""
        region = self.tree.identify_region(event.x, event.y)
        iid = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)  # "#1" is the first column (the status icon)
        row = self._rows_by_repo.get(iid)
        if region == "heading" and column == "#1":  # the "?" header: legend of all status icons
            if self._tip_cell != ("", "legend"):
                self._hide_tip()
                self._tip_cell = ("", "legend")
                self._tooltip.schedule(STATUS_LEGEND, event.x_root, event.y_root)
            return
        if region == "heading":
            heading_index = int(column[1:]) - 1 if column.startswith("#") else -1
            help_text = gui_tooltips.COLUMN_HELP.get(self.TABLE_COLUMNS[heading_index][0], "") if 0 <= heading_index < len(self.TABLE_COLUMNS) else ""
            if help_text and self._tip_cell != ("", column):
                self._hide_tip()
                self._tip_cell = ("", column)
                self._tooltip.schedule(help_text, event.x_root, event.y_root)
            elif not help_text:
                self._hide_tip()
            return
        if region != "cell":
            self._hide_tip()
            return
        index = int(column[1:]) - 1
        if row is None or not 0 <= index < len(self.TABLE_COLUMNS):
            self._hide_tip()
            return
        key = self.TABLE_COLUMNS[index][0]
        if self._tip_cell == (iid, key):
            return
        self._hide_tip()
        if key == "icon":  # status icon: explain this row's status
            self._tip_cell = (iid, key)
            self._tooltip.schedule(f"{row.status}: {gui_tooltips.STATUS_HINTS.get(row.status, '')}", event.x_root, event.y_root)
            return
        full = self._cell_text(row, key)
        if key == "limit" and row.limit_note:  # what the "12 / 15" means and when it was counted
            self._tip_cell = (iid, key)
            self._tooltip.schedule(row.limit_note, event.x_root, event.y_root)
        elif key == "step" and row.pending_note:  # several jobs of this repository are waiting: list them all
            self._tip_cell = (iid, key)
            self._tooltip.schedule(row.pending_note, event.x_root, event.y_root)
        elif self._cell_display(row, key) != full:  # only text that is cut off gets a tooltip
            self._tip_cell = (iid, key)
            self._tooltip.schedule(full, event.x_root, event.y_root)

    def shutdown(self) -> None:
        """Cancel pending timers and close the tooltip (call before the window is destroyed)."""
        if self._fit_job is not None:
            self.after_cancel(self._fit_job)
            self._fit_job = None
        self._hide_tip()

    def _hide_tip(self) -> None:
        """Hide the cell tooltip."""
        self._tip_cell = None
        self._tooltip.hide()

    def _matches(self, row: Any) -> bool:
        """True when the row passes the text filter and the Active/Inactive selector."""
        needle = self.filter_var.get().strip().casefold()
        if needle and needle not in f"{row.folder} {row.repo} {row.destination}".casefold():
            return False
        show = self.show_var.get()
        if show == "Active":
            return row.status != "Inactive"
        if show == "Inactive":
            return row.status == "Inactive"
        if show == "Has pending":
            return row.status in ("Queued", "Waiting", "Running")
        return True

    def _render_rows(self) -> None:
        """Update the tree in place (no flicker, selection kept) to match the filtered rows."""
        visible = [row for row in self._table.rows if self._matches(row)]
        wanted = [row.repo for row in visible]
        self._rendering = True
        try:
            existing = set(self.tree.get_children())
            for iid in existing - set(wanted):
                self.tree.delete(iid)
            self._rows_by_repo = {row.repo: row for row in visible}
            self._column_widths = self._current_widths()
            for index, row in enumerate(visible):
                values = [self._cell_display(row, key) for key, *_ in self.TABLE_COLUMNS]
                tags = ("odd",) if index % 2 else ()  # zebra striping follows the row's position
                if row.limit_warning:
                    tags += ("limit_warn",)
                if row.repo not in existing:
                    self.tree.insert("", index, iid=row.repo, values=values, tags=tags)
                elif self._shown.get(row.repo) != (values, tags):  # untouched rows cost nothing
                    self.tree.item(row.repo, values=values, tags=tags)
                self._shown[row.repo] = (values, tags)
            for iid in existing - set(wanted):
                self._shown.pop(iid, None)
            if list(self.tree.get_children()) != wanted:
                for index, repo in enumerate(wanted):
                    self.tree.move(repo, "", index)
            if self._current_repo in wanted and self.tree.selection() != (self._current_repo,):
                self.tree.selection_set(self._current_repo)
        finally:
            self._rendering = False

    # ----- editor -----

    def _entry_to_form(self, repo: str) -> dict[str, Any]:
        """Convert a mapping.json entry into the editor's field values."""
        entry = self._table.entries[repo]
        return {
            "name": repo,
            "folder": str(entry.get("folder") or ""),
            "destination": str(entry.get("destination") or ""),
            "subfolder": str(entry.get("subfolder") or ""),
            "release_folders": gui_forms.format_list(entry.get("limit_folders")),
            "limit": str(entry.get("limit", "")),
            "recheck": gui_forms.format_list(entry.get("recheck_intervals")),
            "skiplist": gui_forms.format_list(entry.get("skiplist")),
            "sanity_check": gui_forms.sanity_label(entry.get("sanity_check")),
            "last_seen": str(entry.get("last_notification") or ""),
            "last_finalized": str(entry.get("last_finalized") or ""),
            "last_filecount": next((row.files_total for row in self._table.rows if row.repo == repo), ""),
            "shared_destination": entry.get("shared_destination") is True,
            "active": entry.get("active") is not False,
        }

    def _load_form(self, repo: Optional[str]) -> None:
        """Fill the editor from the loaded entry (or clear it when repo is None)."""
        self._loading = True
        try:
            values = self._entry_to_form(repo) if repo else {}
            for key, var in self.vars.items():
                if key == "default_recheck":
                    continue
                var.set(values.get(key, False if key in self.CHECK_KEYS else ""))
        finally:
            self._loading = False
        self._current_repo = repo
        self._loaded = {key: self.vars[key].get() for key in self.EDITABLE_KEYS}
        self._show_message("")
        self._update_buttons()

    def _form_values(self) -> dict[str, Any]:
        """Return the editable values currently in the form."""
        return {key: self.vars[key].get() for key in self.EDITABLE_KEYS}

    def _is_dirty(self) -> bool:
        """True when the form differs from what was loaded (unsaved changes)."""
        return self._current_repo is not None and self._form_values() != self._loaded

    def _on_form_edited(self) -> None:
        """React to an edit in the form by updating the buttons."""
        if not self._loading:
            self._update_buttons()

    def _update_buttons(self) -> None:
        """Enable Save/Revert only with unsaved changes, and Remove only with a selected repository."""
        has_repo = self._current_repo is not None
        dirty = self._is_dirty()
        self.save_button.state(["!disabled"] if dirty else ["disabled"])
        self.revert_button.state(["!disabled"] if dirty else ["disabled"])
        self.remove_button.state(["!disabled"] if has_repo else ["disabled"])
        for widget in self.form_widgets:
            widget.state(["!disabled"] if has_repo else ["disabled"])

    def _show_message(self, text: str, kind: str = "error") -> None:
        """Show a validation message (or hide the message row when the text is empty)."""
        color = {"error": COLOR_ERROR, "warning": COLOR_WARNING, "info": COLOR_MUTED}[kind]
        self.message_label.configure(text=text, foreground=color)
        if text:
            self.message_label.grid()
        else:
            self.message_label.grid_remove()

    def _on_select(self, _event: object = None) -> None:
        """Load the selected repository into the editor, asking first if there are unsaved changes."""
        if self._rendering:
            return
        selection = self.tree.selection()
        repo = selection[0] if selection else None
        if repo == self._current_repo or repo is None:
            return
        if self._is_dirty() and not messagebox.askyesno(
            "Unsaved changes", f"Discard the unsaved changes to {self._current_repo}?", parent=self
        ):
            self._rendering = True
            try:
                if self._current_repo is not None:
                    self.tree.selection_set(self._current_repo)
            finally:
                self._rendering = False
            return
        self._load_form(repo)

    def _sync_form_after_refresh(self) -> None:
        """After a table refresh, keep the editor on its repository or clear it if that repository is gone."""
        repo = self._current_repo
        if repo is None:
            return
        if repo not in self._table.entries:
            self._load_form(None)
            self._set_status(f"{repo} was removed from mapping.json.")
        elif not self._is_dirty():
            message = self.message_label.cget("text"), self.message_label.cget("foreground")
            self._load_form(repo)  # picks up changes made by the daemon (last notification etc.)
            if message[0]:
                self.message_label.configure(text=message[0], foreground=message[1])
                self.message_label.grid()

    def _revert(self) -> None:
        """Throw away the edits and reload the form from the saved entry."""
        self._load_form(self._current_repo)

    def _save(self) -> None:
        """Validate the form and save the changes through mapping_manager."""
        repo = self._current_repo
        if repo is None or not self._is_dirty():
            return
        result = gui_forms.build_repo_changes(self._form_values())
        if not result.ok:
            self._show_message(" ".join(result.errors))
            return
        try:
            changed = mapping_manager.update_repository_fields(repo, result.changes)
        except Exception as exc:  # validation (duplicates), repo gone, lock timeout, I/O
            self._show_message(describe_error(exc))
            return
        self._table = gui_data.load_repo_table()
        self._load_form(repo if repo in self._table.entries else None)
        self._render_rows()
        if result.warnings:
            self._show_message(" ".join(result.warnings), "warning")
        self._set_status(f"Saved {repo}." if changed else f"No changes to save for {repo}.")

    # ----- add / remove -----

    def _add_repository(self) -> None:
        """Open the Add repository dialog (after confirming that unsaved edits may be discarded)."""
        if self._is_dirty() and not messagebox.askyesno(
            "Unsaved changes", f"Discard the unsaved changes to {self._current_repo}?", parent=self
        ):
            return
        AddRepositoryDialog(self, self._after_added)

    def _after_added(self, repo: str) -> None:
        """Refresh the table and select the repository that was just added."""
        self.filter_var.set("")
        self.show_var.set(self.SHOW_FILTERS[0])
        self.refresh()
        self._load_form(repo if repo in self._table.entries else None)
        if repo in self._table.entries:
            self._render_rows()
            self.tree.see(repo)
        self._set_status(f"Added {repo}.")

    def _remove_repository(self) -> None:
        """After a confirmation, remove the selected repository from mapping.json."""
        repo = self._current_repo
        if repo is None:
            return
        if not messagebox.askyesno(
            "Remove repository",
            f"Remove {repo} from mapping.json?\n\nQueued jobs and history in state.db are kept. "
            "A new notification for it would add a fresh, unmapped entry.",
            parent=self,
        ):
            return
        try:
            removed = mapping_manager.remove_repository(repo)
        except Exception as exc:
            self._show_message(describe_error(exc))
            return
        self._load_form(None)
        self.refresh()
        self._set_status(f"Removed {repo}." if removed else f"{repo} was already gone.")


class TerminalLogTab(ttk.Frame):
    """Follows the newest terminal log file: last ~1000 lines, smart auto-scroll, filter, copy."""

    MAX_LINES = log_tail.DEFAULT_MAX_LINES
    POLL_INTERVAL_MS = 300
    IDLE_INTERVAL_MS = 1000  # while the tab is hidden or the window minimized
    DIRECTORY_REFRESH_SECONDS = 5.0
    USER_SCROLL_WINDOW_SECONDS = 0.5  # a view change this soon after the user's wheel/key/scrollbar input is theirs

    def __init__(
        self,
        master: tk.Misc,
        is_active: Callable[[], bool],
        on_enable_log: Callable[[], None],
        set_status: Callable[[str], None],
    ) -> None:
        """Build the Terminal log tab.

        The callbacks tell whether the log is active, switch it on, and set the footer status text.
        """
        super().__init__(master, padding=10)
        self._is_active = is_active
        self._on_enable_log = on_enable_log
        self._set_status = set_status
        self._lines: collections.deque[str] = collections.deque(maxlen=self.MAX_LINES)
        self._updating = False  # True while we change the text ourselves (not a user scroll)
        self._user_input_at = -1e9  # monotonic time of the last wheel/key/scrollbar input on the log
        self._daemon_running = False
        self._daemon_log_on = False
        self._has_log_file = False
        self._directory = ""
        self._directory_checked = 0.0
        self._poll_job: Optional[str] = None
        self._tailer = log_tail.LogTailer(self._log_directory, max_lines=self.MAX_LINES)

        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        self._build_notice()
        self._build_toolbar()
        self._build_text()
        self._update_notice()
        self._poll_job = self.after(self.POLL_INTERVAL_MS, self._poll)

    # ----- construction -----

    def _build_notice(self) -> None:
        """Create the notice row (e.g. "turn on the terminal log") with its action button."""
        self.notice = ttk.Frame(self)
        self.notice.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        self.notice.columnconfigure(0, weight=1)
        self.notice_label = ttk.Label(self.notice, text="", foreground=COLOR_WARNING, wraplength=900)
        self.notice_label.grid(row=0, column=0, sticky="w")
        self.enable_button = ttk.Button(self.notice, text="Turn on terminal log", command=self._on_enable_log)
        self.enable_button.grid(row=0, column=1, padx=(10, 0))

    def _build_toolbar(self) -> None:
        """Create the filter field, the follow checkbox and the copy/open-folder buttons."""
        toolbar = ttk.Frame(self)
        toolbar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        toolbar.columnconfigure(3, weight=1)
        ttk.Label(toolbar, text="Filter:").grid(row=0, column=0, padx=(0, 4))
        self.filter_var = tk.StringVar()
        FilterEntry(toolbar, self.filter_var, width=75).grid(row=0, column=1, sticky="w")
        self.filter_var.trace_add("write", lambda *_: self._render_all())
        # Follow sits right of the filter's clear button, away from the tab headers (a stray click there unticked it)
        self.follow_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(toolbar, text="Follow", variable=self.follow_var, command=self._on_follow_toggle).grid(
            row=0, column=2, padx=(14, 0), sticky="w"
        )
        self.file_label = ttk.Label(toolbar, text="", foreground=COLOR_MUTED)
        self.file_label.grid(row=0, column=3, sticky="e", padx=(0, 8))
        ttk.Button(toolbar, text="Copy", command=self._copy).grid(row=0, column=4, padx=(0, 6))
        self.open_log_button = ttk.Button(toolbar, text="Open log", command=self._open_log, state="disabled")
        self.open_log_button.grid(row=0, column=5, padx=(0, 6))
        attach_tooltip(self.open_log_button, gui_tooltips.CONTROL_HELP["open_log"])
        ttk.Button(toolbar, text="Open folder", command=self._open_folder).grid(row=0, column=6)

    def _build_text(self) -> None:
        """Create the read-only log text with its scrollbars and the warning/error colours."""
        self.text = tk.Text(self, wrap="none", height=10, font=("Consolas", 10), state="disabled", undo=False)
        self.text.tag_configure(log_tail.LEVEL_ERROR, foreground=COLOR_ERROR)
        self.text.tag_configure(log_tail.LEVEL_WARNING, foreground=COLOR_WARNING)
        self.text.grid(row=2, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self._xscroll = ttk.Scrollbar(self, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=lambda first, last: self._on_yview(yscroll, first, last), xscrollcommand=self._set_xscroll)
        yscroll.grid(row=2, column=1, sticky="ns")
        self._xscroll.grid(row=3, column=0, sticky="ew")
        for widget, sequences in (
            (self.text, ("<MouseWheel>", "<Button-4>", "<Button-5>", "<ButtonPress-1>", "<B1-Motion>", "<Key>")),
            (yscroll, ("<MouseWheel>", "<ButtonPress-1>", "<B1-Motion>")),
        ):
            for sequence in sequences:
                widget.bind(sequence, self._note_user_input, add="+")

    # ----- scrolling -----

    def _set_xscroll(self, first: float | str, last: float | str) -> None:
        """Show the horizontal scrollbar only when the text is wider than the view."""
        needed = float(first) > 0.0 or float(last) < 1.0
        if needed and not self._xscroll.winfo_ismapped():
            self._xscroll.grid()
        elif not needed and self._xscroll.winfo_ismapped():
            self._xscroll.grid_remove()
        self._xscroll.set(first, last)

    def _note_user_input(self, _event: object = None) -> None:
        """Remember when the user last scrolled or clicked, so auto-scroll does not fight them."""
        self._user_input_at = time.monotonic()

    def _on_yview(self, scrollbar: ttk.Scrollbar, first: float | str, last: float | str) -> None:
        """The user scrolling up pauses following; scrolling back to the bottom resumes it.

        Tk reports a view change a moment after it happens, and also when only the layout changed (a long line
        made the horizontal scrollbar appear, the tab was hidden or shown, the window was resized). Only a change
        right after the user's own input counts as a scroll; any other change keeps following, if that is on.
        """
        scrollbar.set(first, last)
        if self._updating:
            return
        at_bottom = float(last) >= 0.999
        if time.monotonic() - self._user_input_at <= self.USER_SCROLL_WINDOW_SECONDS:
            self.follow_var.set(at_bottom)
        elif self.follow_var.get() and not at_bottom:
            self.after_idle(self._follow_end)

    def _follow_end(self) -> None:
        """Scroll to the newest line while "follow" is on."""
        if self.follow_var.get():
            self.text.see("end")

    def _on_follow_toggle(self) -> None:
        """Jump to the end when "follow" is switched on."""
        if self.follow_var.get():
            self.text.see("end")

    # ----- data -----

    def _log_directory(self) -> str:
        """The configured log folder (re-read every few seconds, so a Settings change is picked up)."""
        now = time.monotonic()
        if not self._directory or now - self._directory_checked >= self.DIRECTORY_REFRESH_SECONDS:
            raw = config_manager.get_terminal_log_settings()["directory"] or ""
            self._directory = os.path.expandvars(os.path.expanduser(raw))
            self._directory_checked = now
        return self._directory

    def poll_now(self) -> None:
        """Look for new log lines immediately (called when the tab is shown)."""
        if self._poll_job is not None:
            self.after_cancel(self._poll_job)
        self._poll()

    def _poll(self) -> None:
        """Read new log lines (fast while the log is active, slowly otherwise) and schedule the next read."""
        interval = self.IDLE_INTERVAL_MS
        try:
            if self._is_active():
                interval = self.POLL_INTERVAL_MS
                update = self._tailer.poll()
                if update is not None:
                    self._apply(update)
        except Exception as exc:  # a log hiccup must never break the GUI
            self.file_label.configure(text=f"log unavailable: {exc}")
        finally:
            self._poll_job = self.after(interval, self._poll)

    def _apply(self, update: log_tail.TailUpdate) -> None:
        """Show one tail update: new file name, reset or appended lines."""
        if update.changed_file or update.reset:
            self._has_log_file = update.file_name is not None
            self.file_label.configure(text=update.file_name or "")
            self.open_log_button.state(["!disabled"] if self._has_log_file else ["disabled"])
            self._update_notice()
        if update.reset:
            self._lines.clear()
            self._lines.extend(update.lines)
            self._render_all()
        elif update.lines:
            self._lines.extend(update.lines)
            self._append(update.lines)

    # ----- rendering -----

    def _filter_text(self) -> str:
        """Return the filter text, lower-cased for comparison."""
        return self.filter_var.get().strip().casefold()

    def _matching(self, lines: Iterable[str]) -> list[str]:
        """Return the lines that contain the filter text (all of them when it is empty)."""
        needle = self._filter_text()
        return [line for line in lines if needle in line.casefold()] if needle else list(lines)

    def _insert(self, lines: list[str]) -> None:
        """Append lines to the text, coloured by level."""
        for line in lines:
            level = log_tail.line_level(line)
            self.text.insert("end", line + "\n", (level,) if level else ())

    def _render_all(self) -> None:
        """Redraw the whole text from the stored lines (after a filter change)."""
        self._updating = True
        try:
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            self._insert(self._matching(self._lines))
            self.text.configure(state="disabled")
            if self.follow_var.get():
                self.text.see("end")
        finally:
            self._updating = False

    def _append(self, lines: list[str]) -> None:
        """Add new lines at the end, drop the oldest beyond the cap, and follow if asked to."""
        self._updating = True
        try:
            self.text.configure(state="normal")
            self._insert(self._matching(lines))
            excess = int(self.text.index("end-1c").split(".")[0]) - 1 - self.MAX_LINES
            if excess > 0:
                self.text.delete("1.0", f"{excess + 1}.0")
            self.text.configure(state="disabled")
            if self.follow_var.get():
                self.text.see("end")
        finally:
            self._updating = False

    # ----- notice about the detailed log -----

    def set_daemon_state(self, running: bool, log_on: bool) -> None:
        """Tell the tab whether the daemon runs and its log is on, so it can show the right notice."""
        if (running, log_on) != (self._daemon_running, self._daemon_log_on):
            self._daemon_running, self._daemon_log_on = running, log_on
            self._update_notice()

    def _update_notice(self) -> None:
        """Choose the notice text and whether the "turn on" button is offered."""
        if not self._daemon_running:
            if self._has_log_file:
                text, can_enable = "The daemon is not running; showing the last log.", False
            else:
                text, can_enable = "No log file yet. Start the daemon and turn on the terminal log to see its output here.", False
        elif not self._daemon_log_on:
            text = (
                "The terminal log is off, so nothing new appears here."
                if self._has_log_file
                else "No log file yet. The terminal log writes the daemon's output to a file that this tab follows."
            )
            can_enable = True
        else:
            text, can_enable = "", False
        if text:
            self.notice.grid()
            self.notice_label.configure(text=text)
            if can_enable:
                self.enable_button.grid()
            else:
                self.enable_button.grid_remove()
        else:
            self.notice.grid_remove()

    # ----- buttons -----

    def _copy(self) -> None:
        """Copy the selected text, or everything shown when nothing is selected."""
        try:
            content = self.text.get("sel.first", "sel.last")
        except tk.TclError:  # no selection: copy everything shown
            content = self.text.get("1.0", "end-1c")
        self.clipboard_clear()
        self.clipboard_append(content)
        self._set_status("Copied the selection." if self.text.tag_ranges("sel") else "Copied the shown log lines.")

    def _open_folder(self) -> None:
        """Open the terminal log folder in the file manager."""
        directory = self._log_directory()
        if not os.path.isdir(directory):
            self._set_status(f"The log folder does not exist yet: {directory}")
            return
        try:
            open_in_file_manager(directory)
        except OSError as exc:
            self._set_status(f"Could not open the folder: {exc}")

    def _open_log(self) -> None:
        """Open the log file this tab is following in the system's default program for .log files."""
        path = self._tailer.path
        if not path or not os.path.isfile(path):
            self._set_status("There is no log file to open yet.")
            return
        try:
            open_in_file_manager(path)  # a file opens in its default program
        except OSError as exc:
            self._set_status(f"Could not open the log: {exc}")

    def shutdown(self) -> None:
        """Cancel the polling timer (called when the window closes)."""
        if self._poll_job is not None:
            self.after_cancel(self._poll_job)
            self._poll_job = None


class StatusTab(ttk.Frame):
    """A read-only list for one status tab: events from state.db or the unmapped repositories.

    Rows arrive through set_rows(); the tab never queries anything itself. Cut-off cells show their full text on
    hover, unseen rows are bold while the tab is shown, and a double-click opens the row's repository in the
    Mappings tab.
    """

    # (key, heading, width, anchor, stretch)
    Column = tuple[str, str, int, Anchor, bool]

    def __init__(
        self,
        master: tk.Misc,
        columns: tuple["StatusTab.Column", ...],
        empty_text: str,
        open_repo: Callable[[str], None],
        hint: str = "",
        on_mark_read: Optional[Callable[[], None]] = None,
        on_clear: Optional[Callable[[], None]] = None,
        on_clear_repo: Optional[Callable[[str], None]] = None,
        detail: bool = False,
        filterable: bool = False,
        on_open_folder: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Build a status tab: the table with the given columns plus the optional filter, detail pane and buttons."""
        super().__init__(master, padding=10)
        self._has_detail = detail
        self._empty_text = empty_text
        self._base = 1 if filterable else 0  # grid row of the table (the filter bar is above it)
        self._all_rows: list[tuple[str, ...]] = []  # everything the tab was given; _rows is what the filter lets through
        self._all_repos: list[str] = []
        self._all_ids: list[int] = []
        self._columns = columns
        self._open_repo = open_repo
        self._rows: list[tuple[str, ...]] = []  # full (untruncated) cell texts, in display order
        self._repos: list[str] = []
        self._ids: list[int] = []
        self._highlight_after: Optional[int] = None
        self._fit_job: Optional[str] = None
        self._tip_cell: Optional[tuple[str, str]] = None
        self._measure_cache: dict[str, int] = {}
        self._column_widths: tuple[int, ...] = ()
        self.columnconfigure(0, weight=1)
        self.rowconfigure(self._base, weight=1)
        self.filter_var = tk.StringVar()
        if filterable:
            bar = ttk.Frame(self)
            bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
            bar.columnconfigure(1, weight=1)
            ttk.Label(bar, text="Filter:").grid(row=0, column=0, padx=(0, 4))
            FilterEntry(bar, self.filter_var).grid(row=0, column=1, sticky="ew")
            self.filter_var.trace_add("write", lambda *_: self._apply_filter())

        keys = [column[0] for column in columns]
        self.tree = ttk.Treeview(self, columns=keys, show="headings", selectmode="browse")
        for key, heading, width, anchor, stretch in columns:
            self.tree.heading(key, text=heading, anchor=anchor)
            self.tree.column(key, width=width, minwidth=40, anchor=anchor, stretch=stretch)
        self.tree.tag_configure("odd", background=COLOR_STRIPE)
        bold = tkfont.nametofont("TkDefaultFont").copy()
        bold.configure(weight="bold")
        self._bold_font = bold  # keep a reference or Tk drops it
        self.tree.tag_configure("unread", font=bold)
        self.tree.grid(row=self._base, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.grid(row=self._base, column=1, sticky="ns")

        self.empty_label = ttk.Label(self, text=empty_text, foreground=COLOR_MUTED)
        bottom_row = self._base + 1
        if detail:
            self._build_detail()
            bottom_row = self._base + 2
        bottom = ttk.Frame(self)
        bottom.grid(row=bottom_row, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        bottom.columnconfigure(0, weight=1)
        ttk.Label(bottom, text=hint, foreground=COLOR_MUTED).grid(row=0, column=0, sticky="w")
        self.open_folder_button: Optional[ttk.Button] = None  # opens the selected row's repository folder
        if on_open_folder is not None:
            self._open_folder = on_open_folder
            self.open_folder_button = ttk.Button(bottom, text="Open folder", command=self._on_open_folder, state="disabled")
            self.open_folder_button.grid(row=0, column=1, padx=(0, 6))
            attach_tooltip(self.open_folder_button, gui_tooltips.CONTROL_HELP["open_repo_folder"])
        if detail:
            self.copy_button = ttk.Button(bottom, text="Copy text", command=self._copy_selected)
            self.copy_button.grid(row=0, column=2, padx=(0, 6))
        self.mark_read_button: Optional[ttk.Button] = None  # only tabs with an unread counter have one
        if on_mark_read is not None:
            self.mark_read_button = ttk.Button(bottom, text="Mark all read", command=on_mark_read)
            self.mark_read_button.grid(row=0, column=3)
        self.clear_button: Optional[ttk.Button] = None  # deletes the listed events from state.db
        if on_clear is not None:
            self.clear_button = ttk.Button(bottom, text="Clear…", command=on_clear)
            self.clear_button.grid(row=0, column=4, padx=(6, 0))
            attach_tooltip(self.clear_button, gui_tooltips.CONTROL_HELP["clear_tab"])
        self.clear_repo_button: Optional[ttk.Button] = None  # deletes one repository's events
        if on_clear_repo is not None:
            self._clear_repo = on_clear_repo
            self.clear_repo_button = ttk.Button(bottom, text="Clear selected", command=self._on_clear_repo, state="disabled")
            self.clear_repo_button.grid(row=0, column=3, padx=(0, 6))
            attach_tooltip(self.clear_repo_button, gui_tooltips.CONTROL_HELP["clear_repo"])

        font_spec = ttk.Style(self).lookup("Treeview", "font") or "TkDefaultFont"
        try:
            self._cell_font: tkfont.Font = tkfont.Font(root=self, font=font_spec)
        except tk.TclError:
            self._cell_font = tkfont.nametofont("TkDefaultFont")
        self._tooltip = Tooltip(self.tree)
        self.tree.bind("<<TreeviewSelect>>", self._show_detail)
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._update_clear_repo_button(), add="+")
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._update_open_folder_button(), add="+")
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Return>", self._on_double_click)
        self.tree.bind("<Motion>", self._on_motion)
        self.tree.bind("<Leave>", lambda _event: self._hide_tip())
        self.tree.bind("<ButtonPress>", lambda _event: self._hide_tip())
        self.tree.bind("<MouseWheel>", lambda _event: self._hide_tip())
        self.tree.bind("<Configure>", lambda _event: self._schedule_refit())
        self.tree.bind("<ButtonRelease-1>", lambda _event: self._schedule_refit())

    # ----- detail pane: the whole text of the selected row (long messages never fit a table cell) -----

    DETAIL_PLACEHOLDER = "Select a row to read its full text here."

    def _build_detail(self) -> None:
        """Create the detail pane that shows the full text of the selected row."""
        frame = ttk.Frame(self)
        frame.grid(row=self._base + 1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        frame.columnconfigure(0, weight=1)
        self.detail = tk.Text(frame, height=5, wrap="word", state="disabled", relief="solid", borderwidth=1,
                              background=COLOR_DETAIL, font=("Segoe UI", 10))
        self.detail.tag_configure("head", font=("Segoe UI", 10, "bold"))
        self.detail.tag_configure("hint", foreground=COLOR_MUTED)
        self.detail.grid(row=0, column=0, sticky="ew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.detail.yview)
        self.detail.configure(yscrollcommand=scroll.set)
        scroll.grid(row=0, column=1, sticky="ns")
        self._set_detail("", "")

    def _set_detail(self, head: str, body: str) -> None:
        """Replace the detail pane's text (bold heading line, then the body)."""
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        if head or body:
            self.detail.insert("end", head + "\n" if head else "", "head")
            self.detail.insert("end", body)
        else:
            self.detail.insert("end", self.DETAIL_PLACEHOLDER, "hint")
        self.detail.configure(state="disabled")

    def _selected_index(self) -> Optional[int]:
        """Return the index of the selected row, or None."""
        selection = self.tree.selection()
        return int(selection[0]) if selection else None

    def _selected_text(self) -> tuple[str, str]:
        """(header, body) of the selected row: every column but the last as the header, the last as the text."""
        index = self._selected_index()
        if index is None or index >= len(self._rows):
            return "", ""
        row = self._rows[index]
        return "   \u00b7   ".join(cell for cell in row[:-1] if cell), row[-1]

    def _show_detail(self, _event: object = None) -> None:
        """Show the selected row's full text in the detail pane."""
        if self._has_detail:
            self._set_detail(*self._selected_text())

    def _copy_selected(self) -> None:
        """Copy the selected row's full text to the clipboard."""
        head, body = self._selected_text()
        if head or body:
            self.clipboard_clear()
            self.clipboard_append(f"{head}\n{body}" if head else body)

    # ----- content -----

    def set_rows(
        self,
        rows: list[tuple[str, ...]],
        repos: list[str],
        ids: Optional[list[int]] = None,
        highlight_after: Optional[int] = None,
    ) -> None:
        """Show these rows (full texts, newest first); `ids` + `highlight_after` mark unseen rows bold."""
        self._all_rows, self._all_repos = rows, repos
        self._all_ids = ids if ids is not None else [0] * len(rows)
        self._highlight_after = highlight_after
        self._apply_filter()

    def _apply_filter(self) -> None:
        """Keep the rows that contain the filter text in any cell (the row lists stay index-aligned)."""
        needle = self.filter_var.get().strip().casefold()
        if needle:
            keep = [i for i, row in enumerate(self._all_rows) if needle in " ".join(row).casefold()]
        else:
            keep = list(range(len(self._all_rows)))
        self._rows = [self._all_rows[i] for i in keep]
        self._repos = [self._all_repos[i] for i in keep]
        self._ids = [self._all_ids[i] for i in keep]
        filtered_out = bool(self._all_rows) and not self._rows
        self.empty_label.configure(text="No rows match the filter." if filtered_out else self._empty_text)
        self._render()

    def set_highlight_after(self, highlight_after: Optional[int]) -> None:
        """Set the id above which rows count as unread (shown bold); None highlights nothing."""
        if highlight_after != self._highlight_after:
            self._highlight_after = highlight_after
            self._render()

    def _tags(self, index: int) -> tuple[str, ...]:
        """Return the row's style tags: zebra stripe and unread."""
        tags = ["odd"] if index % 2 else []
        if self._highlight_after is not None and self._ids[index] > self._highlight_after:
            tags.append("unread")
        return tuple(tags)

    def _render(self) -> None:
        """Redraw the table from the current rows, keeping the selection."""
        self._column_widths = self._current_widths()
        self._hide_tip()
        selected = self._selected_index()
        selected_id = self._ids[selected] if selected is not None and selected < len(self._ids) else None
        self.tree.delete(*self.tree.get_children())
        for index, row in enumerate(self._rows):
            self.tree.insert(
                "", "end", iid=str(index), values=self._display(row, self._is_unread(index)), tags=self._tags(index)
            )
        if self._has_detail:
            if selected_id is not None and selected_id in self._ids:
                self.tree.selection_set(str(self._ids.index(selected_id)))  # same event, new position
            else:
                self._show_detail()
        if self._rows:
            self.empty_label.place_forget()
        else:
            self.empty_label.place(relx=0.5, rely=0.35, anchor="center")

    # ----- ellipsis and hover text -----

    def _measure(self, text: str, bold: bool = False) -> int:
        """Return the pixel width of a text in the table font (cached)."""
        cache_key = ("b" if bold else "n") + text
        width = self._measure_cache.get(cache_key)
        if width is None:
            if len(self._measure_cache) > 20000:
                self._measure_cache.clear()
            font = self._bold_font if bold else self._cell_font
            width = self._measure_cache[cache_key] = font.measure(text)
        return width

    def _is_unread(self, index: int) -> bool:
        """True when the row's id is above the highlight mark."""
        return self._highlight_after is not None and self._ids[index] > self._highlight_after

    def _display(self, row: tuple[str, ...], bold: bool = False) -> list[str]:
        """Cell texts shortened to their column; bold rows are measured in the (wider) bold font."""
        shown = []
        for (key, _heading, _width, _anchor, _stretch), text in zip(self._columns, row):
            width = int(self.tree.column(key, "width")) - 14
            shown.append(fit_text(text, max(width, 20), lambda value: self._measure(value, bold)))
        return shown

    def _current_widths(self) -> tuple[int, ...]:
        """Return the current width of every table column."""
        return tuple(int(self.tree.column(column[0], "width")) for column in self._columns)

    def _schedule_refit(self) -> None:
        """Re-render once the user stopped dragging a column (debounced 200 ms)."""
        if self._fit_job is not None:
            self.after_cancel(self._fit_job)
        self._fit_job = self.after(200, self._refit)

    def _refit(self) -> None:
        """Re-render the rows when the column widths changed, so cut-off cells are redone."""
        self._fit_job = None
        if self._rows and self._current_widths() != self._column_widths:
            self._render()

    def _on_motion(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        """Show hover help for the cut-off cell under the mouse."""
        if self.tree.identify_region(event.x, event.y) != "cell":
            self._hide_tip()
            return
        iid = self.tree.identify_row(event.y)
        index = int(self.tree.identify_column(event.x)[1:]) - 1
        if not iid or not 0 <= index < len(self._columns) or (iid, str(index)) == self._tip_cell:
            return
        self._hide_tip()
        full = self._rows[int(iid)][index]
        if self._display(self._rows[int(iid)], self._is_unread(int(iid)))[index] != full:
            self._tip_cell = (iid, str(index))
            self._tooltip.schedule(full, event.x_root, event.y_root)

    def _hide_tip(self) -> None:
        """Hide the cell tooltip."""
        self._tip_cell = None
        self._tooltip.hide()

    def shutdown(self) -> None:
        """Cancel pending timers and hide the tooltip (called when the window closes)."""
        if self._fit_job is not None:
            self.after_cancel(self._fit_job)
            self._fit_job = None
        self._hide_tip()

    # ----- actions -----

    def _update_clear_repo_button(self) -> None:
        """Enable "Clear selected" only while a row with a repository is selected."""
        if self.clear_repo_button is not None:
            self.clear_repo_button.state(["!disabled"] if self.selected_repo() else ["disabled"])

    def _on_clear_repo(self) -> None:
        """Clear the selected repository's rows (after the caller's confirmation)."""
        repo = self.selected_repo()
        if repo:
            self._clear_repo(repo)

    def selected_repo(self) -> str:
        """Return the repository of the selected row, or ""."""
        selection = self.tree.selection()
        return self._repos[int(selection[0])] if selection else ""

    def _update_open_folder_button(self) -> None:
        """Enable "Open folder" only while a row with a repository is selected."""
        if self.open_folder_button is not None:
            self.open_folder_button.state(["!disabled"] if self.selected_repo() else ["disabled"])

    def _on_open_folder(self) -> None:
        """Open the selected row's folder."""
        repo = self.selected_repo()
        if repo:
            self._open_folder(repo)

    def _on_double_click(self, _event: object = None) -> None:
        """Jump to the double-clicked row's repository in the Mappings tab."""
        selection = self.tree.selection()
        if selection:
            repo = self._repos[int(selection[0])]
            if repo:
                self._open_repo(repo)


class SettingsDialog(tk.Toplevel):
    """Global settings from config.json."""

    def __init__(
        self,
        master: tk.Misc,
        on_saved: Callable[[str], None],
        on_warnings_saved: Optional[Callable[[], None]] = None,
        on_credentials_saved: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Build the Settings dialog from gui_forms.SETTINGS_KEYS; the callbacks report saves to the main window."""
        super().__init__(master)
        self.title("Settings")
        self.resizable(False, False)
        self.transient(master)  # type: ignore[arg-type]
        self._on_saved = on_saved
        self._on_warnings_saved = on_warnings_saved  # the warning checklist saves by itself; tell the main window
        self._on_credentials_saved = on_credentials_saved  # so does the Gmail & GitHub window

        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)
        # Two columns of sections on top (what the daemon does on the left; the GUI's own settings and the log files
        # on the right), the note and the buttons in a row of their own below.
        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew")
        left.columnconfigure(0, weight=1)
        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew", padx=(14, 0))
        right.columnconfigure(0, weight=1)
        body.columnconfigure((0, 1), weight=1, uniform="settings")

        current = gui_data.load_settings_form()
        # Warning types without a notification: a list with its own window, Save and Cancel (it is not part of this
        # form, so this window's Save neither writes nor undoes it).
        self._silenced: set[str] = set(current.pop("silenced_warning_types", []))
        self._initial_form = dict(current)  # to tell whether the form has unsaved changes
        self.vars: dict[str, tk.Variable] = {
            key: tk.BooleanVar(value=value) if isinstance(value, bool) else tk.StringVar(value=value)
            for key, value in current.items()
        }

        def row(frame: ttk.LabelFrame, index: int, label: str, widget: tk.Widget) -> None:
            """Add a labelled widget to a settings group."""
            ttk.Label(frame, text=label).grid(row=index, column=0, sticky="w", pady=4, padx=(0, 12))
            widget.grid(row=index, column=1, sticky="ew", pady=4)

        def spin(frame: ttk.LabelFrame, key: str, low: int, high: int) -> ttk.Spinbox:
            """Create a number box for a settings key with the given range."""
            return ttk.Spinbox(frame, from_=low, to=high, width=8, textvariable=self.vars[key])

        processing = ttk.LabelFrame(left, text="Processing", padding=10)
        processing.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        processing.columnconfigure(1, weight=1)
        first_entry = ttk.Entry(processing, textvariable=self.vars["recheck"], width=28)
        row(processing, 0, "Default recheck (minutes)", first_entry)
        row(processing, 1, "Max emails per poll (0 = all)", spin(processing, "max_emails", 0, 9999))
        row(processing, 2, "Destination check every N polls (0 = off)", spin(processing, "dest_check", 0, 999))
        row(processing, 3, "Default limit (new mappings, 0 = none)", spin(processing, "default_limit", 0, 9999))

        polling = ttk.LabelFrame(left, text="Polling", padding=10)
        polling.grid(row=0, column=0, sticky="ew")
        polling.columnconfigure(1, weight=1)
        # A checkbox carries its own text (not a separate label), so its keyboard focus ring wraps the text.
        polling_check = ttk.Checkbutton(polling, text="Enable polling", variable=self.vars["polling_enabled"])
        polling_check.grid(row=0, column=0, columnspan=2, sticky="w", pady=4)
        attach_tooltip(polling_check, gui_tooltips.CONTROL_HELP["polling_enabled"])
        row(polling, 1, "Interval (seconds)", spin(polling, "interval", gui_forms.MIN_POLL_INTERVAL_SECONDS, 86400))
        row(polling, 2, "Jitter min (seconds)", spin(polling, "jitter_min", 0, 3600))
        row(polling, 3, "Jitter max (seconds)", spin(polling, "jitter_max", 0, 3600))

        paths = ttk.LabelFrame(left, text="Paths", padding=10)
        paths.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        paths.columnconfigure(1, weight=1)
        dir_frame = ttk.Frame(paths)
        dir_frame.columnconfigure(0, weight=1)
        ttk.Entry(dir_frame, textvariable=self.vars["download_dir"]).grid(row=0, column=0, sticky="ew")
        ttk.Button(dir_frame, text="Browse…", width=9, command=lambda: pick_directory(self, self.vars["download_dir"])).grid(
            row=0, column=1, padx=(6, 0)
        )
        row(paths, 0, "Default download folder", dir_frame)
        row(paths, 1, "Default subfolder (new mappings)", ttk.Entry(paths, textvariable=self.vars["default_subfolder"], width=28))

        logging_box = ttk.LabelFrame(right, text="Logging", padding=10)
        logging_box.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        logging_box.columnconfigure(1, weight=1)
        ttk.Checkbutton(logging_box, text="Terminal log on at startup", variable=self.vars["log_enabled"]).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=4
        )
        row(logging_box, 1, "Max log file (MB, 0 = no rollover)", spin(logging_box, "log_max_mb", 0, 1000))
        row(logging_box, 2, "Keep log files (0 = all)", spin(logging_box, "log_keep", 0, 9999))

        warnings_box = ttk.LabelFrame(right, text="Warning notifications", padding=10)
        warnings_box.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        warnings_box.columnconfigure(0, weight=1)
        self._warnings_summary = ttk.Label(warnings_box, text=warning_types.summary(self._silenced))
        self._warnings_summary.grid(row=0, column=0, sticky="w")
        self._warnings_button = ttk.Button(warnings_box, text="Choose warnings…", command=self._choose_warnings)
        self._warnings_button.grid(row=0, column=1, sticky="e")
        attach_tooltip(self._warnings_button, gui_tooltips.CONTROL_HELP["choose_warnings"])

        # Autostart lives in the operating system (not in config.json) and is applied on Save. Asking the
        # system (schtasks, systemctl, ...) can take a moment, so it happens in a thread and the box stays
        # disabled until the answer is in; the dialog itself never waits for it.
        self._autostart_before: Optional[bool] = None  # None = not known (yet): Save leaves autostart alone
        self.autostart_var = tk.BooleanVar(value=False)
        self._autostart_answer: list[autostart.AutostartStatus] = []
        self._autostart_check: Optional[ttk.Checkbutton] = None
        self._shortcuts_answer: list[Any] = []
        self._shortcuts_button: Optional[ttk.Button] = None
        # The tray settings are GUI-side (gui section of config.json) and apply the next time the GUI starts.
        tray_box = ttk.LabelFrame(right, text="System tray", padding=10)
        tray_box.grid(row=0, column=0, sticky="ew")
        tray_box.columnconfigure((0, 1), weight=1, uniform="tray")
        self._tray_checks: list[ttk.Checkbutton] = []
        for index, (key, text) in enumerate(
            (
                ("tray", "Show the tray icon"),
                ("notifications", "Show notifications"),
                ("minimize_to_tray", "Minimize to the tray"),
                ("close_to_tray", "Close to the tray"),
            )
        ):
            tray_check = ttk.Checkbutton(tray_box, text=text, variable=self.vars[key])
            tray_check.grid(row=index // 2, column=index % 2, sticky="w", pady=(0 if index < 2 else 4, 0))
            attach_tooltip(tray_check, gui_tooltips.CONTROL_HELP[key])
            if key != "tray":
                self._tray_checks.append(tray_check)
        if not gui_tray.tray_available():
            ttk.Label(
                tray_box, text="Needs the optional packages: pip install -r requirements-optional.txt", foreground=COLOR_MUTED
            ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
            for child in tray_box.winfo_children():
                if isinstance(child, ttk.Checkbutton):
                    child.state(["disabled"])  # the values are kept as they are; there is just nothing to switch
            self._warnings_button.state(["disabled"])
        else:
            self.vars["tray"].trace_add("write", lambda *_: self._enable_tray_checks())
            self.vars["notifications"].trace_add("write", lambda *_: self._enable_tray_checks())
            self._enable_tray_checks()

        startup = ttk.LabelFrame(right, text="Startup", padding=10)
        startup.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        startup.columnconfigure(0, weight=1)
        for index, (key, text) in enumerate(
            (
                ("start_daemon", "Start the daemon when the GUI starts"),
                ("start_minimized", "Start the GUI minimized"),
            ),
            start=1,
        ):
            gui_check = ttk.Checkbutton(startup, text=text, variable=self.vars[key])
            gui_check.grid(row=index, column=0, columnspan=2, sticky="w", pady=(4, 0))
            attach_tooltip(gui_check, gui_tooltips.CONTROL_HELP[key])
        if autostart.is_supported():
            self._autostart_check = ttk.Checkbutton(
                startup,
                text="Start the daemon at login or boot" if sys.platform.startswith("linux") else "Start the daemon when I log in",
                variable=self.autostart_var,
                state="disabled",
            )
            self._autostart_check.grid(row=0, column=0, sticky="w")
            attach_tooltip(self._autostart_check, gui_tooltips.CONTROL_HELP["autostart"])
            threading.Thread(target=self._probe_autostart, daemon=True).start()
            self.after(100, self._apply_autostart_answer)
        if shortcuts.is_supported():
            self._shortcuts_button = ttk.Button(startup, text="Create shortcuts…", command=self._create_shortcuts)
            self._shortcuts_button.grid(row=3, column=0, columnspan=2, sticky="w", pady=(10, 0))
            attach_tooltip(self._shortcuts_button, gui_tooltips.CONTROL_HELP["create_shortcuts"])

        ttk.Label(
            body,
            text="Changes apply the next time the daemon starts (new-repository defaults apply at once). "
            "The System tray and Startup choices apply the next time the GUI starts.",
            foreground=COLOR_MUTED,
            wraplength=860,
        ).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(10, 0)
        )
        self.message = ttk.Label(body, text="", foreground=COLOR_ERROR, wraplength=820)
        self.message.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        buttons = ttk.Frame(body)
        buttons.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        buttons.columnconfigure(1, weight=1)
        file_buttons = ttk.Frame(buttons)
        file_buttons.grid(row=0, column=0, sticky="w")
        open_config_button = ttk.Button(file_buttons, text="Open config.json…", command=self._open_config)
        open_config_button.grid(row=0, column=0, padx=(0, 6))
        attach_tooltip(open_config_button, gui_tooltips.CONTROL_HELP["open_config"])
        accounts_button = ttk.Button(file_buttons, text="Gmail & GitHub…", command=self._open_credentials)
        accounts_button.grid(row=0, column=1, padx=(0, 6))
        attach_tooltip(accounts_button, gui_tooltips.CONTROL_HELP["credentials"])
        backup_button = ttk.Button(file_buttons, text="Backup…", command=self._open_backup)
        backup_button.grid(row=0, column=2, padx=(0, 6))
        attach_tooltip(backup_button, gui_tooltips.CONTROL_HELP["backup"])
        viewer_button = ttk.Button(file_buttons, text="Viewer…", command=self._open_viewer)
        viewer_button.grid(row=0, column=3)
        attach_tooltip(viewer_button, gui_tooltips.CONTROL_HELP["viewer"])
        ttk.Button(buttons, text="Cancel", command=self._close_request).grid(row=0, column=2, padx=(0, 6))
        ttk.Button(buttons, text="Save", command=self._save).grid(row=0, column=3)

        self.bind("<Escape>", lambda _event: self._close_request())
        self.protocol("WM_DELETE_WINDOW", self._close_request)
        center_dialog(self, master, focus=first_entry)
        self.grab_set()

    def _close_request(self) -> None:
        """Escape, Cancel and the window's X: ask first when the form holds changes that were not saved."""
        if self._form_changed() and not messagebox.askyesno(
            "Settings",
            "You have changes that are not saved.\n\nClose the window and discard them?",
            icon="warning", default="no", parent=self,
        ):
            return
        self.destroy()

    def _enable_tray_checks(self) -> None:
        """The other tray choices mean nothing while the tray icon itself is switched off."""
        state = ["!disabled"] if self.vars["tray"].get() else ["disabled"]
        for check in self._tray_checks:
            check.state(state)
        # Choosing which warnings notify only matters while there are notifications at all.
        notifying = self.vars["tray"].get() and self.vars["notifications"].get()
        self._warnings_button.state(["!disabled"] if notifying else ["disabled"])

    def _choose_warnings(self) -> None:
        """Open the checklist of warning types. It saves by itself (its own Save button), whatever this window does."""
        WarningTypesDialog(self, self._silenced, self._warning_types_saved)

    def _open_credentials(self) -> None:
        """Open the Gmail & GitHub window. It saves by itself (its own Save button), whatever this window does."""
        CredentialsDialog(self, self._credentials_saved)

    def _open_backup(self) -> None:
        """Open the Backup window. It saves by itself (its own Save button), whatever this window does."""
        BackupDialog(self, self._credentials_saved)  # the callback just passes the message on to the main window

    def _open_viewer(self) -> None:
        """Open the Viewer window. It saves by itself (its own Save button), whatever this window does."""
        ViewerDialog(self, self._credentials_saved)  # the callback just passes the message on to the main window

    def _credentials_saved(self, message: str) -> None:
        """Pass the "credentials saved" message on to the main window."""
        if self._on_credentials_saved is not None:
            self._on_credentials_saved(message)

    def _warning_types_saved(self, silenced: set[str]) -> None:
        """Remember the saved list of silenced warning types and tell the main window to reload it."""
        self._silenced = set(silenced)
        self._warnings_summary.configure(text=warning_types.summary(self._silenced))
        if self._on_warnings_saved is not None:
            self._on_warnings_saved()

    def _form_changed(self) -> bool:
        """True when something in the form differs from what config.json had when the dialog opened."""
        if any(str(var.get()) != str(self._initial_form.get(key)) for key, var in self.vars.items()):
            return True
        return self._autostart_before is not None and bool(self.autostart_var.get()) != self._autostart_before

    def _open_config(self) -> None:
        """Open config.json in the default editor and close this window.

        The optional settings it lacks are added first, so they are there to edit.
        """
        if self._form_changed() and not messagebox.askyesno(
            "Open config.json",
            "Changes you made in this window are not saved and will be lost when the file opens.\n\nOpen config.json anyway?",
            icon="warning", default="no", parent=self,
        ):
            return
        try:
            config_manager.add_missing_defaults()
        except (config_manager.ConfigLockTimeout, config_manager.ConfigUnreadableError, OSError) as exc:
            self.message.configure(text=describe_error(exc), foreground=COLOR_ERROR)
            return
        try:
            open_in_file_manager(config_manager.get_config_path())
        except OSError as exc:
            self.message.configure(text=f"Could not open config.json: {describe_error(exc)}", foreground=COLOR_ERROR)
            return
        self.destroy()
        self._on_saved(
            "config.json opened in your editor. Save the file there; most changes apply the next time the daemon starts "
            "(the gui section, the next time the GUI starts)."
        )

    def _create_shortcuts(self) -> None:
        """Ask for a folder, then make the shortcuts there (in a thread: PowerShell takes a moment)."""
        start = shortcuts.default_folder()
        folder = filedialog.askdirectory(
            parent=self,
            initialdir=start if os.path.isdir(start) else None,
            title="Folder for the GHAADD shortcuts",
            mustexist=False,
        )
        if not folder or self._shortcuts_button is None:
            return
        folder = os.path.normpath(folder)
        self._shortcuts_button.configure(state="disabled")
        self.message.configure(text="Creating the shortcuts…", foreground=COLOR_MUTED)
        self._shortcuts_answer.clear()
        # The ticked boxes count at once, saved or not: the GUI shortcut gets the matching options.
        options = shortcuts.gui_options(
            minimized=bool(self.vars["start_minimized"].get()), start_daemon=bool(self.vars["start_daemon"].get())
        )
        threading.Thread(target=self._make_shortcuts, args=(folder, options), daemon=True).start()
        self.after(100, self._apply_shortcuts_answer)

    def _make_shortcuts(self, folder: str, options: tuple[str, ...]) -> None:
        """Create the shortcuts in a thread and keep the answer for the polling timer."""
        try:
            self._shortcuts_answer.append(shortcuts.create_shortcuts(folder, options=options))
        except Exception as exc:  # report it instead of losing it in the thread
            self._shortcuts_answer.append(autostart.AutostartResult(False, f"Could not create the shortcuts: {exc}"))

    def _apply_shortcuts_answer(self) -> None:
        """Show the shortcut result once the thread has answered (polls every 100 ms)."""
        try:
            if not self._shortcuts_answer:
                self.after(100, self._apply_shortcuts_answer)
                return
            result = self._shortcuts_answer[0]
            if self._shortcuts_button is not None:
                self._shortcuts_button.configure(state="normal")
            self.message.configure(text="")
            if result.ok:
                messagebox.showinfo("Shortcuts", result.message, parent=self)
            else:
                messagebox.showerror("Shortcuts", result.message, parent=self)
        except tk.TclError:  # the dialog was closed meanwhile
            pass

    def _probe_autostart(self) -> None:
        """Ask the system whether start-at-login is installed (in a thread; failure disables the box)."""
        try:
            status = autostart.autostart_status()
        except Exception:  # a probe that fails leaves the box disabled
            status = autostart.AutostartStatus(False, False, "")
        self._autostart_answer.append(status)

    def _apply_autostart_answer(self) -> None:
        """Poll (from the Tk loop) for the background probe's answer, then enable the box."""
        try:
            if not self._autostart_answer:
                self.after(100, self._apply_autostart_answer)
                return
            status = self._autostart_answer[0]
            if status.supported and self._autostart_check is not None:
                self._autostart_before = status.installed
                self.autostart_var.set(status.installed)
                self._autostart_check.configure(state="normal")
        except tk.TclError:  # the dialog was closed meanwhile
            pass

    def _save(self) -> None:
        """Validate the form, write config.json (and the autostart choice) and close on success."""
        result = gui_forms.build_settings_changes({key: var.get() for key, var in self.vars.items()})
        if not result.ok:
            self.message.configure(text="\n".join(result.errors))
            return
        try:
            changed = config_manager.set_config_values(result.changes)
            changed = config_manager.add_missing_defaults() or changed  # also lists the settings without a field here
        except (config_manager.ConfigLockTimeout, config_manager.ConfigUnreadableError, OSError) as exc:
            self.message.configure(text=describe_error(exc))
            return
        text = "Settings saved; they apply when the daemon next starts." if changed else "No settings changed."
        if result.warnings:
            text += " " + " ".join(result.warnings)
        if self._autostart_before is not None and bool(self.autostart_var.get()) != self._autostart_before:
            change = autostart.set_autostart(bool(self.autostart_var.get()))
            text += " " + ("Start at login is now " + ("on." if self.autostart_var.get() else "off.") if change.ok else f"Start at login could not be changed: {change.message}")
        self.destroy()
        self._on_saved(text)


class WarningTypesDialog(tk.Toplevel):
    """A checklist of the warning types: ticked = may pop up a notification (the Warnings tab shows all of them).

    It has its own Save and Cancel: Save writes config.json at once (and calls `on_saved`), whatever the window it was
    opened from does with its own Save.
    """

    def __init__(self, master: tk.Misc, silenced: set[str], on_saved: Callable[[set[str]], None]) -> None:
        """Build the list of warning types with one checkbox each (checked = notify)."""
        super().__init__(master)
        self.title("Warning notifications")
        self.resizable(False, False)
        self.transient(master)  # type: ignore[arg-type]
        self._on_saved = on_saved
        self._initial = frozenset(silenced)
        self._unknown = {code for code in silenced if code not in warning_types.KNOWN_TYPES}  # kept as they are

        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)
        ttk.Label(
            body,
            text="Tick the kinds of warning that may pop up a notification. The Warnings tab and its unread counter "
            "always show every warning.",
            foreground=COLOR_MUTED,
            wraplength=700,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        self.vars: dict[str, tk.BooleanVar] = {}
        for index, (code, meaning) in enumerate(warning_types.WARNING_TYPES, start=1):
            self.vars[code] = tk.BooleanVar(value=code not in silenced)
            ttk.Checkbutton(body, text=code, variable=self.vars[code]).grid(
                row=index, column=0, sticky="w", pady=2, padx=(0, 16)
            )
            ttk.Label(body, text=meaning, foreground=COLOR_MUTED, wraplength=520).grid(
                row=index, column=1, sticky="w", pady=2
            )

        last_row = len(warning_types.WARNING_TYPES) + 1
        self.message = ttk.Label(body, text="", foreground=COLOR_ERROR, wraplength=700)
        self.message.grid(row=last_row, column=0, columnspan=2, sticky="w", pady=(8, 0))
        buttons = ttk.Frame(body)
        buttons.grid(row=last_row + 1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        buttons.columnconfigure(3, weight=1)
        ttk.Button(buttons, text="All", command=lambda: self._set_all(True)).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(buttons, text="None", command=lambda: self._set_all(False)).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(buttons, text="Defaults", command=self._set_defaults).grid(row=0, column=2)
        ttk.Button(buttons, text="Cancel", command=self._close_request).grid(row=0, column=4, padx=(0, 6))
        save_button = ttk.Button(buttons, text="Save", command=self._save)
        save_button.grid(row=0, column=5)

        self.bind("<Escape>", lambda _event: self._close_request())
        self.protocol("WM_DELETE_WINDOW", self._close_request)
        center_dialog(self, master, focus=save_button)
        self.grab_set()

    def _chosen_silenced(self) -> set[str]:
        """Return the codes left unchecked, plus unknown codes that were already in the config."""
        return {code for code, var in self.vars.items() if not var.get()} | self._unknown

    def _close_request(self) -> None:
        """Cancel, Escape and the window's X: ask first when the ticks differ from what is saved."""
        if frozenset(self._chosen_silenced()) != self._initial and not messagebox.askyesno(
            "Warning notifications",
            "You have changes that are not saved.\n\nClose the window and discard them?",
            icon="warning", default="no", parent=self,
        ):
            return
        self.destroy()

    def _set_all(self, value: bool) -> None:
        """Tick or untick every box."""
        for var in self.vars.values():
            var.set(value)

    def _set_defaults(self) -> None:
        """Set the boxes to the built-in default (API and LIMIT silent)."""
        for code, var in self.vars.items():
            var.set(code not in warning_types.DEFAULT_SILENCED)

    def _save(self) -> None:
        """Write the choice to config.json now; the window stays open (with the reason) if that fails."""
        silenced = self._chosen_silenced()
        try:
            config_manager.set_config_values({"gui.silenced_warning_types": sorted(silenced)})
        except (config_manager.ConfigLockTimeout, config_manager.ConfigUnreadableError, OSError) as exc:
            self.message.configure(text=describe_error(exc))
            return
        self.destroy()
        self._on_saved(silenced)


class CredentialsDialog(tk.Toplevel):
    """Gmail login, GitHub token and the Gmail folder, so nobody has to edit `.env` or config.json by hand.

    It has its own Save and Cancel (the Settings window's Save neither writes nor undoes it). Save writes the login to
    `.env` (`env_manager`) and the folder to config.json; the Test buttons try the values as typed, without saving,
    in a thread so the window never freezes.
    """

    def __init__(self, master: tk.Misc, on_saved: Callable[[str], None]) -> None:
        """Build the Gmail & GitHub window: login fields, test buttons and its own Save/Cancel."""
        super().__init__(master)
        self.title("Gmail & GitHub")
        self.resizable(False, False)
        self.transient(master)  # type: ignore[arg-type]
        self._on_saved = on_saved
        self._results: list[tuple[str, connection_tests.ConnectionResult]] = []
        self._busy: set[str] = set()

        saved = env_manager.read_values()
        self._initial = {
            "user": saved[env_manager.GMAIL_USER],
            "password": saved[env_manager.GMAIL_APP_PASSWORD],
            "token": saved[env_manager.GITHUB_PAT],
            "folder": config_manager.get_gmail_folder(),
        }
        self.vars = {key: tk.StringVar(value=value) for key, value in self._initial.items()}
        self.show_var = tk.BooleanVar(value=False)
        self._baseline = self._values()  # normalised like the form, so a password saved with spaces is not a "change"

        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)

        def field(frame: tk.Misc, row: int, label: str, widget: tk.Widget) -> None:
            """Add a labelled widget to the form."""
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=4, padx=(0, 12))
            widget.grid(row=row, column=1, sticky="ew", pady=4)

        ttk.Label(body, text="Saved now: " + env_manager.summary(), foreground=COLOR_MUTED).grid(row=0, column=0, sticky="w", pady=(0, 8))
        gmail = ttk.LabelFrame(body, text="Gmail (where the GitHub notification mails arrive)", padding=10)
        gmail.grid(row=1, column=0, sticky="ew")
        gmail.columnconfigure(1, weight=1)
        ttk.Label(
            gmail,
            text="GHAADD reads the mails over IMAP with a Gmail app password, not your normal password. "
                 "An app password needs 2-step verification on your Google account.",
            foreground=COLOR_MUTED, wraplength=520, justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.user_entry = ttk.Entry(gmail, textvariable=self.vars["user"], width=44)
        field(gmail, 1, "Gmail address", self.user_entry)
        self.password_entry = ttk.Entry(gmail, textvariable=self.vars["password"], show="•", width=44)
        field(gmail, 2, "App password", self.password_entry)
        self.folder_box = ttk.Combobox(gmail, textvariable=self.vars["folder"], values=[self._initial["folder"]], width=42)
        field(gmail, 3, "Mailbox folder", self.folder_box)
        # The folder is a Gmail label that a filter fills; the recipe follows the folder name as it is typed.
        self.filter_hint = ttk.Label(gmail, text="", foreground=COLOR_MUTED, wraplength=520, justify="left")
        self.filter_hint.grid(row=4, column=0, columnspan=2, sticky="w", pady=(2, 0))
        self.vars["folder"].trace_add("write", lambda *_: self._update_filter_hint())
        self._update_filter_hint()
        gmail_actions = ttk.Frame(gmail)
        gmail_actions.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        gmail_actions.columnconfigure(2, weight=1)
        self.test_gmail_button = ttk.Button(gmail_actions, text="Test Gmail login", command=self._test_gmail)
        self.test_gmail_button.grid(row=0, column=0, padx=(0, 6))
        attach_tooltip(self.test_gmail_button, gui_tooltips.CONTROL_HELP["test_gmail"])
        self.gmail_result = ttk.Label(gmail, text="", wraplength=520, justify="left")
        self.gmail_result.grid(row=6, column=0, columnspan=2, sticky="w", pady=(6, 0))

        github = ttk.LabelFrame(body, text="GitHub (optional)", padding=10)
        github.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        github.columnconfigure(1, weight=1)
        ttk.Label(
            github,
            text="A token raises GitHub's request limit from 60 to 5000 an hour. Use a personal access token (classic); it needs no scopes for public repositories.",
            foreground=COLOR_MUTED, wraplength=520, justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.token_entry = ttk.Entry(github, textvariable=self.vars["token"], show="•", width=44)
        field(github, 1, "Personal access token", self.token_entry)
        github_actions = ttk.Frame(github)
        github_actions.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.test_token_button = ttk.Button(github_actions, text="Test token", command=self._test_token)
        self.test_token_button.grid(row=0, column=0, padx=(0, 6))
        attach_tooltip(self.test_token_button, gui_tooltips.CONTROL_HELP["test_token"])
        self.token_result = ttk.Label(github, text="", wraplength=520, justify="left")
        self.token_result.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

        show = ttk.Checkbutton(body, text="Show the password and the token", variable=self.show_var, command=self._toggle_show)
        show.grid(row=3, column=0, sticky="w", pady=(10, 0))
        attach_tooltip(show, gui_tooltips.CONTROL_HELP["show_secrets"])
        self.message = ttk.Label(body, text="", foreground=COLOR_ERROR, wraplength=540, justify="left")
        self.message.grid(row=4, column=0, sticky="w", pady=(6, 0))
        buttons = ttk.Frame(body)
        buttons.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        buttons.columnconfigure(1, weight=1)
        internet = ttk.Menubutton(buttons, text="Internet ▾")  # where to get the app password, the token, ...
        menu = tk.Menu(internet, tearoff=False)
        for label, url in env_manager.HELP_LINKS:
            menu.add_command(label=label, command=lambda url=url: self._open_link(url))
        internet.configure(menu=menu)
        internet.grid(row=0, column=0, sticky="w")
        attach_tooltip(internet, gui_tooltips.CONTROL_HELP["internet_links"])
        ttk.Button(buttons, text="Cancel", command=self._close_request).grid(row=0, column=2, padx=(0, 6))
        self.save_button = ttk.Button(buttons, text="Save", command=self._save)
        self.save_button.grid(row=0, column=3)

        self.bind("<Escape>", lambda _event: self._close_request())
        self.protocol("WM_DELETE_WINDOW", self._close_request)
        center_dialog(self, master, focus=self.user_entry)
        self.user_entry.selection_range(0, "end")
        self.grab_set()

    # ----- helpers -----

    def _values(self) -> dict[str, str]:
        """The form as typed: the address and token trimmed, the app password without its spaces."""
        return {
            "user": self.vars["user"].get().strip(),
            "password": env_manager.normalize_password(self.vars["password"].get()),
            "token": self.vars["token"].get().strip(),
            "folder": self.vars["folder"].get().strip(),
        }

    def _changed(self) -> bool:
        """True when the fields differ from what was loaded."""
        return self._values() != self._baseline

    def _update_filter_hint(self) -> None:
        """Show the Gmail search that finds the notification mails in the chosen folder."""
        folder = self.vars["folder"].get().strip() or config_manager.DEFAULT_GMAIL_FOLDER
        self.filter_hint.configure(text=gui_forms.gmail_filter_hint(folder))

    def _open_link(self, url: str) -> None:
        """Open a help link in the browser; show a message when that fails."""
        try:
            opened = webbrowser.open(url)
        except webbrowser.Error:
            opened = False
        self.message.configure(
            text="" if opened else f"Could not open a browser. The address is: {url}", foreground=COLOR_ERROR
        )

    def _toggle_show(self) -> None:
        """Show or hide the password and token characters."""
        shown = "" if self.show_var.get() else "•"
        self.password_entry.configure(show=shown)
        self.token_entry.configure(show=shown)

    def _close_request(self) -> None:
        """Close the window, asking first when there are unsaved changes."""
        if self._changed() and not messagebox.askyesno(
            "Gmail & GitHub",
            "You have changes that are not saved.\n\nClose the window and discard them?",
            icon="warning", default="no", parent=self,
        ):
            return
        self.destroy()

    # ----- the Test buttons (network in a thread, the answer is picked up by _poll) -----

    def _start_test(self, name: str, button: ttk.Button, label: ttk.Label, work: Callable[[], connection_tests.ConnectionResult]) -> None:
        """Run a connection test in a thread, with the button disabled and a "Testing..." label meanwhile."""
        if name in self._busy:
            return
        self._busy.add(name)
        button.state(["disabled"])
        label.configure(text="Testing…", foreground=COLOR_MUTED)

        def run() -> None:
            """Thread body: run the test and queue its result (an exception becomes a failed result)."""
            try:
                result = work()
            except Exception as exc:  # shown in the window instead of vanishing
                result = connection_tests.ConnectionResult(ok=False, message=f"The test could not run: {exc}")
            self._results.append((name, result))

        threading.Thread(target=run, daemon=True).start()
        self.after(100, self._poll)

    def _test_gmail(self) -> None:
        """Test the Gmail login and folder with the values typed in (nothing is saved)."""
        values = self._values()
        self._start_test(
            "gmail", self.test_gmail_button, self.gmail_result,
            lambda: connection_tests.check_gmail(values["user"], values["password"], values["folder"]),
        )

    def _test_token(self) -> None:
        """Test the GitHub token with the value typed in (nothing is saved)."""
        token = self._values()["token"]
        self._start_test("token", self.test_token_button, self.token_result, lambda: connection_tests.check_github_token(token))

    def _poll(self) -> None:
        """Show finished test results from the thread queue (polls while the window exists)."""
        if not self.winfo_exists():
            return
        while self._results:
            name, result = self._results.pop(0)
            self._busy.discard(name)
            ok_color = COLOR_OK if result.ok and result.folder_found is not False else COLOR_WARNING if result.ok else COLOR_ERROR
            if name == "gmail":
                self.test_gmail_button.state(["!disabled"])
                self.gmail_result.configure(text=result.message, foreground=ok_color)
                if result.folders:  # the drop-down offers the real folders; what was typed stays
                    self.folder_box.configure(values=result.folders)
            else:
                self.test_token_button.state(["!disabled"])
                self.token_result.configure(text=result.message, foreground=ok_color)
        if self._busy:
            self.after(100, self._poll)

    # ----- Save -----

    def _save(self) -> None:
        """Validate and write the credentials to .env and the mailbox folder to config.json, then close."""
        values = self._values()
        problems = env_manager.validate(values["user"], values["password"], values["token"])
        if not values["folder"]:
            problems.append(f"Enter the mailbox folder (the default is {config_manager.DEFAULT_GMAIL_FOLDER}).")
        if problems:
            self.message.configure(text="\n".join(problems))
            return
        try:
            env_manager.update_values({
                env_manager.GMAIL_USER: values["user"],
                env_manager.GMAIL_APP_PASSWORD: values["password"],
                env_manager.GITHUB_PAT: values["token"] or None,
            })
            config_manager.set_config_values({"mailbox.folder": values["folder"]})
        except (env_manager.EnvLockTimeout, config_manager.ConfigLockTimeout, config_manager.ConfigUnreadableError, OSError) as exc:
            self.message.configure(text=describe_error(exc))
            return
        self.destroy()
        self._on_saved("Gmail & GitHub settings saved. A running daemon uses them after a restart.")


class BackupDialog(tk.Toplevel):
    """Back up state.db and the settings files now, and set the schedule the daemon keeps.

    It has its own Save and Cancel (the Settings window's Save neither writes nor undoes it). Save writes the
    `backup` section of config.json; "Back up now" uses the values as typed, without saving, in a thread so the
    window never freezes. The daemon reads the schedule afresh, so no restart is needed.
    """

    def __init__(self, master: tk.Misc, on_saved: Callable[[str], None]) -> None:
        """Build the Backup window: schedule, folder, what to include, and the Back up now button."""
        super().__init__(master)
        self.title("Backup")
        self.resizable(False, False)
        self.transient(master)  # type: ignore[arg-type]
        self._on_saved = on_saved
        self._answer: list[backup_manager.BackupResult] = []

        saved = config_manager.get_backup_settings()
        configured = config_manager.load_config().get("backup")
        shown_directory = str(configured.get("directory", "")) if isinstance(configured, dict) else ""
        self.vars: dict[str, tk.Variable] = {
            "enabled": tk.BooleanVar(value=saved["enabled"]),
            "every_hours": tk.StringVar(value=str(saved["every_hours"])),
            "keep_files": tk.StringVar(value=str(saved["keep_files"])),
            "directory": tk.StringVar(value=shown_directory),
            "include_env": tk.BooleanVar(value=saved["include_env"]),
        }
        self._baseline = self._values()

        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)

        schedule = ttk.LabelFrame(body, text="Schedule (kept by the daemon)", padding=10)
        schedule.grid(row=0, column=0, sticky="ew")
        schedule.columnconfigure(1, weight=1)
        ttk.Checkbutton(
            schedule, text="Back up automatically (the daemon checks after every poll)", variable=self.vars["enabled"]
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        ttk.Label(schedule, text="Back up every (hours)").grid(row=1, column=0, sticky="w", pady=4, padx=(0, 12))
        ttk.Spinbox(schedule, from_=1, to=8760, width=8, textvariable=self.vars["every_hours"]).grid(row=1, column=1, sticky="w")
        ttk.Label(schedule, text="Keep the newest (0 = all)").grid(row=2, column=0, sticky="w", pady=4, padx=(0, 12))
        ttk.Spinbox(schedule, from_=0, to=9999, width=8, textvariable=self.vars["keep_files"]).grid(row=2, column=1, sticky="w")

        where = ttk.LabelFrame(body, text="Where and what", padding=10)
        where.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        where.columnconfigure(0, weight=1)
        ttk.Label(where, text="Folder (empty = a 'backups' folder in the app folder)", foreground=COLOR_MUTED).grid(
            row=0, column=0, columnspan=3, sticky="w"
        )
        self.directory_entry = ttk.Entry(where, textvariable=self.vars["directory"], width=52)
        self.directory_entry.grid(row=1, column=0, sticky="ew", pady=(2, 6))
        ttk.Button(where, text="Browse…", command=lambda: pick_directory(self, self.vars["directory"])).grid(
            row=1, column=1, padx=(6, 0)
        )
        ttk.Button(where, text="Open folder", command=self._open_folder).grid(row=1, column=2, padx=(6, 0))
        ttk.Label(
            where,
            text="Every backup holds state.db (a consistent copy, safe while the daemon runs), config.json and mapping.json.",
            foreground=COLOR_MUTED, wraplength=560, justify="left",
        ).grid(row=2, column=0, columnspan=3, sticky="w")
        ttk.Checkbutton(
            where, text="Also include .env (contains your Gmail app password and GitHub token in plain text)",
            variable=self.vars["include_env"],
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))

        self.last_label = ttk.Label(body, text="", foreground=COLOR_MUTED, wraplength=580, justify="left")
        self.last_label.grid(row=2, column=0, sticky="w", pady=(10, 0))
        ttk.Label(
            body,
            text="To restore: stop the daemon, close the GUI, and unzip the files over the originals in the app folder.",
            foreground=COLOR_MUTED, wraplength=580, justify="left",
        ).grid(row=3, column=0, sticky="w", pady=(4, 0))
        self.message = ttk.Label(body, text="", foreground=COLOR_ERROR, wraplength=580, justify="left")
        self.message.grid(row=4, column=0, sticky="w", pady=(6, 0))

        buttons = ttk.Frame(body)
        buttons.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        buttons.columnconfigure(1, weight=1)
        self.now_button = ttk.Button(buttons, text="Back up now", command=self._backup_now)
        self.now_button.grid(row=0, column=0)
        ttk.Button(buttons, text="Cancel", command=self._close_request).grid(row=0, column=2, padx=(0, 6))
        ttk.Button(buttons, text="Save", command=self._save).grid(row=0, column=3)

        self._update_last()
        self.bind("<Escape>", lambda _event: self._close_request())
        self.protocol("WM_DELETE_WINDOW", self._close_request)
        center_dialog(self, master, focus=self.directory_entry)
        self.grab_set()

    def _values(self) -> dict[str, Any]:
        """The form as typed."""
        return {
            "enabled": bool(self.vars["enabled"].get()),
            "every_hours": str(self.vars["every_hours"].get()).strip(),
            "keep_files": str(self.vars["keep_files"].get()).strip(),
            "directory": str(self.vars["directory"].get()).strip(),
            "include_env": bool(self.vars["include_env"].get()),
        }

    def _folder(self) -> str:
        """The backup folder as typed, with ~ and variables expanded (the default folder when empty)."""
        typed = str(self.vars["directory"].get()).strip()
        if not typed:
            return config_manager.get_backup_settings({})["directory"]
        return os.path.expandvars(os.path.expanduser(typed))

    def _typed_settings(self) -> Optional[config_manager.BackupSettings]:
        """The form as backup settings, or None (with the reason shown) when it is not valid."""
        result = gui_forms.build_backup_changes(self._values())
        if not result.ok:
            self.message.configure(text="\n".join(result.errors), foreground=COLOR_ERROR)
            return None
        changes = result.changes
        return {
            "enabled": bool(changes["backup.enabled"]),
            "every_hours": int(changes["backup.every_hours"]),
            "keep_files": int(changes["backup.keep_files"]),
            "directory": self._folder(),
            "include_env": bool(changes["backup.include_env"]),
        }

    def _update_last(self) -> None:
        """Show the newest backup in the folder as typed (or that there is none)."""
        backups = backup_manager.list_backups(self._folder())
        if not backups:
            self.last_label.configure(text="No backup in this folder yet.")
            return
        newest = backups[0]
        when = datetime.fromtimestamp(newest.created).strftime("%Y-%m-%d %H:%M")
        size_mb = newest.size / (1024 * 1024)
        self.last_label.configure(text=f"Newest backup: {when} ({size_mb:.1f} MB); {len(backups)} in this folder.")

    def _open_folder(self) -> None:
        """Open the backup folder in the file manager (it is created first if needed)."""
        folder = self._folder()
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            self.message.configure(text=f"Could not create the folder: {exc}", foreground=COLOR_ERROR)
            return
        try:
            open_in_file_manager(folder)
        except OSError as exc:
            self.message.configure(text=f"Could not open the folder: {exc}", foreground=COLOR_ERROR)

    def _backup_now(self) -> None:
        """Make a backup with the values as typed, in a thread (a big database takes a moment)."""
        settings = self._typed_settings()
        if settings is None:
            return
        self.message.configure(text="Backing up…", foreground=COLOR_MUTED)
        self.now_button.state(["disabled"])
        self._answer.clear()

        def run() -> None:
            """Thread body: make the backup and queue its result."""
            self._answer.append(backup_manager.create_backup(settings))

        threading.Thread(target=run, daemon=True).start()
        self.after(100, self._poll)

    def _poll(self) -> None:
        """Wait (polling every 100 ms) for the backup thread, then show the result."""
        if not self.winfo_exists():
            return
        if not self._answer:
            self.after(100, self._poll)
            return
        result = self._answer[0]
        self.now_button.state(["!disabled"])
        self.message.configure(text=result.message, foreground=COLOR_OK if result.ok else COLOR_ERROR)
        self._update_last()

    def _close_request(self) -> None:
        """Close the window, asking first when there are unsaved changes."""
        if self._values() != self._baseline and not messagebox.askyesno(
            "Backup",
            "You have changes that are not saved.\n\nClose the window and discard them?",
            icon="warning", default="no", parent=self,
        ):
            return
        self.destroy()

    def _save(self) -> None:
        """Validate the form and write the `backup` section of config.json, then close."""
        result = gui_forms.build_backup_changes(self._values())
        if not result.ok:
            self.message.configure(text="\n".join(result.errors), foreground=COLOR_ERROR)
            return
        try:
            config_manager.set_config_values(result.changes)
        except (config_manager.ConfigLockTimeout, config_manager.ConfigUnreadableError, OSError) as exc:
            self.message.configure(text=describe_error(exc), foreground=COLOR_ERROR)
            return
        self.destroy()
        self._on_saved("Backup settings saved. The daemon applies them at its next check.")


class ViewerDialog(tk.Toplevel):
    """Set up the push to the standalone web viewer, test the connection and switch the push of the running daemon.

    It has its own Save and Cancel (the Settings window's Save neither writes nor undoes it). Save writes the `viewer`
    section of config.json; the daemon reads the address and token when it starts, so after a change the Restart button
    appears by itself. Start/Stop sending change the running daemon's push at once. Test connection uses the values as
    typed, without saving, in a thread so the window never freezes.
    """

    STATUS_REFRESH_MS = 2000

    def __init__(self, master: tk.Misc, on_saved: Callable[[str], None]) -> None:
        """Build the Viewer window: the settings, the connection test and the daemon's live push status."""
        super().__init__(master)
        self.title("Viewer")
        self.resizable(False, False)
        self.transient(master)  # type: ignore[arg-type]
        self._on_saved = on_saved
        self._answer: list[connection_tests.ConnectionResult] = []
        self._token_visible = False

        saved = config_manager.get_viewer_settings()
        configured = config_manager.load_config().get("viewer")
        shown_name = str(configured.get("name", "")) if isinstance(configured, dict) else ""
        self.vars: dict[str, tk.Variable] = {
            "enabled": tk.BooleanVar(value=saved["enabled"]),
            "url": tk.StringVar(value=saved["url"]),
            "token": tk.StringVar(value=saved["token"]),
            "name": tk.StringVar(value=shown_name),
        }
        self._baseline = self._values()

        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)

        send = ttk.LabelFrame(body, text="Send to the web viewer", padding=10)
        send.grid(row=0, column=0, sticky="ew")
        send.columnconfigure(1, weight=1)
        ttk.Checkbutton(
            send, text="Send read-only snapshots to the viewer (the daemon connects out and opens no port)",
            variable=self.vars["enabled"],
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        ttk.Label(send, text="Viewer address").grid(row=1, column=0, sticky="w", padx=(0, 12))
        self.url_entry = ttk.Entry(send, textvariable=self.vars["url"], width=48)
        self.url_entry.grid(row=1, column=1, columnspan=2, sticky="ew")
        ttk.Label(
            send, text="For example http://192.168.0.100:8888: the computer that runs viewer/ghaadd_viewer.py.",
            foreground=COLOR_MUTED, wraplength=720, justify="left",
        ).grid(row=2, column=1, columnspan=2, sticky="w", pady=(2, 8))
        ttk.Label(send, text="Name").grid(row=3, column=0, sticky="w", padx=(0, 12))
        ttk.Entry(send, textvariable=self.vars["name"], width=48).grid(row=3, column=1, columnspan=2, sticky="ew")
        ttk.Label(
            send, text=f"How this daemon is listed in the viewer. Empty = this computer's name ({gui_viewer.computer_name()}).",
            foreground=COLOR_MUTED, wraplength=720, justify="left",
        ).grid(row=4, column=1, columnspan=2, sticky="w", pady=(2, 8))
        ttk.Label(send, text="Token").grid(row=5, column=0, sticky="w", padx=(0, 12))
        self.token_entry = ttk.Entry(send, textvariable=self.vars["token"], show="•", width=48)
        self.token_entry.grid(row=5, column=1, sticky="ew")
        token_buttons = ttk.Frame(send)
        token_buttons.grid(row=5, column=2, padx=(6, 0))
        self.show_button = ttk.Button(token_buttons, text="Show", width=6, command=self._toggle_token)
        self.show_button.grid(row=0, column=0, padx=(0, 4))
        ttk.Button(token_buttons, text="Generate", command=self._generate_token).grid(row=0, column=1, padx=(0, 4))
        ttk.Button(token_buttons, text="Copy", width=6, command=self._copy_token).grid(row=0, column=2)
        ttk.Label(
            send,
            text="The viewer needs the same token in its .env file, as name=token (only this daemon) or plain. "
            "It is stored in config.json, and so in backups.",
            foreground=COLOR_MUTED, wraplength=720, justify="left",
        ).grid(row=6, column=1, columnspan=2, sticky="w", pady=(2, 0))

        live = ttk.LabelFrame(body, text="Connection", padding=10)
        live.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        live.columnconfigure(1, weight=1)
        self.test_button = ttk.Button(live, text="Test connection", command=self._test)
        self.test_button.grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.test_result = ttk.Label(live, text="", wraplength=520, justify="left")
        self.test_result.grid(row=0, column=1, sticky="w")
        self.status_label = ttk.Label(live, text="", wraplength=720, justify="left", foreground=COLOR_MUTED)
        self.status_label.grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 0))
        push_buttons = ttk.Frame(live)
        push_buttons.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.start_button = ttk.Button(push_buttons, text="Start sending", command=lambda: self._set_push(True))
        self.start_button.grid(row=0, column=0, padx=(0, 6))
        self.stop_button = ttk.Button(push_buttons, text="Stop sending", command=lambda: self._set_push(False))
        self.stop_button.grid(row=0, column=1)

        ttk.Label(
            body,
            text="The address and token are read when the daemon starts: after a change use the Restart button. "
            "Start/Stop sending work at once, for this run of the daemon. No paths are ever sent.",
            foreground=COLOR_MUTED, wraplength=800, justify="left",
        ).grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.message = ttk.Label(body, text="", foreground=COLOR_ERROR, wraplength=800, justify="left")
        self.message.grid(row=3, column=0, sticky="w", pady=(6, 0))

        buttons = ttk.Frame(body)
        buttons.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        buttons.columnconfigure(0, weight=1)
        ttk.Button(buttons, text="Cancel", command=self._close_request).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(buttons, text="Save", command=self._save).grid(row=0, column=2)

        self._refresh_status()
        self.bind("<Escape>", lambda _event: self._close_request())
        self.protocol("WM_DELETE_WINDOW", self._close_request)
        center_dialog(self, master, focus=self.url_entry)
        self.grab_set()

    def _values(self) -> dict[str, Any]:
        """The form as typed."""
        return {
            "enabled": bool(self.vars["enabled"].get()),
            "url": str(self.vars["url"].get()).strip(),
            "token": str(self.vars["token"].get()).strip(),
            "name": str(self.vars["name"].get()).strip(),
        }

    # ----- the token -----

    def _toggle_token(self) -> None:
        """Show or hide the token in its field."""
        self._token_visible = not self._token_visible
        self.token_entry.configure(show="" if self._token_visible else "•")
        self.show_button.configure(text="Hide" if self._token_visible else "Show")

    def _generate_token(self) -> None:
        """Fill in a new random token, after asking when there already is one (the viewer must get the new one)."""
        if str(self.vars["token"].get()).strip() and not messagebox.askyesno(
            "Viewer",
            "Replace the token?\n\nThe viewer must be given the new one, or it will reject this daemon.",
            icon="warning", default="no", parent=self,
        ):
            return
        self.vars["token"].set(gui_viewer.generate_token())
        if not self._token_visible:
            self._toggle_token()  # a token nobody can read cannot be copied to the viewer
        self.message.configure(text="New token made. Copy it to the viewer, then Save.", foreground=COLOR_MUTED)

    def _copy_token(self) -> None:
        """Put the token on the clipboard."""
        token = str(self.vars["token"].get()).strip()
        if not token:
            self.message.configure(text="There is no token to copy yet.", foreground=COLOR_ERROR)
            return
        self.clipboard_clear()
        self.clipboard_append(token)
        self.message.configure(text="Token copied to the clipboard.", foreground=COLOR_OK)

    # ----- the connection test (network in a thread, the answer is picked up by _poll) -----

    def _test(self) -> None:
        """Ask the viewer, with the address and token as typed, whether it is there and accepts the token."""
        values = self._values()
        self.test_button.state(["disabled"])
        self.test_result.configure(text="Testing…", foreground=COLOR_MUTED)
        self._answer.clear()

        def run() -> None:
            """Thread body: run the check and queue its result (an exception becomes a failed result)."""
            try:
                self._answer.append(gui_viewer.check_viewer(values["url"], values["token"], values["name"]))
            except Exception as exc:  # shown in the window instead of vanishing
                self._answer.append(connection_tests.ConnectionResult(ok=False, message=f"The test could not run: {exc}"))

        threading.Thread(target=run, daemon=True).start()
        self.after(100, self._poll)

    def _poll(self) -> None:
        """Wait (polling every 100 ms) for the test thread, then show the result."""
        if not self.winfo_exists():
            return
        if not self._answer:
            self.after(100, self._poll)
            return
        result = self._answer[0]
        self.test_button.state(["!disabled"])
        self.test_result.configure(text=result.message, foreground=COLOR_OK if result.ok else COLOR_ERROR)

    # ----- the running daemon -----

    def _refresh_status(self) -> None:
        """Show what the running daemon reports about its push, and enable the matching button (every 2 seconds)."""
        if not self.winfo_exists():
            return
        try:
            view = gui_viewer.describe_push(daemon_lock.get_daemon_status(), time.time())
        except OSError:
            view = None
        if view is not None:
            self.status_label.configure(text=view.text, foreground=COLOR_ERROR if view.problem else COLOR_MUTED)
            self.start_button.state(["!disabled"] if view.start_enabled else ["disabled"])
            self.stop_button.state(["!disabled"] if view.stop_enabled else ["disabled"])
        self.after(self.STATUS_REFRESH_MS, self._refresh_status)

    def _set_push(self, on: bool) -> None:
        """Ask the running daemon to start or stop sending right now (the saved address and token are what it uses)."""
        error = gui_daemon.do_set_push(on)
        if error:
            self.message.configure(text=error, foreground=COLOR_ERROR)
            return
        self.message.configure(
            text="Asked the daemon to start sending." if on else "Asked the daemon to stop sending.", foreground=COLOR_MUTED
        )
        self.after(1500, self._refresh_status)

    # ----- close and save -----

    def _close_request(self) -> None:
        """Close the window, asking first when there are unsaved changes."""
        if self._values() != self._baseline and not messagebox.askyesno(
            "Viewer",
            "You have changes that are not saved.\n\nClose the window and discard them?",
            icon="warning", default="no", parent=self,
        ):
            return
        self.destroy()

    def _save(self) -> None:
        """Validate the form and write the `viewer` section of config.json, then close."""
        result = gui_forms.build_viewer_changes(self._values())
        if not result.ok:
            self.message.configure(text="\n".join(result.errors), foreground=COLOR_ERROR)
            return
        try:
            config_manager.set_config_values(result.changes)
        except (config_manager.ConfigLockTimeout, config_manager.ConfigUnreadableError, OSError) as exc:
            self.message.configure(text=describe_error(exc), foreground=COLOR_ERROR)
            return
        self.destroy()
        self._on_saved("Viewer settings saved. A running daemon uses a new address or token after a restart.")


class DoctorDialog(tk.Toplevel):
    """Runs the --doctor checks and shows the result; the first-run notes (if any) come first."""

    def __init__(self, master: tk.Misc, on_report: Callable[[Optional[dict]], None]) -> None:
        """Build the Doctor window and start the checks."""
        super().__init__(master)
        self.title("Doctor")
        self.transient(master)  # type: ignore[arg-type]
        self.minsize(560, 320)
        self._on_report = on_report
        self._result: Optional[dict] = None
        self._error: Optional[str] = None

        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)

        self._bold = tkfont.nametofont("TkDefaultFont").copy()
        self._bold.configure(weight="bold")
        self.summary = ttk.Label(body, text="", font=self._bold)
        self.summary.grid(row=0, column=0, sticky="w", pady=(0, 8))

        text_frame = ttk.Frame(body)
        text_frame.grid(row=1, column=0, sticky="nsew")
        text_frame.columnconfigure(0, weight=1)
        text_frame.rowconfigure(0, weight=1)
        self.text = tk.Text(text_frame, width=92, height=20, wrap="word", state="disabled", relief="solid", borderwidth=1, padx=8, pady=6, font="TkDefaultFont")
        scrollbar = ttk.Scrollbar(text_frame, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scrollbar.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.text.tag_configure("heading", font=self._bold, spacing1=8, spacing3=2)
        self.text.tag_configure("error", foreground=COLOR_ERROR)
        self.text.tag_configure("warning", foreground=COLOR_WARNING)
        self.text.tag_configure("ok", foreground=COLOR_OK)
        self.text.tag_configure("muted", foreground=COLOR_MUTED)

        buttons = ttk.Frame(body)
        buttons.grid(row=2, column=0, sticky="e", pady=(10, 0))
        self.run_button = ttk.Button(buttons, text="Run again", command=self._start)
        self.run_button.grid(row=0, column=0, padx=(0, 6))
        ttk.Button(buttons, text="Close", command=self.destroy).grid(row=0, column=1)

        self.bind("<Escape>", lambda _event: self.destroy())
        center_dialog(self, master)
        self._start()
        self.after(200, self._focus_when_ready)

    def _focus_when_ready(self) -> None:
        """Keep the keyboard on the dialog (Escape closes it) while the checks run in their thread."""
        try:
            self.focus_set()
        except tk.TclError:
            pass

    def _start(self) -> None:
        """Start the checks in a thread so a slow disk or network drive cannot freeze the window."""
        self.summary.configure(text="Checking\u2026")
        self.run_button.state(["disabled"])
        self._result, self._error = None, None
        threading.Thread(target=self._work, daemon=True).start()  # a slow network drive must not freeze the window
        self.after(100, self._poll)

    def _work(self) -> None:
        """Thread body: run the doctor report, or keep the error text."""
        try:
            self._result = dict(gui_doctor.run_report())
        except Exception as exc:  # shown in the dialog instead of vanishing
            self._error = describe_error(exc)

    def _poll(self) -> None:
        """Wait (polling every 100 ms) for the thread, then show its result."""
        if not self.winfo_exists():
            return
        if self._result is None and self._error is None:
            self.after(100, self._poll)
            return
        self.run_button.state(["!disabled"])
        self._show(self._result)

    def _write(self, text: str, tag: str = "") -> None:
        """Append text to the report area with an optional style tag."""
        self.text.insert("end", text, tag)

    def _show(self, report: Optional[dict]) -> None:
        """Draw the doctor report (or the first-run help and errors) into the text area."""
        reasons = gui_doctor.first_run_reasons()
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        if self._error:
            self.summary.configure(text="The checks could not run.")
            self._write(self._error + "\n", "error")
        elif report is not None:
            self.summary.configure(text=gui_doctor.summary_line(report))
            if reasons:
                self._write("Looks like a first run\n", "heading")
                for reason in reasons:
                    self._write(f"\u2022 {reason}\n")
            if report["errors"]:
                self._write("Problems\n", "heading")
                for line in report["errors"]:
                    self._write(f"\u2717 {line}\n", "error")
            if report["warnings"]:
                self._write("Warnings\n", "heading")
                for line in report["warnings"]:
                    self._write(f"\u26a0 {line}\n", "warning")
            self._write("Checked\n", "heading")
            for line in report["checks"]:
                self._write(f"\u2713 {line}\n", "ok")
            self._write(f"\nPlatform: {report['platform']}\n", "muted")
        self.text.configure(state="disabled")
        self._on_report(report)


def calendar_module() -> Any:
    """Return the optional tkcalendar module, or None when it is not installed (the date fields still work)."""
    try:
        import tkcalendar  # optional extra, see requirements-optional.txt
    except ImportError:
        return None
    return tkcalendar


class DatePicker(ttk.Frame):
    """A calendar panel (needs tkcalendar) drawn inside the dialog below a date field, like a drop-down list.

    It is a widget, not a window: a separate pop-up window is first placed by the window manager (at the screen's
    corner) and re-sized in plain sight, which showed as a flicker. Clicking a day hands it to `on_pick`.
    """

    def __init__(self, dialog: tk.Toplevel, anchor: tk.Misc, initial: Optional[date], on_pick: Callable[[date], None]) -> None:
        """Open the calendar just below `anchor` (above it when there is no room), showing `initial` or today."""
        super().__init__(dialog, relief="solid", borderwidth=1)
        start = initial or date.today()
        calendar = calendar_module().Calendar(
            self, selectmode="day", year=start.year, month=start.month, day=start.day, date_pattern="yyyy-mm-dd",
            firstweekday="monday", showweeknumbers=False,
        )
        calendar.pack(padx=6, pady=6)
        self._on_pick = on_pick
        self._calendar = calendar
        calendar.bind("<<CalendarSelected>>", self._picked)
        self.update_idletasks()
        width, height = self.winfo_reqwidth(), self.winfo_reqheight()
        field_x = anchor.winfo_rootx() - dialog.winfo_rootx()
        field_y = anchor.winfo_rooty() - dialog.winfo_rooty()
        x = max(4, min(field_x, dialog.winfo_width() - width - 4))
        y = field_y + anchor.winfo_height() + 2
        if y + height > dialog.winfo_height() - 4:  # no room below the field: open above it
            y = max(4, field_y - height - 2)
        self.place(x=x, y=y)
        self.lift()
        calendar.focus_set()

    def contains(self, widget: object) -> bool:
        """True when a widget (or its path name) is this panel or one of its parts."""
        path, own = str(widget), str(self)
        return path == own or path.startswith(own + ".")

    def _picked(self, _event: tk.Event) -> None:  # type: ignore[type-arg]
        """Pass the clicked day on and close the panel."""
        chosen = self._calendar.selection_get()
        self.destroy()
        if chosen:
            self._on_pick(chosen)


class StatsDialog(tk.Toplevel):
    """Statistics about the mapped repositories and the work done so far (the figures of `--stats`)."""

    DAILY_BAR = "#0078d4"
    CUSTOM = "Custom range"

    def __init__(self, master: tk.Misc) -> None:
        """Build the Stats window (tabs Overview, Busiest, Biggest, Per day) and start collecting.

        The window stays hidden until the figures are in (`_reveal`), so it opens once, filled and fitted.
        """
        super().__init__(master)
        self.withdraw()
        self._revealed = False
        self.title("Statistics")
        self.transient(master)  # type: ignore[arg-type]
        self.minsize(760, 480)
        self._report: Optional[dict] = None
        self._data: Optional[dict] = None  # what stats.load() read; every window change re-builds the report from it
        self._result: Optional[dict] = None
        self._range: Optional[stats.DateRange] = None
        self._applied_dates = ("", "")
        self._picker: Optional[DatePicker] = None
        self._picker_field = ""
        self._calendar_buttons: list[tk.Misc] = []
        self._error: Optional[str] = None
        self._bold = tkfont.nametofont("TkDefaultFont").copy()
        self._bold.configure(weight="bold")

        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)
        self.summary = ttk.Label(body, text="", font=self._bold)
        self.summary.grid(row=0, column=0, sticky="w", pady=(0, 8))
        self._build_period_bar(body).grid(row=1, column=0, sticky="ew", pady=(0, 8))

        self.notebook = ttk.Notebook(body)
        self.notebook.grid(row=2, column=0, sticky="nsew")

        overview = ttk.Frame(self.notebook, padding=8)
        overview.columnconfigure(0, weight=1)
        overview.rowconfigure(2, weight=1)
        self.overview_text = tk.Text(overview, width=96, height=11, wrap="word", state="disabled", relief="flat",
                                     borderwidth=0, font="TkDefaultFont", background=self.cget("background"))
        self.overview_text.grid(row=0, column=0, sticky="ew")
        self.overview_text.tag_configure("heading", font=self._bold, spacing1=6, spacing3=2)
        self._overview_width = 0
        self.overview_text.bind("<Configure>", self._on_overview_resized)  # lines re-wrap when the width changes
        self.overview_text.bind("<MouseWheel>", lambda _event: "break")  # it is sized to fit, never scrolled
        ttk.Label(overview, text="Activity", font=self._bold).grid(row=1, column=0, sticky="w", pady=(10, 2))
        self.periods = self._make_table(overview, stats.PERIOD_HEADERS, row=2)
        self.notebook.add(overview, text="Overview")

        busiest = ttk.Frame(self.notebook, padding=8)
        busiest.columnconfigure(0, weight=1)
        busiest.rowconfigure(1, weight=1)
        self.busiest_title = ttk.Label(busiest, text="", font=self._bold)
        self.busiest_title.grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.busiest_show = self._make_show_box(busiest, self._on_show_changed)
        self.busiest = self._make_table(busiest, stats.BUSIEST_HEADERS, row=1)
        self.notebook.add(busiest, text="Busiest")

        biggest = ttk.Frame(self.notebook, padding=8)
        biggest.columnconfigure(0, weight=1)
        biggest.rowconfigure(1, weight=1)
        biggest.rowconfigure(3, weight=1)
        self.biggest_title = ttk.Label(biggest, text="", font=self._bold)
        self.biggest_title.grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.biggest_show = self._make_show_box(biggest, self._on_show_changed)
        self.biggest = self._make_table(biggest, stats.BIGGEST_HEADERS, row=1)
        self.releases_title = ttk.Label(biggest, text="", font=self._bold)
        self.releases_title.grid(row=2, column=0, sticky="w", pady=(10, 4))
        self.releases = self._make_table(biggest, stats.RELEASE_HEADERS, row=3)
        self.notebook.add(biggest, text="Biggest")

        daily = ttk.Frame(self.notebook, padding=8)
        daily.columnconfigure(0, weight=1)
        daily.rowconfigure(1, weight=1)
        self.daily_title = ttk.Label(daily, text="", font=self._bold)
        self.daily_title.grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.chart = tk.Canvas(daily, background=COLOR_FIELD, highlightthickness=1, highlightbackground=COLOR_BORDER, height=260)
        self.chart.grid(row=1, column=0, sticky="nsew")
        self.chart.bind("<Configure>", lambda _event: self._draw_chart())
        self.notebook.add(daily, text="Per day")

        self.footer = ttk.Label(body, text="", foreground=COLOR_MUTED)
        self.footer.grid(row=3, column=0, sticky="w", pady=(8, 0))
        buttons = ttk.Frame(body)
        buttons.grid(row=4, column=0, sticky="e", pady=(8, 0))
        self.refresh_button = ttk.Button(buttons, text="Refresh", command=self._start)
        self.refresh_button.grid(row=0, column=0, padx=(0, 6))
        self.copy_button = ttk.Button(buttons, text="Copy as text", command=self._copy)
        self.copy_button.grid(row=0, column=1, padx=(0, 6))
        ttk.Button(buttons, text="Close", command=self.destroy).grid(row=0, column=2)

        self.bind("<Escape>", self._on_escape)
        self.bind("<Button-1>", self._on_click, add="+")  # a click anywhere else closes an open calendar
        self._start()

    def _reveal(self) -> None:
        """Show the finished window once: lay it out while it is invisible, centre it, then make it visible."""
        if self._revealed:
            return
        self._revealed = True

        def place() -> None:
            """Fit the overview and the chart to the final size, then centre over the main window."""
            self._fit_overview()
            self._draw_chart()
            center_dialog(self, self.master)  # type: ignore[arg-type]

        reveal_window(self, place)

    def _build_period_bar(self, parent: tk.Misc) -> ttk.Frame:
        """Create the "Period: preset, From, To" bar that narrows Busiest, Biggest and Per day to a window."""
        bar = ttk.Frame(parent)
        ttk.Label(bar, text="Period:").pack(side="left", padx=(0, 4))
        self.preset = ttk.Combobox(bar, values=[label for label, _days in stats.PRESETS] + [self.CUSTOM], state="readonly",
                                   width=14)
        self.preset.current(0)
        self.preset.pack(side="left", padx=(0, 12))
        self.preset.bind("<<ComboboxSelected>>", lambda _event: self._on_preset())
        self.date_entries: dict[str, ttk.Entry] = {}
        for name, title in (("from", "From"), ("to", "To")):
            ttk.Label(bar, text=f"{title}:").pack(side="left", padx=(0, 4))
            entry = ttk.Entry(bar, width=11)
            entry.pack(side="left")
            entry.bind("<Return>", lambda _event: self._on_dates_typed())
            entry.bind("<FocusOut>", lambda _event: self._on_dates_typed())
            self.date_entries[name] = entry
            if calendar_module() is not None:  # without tkcalendar the typed YYYY-MM-DD field is all there is
                button = ttk.Button(bar, text="\u25be", width=2, command=lambda n=name: self._open_calendar(n))
                button.pack(side="left")
                self._calendar_buttons.append(button)
            ttk.Frame(bar, width=10).pack(side="left")
        self.period_message = ttk.Label(bar, text="", foreground=COLOR_ERROR)
        self.period_message.pack(side="left")
        hint = "dates as YYYY-MM-DD; empty = no limit" if calendar_module() is None else "pick a day or type YYYY-MM-DD"
        ttk.Label(bar, text=hint, foreground=COLOR_MUTED).pack(side="right")
        return bar

    def _close_picker(self) -> None:
        """Close the calendar panel if one is open."""
        if self._picker is not None and self._picker.winfo_exists():
            self._picker.destroy()
        self._picker = None
        self._picker_field = ""

    def _on_escape(self, _event: tk.Event) -> None:  # type: ignore[type-arg]
        """Escape closes an open calendar first, and the window when there is none."""
        if self._picker is not None and self._picker.winfo_exists():
            self._close_picker()
        else:
            self.destroy()

    def _on_click(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        """Close the calendar on a click outside it (the ▾ buttons toggle it themselves)."""
        picker = self._picker
        if picker is None or not picker.winfo_exists():
            return
        if not picker.contains(event.widget) and event.widget not in self._calendar_buttons:
            self._close_picker()

    def _open_calendar(self, name: str) -> None:
        """Open the calendar for the From or To field (a second click on its ▾ closes it) and fill the clicked day in."""
        reopen = self._picker_field != name
        self._close_picker()
        if not reopen:
            return
        entry = self.date_entries[name]
        try:
            initial = stats.parse_date(entry.get())
        except ValueError:
            initial = None

        def picked(day: date) -> None:
            """Write the chosen day into the field and apply the window."""
            entry.delete(0, "end")
            entry.insert(0, day.isoformat())
            self._picker, self._picker_field = None, ""
            self._on_dates_typed()

        self._picker, self._picker_field = DatePicker(self, entry, initial, picked), name

    def _on_preset(self) -> None:
        """A preset was chosen: fill the date fields with what it means and apply it ("Custom range" leaves them)."""
        label = self.preset.get()
        if label == self.CUSTOM:
            self.date_entries["from"].focus_set()
            return
        window = stats.preset_range(dict(stats.PRESETS)[label], date.today())
        first = last = ""
        if window is not None and window.start is not None:
            first = datetime.fromtimestamp(window.start).date().isoformat()
            last = date.today().isoformat()
        for name, text in (("from", first), ("to", last)):
            self.date_entries[name].delete(0, "end")
            self.date_entries[name].insert(0, text)
        self._on_dates_typed(from_preset=True)

    def _on_dates_typed(self, from_preset: bool = False) -> None:
        """Read the two date fields; apply the window when they are valid and changed, else say what is wrong."""
        texts = (self.date_entries["from"].get().strip(), self.date_entries["to"].get().strip())
        if texts == self._applied_dates:
            return
        try:
            window = stats.date_range(stats.parse_date(texts[0]), stats.parse_date(texts[1]))
        except ValueError as exc:
            self.period_message.configure(text=str(exc))
            return
        self.period_message.configure(text="")
        self._applied_dates = texts
        self._range = window
        if not from_preset:
            self.preset.set(self.CUSTOM if window is not None else stats.PRESETS[0][0])
        if self._data is not None:
            self._rebuild()

    @staticmethod
    def _make_show_box(parent: tk.Misc, command: Callable[[], None]) -> ttk.Combobox:
        """Add the "Show: Top 10 / 25 / 50 / All" drop-down at the right end of a tab's title row."""
        box = ttk.Frame(parent)
        box.grid(row=0, column=1, sticky="e", pady=(0, 4))
        ttk.Label(box, text="Show:").pack(side="left", padx=(0, 4))
        combo = ttk.Combobox(box, values=[label for label, _rows in stats.TOP_CHOICES], state="readonly", width=8)
        combo.current(0)
        combo.pack(side="left")
        combo.bind("<<ComboboxSelected>>", lambda _event: command())
        return combo

    @staticmethod
    def _choice(combo: ttk.Combobox) -> Optional[int]:
        """Return the number of rows a "Show" drop-down asks for (None = all)."""
        index = combo.current()
        return stats.TOP_CHOICES[index][1] if 0 <= index < len(stats.TOP_CHOICES) else stats.TOP_COUNT

    def _make_table(self, parent: tk.Misc, headers: tuple[str, ...], row: int) -> ttk.Treeview:
        """Create a table with the given headings in a grid row and return it."""
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=0, columnspan=2, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        tree = ttk.Treeview(frame, columns=headers, show="headings", selectmode="browse", height=6)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        for index, header in enumerate(headers):
            tree.heading(header, text=header, anchor="w" if index == 0 else "e")
            tree.column(header, anchor="w" if index == 0 else "e", width=230 if index == 0 else 90, stretch=index == 0)
        tree.tag_configure("odd", background=COLOR_STRIPE)
        tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        return tree

    @staticmethod
    def _fill(tree: ttk.Treeview, rows: list[tuple[str, ...]]) -> None:
        """Replace a table's rows, with zebra striping."""
        tree.delete(*tree.get_children())
        for index, row in enumerate(rows):
            tree.insert("", "end", values=row, tags=("odd",) if index % 2 else ())

    def _start(self) -> None:
        """Collect the statistics in a thread so a big history on a slow disk cannot freeze the window."""
        self.summary.configure(text="Counting…")
        self.refresh_button.state(["disabled"])
        self._result, self._error = None, None
        threading.Thread(target=self._work, daemon=True).start()  # a big history on a slow disk must not freeze the window
        self.after(100, self._poll)

    def _work(self) -> None:
        """Thread body: collect the figures, or keep the error text."""
        try:
            self._result = stats.load()  # the window and "Show" drop-downs cut it down later without a new query
        except Exception as exc:  # shown in the dialog instead of vanishing
            self._error = describe_error(exc)

    def _poll(self) -> None:
        """Wait (polling every 100 ms) for the thread, then show its result."""
        if not self.winfo_exists():
            return
        if self._result is None and self._error is None:
            self.after(100, self._poll)
            return
        self.refresh_button.state(["!disabled"])
        if self._error:
            self.summary.configure(text="The statistics could not be counted.")
            self.footer.configure(text=self._error)
            self._reveal()
            return
        self._data = self._result
        self._rebuild()
        self._reveal()

    def _rebuild(self) -> None:
        """Build the report for the chosen window from the loaded data and show it."""
        if self._data is not None:
            self._show(stats.report_from(self._data, top=None, date_range=self._range))

    def _show(self, report: dict) -> None:
        """Fill the summary line, the overview and the tables from the report."""
        self._report = report
        mapping, history = report["mapping"], report["history"]
        days = f", {history['days']} days of history" if history["days"] else ""
        self.summary.configure(
            text=f"{mapping['total']} repositories mapped, {mapping['active']} active; {history['jobs']:,} jobs on record{days}"
        )
        self.overview_text.configure(state="normal")
        self.overview_text.delete("1.0", "end")
        for line in stats.summary_lines(report):
            heading = bool(line) and not line.startswith(" ")
            self.overview_text.insert("end", line + "\n", "heading" if heading else "")
        self.overview_text.configure(state="disabled")
        self._fit_overview()
        self._fill(self.periods, stats.period_rows(report))
        self._show_lists()
        daily = report["daily"]
        self.daily_title.configure(text=stats.daily_title(report))
        self._draw_chart()
        self.footer.configure(text="  |  ".join(stats.storage_lines(report)) + "   (data sizes are those of the releases on record)")

    def _shown_report(self) -> dict:
        """The report cut to what the two "Show" drop-downs ask for (what is on screen and what Copy gives)."""
        return stats.limited(self._report or {}, self._choice(self.busiest_show), self._choice(self.biggest_show))

    def _show_lists(self) -> None:
        """Fill the Busiest and Biggest tables and their headings for the chosen number of rows."""
        report = self._shown_report()
        busiest_title, biggest_title, releases_title = stats.list_titles(report)
        self.busiest_title.configure(text=busiest_title)
        self.biggest_title.configure(text=biggest_title)
        self.releases_title.configure(text=releases_title)
        self._fill(self.busiest, stats.busiest_rows(report))
        self._fill(self.biggest, stats.biggest_rows(report))
        self._fill(self.releases, stats.release_rows(report))

    def _on_show_changed(self) -> None:
        """Re-cut the lists after a "Show" drop-down changed (the data is already loaded)."""
        if self._report:
            self._show_lists()

    def _on_overview_resized(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        """Refit only when the WIDTH changed: fitting changes the height, and reacting to that would loop forever."""
        if event.width != self._overview_width:
            self._overview_width = event.width
            self.after_idle(self._fit_overview)

    def _fit_overview(self) -> None:
        """Make the summary box as tall as its (wrapped) text, so it neither scrolls nor leaves a gap."""
        try:
            self._fit_overview_now()
        except tk.TclError:  # the window was closed while the fit was still queued
            pass

    def _fit_overview_now(self) -> None:
        """Size the overview text to its content so no inner scrollbar is needed."""
        text = self.overview_text
        text.update_idletasks()
        pixels = text.count("1.0", "end-1c", "ypixels")  # headings add spacing, so count pixels, not lines
        line_height = max(tkfont.nametofont(text.cget("font")).metrics("linespace"), 1)
        wanted = max(-(-int(pixels[0] if pixels else line_height) // line_height), 1)
        current = int(text.cget("height"))
        if current < wanted or current > wanted + 1:  # one line of slack is fine: leave it alone so nothing flaps
            text.configure(height=wanted)
            current = wanted
        for _ in range(4):  # a few pixels of heading spacing can still be hidden: add a line until all text shows
            text.update_idletasks()
            if text.yview()[1] >= 1.0:
                break
            current += 1
            text.configure(height=current)

    def _draw_chart(self) -> None:
        """Draw the jobs bar chart: bars (days, or weeks/months for long ranges), then day, month and year rows."""
        canvas = self.chart
        canvas.delete("all")
        if not self._report:
            return
        small = tkfont.Font(font=("TkDefaultFont", 8))
        row_h = small.metrics("linespace") + 3
        width, height = max(canvas.winfo_width(), 200), max(canvas.winfo_height(), 120)
        left, right, top = 36, 12, 18
        # Bars thinner than ~5 px are useless: a long range is summed into weeks, and a very long one into months.
        unit, daily = stats.bucket_daily(self._report["daily"], max(1, (width - left - right) // 5))
        self.daily_title.configure(text=stats.daily_title(self._report, unit))
        slot = (width - left - right) / max(len(daily), 1)
        # Day numbers lie on their side when two digits do not fit across a bar; the week/month views have none.
        vertical = slot < small.measure("00") + 10
        day_row_h = small.measure("00") + 6 if vertical else row_h
        show_days = unit == "day" and (not vertical or slot >= small.metrics("linespace") * 0.6)
        bottom = (day_row_h if show_days else 0) + 2 * row_h + 6
        peak = max((day["jobs"] for day in daily), default=0) or 1
        plot_h = height - top - bottom
        axis_y = top + plot_h
        canvas.create_line(left, top, left, axis_y, fill=COLOR_AXIS)
        canvas.create_line(left, axis_y, width - right, axis_y, fill=COLOR_AXIS)
        canvas.create_text(left - 4, top, text=str(peak), anchor="e", fill=COLOR_MUTED)
        canvas.create_text(left - 4, axis_y, text="0", anchor="e", fill=COLOR_MUTED)

        # Day numbers need their width (lying down: their height) plus a gap; when that is more than a bar, every
        # n-th day is labelled. The last day is always labelled, so a regular label too close before it makes way.
        labelled: set[int] = set()
        if show_days:
            need = (small.metrics("linespace") + 4) if vertical else (small.measure("00") + 10)
            marks = list(range(0, len(daily), max(1, round(need / slot))))
            if marks and marks[-1] != len(daily) - 1:
                if (len(daily) - 1 - marks[-1]) * slot < need:
                    marks.pop()
                marks.append(len(daily) - 1)
            labelled = set(marks)
        for index, day in enumerate(daily):
            x0 = left + index * slot + slot * 0.15
            x1 = left + (index + 1) * slot - slot * 0.15
            bar_h = plot_h * day["jobs"] / peak
            if day["jobs"]:
                canvas.create_rectangle(x0, axis_y - bar_h, x1, axis_y, fill=self.DAILY_BAR, outline="")
                if slot >= 22:
                    canvas.create_text((x0 + x1) / 2, axis_y - bar_h - 2, text=str(day["jobs"]), anchor="s",
                                       fill=COLOR_MUTED, font=("TkDefaultFont", 8))
            if index in labelled:
                label = str(int(day["date"][8:10]))
                # A rotated text is placed by its centre (an anchor like "n" refers to the unrotated text and shifts it
                # sideways), so the centre sits half the text's length below the axis.
                canvas.create_text((x0 + x1) / 2, axis_y + 4 + (small.measure(label) / 2 if vertical else small.metrics("linespace") / 2),
                                   text=label, anchor="center", angle=90 if vertical else 0, fill=COLOR_MUTED,
                                   font=("TkDefaultFont", 8))

        # Months and years: each name is centred under the bars it covers (a cut-off first or last month is centred
        # under the part that is shown), the full name when it fits, else the short one, else nothing.
        month_names = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
                       "October", "November", "December"]
        month_y = axis_y + 4 + (day_row_h if show_days else 0)
        for key_len, row, names in ((7, 0, month_names), (4, 1, None)):
            start = 0
            for index in range(1, len(daily) + 1):
                if index < len(daily) and daily[index]["date"][:key_len] == daily[start]["date"][:key_len]:
                    continue
                span_l, span_r = left + start * slot, left + index * slot
                key = daily[start]["date"][:key_len]
                full = names[int(key[5:7]) - 1] if names else key
                for text in ((full, full[:3]) if names else (full,)):
                    if small.measure(text) + 8 <= span_r - span_l:
                        canvas.create_text((span_l + span_r) / 2, month_y + row * row_h, text=text, anchor="n",
                                           fill=COLOR_MUTED, font=("TkDefaultFont", 8))
                        break
                if row == 0 and start > 0:  # a faint tick marks where a month begins
                    canvas.create_line(span_l, axis_y, span_l, month_y + row_h * 2 - 3, fill=COLOR_BORDER)
                start = index

    def _copy(self) -> None:
        """Copy the plain-text report to the clipboard."""
        if not self._report:
            return
        self.clipboard_clear()
        self.clipboard_append(stats.format_report(self._shown_report()))
        self.footer.configure(text="Copied to the clipboard.")


class MainWindow(tk.Tk):
    """The application window: tabs, control bar, footer and the timers that keep them current.

    It only reads state.db, mapping.json and config.json and talks to the daemon through daemon_control; the
    logic lives in the toolkit-independent gui_* modules. The daemon is never stopped by closing this window.
    """
    DEFAULT_SIZE = "1280x720"
    _dark_active = False  # the mode in use, for the static style helper
    MIN_SIZE = (1080, 520)  # narrower and the editor's last column (Active / Shared folder / buttons) is cut off

    def __init__(self, theme: Optional[str] = None, start_minimized: bool = False, start_daemon: bool = False) -> None:
        """Build the main window: theme, tabs, control bar, footer, timers, tray and the single-instance poll."""
        super().__init__()
        self.title(APP_TITLE)
        # gui.refresh_seconds / gui.status_message_seconds in config.json (read once; a restart of the GUI applies changes)
        config = config_manager.load_config()
        self._refresh_ms = int(config_manager.get_gui_refresh_seconds(config) * 1000)
        self._status_message_ms = int(config_manager.get_gui_status_message_seconds(config) * 1000)
        self._folder_counts = work_folders.folder_count_cache()
        self._icon_image: Optional[tk.PhotoImage] = None  # keep a reference or Tk drops the icon
        self._set_icon()
        self.minsize(*self.MIN_SIZE)
        self._restore_window_state()
        self._normal_state: Optional[gui_state.WindowState] = None  # last size/position while not maximized
        self.bind("<Configure>", self._remember_normal_state)
        self.protocol("WM_DELETE_WINDOW", self._on_close_request)
        self._theme_name = theme
        self._dark = MainWindow._dark_active = config_manager.get_gui_dark_mode(config)
        set_palette(self._dark)
        self._apply_theme(theme)
        self._status_reset_job: Optional[str] = None
        try:  # a mapping.json with pre-2.0 key names is upgraded now (after a backup), unless an older daemon runs
            mapping_manager.migrate_mapping_file()
        except Exception:  # not fatal: the old names are still understood when reading
            pass

        self._snapshot = gui_daemon.DaemonSnapshot()
        self._reader = gui_daemon.SnapshotReader()
        self._starting_since: Optional[float] = None  # set by Start/Stop until the daemon appears/disappears
        self._stopping_since: Optional[float] = None
        self._restart_pending = False  # a restart was requested: start again once the daemon has exited
        self._gui_started_at: Optional[float] = None  # when this GUI last launched a daemon
        self._was_running = False

        self._tray: Optional[gui_tray.TrayIcon] = None
        self._tray_job: Optional[str] = None
        # Looks for a "come forward" note from a second start of the GUI (see gui_instance).
        self._instance_job: Optional[str] = self.after(500, self._poll_instance)
        self._notifications = config_manager.get_gui_notifications_enabled(config)
        self._silenced_warning_types = set(config_manager.get_gui_silenced_warning_types(config))
        self._minimize_to_tray = config_manager.get_gui_minimize_to_tray(config)
        self._close_to_tray = config_manager.get_gui_close_to_tray(config)
        self._notified_warning_id: Optional[int] = None  # newest warning already announced (None = not looked yet)
        self._limit_tracker = gui_tray.NewItemTracker()
        self._unmapped_tracker = gui_tray.NewItemTracker()
        self._failed_unseen = 0  # jobs that failed while the window was hidden (cleared when it is shown again)

        # The footer holds the status messages on the left and the daemon's status on the right.
        self.footer = ttk.Frame(self)
        self.footer.columnconfigure(0, weight=1)
        self.control_bar = ControlBar(self, status_parent=self.footer)
        self.control_bar.pack(fill="x")
        self.control_bar.settings_button.configure(command=self._open_settings)
        self.control_bar.start_button.configure(command=self._on_start)
        self.control_bar.stop_button.configure(command=self._on_stop)
        self.control_bar.pause_button.configure(command=self._on_pause)
        self.control_bar.poll_button.configure(command=self._on_poll_now)
        self.control_bar.single_button.configure(command=self._on_single_poll)
        self.control_bar.check_button.configure(command=self._on_check_folders)
        self.control_bar.log_check.configure(command=self._on_log_toggle)
        self.control_bar.restart_button.configure(command=self._on_restart)
        self.control_bar.doctor_button.configure(command=self._open_doctor)
        self.control_bar.stats_button.configure(command=self._open_stats)
        for key, button in self.control_bar.folder_buttons.items():
            button.configure(command=lambda key=key: self._open_work_folder(key))
        self.control_bar.theme_button.configure(command=self._toggle_dark_mode)
        self.control_bar.theme_button.configure(text=gui_theme.button_text(self._dark))
        ttk.Separator(self).pack(fill="x")

        self.status_bar = ttk.Label(self.footer, text=DEFAULT_STATUS_TEXT, anchor="w", padding=(10, 3))
        self.status_bar.grid(row=0, column=0, sticky="ew")
        # The queue figures sit in the middle of the footer, whatever the messages on the left say.
        self.queue_label = ttk.Label(self.footer, text="", foreground=COLOR_MUTED)
        self.queue_label.place(relx=0.5, rely=0.5, anchor="center")
        # Its hover text changes with what it shows (the queue figures, or the job in progress).
        self._queue_tip = Tooltip(self.queue_label)
        self._queue_tip_text = gui_tooltips.CONTROL_HELP["queue_counts"]
        self.queue_label.bind(
            "<Enter>", lambda event: self._queue_tip.schedule(self._queue_tip_text, event.x_root, event.y_root - 70)
        )
        self.queue_label.bind("<Leave>", lambda _event: self._queue_tip.hide())
        self.queue_label.bind("<ButtonPress>", lambda _event: self._queue_tip.hide())
        self.footer.pack(fill="x", side="bottom")

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=8)

        self.mappings_tab = MappingsTab(self.notebook, self.set_status)
        self.log_tab = TerminalLogTab(self.notebook, self._terminal_log_active, self._on_enable_log, self.set_status)
        self.notebook.add(self.mappings_tab, text="Mappings")
        self.notebook.add(self.log_tab, text="Terminal log")
        self.status_feed = gui_data.StatusFeed()
        self._status_tabs: dict[str, StatusTab] = {}
        self._status_titles: dict[str, str] = {}
        self._unmapped_rows: Optional[list[status_tabs.UnmappedRow]] = None  # None = not shown yet, so an empty list still draws its message
        self._current_status_key: Optional[str] = None
        for definition in status_tabs.TAB_DEFS:
            tab = self._build_status_tab(definition)
            self._status_tabs[definition.key] = tab
            self._status_titles[definition.key] = definition.title
            self.notebook.add(tab, text=definition.title)

        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self.bind("<Map>", self._on_map)
        self._tick_job: Optional[str] = self.after(self._refresh_ms, self._tick)
        self._daemon_job: Optional[str] = None
        self._verify_log_job: Optional[str] = None
        self._update_daemon_view()
        self._refresh_status_tabs()  # titles and read marks from the start, not after the first tick
        self._refresh_doctor_attention()
        self._start_tray(config)
        self._apply_launch_options(
            start_minimized or config_manager.get_gui_start_minimized(config),
            start_daemon or config_manager.get_gui_start_daemon(config),
        )

    def _apply_launch_options(self, start_minimized: bool, start_daemon: bool) -> None:
        """Do what was asked for at launch: start the daemon if none runs, and begin minimized."""
        if start_daemon and not self._snapshot.running:
            error = gui_daemon.do_start()
            self.set_status(error or "Starting the daemon…")
            if error is None:
                self._starting_since = self._gui_started_at = time.time()
                self._update_daemon_view()
        if start_minimized:
            self.after_idle(self._start_hidden)

    def _start_hidden(self) -> None:
        """Do what the minimize button does.

        Hide in the tray (tray active and minimize_to_tray on), else minimize to the taskbar.
        """
        try:
            if self._tray_active() and self._minimize_to_tray:
                self.withdraw()
            else:
                self.iconify()
        except tk.TclError:
            pass

    def set_status(self, text: str) -> None:
        """Show a message in the status bar for a few seconds, then restore the default."""
        self.status_bar.configure(text=text)
        if self._status_reset_job is not None:
            self.after_cancel(self._status_reset_job)
        self._status_reset_job = self.after(self._status_message_ms, self._reset_status)

    def _reset_status(self) -> None:
        """Restore the footer's default text once a status message has been shown long enough."""
        self._status_reset_job = None
        self.status_bar.configure(text=DEFAULT_STATUS_TEXT)

    # ----- status tabs -----

    EVENT_COLUMNS: tuple[StatusTab.Column, ...] = (
        ("time", "Time", 150, "w", False),
        ("repo", "Repository (owner/repo)", 220, "w", False),
        ("kind", "Type", 175, "w", False),
        ("message", "Message", 420, "w", True),
    )
    # The Completed tab's third column holds the release tag, not a type: heading and key differ per tab.
    COMPLETED_COLUMNS: tuple[StatusTab.Column, ...] = (
        ("time", "Time", 150, "w", False),
        ("repo", "Repository (owner/repo)", 220, "w", False),
        ("folder", "Name (folder)", 200, "w", False),  # double-click opens the folder
        ("kind", "Tag", 175, "w", False),
        ("message", "Message", 420, "w", True),
    )
    FILTERABLE_TABS = ("warnings", "completed")
    UNMAPPED_COLUMNS: tuple[StatusTab.Column, ...] = (
        ("repo", "Repository (owner/repo)", 280, "w", True),
        ("folder", "Name (folder)", 280, "w", True),
        ("first_seen", "Last notification", 160, "w", False),
    )
    EMPTY_TEXTS = {
        "warnings": "No warnings.",
        "completed": "Nothing has been completed yet.",
        "limits": "No folder is over its limit.",
        "unmapped": "Every repository has a destination.",
    }

    def _build_status_tab(self, definition: status_tabs.TabDef) -> StatusTab:
        """Create the StatusTab for one tab definition (events or the unmapped list)."""
        if definition.kind == status_tabs.KIND_UNMAPPED:
            return StatusTab(
                self.notebook, self.UNMAPPED_COLUMNS, self.EMPTY_TEXTS[definition.key], self._show_repo,
                hint="These repositories were added without a destination (they use default routing). "
                     "Double-click one to set it up in the Mappings tab.",
            )
        return StatusTab(
            self.notebook,
            self.COMPLETED_COLUMNS if definition.key == "completed" else self.EVENT_COLUMNS,
            self.EMPTY_TEXTS.get(definition.key, "Nothing to show."), self._show_repo,
            hint="Double-click a row to open its repository in the Mappings tab.",
            on_open_folder=self._open_repo_folder,  # every event tab; Unmapped has no folder to open
            on_mark_read=(lambda key=definition.key: self._mark_status_read(key)) if definition.counter else None,
            on_clear=lambda key=definition.key: self._clear_status_tab(key),
            on_clear_repo=(lambda repo: self._clear_repo_limit_warnings(repo)) if definition.key == "limits" else None,
            detail=True,
            filterable=definition.key in self.FILTERABLE_TABS,
        )

    def _show_repo(self, repo: str) -> None:
        """Select a repository in the Mappings tab and switch to it."""
        if self.mappings_tab.select_repo(repo):
            self.notebook.select(self.mappings_tab)
        else:
            self.set_status(f"{repo} is not in mapping.json.")

    def _selected_status_key(self) -> Optional[str]:
        """Return the key of the status tab that is shown, or None for the other tabs."""
        selected = self.notebook.select()
        for key, tab in self._status_tabs.items():
            if selected == str(tab):
                return key
        return None

    def _set_status_title(self, key: str, title: str) -> None:
        """Update a status tab's title (with its unread count) only when it changed."""
        if self._status_titles.get(key) != title:
            self._status_titles[key] = title
            self.notebook.tab(self._status_tabs[key], text=title)

    def _open_work_folder(self, key: str) -> None:
        """Open one of the working folders (Complete, Logs, Partial, Processing) in the file manager."""
        folder = next((item for item in work_folders.work_folders() if item.key == key), None)
        target = work_folders.open_target(folder.path) if folder else None
        if folder is None or target is None:
            self.set_status("That folder does not exist yet: it is created at the first download.")
            return
        try:
            open_in_file_manager(target)
        except OSError as exc:
            self.set_status(f"Cannot open {target}: {describe_error(exc)}")
            return
        self.set_status(f"Opened {target}")

    def _update_folder_counts(self) -> None:
        """Refresh the Folders buttons' counts (one directory listing per folder; a failure leaves the old text)."""
        try:
            self.control_bar.set_folder_counts(self._folder_counts.get())  # re-lists only a changed folder
        except Exception:  # a hiccup in a slow or missing drive must not disturb the refresh loop
            pass

    def _open_repo_folder(self, repo: str) -> None:
        """Open a repository's folder like the Mappings tab's "Open folder" button does."""
        entry = mapping_manager.get_repository_mapping(repo)
        if not isinstance(entry, dict):
            self.set_status(f"{repo} is not in mapping.json.")
            return
        folder = gui_forms.open_folder_for_entry(repo, entry)
        if folder is None:
            self.set_status(f"No existing folder to open for {repo}: set a destination that exists first.")
            return
        try:
            open_in_file_manager(folder)
        except OSError as exc:
            self.set_status(f"Cannot open {folder}: {describe_error(exc)}")
            return
        self.set_status(f"Opened {folder}")

    def _show_event_rows(self, key: str, highlight_after: Optional[int]) -> None:
        """Fill an event tab with its rows (the Completed tab also gets folder names and tags)."""
        rows = self.status_feed.model.rows(key)
        if key == "completed":
            entries = {
                str(entry.get("repository", "")).lower(): entry
                for entry in mapping_manager.load_mapping().get("repositories", [])
            }
            table = [
                (r.time, r.repo, gui_forms.folder_display_name(r.repo, entries.get(r.repo.lower())), r.kind, r.message)
                for r in rows
            ]
        else:
            table = [(r.time, r.repo, r.kind, r.message) for r in rows]
        self._status_tabs[key].set_rows(
            table,
            [r.repo for r in rows],
            [r.id for r in rows],
            highlight_after,
        )

    def _refresh_status_tabs(self) -> None:
        """Pull new events/unmapped repos, update the tab titles, and show them in the visible tab."""
        model = self.status_feed.model
        try:
            changed = self.status_feed.refresh()
            unmapped = self.status_feed.unmapped()
        except Exception as exc:  # a database hiccup must not break the GUI
            self.set_status(f"Status tabs unavailable: {exc}")
            return
        visible = self._current_status_key
        for key in model.event_tab_keys:
            if key == visible and not self._window_hidden():
                if key in changed:  # new rows while the tab is open: shown now, and read at once
                    self._show_event_rows(key, self._status_tabs[key]._highlight_after)
                    model.mark_read(key)
            self._set_status_title(key, model.title(key))
        if unmapped != self._unmapped_rows:
            self._unmapped_rows = unmapped
            self._status_tabs["unmapped"].set_rows(
                [(r.repo, r.folder, r.first_seen) for r in unmapped], [r.repo for r in unmapped]
            )
        self._set_status_title("unmapped", format_status_title("Unmapped", len(unmapped)))
        self._announce_news(unmapped)
        self._update_tray_icon()

    def _enter_status_tab(self, key: str) -> None:
        """Opening an event tab shows its rows, bold where new since the last visit, and marks them read."""
        model = self.status_feed.model
        if key in model.event_tab_keys:
            self._show_event_rows(key, model.seen_id(key))
            model.mark_read(key)
            self._set_status_title(key, model.title(key))

    def _leave_status_tab(self, key: str) -> None:
        """Stop highlighting new rows when the user leaves a tab."""
        if key in self.status_feed.model.event_tab_keys:
            self._status_tabs[key].set_highlight_after(None)

    def _mark_status_read(self, key: str) -> None:
        """Mark every row of a tab as read and refresh its title."""
        model = self.status_feed.model
        model.mark_read(key)
        self._status_tabs[key].set_highlight_after(None)
        self._set_status_title(key, model.title(key))

    def _clear_status_tab(self, key: str) -> None:
        """Delete the events a tab lists (after a confirmation that names the number)."""
        title = next(d.title for d in self.status_feed.defs if d.key == key)
        try:
            count = self.status_feed.count_tab(key)
        except Exception as exc:
            self.set_status(f"Cannot read the database: {describe_error(exc)}")
            return
        if count == 0:
            self.set_status(f"{title}: nothing to clear.")
            return
        if not messagebox.askyesno(
            f"Clear {title}",
            f"Permanently delete all {count:,} {title.lower()} event(s) from the database?\n\n"
            f"That is every such event, including older ones this tab does not show "
            f"(it lists at most the newest {status_tabs.DEFAULT_ROW_LIMIT}, and Folder limits only the latest per repository).\n\n"
            "Only this event history is removed; downloads, files and the job queue are not touched.",
            icon="warning", default="no", parent=self,
        ):
            return
        try:
            removed = self.status_feed.clear_tab(key)
        except Exception as exc:  # e.g. the database is locked by a busy daemon: nothing was deleted
            self.set_status(f"Could not clear {title}: {describe_error(exc)}")
            return
        self._show_event_rows(key, None)
        self._set_status_title(key, self.status_feed.model.title(key))
        self.set_status(f"Cleared {removed:,} {title.lower()} event(s).")

    def _clear_repo_limit_warnings(self, repo: str) -> None:
        """Delete one repository's folder-limit warnings (after a confirmation that names the number)."""
        try:
            count = self.status_feed.count_repo_limit_warnings(repo)
        except Exception as exc:
            self.set_status(f"Cannot read the database: {describe_error(exc)}")
            return
        if count == 0:
            self.set_status(f"No folder-limit warnings for {repo}.")
            return
        if not messagebox.askyesno(
            "Clear folder-limit warnings",
            f"Delete {count:,} folder-limit warning(s) for {repo}?\n\n"
            "They also clear themselves once the folder is back under its limit.",
            icon="warning", default="no", parent=self,
        ):
            return
        try:
            removed = self.status_feed.clear_repo_limit_warnings(repo)
        except Exception as exc:
            self.set_status(f"Could not clear the warnings: {describe_error(exc)}")
            return
        self._show_event_rows("limits", None)
        self.set_status(f"Cleared {removed:,} folder-limit warning(s) for {repo}.")

    def _terminal_log_active(self) -> bool:
        """True when somebody can see the Terminal log tab (window not minimized, its tab selected)."""
        return not self._window_hidden() and self.notebook.select() == str(self.log_tab)

    def _on_enable_log(self) -> None:
        """Switch the terminal log on (the Terminal log tab's button)."""
        self._report(gui_daemon.do_set_log(True), "Terminal log switched on (a new .log file).")

    def _mappings_visible(self) -> bool:
        """True when somebody can see the Mappings table (window not minimized, its tab selected)."""
        return not self._window_hidden() and self.notebook.select() == str(self.mappings_tab)

    def _refresh_mappings_if_visible(self) -> None:
        """Reload the Mappings table, but only while that tab is shown."""
        if self._mappings_visible():
            self.mappings_tab.refresh()

    def _on_tab_changed(self, _event: object = None) -> None:
        """Handle a tab switch: leave the previous status tab and enter the new one (marks it read)."""
        previous, self._current_status_key = self._current_status_key, self._selected_status_key()
        if previous is not None and previous != self._current_status_key:
            self._leave_status_tab(previous)
        if self._current_status_key is not None and self._current_status_key != previous:
            self._enter_status_tab(self._current_status_key)
        self._refresh_mappings_if_visible()
        if self._terminal_log_active():
            self.log_tab.poll_now()

    def _on_map(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        """The window was restored: catch up immediately instead of waiting for the next tick."""
        if event.widget is self:
            self._failed_unseen = 0  # the user is looking now
            self._refresh_mappings_if_visible()
            if self._terminal_log_active():
                self.log_tab.poll_now()
            if self.state() != "iconic":
                self._refresh_status_tabs()
            self._update_daemon_view()

    def _tick(self) -> None:
        """The periodic refresh: Mappings, status tabs and the daemon view; reschedules itself."""
        try:
            self._refresh_mappings_if_visible()
            if self.state() != "iconic":
                self._update_folder_counts()
            if self.state() != "iconic" or self._tray_active():  # a tray icon still has news to deliver
                self._refresh_status_tabs()
        finally:
            self._tick_job = self.after(self._refresh_ms, self._tick)

    # ----- daemon status and control -----

    def _daemon_action_pending(self) -> bool:
        """True while a restart, start or stop is still in progress."""
        return bool(self._restart_pending or self._starting_since or self._stopping_since)

    def _update_daemon_view(self, force_control: bool = False) -> None:
        """Read what the daemon publishes, update the control bar, and schedule the next look.

        A minimized window is not looked at, so it is left alone (unless a Start/Stop/Restart is
        in flight, which must keep running); the first look after restoring happens at once.
        """
        try:
            if self.state() == "iconic" and not self._tray_active() and not self._daemon_action_pending() and not force_control:
                return
            self._snapshot = self._reader.read(force_control=force_control)
            now = time.time()
            self._note_unexpected_exit(now)
            if self._snapshot.running:
                self._starting_since = None
                if self._restart_pending and not self._is_stopping(now):
                    # The stop never completed (e.g. an older daemon ignores it): give up on the restart.
                    self._restart_pending = False
                    self.set_status("The daemon did not stop, so it was not restarted.")
            else:
                self._stopping_since = None
                if self._restart_pending:  # the old daemon has exited: bring up a new one
                    self._restart_pending = False
                    error = gui_daemon.do_start()
                    self.set_status(error or "Daemon restarting with the current settings\u2026")
                    if error is None:
                        self._starting_since = self._gui_started_at = now
            self.control_bar.apply_view(
                gui_daemon.build_view(self._snapshot, now, self._starting_since, self._stopping_since)
            )
            self.log_tab.set_daemon_state(self._snapshot.running, self._snapshot.log_on)
            self._update_queue_label(now)
            self._update_tray_icon()
        except Exception as exc:  # a status hiccup must never kill the GUI loop
            self.control_bar.status_label.configure(text=f"Daemon status unavailable: {exc}")
        finally:
            if self._daemon_job is not None:
                self.after_cancel(self._daemon_job)
            self._daemon_job = self.after(DAEMON_REFRESH_INTERVAL_MS, self._update_daemon_view)

    def _update_queue_label(self, now: float) -> None:
        """The footer's middle: the job in progress ("Processing 3 of 36: ..."), else the queue figures.

        The figures cost one query per change of state.db; the due count follows the clock.
        """
        progress = self._snapshot.progress if self._snapshot.running else None
        if progress is not None:
            text = gui_daemon.progress_summary(progress)
            self._queue_tip_text = gui_daemon.progress_detail(progress, now)
        else:
            try:
                counts = gui_data.load_queue_counts(now)
            except Exception:  # a locked database just leaves the last text
                return
            text = gui_data.queue_text(counts)
            self._queue_tip_text = gui_data.queue_tip(counts, gui_tooltips.CONTROL_HELP["queue_counts"])
        if text != self.queue_label.cget("text"):
            self.queue_label.configure(text=text)

    def _note_unexpected_exit(self, now: float) -> None:
        """Tell the user when a daemon this GUI just started disappears without Stop/Restart being used."""
        running = self._snapshot.running
        died = self._was_running and not running
        self._was_running = running
        if died and self._stopping_since is None and not self._restart_pending:
            self._send_notice(gui_tray.daemon_stopped_notice())
        if (
            died
            and self._gui_started_at is not None
            and now - self._gui_started_at < UNEXPECTED_EXIT_WINDOW_SECONDS
            and self._stopping_since is None
            and not self._restart_pending
        ):
            self.set_status(
                "The daemon stopped by itself shortly after starting. See "
                f"{daemon_launcher.STDERR_FILE_NAME} (or turn on the terminal log and start it again)."
            )

    def _is_stopping(self, now: float) -> bool:
        """True during the short time after Stop was clicked while the daemon is still shutting down."""
        return self._stopping_since is not None and now - self._stopping_since < gui_daemon.STOPPING_TIMEOUT_SECONDS

    def _report(self, error: Optional[str], success_text: str) -> bool:
        """Show the outcome of a control action in the status bar; True when it worked."""
        self.set_status(error or success_text)
        self._update_daemon_view(force_control=True)  # we just changed something: read it back now
        return error is None

    def _on_start(self) -> None:
        """Start button: launch the daemon detached."""
        if self._report(gui_daemon.do_start(), "Starting the daemon\u2026"):
            self._starting_since = self._gui_started_at = time.time()
            self._update_daemon_view()

    def _on_stop(self) -> None:
        """Stop button: after a confirmation, request a graceful stop."""
        if not messagebox.askyesno(
            "Stop daemon",
            "Stop the polling daemon?\n\nThe job in progress finishes first, then the daemon exits. "
            "Use Start to run it again.",
            parent=self,
        ):
            return
        if self._report(gui_daemon.do_stop(), "Stop requested; the daemon exits after the current job."):
            self._stopping_since = time.time()
            self._restart_pending = False
            self._update_daemon_view()

    def _on_restart(self) -> None:
        """Restart button: after a confirmation, stop the daemon and start it again."""
        if not messagebox.askyesno(
            "Restart daemon",
            "Restart the polling daemon to apply the changed settings?\n\n"
            "The job in progress finishes first, then the daemon stops and starts again.",
            parent=self,
        ):
            return
        if self._report(gui_daemon.do_stop(), "Restart requested; the daemon restarts after the current job."):
            self._stopping_since = time.time()
            self._restart_pending = True
            self._update_daemon_view()

    def _on_pause(self) -> None:
        """Pause/Resume button: toggle the global pause."""
        pausing = not self._snapshot.paused
        self._report(
            gui_daemon.do_set_paused(pausing),
            "Pause requested; the countdown freezes." if pausing else "Resumed.",
        )

    def _on_poll_now(self) -> None:
        """Poll now button: ask the daemon for an immediate poll."""
        self._report(gui_daemon.do_poll_now(), "Poll requested; it runs within a second.")

    def _on_single_poll(self) -> None:
        """Poll one button: ask for one notification and one queue item."""
        self._report(
            gui_daemon.do_single_poll(),
            "Single poll requested: one notification and one queue item, within a second.",
        )

    def _on_check_folders(self) -> None:
        """Check folders button: ask the daemon to check destinations and folder limits."""
        self._report(
            gui_daemon.do_check_folders(),
            "Folder check requested; the counts in the Limit column update within a few seconds.",
        )

    def _on_log_toggle(self) -> None:
        """Terminal log checkbox: switch the daemon's log file on or off (undone if the request fails)."""
        wanted = bool(self.control_bar.detailed_log.get())  # the click has already flipped the box
        if not self._report(
            gui_daemon.do_set_log(wanted),
            "Terminal log switched on (a new .log file)." if wanted else "Terminal log switched off.",
        ):
            self.control_bar.detailed_log.set(self._snapshot.log_on)  # show the real state again
            return
        if wanted:
            if self._verify_log_job is not None:
                self.after_cancel(self._verify_log_job)
            self._verify_log_job = self.after(int(gui_daemon.INTENT_SECONDS * 1000) + 700, self._verify_log_switched_on)

    def _verify_log_switched_on(self) -> None:
        """The daemon reports whether its log file is really open; tell the user if the switch did not take."""
        self._verify_log_job = None
        try:
            snapshot = self._reader.read()
        except Exception:
            return
        if snapshot.running and not snapshot.log_on:
            self.set_status(
                "The daemon could not start the log file. Check that the log folder exists and is writable "
                "(Settings, default download folder)."
            )

    def _open_settings(self) -> None:
        """Open the Settings dialog."""
        SettingsDialog(self, self._after_settings_saved, self._reload_silenced_warnings, self._after_credentials_saved)

    def _after_credentials_saved(self, message: str) -> None:
        """The Gmail & GitHub window saved: say so, and re-check the first-run hints (the Doctor button)."""
        self.set_status(message)
        self._refresh_doctor_attention()

    def _reload_silenced_warnings(self) -> None:
        """The warning checklist saved its choice: use it from the next warning on (no restart needed)."""
        try:
            self._silenced_warning_types = set(config_manager.get_gui_silenced_warning_types())
        except Exception:  # an unreadable config.json keeps the choice made at start
            return
        self._update_tray_icon()  # the red dot follows the same choice
        self.set_status("Warning notifications saved.")

    def _open_doctor(self) -> None:
        """Open the Doctor dialog."""
        DoctorDialog(self, self._refresh_doctor_attention)

    def _open_stats(self) -> None:
        """Open the Stats window."""
        StatsDialog(self)

    def _refresh_doctor_attention(self, report: Optional[dict] = None) -> None:
        """Highlight the Doctor button on a first run (missing files / login) or after a report with problems."""
        try:
            reasons = gui_doctor.first_run_reasons()
        except Exception:
            return
        self.control_bar.set_doctor_attention(reasons, gui_doctor.needs_attention(reasons, report))

    def _after_settings_saved(self, message: str) -> None:
        """Refresh what depends on the settings after the Settings dialog saved."""
        self._refresh_doctor_attention()  # saving the Settings creates config.json
        self.set_status(message)
        self.mappings_tab.refresh()  # the default recheck shown in the editor may have changed
        self._update_daemon_view()  # shows the Restart button right away when a running daemon is affected

    def _restore_window_state(self) -> None:
        """Apply the size/position saved in config.json (fitted to the screen), else the default."""
        saved = gui_state.load_window_state()
        if saved is None:
            self.geometry(self.DEFAULT_SIZE)
            return
        fitted = gui_state.fit_to_screen(
            saved,
            self.winfo_vrootx(),
            self.winfo_vrooty(),
            self.winfo_vrootwidth(),
            self.winfo_vrootheight(),
            *self.MIN_SIZE,
        )
        self.geometry(gui_state.to_geometry(fitted))
        if fitted.maximized:
            self.after_idle(lambda: self.state("zoomed"))

    def _remember_normal_state(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        """Track the window rectangle while it is in its normal (not maximized/minimized) state."""
        if event.widget is self and self.state() == "normal":
            self._normal_state = gui_state.parse_geometry(self.geometry())

    # ----- system tray -----

    def _tray_active(self) -> bool:
        """True when the tray icon is running."""
        return self._tray is not None and self._tray.active

    def _window_hidden(self) -> bool:
        """True while nobody can see the window: minimized or hidden in the tray."""
        return self.state() in ("iconic", "withdrawn")

    def _start_tray(self, config: dict) -> None:
        """Create the tray icon when it is enabled and the optional packages are installed."""
        if not (config_manager.get_gui_tray_enabled(config) and gui_tray.tray_available()):
            return
        png_path = os.path.join(ICON_DIR, "ghaadd.png")
        if sys.platform == "win32":  # notifications would otherwise be listed under "Python"
            gui_tray.register_windows_identity(WINDOWS_APP_ID, APP_NAME, os.path.join(ICON_DIR, "ghaadd.ico"))
        tray = gui_tray.TrayIcon(
            APP_NAME,
            lambda: "Resume polling" if self._snapshot.paused else "Pause polling",
            png_path,
            toast_app_id=APP_USER_MODEL_ID if sys.platform == "win32" else None,
        )
        if not tray.start():
            return
        self._tray = tray
        if self._minimize_to_tray:
            self.bind("<Unmap>", self._on_unmap, add="+")
        self._tray_job = self.after(250, self._poll_tray)
        self._update_tray_icon()

    def _on_unmap(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        """React to the window being minimized (hide it when the tray is active)."""
        if event.widget is self:
            self.after_idle(self._hide_if_minimized)

    def _hide_if_minimized(self) -> None:
        """Hide a minimized window from the taskbar; the tray icon brings it back."""
        try:
            if self._tray_active() and self.state() == "iconic":
                self.withdraw()  # gone from the taskbar; the tray icon brings it back
        except tk.TclError:
            pass

    def _show_from_tray(self) -> None:
        """Bring the window back to the front (tray menu, second start of the GUI)."""
        self.deiconify()
        self.lift()
        self.focus_force()

    def _poll_tray(self) -> None:
        """Carry out what the tray menu asked for (the tray thread only queues it)."""
        try:
            if self._tray is not None:
                for action in self._tray.pending_actions():
                    if action == gui_tray.ACTION_SHOW:
                        self._show_from_tray()
                    elif action == gui_tray.ACTION_TOGGLE:
                        if self._window_hidden():
                            self._show_from_tray()
                        else:
                            self.withdraw()  # gone from the taskbar too; the tray icon brings it back
                    elif action == gui_tray.ACTION_START:
                        if not self._snapshot.running:
                            self._on_start()
                    elif action == gui_tray.ACTION_POLL:
                        self._on_poll_now()
                    elif action == gui_tray.ACTION_SINGLE:
                        self._on_single_poll()
                    elif action == gui_tray.ACTION_PAUSE:
                        self._on_pause()
                    elif action == gui_tray.ACTION_QUIT:
                        self._on_close()
                        return
        finally:
            if self._tray is not None:
                self._sync_tray_menu()
                self._tray_job = self.after(250, self._poll_tray)

    def _sync_tray_menu(self) -> None:
        """Keep the tray menu in step with the window (open/hidden) and the daemon (running/paused)."""
        if self._tray_active():
            try:
                visible = not self._window_hidden()
            except tk.TclError:
                return
            self._tray.set_menu_state(  # type: ignore[union-attr]
                gui_tray.MenuState(visible, bool(self._snapshot.running), bool(self._snapshot.paused))
            )

    def _update_tray_icon(self) -> None:
        """Update the tray icon's state, dot and hover text."""
        if not self._tray_active():
            return
        model = self.status_feed.model
        # The red dot and the hover text follow the same choice as the notifications: a silenced warning type does not
        # light the tray icon up (the Warnings tab and its title still count every unread warning).
        unread = warning_types.count_notifying(
            (row.kind for row in model.unread_rows("warnings")), self._silenced_warning_types
        )
        unmapped = len(self._unmapped_rows or [])
        state = gui_tray.icon_state(self._snapshot.running, self._snapshot.paused)
        attention = gui_tray.needs_attention(unread, unmapped, self._failed_unseen)
        self._tray.update(state, attention, gui_tray.tooltip_text(APP_NAME, state, unread, unmapped))  # type: ignore[union-attr]
        self._sync_tray_menu()

    def _send_notice(self, notice: Optional[gui_tray.Notice]) -> None:
        """A tray notification, only while nobody is looking at the window."""
        if notice is not None and self._notifications and self._tray_active() and self._window_hidden():
            self._tray.notify(notice)  # type: ignore[union-attr]

    def _announce_news(self, unmapped: list[status_tabs.UnmappedRow]) -> None:
        """Work out what is new since the last look (always, so the baselines move) and tell the tray."""
        model = self.status_feed.model
        warnings = model.rows("warnings")
        if self._notified_warning_id is None:
            fresh = []  # what was already there at start is not news
        else:
            fresh = [
                row for row in warnings
                if row.id > self._notified_warning_id and warning_types.wants_notice(row.kind, self._silenced_warning_types)
            ]
        if warnings:
            self._notified_warning_id = max(row.id for row in warnings)
        elif self._notified_warning_id is None:
            self._notified_warning_id = 0
        limit_repos = self._limit_tracker.new(row.repo for row in model.rows("limits") if row.repo)
        unmapped_repos = self._unmapped_tracker.new(row.repo for row in unmapped)
        failed = self.status_feed.take_failed_jobs()
        if self._window_hidden():
            self._failed_unseen += len(failed)
        for notice in (
            gui_tray.warnings_notice(fresh),
            gui_tray.limit_notice(limit_repos),
            gui_tray.unmapped_notice(unmapped_repos),
            gui_tray.failed_jobs_notice(failed),
        ):
            self._send_notice(notice)

    def _on_close_request(self) -> None:
        """The window's close button: hide to the tray when asked to, else close."""
        if self._close_to_tray and self._tray_active():
            self.withdraw()
            return
        self._on_close()

    def _on_close(self) -> None:
        """Save the window state, then close. A failed save must never keep the window open."""
        try:
            normal = self._normal_state or gui_state.parse_geometry(self.geometry())
            if normal is not None:
                maximized = self.state() == "zoomed"
                gui_state.save_window_state(
                    gui_state.WindowState(normal.width, normal.height, normal.x, normal.y, maximized)
                )
        except Exception:
            pass
        self.destroy()

    def _poll_instance(self) -> None:
        """Another start of the GUI asked this window to come forward (also when it is hidden or minimized)."""
        try:
            if gui_instance.take_show_request():
                self._show_from_tray()
        except tk.TclError:
            return
        finally:
            if self._instance_job is not None:
                self._instance_job = self.after(500, self._poll_instance)

    def destroy(self) -> None:
        """Cancel pending timers first, so nothing fires into a window that is already gone."""
        for job in (
            self._tick_job, self._status_reset_job, self._daemon_job, self._verify_log_job, self._tray_job,
            self._instance_job,
        ):
            if job is not None:
                self.after_cancel(job)
        self._tick_job = self._status_reset_job = self._daemon_job = self._verify_log_job = self._tray_job = None
        self._instance_job = None
        if self._tray is not None:
            tray, self._tray = self._tray, None
            tray.stop()
        self.mappings_tab.shutdown()
        self.log_tab.shutdown()
        for tab in self._status_tabs.values():
            tab.shutdown()
        super().destroy()

    def _set_icon(self) -> None:
        """Use assets/ghaadd.ico or assets/ghaadd.png when present; otherwise keep Tk's default icon."""
        if sys.platform == "win32":
            try:  # must happen before the window is shown
                import ctypes

                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(WINDOWS_APP_ID)
            except (AttributeError, OSError):
                pass
        ico_path = os.path.join(ICON_DIR, "ghaadd.ico")
        png_path = os.path.join(ICON_DIR, "ghaadd.png")
        try:
            if sys.platform == "win32" and os.path.isfile(ico_path):
                self.iconbitmap(default=ico_path)
            elif os.path.isfile(png_path):
                self._icon_image = tk.PhotoImage(file=png_path)
                self.iconphoto(True, self._icon_image)
        except tk.TclError:
            pass  # a broken icon file must never stop the GUI from starting

    def _apply_theme(self, preferred: Optional[str] = None) -> None:
        """Pick the ttk theme: dark mode needs clam (the native theme ignores colors); else the requested one."""
        style = ttk.Style(self)
        order = ("clam",) if self._dark else ((preferred,) if preferred else ()) + ("vista", "winnative", "clam")
        for theme in order:
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        if style.theme_use() == "clam":  # clam draws everything from the palette, light or dark
            self._configure_palette_styles(style)
        self._configure_styles(style)
        colors = gui_theme.palette(self._dark)
        self.configure(background=colors["window"])
        # Plain Tk widgets (and the drop-down of a combobox) created from now on, e.g. in dialogs, start in these colors.
        for pattern, value in (
            ("*Toplevel.background", colors["window"]), ("*Frame.background", colors["window"]),
            ("*Label.background", colors["window"]), ("*Label.foreground", colors["text"]),
            ("*Text.background", colors["field"]), ("*Text.foreground", colors["text"]),
            ("*Text.insertBackground", colors["text"]), ("*Canvas.background", colors["field"]),
            ("*Menu.background", colors["window"]), ("*Menu.foreground", colors["text"]),
            ("*TCombobox*Listbox.background", colors["field"]), ("*TCombobox*Listbox.foreground", colors["text"]),
            ("*TCombobox*Listbox.selectBackground", colors["selected"]),
        ):
            self.option_add(pattern, value)

    def _configure_palette_styles(self, style: ttk.Style) -> None:
        """Give the clam theme's widgets the current palette (so the same code serves dark and light)."""
        c = gui_theme.palette(self._dark)
        style.configure(
            ".", background=c["window"], foreground=c["text"], fieldbackground=c["field"], bordercolor=c["border"],
            darkcolor=c["window"], lightcolor=c["window"], troughcolor=c["field"], insertcolor=c["text"],
            selectbackground=c["selected"], selectforeground="#ffffff",
        )
        style.map(".", foreground=[("disabled", c["disabled"])])
        style.configure("TButton", background=c["button"], bordercolor=c["border"], lightcolor=c["button"], darkcolor=c["button"])
        style.map("TButton", background=[("active", c["button_active"]), ("disabled", c["window"])])
        style.configure("TEntry", fieldbackground=c["field"], foreground=c["text"])
        style.configure("TCombobox", fieldbackground=c["field"], foreground=c["text"], background=c["button"], arrowcolor=c["text"])
        style.map("TCombobox", fieldbackground=[("readonly", c["field"])], foreground=[("readonly", c["text"])])
        style.configure("TSpinbox", fieldbackground=c["field"], foreground=c["text"], arrowcolor=c["text"])
        # clam maps its own greys onto the disabled and hover states of these, which showed as light boxes in dark mode
        for kind in ("TCheckbutton", "TRadiobutton"):
            style.configure(kind, background=c["window"], indicatorbackground=c["field"], indicatorforeground=c["text"])
            style.map(
                kind,
                background=[("disabled", c["window"]), ("active", c["window"])],
                indicatorbackground=[("disabled", c["window"]), ("pressed", c["field"])],
                foreground=[("disabled", c["disabled"])],
            )
        style.configure("TNotebook", background=c["window"], bordercolor=c["border"])
        style.configure("TNotebook.Tab", background=c["button"], foreground=c["text"])
        style.map("TNotebook.Tab", background=[("selected", c["window"]), ("active", c["button_active"])])
        style.configure("TScrollbar", background=c["button"], troughcolor=c["field"], arrowcolor=c["text"], bordercolor=c["border"])
        style.configure("Treeview", background=c["field"], fieldbackground=c["field"], foreground=c["text"])

    def _toggle_dark_mode(self) -> None:
        """Switch between the light and dark colors and remember the choice in config.json."""
        self._dark = not self._dark
        MainWindow._dark_active = self._dark
        set_palette(self._dark)
        self._apply_theme(self._theme_name)
        self._recolor(self, gui_theme.color_swaps(self._dark))
        self.control_bar.theme_button.configure(text=gui_theme.button_text(self._dark))
        try:
            config_manager.set_config_values({"gui.dark_mode": self._dark})
        except Exception as exc:  # the colors still changed; only the memory of them failed
            self.set_status(f"Could not save the color mode: {describe_error(exc)}")

    def _recolor(self, widget: tk.Misc, swaps: dict[str, str]) -> None:
        """Recolor a widget and all its children: palette colors are swapped, plain Tk widgets get the base colors."""
        colors = gui_theme.palette(self._dark)

        def swapped(value: str) -> Optional[str]:
            """Return the new color for an old palette color, or None when it is not one of ours."""
            try:
                red, green, blue = (part >> 8 for part in widget.winfo_rgb(value))
            except tk.TclError:
                return None
            return swaps.get(f"#{red:02x}{green:02x}{blue:02x}")

        kind = widget.winfo_class()
        field_kinds = ("Text", "Canvas", "Entry", "Listbox", "Spinbox")
        plain_kinds = ("Tk", "Toplevel", "Frame", "Label", "Menu")
        for option in ("foreground", "background", "highlightbackground", "highlightcolor", "insertbackground"):
            if option == "background" and kind in plain_kinds + field_kinds:
                continue  # set below from the widget's role
            try:
                current = str(widget.cget(option))  # type: ignore[call-overload]
            except tk.TclError:
                continue
            new = swapped(current) if current else None
            if new:
                widget.configure(**{option: new})  # type: ignore[call-overload]
        if kind in plain_kinds + field_kinds:
            try:
                current = str(widget.cget("background"))  # type: ignore[call-overload]
                role = colors["field"] if kind in field_kinds else colors["window"]
                widget.configure(background=swapped(current) or role)  # type: ignore[call-overload]
            except tk.TclError:
                pass
            if kind in ("Text", "Entry", "Listbox", "Spinbox"):
                try:
                    if not swapped(str(widget.cget("foreground"))):  # type: ignore[call-overload]
                        widget.configure(foreground=colors["text"])  # type: ignore[call-overload]
                    widget.configure(insertbackground=colors["text"])  # type: ignore[call-overload]
                except tk.TclError:
                    pass
        if isinstance(widget, tk.Text):
            for tag in widget.tag_names():
                for option in ("foreground", "background"):
                    new = swapped(str(widget.tag_cget(tag, option)) or "")
                    if new:
                        widget.tag_configure(tag, **{option: new})
        elif isinstance(widget, ttk.Treeview):
            for tag in ("odd", "limit_warn"):
                for option in ("foreground", "background"):
                    new = swapped(str(widget.tag_configure(tag, option)) or "")  # type: ignore[call-overload]
                    if new:
                        widget.tag_configure(tag, **{option: new})  # type: ignore[call-overload]
        for child in widget.winfo_children():
            self._recolor(child, swaps)

    @staticmethod
    def _configure_styles(style: ttk.Style) -> None:
        """Table colors (set after the theme is chosen: style settings belong to one theme)."""
        # Bold headers set them apart on every theme; the background color only shows on themes that
        # draw headers themselves (clam). The native Windows theme (vista) ignores it.
        heading_font = tkfont.nametofont("TkDefaultFont").copy()
        heading_font.configure(weight="bold")
        style.configure(
            "Treeview.Heading", background=COLOR_HEADER_BG, foreground=COLOR_HEADER_FG, relief="flat", font=heading_font
        )
        MainWindow._heading_font = heading_font  # keep a reference so Tk does not drop the font
        colors = gui_theme.palette(MainWindow._dark_active)
        style.map("Treeview.Heading", background=[("active", colors["header_active"]), ("pressed", colors["header_pressed"])])
        # Explicit selection colors: tag backgrounds (striping) otherwise win over the theme's.
        style.map(
            "Treeview",
            background=[("selected", COLOR_SELECTED)],
            foreground=[("selected", "#ffffff")],
        )


def main() -> None:
    """Parse the GUI options and run the window.

    If another window already holds the single-instance lock, ask that one to show itself instead.
    """
    parser = argparse.ArgumentParser(description="GHAADD GUI")
    parser.add_argument("--theme", help="ttk theme to use (default: native Windows theme; try 'clam')")
    parser.add_argument("--minimized", action="store_true", help="Start minimized (like the minimize button: in the tray, or on the taskbar), whatever the Settings say.")
    parser.add_argument("--start-daemon", action="store_true", help="Start the daemon when the window opens if none is running, whatever the Settings say.")
    args = parser.parse_args()
    if not gui_instance.acquire():
        # Another window is already open (maybe hidden in the tray): bring it forward instead of opening a second one.
        # This also holds for a start that asked to be minimized: someone starting the GUI again most likely cannot
        # find the window, so showing it is more useful than staying quiet.
        gui_instance.request_show()
        return
    try:
        MainWindow(theme=args.theme, start_minimized=args.minimized, start_daemon=args.start_daemon).mainloop()
    finally:
        gui_instance.release()


if __name__ == "__main__":
    main()
