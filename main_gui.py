"""GHAADD GUI (Tkinter).

Phase 4, step 2: the Mappings tab and the Settings dialog work on real data
(mapping.json via mapping_manager, config.json via config_manager, queue state
via gui_data/db_manager). The control bar, Terminal log and status tabs are still
static placeholders (steps 3-5). The widgets here are a thin view: parsing,
validation, ordering and data loading live in the toolkit-independent modules
(gui_forms, gui_data, repo_overview).
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
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Iterable, Literal, Optional

from modules import (
    autostart,
    config_manager,
    daemon_launcher,
    gui_daemon,
    gui_data,
    gui_doctor,
    gui_forms,
    gui_state,
    gui_tooltips,
    gui_tray,
    log_tail,
    mapping_manager,
    shortcuts,
    status_tabs,
)
from modules.app_info import APP_NAME, APP_USER_MODEL_ID, __version__

Anchor = Literal["nw", "n", "ne", "w", "center", "e", "sw", "s", "se"]

APP_TITLE = f"{APP_NAME} {__version__}"
# The app icon is looked up here: ghaadd.ico (preferred on Windows) or ghaadd.png.
ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
WINDOWS_APP_ID = APP_USER_MODEL_ID  # own taskbar identity, so the taskbar shows our icon instead of Python's
DAEMON_REFRESH_INTERVAL_MS = 1000
UNEXPECTED_EXIT_WINDOW_SECONDS = 120.0  # a daemon this GUI started that dies within this long is reported
DEFAULT_STATUS_TEXT = "config.json · mapping.json · state.db"

COLOR_ERROR = "#b3261e"
COLOR_WARNING = "#9a6700"
COLOR_MUTED = "#666666"
COLOR_STRIPE = "#f2f5f9"  # every second table row
COLOR_HEADER_BG = "#dde5ef"  # table column headers
COLOR_HEADER_FG = "#1f2d3d"
COLOR_SELECTED = "#0078d4"

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
        self._widget = widget
        self._window: Optional[tk.Toplevel] = None
        self._job: Optional[str] = None

    def schedule(self, text: str, x_root: int, y_root: int) -> None:
        self.hide()
        self._job = self._widget.after(self.DELAY_MS, lambda: self._show(text, x_root, y_root))

    def hide(self) -> None:
        if self._job is not None:
            self._widget.after_cancel(self._job)
            self._job = None
        if self._window is not None:
            self._window.destroy()
            self._window = None

    def _show(self, text: str, x_root: int, y_root: int) -> None:
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
        self._variable.set("")
        self.entry.focus_set()

    def _sync(self) -> None:
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

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, padding=(10, 8))

        self.status_dot = tk.Label(self, text="\u25cf", fg=self.DOT_COLORS[gui_daemon.DOT_STOPPED], font=("Segoe UI", 14))
        self.status_dot.grid(row=0, column=0, padx=(0, 4))
        self.status_label = ttk.Label(self, text="Checking daemon\u2026")
        self.status_label.grid(row=0, column=1, sticky="w")
        self.countdown_label = ttk.Label(self, text="", foreground=COLOR_MUTED)
        self.countdown_label.grid(row=0, column=2, padx=(16, 0), sticky="w")

        self.columnconfigure(3, weight=1)  # spacer pushes buttons right

        buttons = ttk.Frame(self)
        buttons.grid(row=0, column=4, sticky="e")
        self.start_button = ttk.Button(buttons, text="Start")
        self.stop_button = ttk.Button(buttons, text="Stop")
        self.pause_button = ttk.Button(buttons, text="Pause")
        self.poll_button = ttk.Button(buttons, text="Poll now")
        self.check_button = ttk.Button(buttons, text="Check folders")
        self.detailed_log = tk.BooleanVar(value=False)
        self.log_check = ttk.Checkbutton(buttons, text="Terminal log", variable=self.detailed_log)
        self.restart_button = ttk.Button(buttons, text="\u21bb Restart")
        self.doctor_button = ttk.Button(buttons, text="Doctor")
        self.settings_button = ttk.Button(buttons, text="Settings\u2026")
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

        for column, widget in enumerate(
            (self.start_button, self.stop_button, self.pause_button, self.poll_button, self.check_button)
        ):
            widget.grid(row=0, column=column, padx=(0, 6))
        attach_tooltip(self.check_button, gui_tooltips.CONTROL_HELP["check_folders"])
        self.log_check.grid(row=0, column=5, padx=(6, 12))
        self.doctor_button.grid(row=0, column=6, padx=(0, 6))
        self.restart_button.grid(row=0, column=7, padx=(0, 6))
        self.settings_button.grid(row=0, column=8)
        self.apply_view(gui_daemon.build_view(gui_daemon.DaemonSnapshot(), 0.0))

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
        ("folder", "Name (folder)", 150, "w"),
        ("repo", "Repository (owner/repo)", 150, "w"),
        ("destination", "Destination", 150, "w"),
        ("tag", "Tag", 100, "w"),
        ("last_check", "Last check", 135, "w"),
        ("step", "Recheck", 65, "center"),
        ("next_check", "Next check", 135, "w"),
        ("files", "Files", 70, "center"),
        ("limit", "Limit", 75, "center"),
    )
    STRETCH_COLUMNS = ("folder", "repo", "destination", "tag")
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
        wanted = {"last_check": stamp, "next_check": stamp, "step": heading}
        listed = {key: width for key, _title, width, _anchor in self.TABLE_COLUMNS}
        return {key: max(listed[key], width) for key, width in wanted.items()}

    def _tree_font(self) -> tkfont.Font:
        spec = ttk.Style(self).lookup("Treeview", "font") or "TkDefaultFont"
        try:
            return tkfont.Font(root=self, font=spec)
        except tk.TclError:
            return tkfont.nametofont("TkDefaultFont")

    @staticmethod
    def _cell_text(row: Any, key: str) -> str:
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
        return tuple(int(self.tree.column(key, "width")) for key, *_ in self.TABLE_COLUMNS)

    def _schedule_refit(self) -> None:
        """Re-fit the ellipsis once resizing/dragging has paused (each event restarts the timer)."""
        if self._fit_job is not None:
            self.after_cancel(self._fit_job)
        self._fit_job = self.after(200, self._refit)

    def _refit(self) -> None:
        self._fit_job = None
        if self._current_widths() != self._column_widths:
            self._render_rows()

    def _on_motion(self, event: tk.Event) -> None:  # type: ignore[type-arg]
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
        self._tip_cell = None
        self._tooltip.hide()

    def _matches(self, row: Any) -> bool:
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
        return {key: self.vars[key].get() for key in self.EDITABLE_KEYS}

    def _is_dirty(self) -> bool:
        return self._current_repo is not None and self._form_values() != self._loaded

    def _on_form_edited(self) -> None:
        if not self._loading:
            self._update_buttons()

    def _update_buttons(self) -> None:
        has_repo = self._current_repo is not None
        dirty = self._is_dirty()
        self.save_button.state(["!disabled"] if dirty else ["disabled"])
        self.revert_button.state(["!disabled"] if dirty else ["disabled"])
        self.remove_button.state(["!disabled"] if has_repo else ["disabled"])
        for widget in self.form_widgets:
            widget.state(["!disabled"] if has_repo else ["disabled"])

    def _show_message(self, text: str, kind: str = "error") -> None:
        color = {"error": COLOR_ERROR, "warning": COLOR_WARNING, "info": COLOR_MUTED}[kind]
        self.message_label.configure(text=text, foreground=color)
        if text:
            self.message_label.grid()
        else:
            self.message_label.grid_remove()

    def _on_select(self, _event: object = None) -> None:
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
        self._load_form(self._current_repo)

    def _save(self) -> None:
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
        if self._is_dirty() and not messagebox.askyesno(
            "Unsaved changes", f"Discard the unsaved changes to {self._current_repo}?", parent=self
        ):
            return
        AddRepositoryDialog(self, self._after_added)

    def _after_added(self, repo: str) -> None:
        self.filter_var.set("")
        self.show_var.set(self.SHOW_FILTERS[0])
        self.refresh()
        self._load_form(repo if repo in self._table.entries else None)
        if repo in self._table.entries:
            self._render_rows()
            self.tree.see(repo)
        self._set_status(f"Added {repo}.")

    def _remove_repository(self) -> None:
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
        self.notice = ttk.Frame(self)
        self.notice.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        self.notice.columnconfigure(0, weight=1)
        self.notice_label = ttk.Label(self.notice, text="", foreground=COLOR_WARNING, wraplength=900)
        self.notice_label.grid(row=0, column=0, sticky="w")
        self.enable_button = ttk.Button(self.notice, text="Turn on terminal log", command=self._on_enable_log)
        self.enable_button.grid(row=0, column=1, padx=(10, 0))

    def _build_toolbar(self) -> None:
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
        self.text = tk.Text(self, wrap="none", height=10, font=("Consolas", 10), state="disabled", undo=False)
        self.text.tag_configure(log_tail.LEVEL_ERROR, foreground="#b3261e")
        self.text.tag_configure(log_tail.LEVEL_WARNING, foreground="#9a6700")
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
        needed = float(first) > 0.0 or float(last) < 1.0
        if needed and not self._xscroll.winfo_ismapped():
            self._xscroll.grid()
        elif not needed and self._xscroll.winfo_ismapped():
            self._xscroll.grid_remove()
        self._xscroll.set(first, last)

    def _note_user_input(self, _event: object = None) -> None:
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
        if self.follow_var.get():
            self.text.see("end")

    def _on_follow_toggle(self) -> None:
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
        return self.filter_var.get().strip().casefold()

    def _matching(self, lines: Iterable[str]) -> list[str]:
        needle = self._filter_text()
        return [line for line in lines if needle in line.casefold()] if needle else list(lines)

    def _insert(self, lines: list[str]) -> None:
        for line in lines:
            level = log_tail.line_level(line)
            self.text.insert("end", line + "\n", (level,) if level else ())

    def _render_all(self) -> None:
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
        if (running, log_on) != (self._daemon_running, self._daemon_log_on):
            self._daemon_running, self._daemon_log_on = running, log_on
            self._update_notice()

    def _update_notice(self) -> None:
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
        try:
            content = self.text.get("sel.first", "sel.last")
        except tk.TclError:  # no selection: copy everything shown
            content = self.text.get("1.0", "end-1c")
        self.clipboard_clear()
        self.clipboard_append(content)
        self._set_status("Copied the selection." if self.text.tag_ranges("sel") else "Copied the shown log lines.")

    def _open_folder(self) -> None:
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
        if self._poll_job is not None:
            self.after_cancel(self._poll_job)
            self._poll_job = None


class StatusTab(ttk.Frame):
    """A read-only list for one status tab: events from state.db or the unmapped repositories.

    Rows arrive through set_rows(); the tab never queries anything itself. Cells that do not fit
    end in an ellipsis and show their full text on hover, rows the user has not seen yet are bold
    while the tab is shown, and double-clicking a row that names a repository opens it in the
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
    ) -> None:
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
        if detail:
            self.copy_button = ttk.Button(bottom, text="Copy text", command=self._copy_selected)
            self.copy_button.grid(row=0, column=1, padx=(0, 6))
        self.mark_read_button: Optional[ttk.Button] = None  # only tabs with an unread counter have one
        if on_mark_read is not None:
            self.mark_read_button = ttk.Button(bottom, text="Mark all read", command=on_mark_read)
            self.mark_read_button.grid(row=0, column=2)
        self.clear_button: Optional[ttk.Button] = None  # deletes the listed events from state.db
        if on_clear is not None:
            self.clear_button = ttk.Button(bottom, text="Clear…", command=on_clear)
            self.clear_button.grid(row=0, column=3, padx=(6, 0))
            attach_tooltip(self.clear_button, gui_tooltips.CONTROL_HELP["clear_tab"])
        self.clear_repo_button: Optional[ttk.Button] = None  # deletes one repository's events
        if on_clear_repo is not None:
            self._clear_repo = on_clear_repo
            self.clear_repo_button = ttk.Button(bottom, text="Clear selected", command=self._on_clear_repo, state="disabled")
            self.clear_repo_button.grid(row=0, column=2, padx=(0, 6))
            attach_tooltip(self.clear_repo_button, gui_tooltips.CONTROL_HELP["clear_repo"])

        font_spec = ttk.Style(self).lookup("Treeview", "font") or "TkDefaultFont"
        try:
            self._cell_font: tkfont.Font = tkfont.Font(root=self, font=font_spec)
        except tk.TclError:
            self._cell_font = tkfont.nametofont("TkDefaultFont")
        self._tooltip = Tooltip(self.tree)
        self.tree.bind("<<TreeviewSelect>>", self._show_detail)
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._update_clear_repo_button(), add="+")
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
        frame = ttk.Frame(self)
        frame.grid(row=self._base + 1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        frame.columnconfigure(0, weight=1)
        self.detail = tk.Text(frame, height=5, wrap="word", state="disabled", relief="solid", borderwidth=1,
                              background="#fbfbfb", font=("Segoe UI", 10))
        self.detail.tag_configure("head", font=("Segoe UI", 10, "bold"))
        self.detail.tag_configure("hint", foreground=COLOR_MUTED)
        self.detail.grid(row=0, column=0, sticky="ew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.detail.yview)
        self.detail.configure(yscrollcommand=scroll.set)
        scroll.grid(row=0, column=1, sticky="ns")
        self._set_detail("", "")

    def _set_detail(self, head: str, body: str) -> None:
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        if head or body:
            self.detail.insert("end", head + "\n" if head else "", "head")
            self.detail.insert("end", body)
        else:
            self.detail.insert("end", self.DETAIL_PLACEHOLDER, "hint")
        self.detail.configure(state="disabled")

    def _selected_index(self) -> Optional[int]:
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
        if self._has_detail:
            self._set_detail(*self._selected_text())

    def _copy_selected(self) -> None:
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
        if highlight_after != self._highlight_after:
            self._highlight_after = highlight_after
            self._render()

    def _tags(self, index: int) -> tuple[str, ...]:
        tags = ["odd"] if index % 2 else []
        if self._highlight_after is not None and self._ids[index] > self._highlight_after:
            tags.append("unread")
        return tuple(tags)

    def _render(self) -> None:
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
        cache_key = ("b" if bold else "n") + text
        width = self._measure_cache.get(cache_key)
        if width is None:
            if len(self._measure_cache) > 20000:
                self._measure_cache.clear()
            font = self._bold_font if bold else self._cell_font
            width = self._measure_cache[cache_key] = font.measure(text)
        return width

    def _is_unread(self, index: int) -> bool:
        return self._highlight_after is not None and self._ids[index] > self._highlight_after

    def _display(self, row: tuple[str, ...], bold: bool = False) -> list[str]:
        """Cell texts shortened to their column; bold rows are measured in the (wider) bold font."""
        shown = []
        for (key, _heading, _width, _anchor, _stretch), text in zip(self._columns, row):
            width = int(self.tree.column(key, "width")) - 14
            shown.append(fit_text(text, max(width, 20), lambda value: self._measure(value, bold)))
        return shown

    def _current_widths(self) -> tuple[int, ...]:
        return tuple(int(self.tree.column(column[0], "width")) for column in self._columns)

    def _schedule_refit(self) -> None:
        if self._fit_job is not None:
            self.after_cancel(self._fit_job)
        self._fit_job = self.after(200, self._refit)

    def _refit(self) -> None:
        self._fit_job = None
        if self._rows and self._current_widths() != self._column_widths:
            self._render()

    def _on_motion(self, event: tk.Event) -> None:  # type: ignore[type-arg]
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
        self._tip_cell = None
        self._tooltip.hide()

    def shutdown(self) -> None:
        if self._fit_job is not None:
            self.after_cancel(self._fit_job)
            self._fit_job = None
        self._hide_tip()

    # ----- actions -----

    def _update_clear_repo_button(self) -> None:
        if self.clear_repo_button is not None:
            self.clear_repo_button.state(["!disabled"] if self.selected_repo() else ["disabled"])

    def _on_clear_repo(self) -> None:
        repo = self.selected_repo()
        if repo:
            self._clear_repo(repo)

    def selected_repo(self) -> str:
        selection = self.tree.selection()
        return self._repos[int(selection[0])] if selection else ""

    def _on_double_click(self, _event: object = None) -> None:
        selection = self.tree.selection()
        if selection:
            repo = self._repos[int(selection[0])]
            if repo:
                self._open_repo(repo)


class SettingsDialog(tk.Toplevel):
    """Global settings from config.json."""

    def __init__(self, master: tk.Misc, on_saved: Callable[[str], None]) -> None:
        super().__init__(master)
        self.title("Settings")
        self.resizable(False, False)
        self.transient(master)  # type: ignore[arg-type]
        self._on_saved = on_saved

        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)

        current = gui_data.load_settings_form()
        self._initial_form = dict(current)  # to tell whether the form has unsaved changes
        self.vars: dict[str, tk.Variable] = {
            key: tk.BooleanVar(value=value) if isinstance(value, bool) else tk.StringVar(value=value)
            for key, value in current.items()
        }

        def row(frame: ttk.LabelFrame, index: int, label: str, widget: tk.Widget) -> None:
            ttk.Label(frame, text=label).grid(row=index, column=0, sticky="w", pady=4, padx=(0, 12))
            widget.grid(row=index, column=1, sticky="ew", pady=4)

        def spin(frame: ttk.LabelFrame, key: str, low: int, high: int) -> ttk.Spinbox:
            return ttk.Spinbox(frame, from_=low, to=high, width=8, textvariable=self.vars[key])

        processing = ttk.LabelFrame(body, text="Processing", padding=10)
        processing.grid(row=0, column=0, sticky="ew")
        processing.columnconfigure(1, weight=1)
        first_entry = ttk.Entry(processing, textvariable=self.vars["recheck"], width=28)
        row(processing, 0, "Default recheck (minutes)", first_entry)
        row(processing, 1, "Max emails per poll (0 = all)", spin(processing, "max_emails", 0, 9999))
        row(processing, 2, "Destination check every N polls (0 = off)", spin(processing, "dest_check", 0, 999))
        row(processing, 3, "Default limit (new mappings, 0 = none)", spin(processing, "default_limit", 0, 9999))

        polling = ttk.LabelFrame(body, text="Polling", padding=10)
        polling.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        polling.columnconfigure(1, weight=1)
        row(polling, 0, "Interval (seconds)", spin(polling, "interval", gui_forms.MIN_POLL_INTERVAL_SECONDS, 86400))
        row(polling, 1, "Jitter min (seconds)", spin(polling, "jitter_min", 0, 3600))
        row(polling, 2, "Jitter max (seconds)", spin(polling, "jitter_max", 0, 3600))

        paths = ttk.LabelFrame(body, text="Paths and logging", padding=10)
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
        row(paths, 2, "Terminal log on at startup", ttk.Checkbutton(paths, variable=self.vars["log_enabled"]))
        row(paths, 3, "Max log file (MB, 0 = no rollover)", spin(paths, "log_max_mb", 0, 1000))
        row(paths, 4, "Keep log files (0 = all)", spin(paths, "log_keep", 0, 9999))

        # Autostart lives in the operating system (not in config.json) and is applied on Save. Asking the
        # system (schtasks, systemctl, ...) can take a moment, so it happens in a thread and the box stays
        # disabled until the answer is in; the dialog itself never waits for it.
        self._autostart_before: Optional[bool] = None  # None = not known (yet): Save leaves autostart alone
        self.autostart_var = tk.BooleanVar(value=False)
        self._autostart_answer: list[autostart.AutostartStatus] = []
        self._autostart_check: Optional[ttk.Checkbutton] = None
        self._shortcuts_answer: list[Any] = []
        self._shortcuts_button: Optional[ttk.Button] = None
        if autostart.is_supported() or shortcuts.is_supported():
            startup = ttk.LabelFrame(body, text="Startup", padding=10)
            startup.grid(row=3, column=0, sticky="ew", pady=(10, 0))
            startup.columnconfigure(0, weight=1)
            if autostart.is_supported():
                self._autostart_check = ttk.Checkbutton(
                    startup, text="Start the daemon when I log in", variable=self.autostart_var, state="disabled"
                )
                self._autostart_check.grid(row=0, column=0, sticky="w")
                attach_tooltip(self._autostart_check, gui_tooltips.CONTROL_HELP["autostart"])
                threading.Thread(target=self._probe_autostart, daemon=True).start()
                self.after(100, self._apply_autostart_answer)
            if shortcuts.is_supported():
                self._shortcuts_button = ttk.Button(startup, text="Create shortcuts…", command=self._create_shortcuts)
                self._shortcuts_button.grid(row=0, column=1, sticky="e")
                attach_tooltip(self._shortcuts_button, gui_tooltips.CONTROL_HELP["create_shortcuts"])

        ttk.Label(
            body,
            text="Changes apply the next time the daemon starts (new-repository defaults apply at once).",
            foreground=COLOR_MUTED,
        ).grid(
            row=4, column=0, sticky="w", pady=(10, 0)
        )
        self.message = ttk.Label(body, text="", foreground=COLOR_ERROR, wraplength=440)
        self.message.grid(row=5, column=0, sticky="w", pady=(6, 0))
        buttons = ttk.Frame(body)
        buttons.grid(row=6, column=0, sticky="ew", pady=(10, 0))
        buttons.columnconfigure(1, weight=1)
        open_config_button = ttk.Button(buttons, text="Open config.json…", command=self._open_config)
        open_config_button.grid(row=0, column=0, sticky="w")
        attach_tooltip(open_config_button, gui_tooltips.CONTROL_HELP["open_config"])
        ttk.Button(buttons, text="Cancel", command=self.destroy).grid(row=0, column=2, padx=(0, 6))
        ttk.Button(buttons, text="Save", command=self._save).grid(row=0, column=3)

        self.bind("<Escape>", lambda _event: self.destroy())
        center_dialog(self, master, focus=first_entry)
        self.grab_set()

    def _form_changed(self) -> bool:
        """True when something in the form differs from what config.json had when the dialog opened."""
        if any(str(var.get()) != str(self._initial_form.get(key)) for key, var in self.vars.items()):
            return True
        return self._autostart_before is not None and bool(self.autostart_var.get()) != self._autostart_before

    def _open_config(self) -> None:
        """Open config.json in the default editor (after adding the optional settings it lacks) and close this window."""
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
        threading.Thread(target=self._make_shortcuts, args=(folder,), daemon=True).start()
        self.after(100, self._apply_shortcuts_answer)

    def _make_shortcuts(self, folder: str) -> None:
        try:
            self._shortcuts_answer.append(shortcuts.create_shortcuts(folder))
        except Exception as exc:  # report it instead of losing it in the thread
            self._shortcuts_answer.append(autostart.AutostartResult(False, f"Could not create the shortcuts: {exc}"))

    def _apply_shortcuts_answer(self) -> None:
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


class DoctorDialog(tk.Toplevel):
    """Runs the --doctor checks and shows the result; the first-run notes (if any) come first."""

    def __init__(self, master: tk.Misc, on_report: Callable[[Optional[dict]], None]) -> None:
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
        self.text.tag_configure("ok", foreground="#2e7d32")
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
        self.summary.configure(text="Checking\u2026")
        self.run_button.state(["disabled"])
        self._result, self._error = None, None
        threading.Thread(target=self._work, daemon=True).start()  # a slow network drive must not freeze the window
        self.after(100, self._poll)

    def _work(self) -> None:
        try:
            self._result = dict(gui_doctor.run_report())
        except Exception as exc:  # shown in the dialog instead of vanishing
            self._error = describe_error(exc)

    def _poll(self) -> None:
        if not self.winfo_exists():
            return
        if self._result is None and self._error is None:
            self.after(100, self._poll)
            return
        self.run_button.state(["!disabled"])
        self._show(self._result)

    def _write(self, text: str, tag: str = "") -> None:
        self.text.insert("end", text, tag)

    def _show(self, report: Optional[dict]) -> None:
        reasons = gui_doctor.first_run_reasons()
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        if self._error:
            self.summary.configure(text="The checks could not run.")
            self._write(self._error + "\n", "error")
        elif report is not None:
            self.summary.configure(text=gui_doctor.summary_line(report))  # type: ignore[arg-type]
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


class MainWindow(tk.Tk):
    DEFAULT_SIZE = "1280x720"
    MIN_SIZE = (1080, 520)  # narrower and the editor's last column (Active / Shared folder / buttons) is cut off

    def __init__(self, theme: Optional[str] = None) -> None:
        super().__init__()
        self.title(APP_TITLE)
        # gui.refresh_seconds / gui.status_message_seconds in config.json (read once; a restart of the GUI applies changes)
        config = config_manager.load_config()
        self._refresh_ms = int(config_manager.get_gui_refresh_seconds(config) * 1000)
        self._status_message_ms = int(config_manager.get_gui_status_message_seconds(config) * 1000)
        self._icon_image: Optional[tk.PhotoImage] = None  # keep a reference or Tk drops the icon
        self._set_icon()
        self.minsize(*self.MIN_SIZE)
        self._restore_window_state()
        self._normal_state: Optional[gui_state.WindowState] = None  # last size/position while not maximized
        self.bind("<Configure>", self._remember_normal_state)
        self.protocol("WM_DELETE_WINDOW", self._on_close_request)
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
        self._notifications = config_manager.get_gui_notifications_enabled(config)
        self._minimize_to_tray = config_manager.get_gui_minimize_to_tray(config)
        self._close_to_tray = config_manager.get_gui_close_to_tray(config)
        self._notified_warning_id: Optional[int] = None  # newest warning already announced (None = not looked yet)
        self._limit_tracker = gui_tray.NewItemTracker()
        self._unmapped_tracker = gui_tray.NewItemTracker()
        self._failed_unseen = 0  # jobs that failed while the window was hidden (cleared when it is shown again)

        self.control_bar = ControlBar(self)
        self.control_bar.pack(fill="x")
        self.control_bar.settings_button.configure(command=self._open_settings)
        self.control_bar.start_button.configure(command=self._on_start)
        self.control_bar.stop_button.configure(command=self._on_stop)
        self.control_bar.pause_button.configure(command=self._on_pause)
        self.control_bar.poll_button.configure(command=self._on_poll_now)
        self.control_bar.check_button.configure(command=self._on_check_folders)
        self.control_bar.log_check.configure(command=self._on_log_toggle)
        self.control_bar.restart_button.configure(command=self._on_restart)
        self.control_bar.doctor_button.configure(command=self._open_doctor)
        ttk.Separator(self).pack(fill="x")

        self.status_bar = ttk.Label(self, text=DEFAULT_STATUS_TEXT, anchor="w", padding=(10, 3))
        self.status_bar.pack(fill="x", side="bottom")

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

    def set_status(self, text: str) -> None:
        """Show a message in the status bar for a few seconds, then restore the default."""
        self.status_bar.configure(text=text)
        if self._status_reset_job is not None:
            self.after_cancel(self._status_reset_job)
        self._status_reset_job = self.after(self._status_message_ms, self._reset_status)

    def _reset_status(self) -> None:
        self._status_reset_job = None
        self.status_bar.configure(text=DEFAULT_STATUS_TEXT)

    # ----- status tabs -----

    EVENT_COLUMNS: tuple[StatusTab.Column, ...] = (
        ("time", "Time", 150, "w", False),
        ("repo", "Repository", 220, "w", False),
        ("kind", "Type", 175, "w", False),
        ("message", "Message", 420, "w", True),
    )
    # The Completed tab's third column holds the release tag, not a type: heading and key differ per tab.
    COMPLETED_COLUMNS: tuple[StatusTab.Column, ...] = (
        ("time", "Time", 150, "w", False),
        ("repo", "Repository", 220, "w", False),
        ("kind", "Tag", 175, "w", False),
        ("message", "Message", 420, "w", True),
    )
    FILTERABLE_TABS = ("warnings", "completed")
    UNMAPPED_COLUMNS: tuple[StatusTab.Column, ...] = (
        ("repo", "Repository", 280, "w", True),
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
            on_mark_read=(lambda key=definition.key: self._mark_status_read(key)) if definition.counter else None,
            on_clear=lambda key=definition.key: self._clear_status_tab(key),
            on_clear_repo=(lambda repo: self._clear_repo_limit_warnings(repo)) if definition.key == "limits" else None,
            detail=True,
            filterable=definition.key in self.FILTERABLE_TABS,
        )

    def _show_repo(self, repo: str) -> None:
        if self.mappings_tab.select_repo(repo):
            self.notebook.select(self.mappings_tab)
        else:
            self.set_status(f"{repo} is not in mapping.json.")

    def _selected_status_key(self) -> Optional[str]:
        selected = self.notebook.select()
        for key, tab in self._status_tabs.items():
            if selected == str(tab):
                return key
        return None

    def _set_status_title(self, key: str, title: str) -> None:
        if self._status_titles.get(key) != title:
            self._status_titles[key] = title
            self.notebook.tab(self._status_tabs[key], text=title)

    def _show_event_rows(self, key: str, highlight_after: Optional[int]) -> None:
        rows = self.status_feed.model.rows(key)
        self._status_tabs[key].set_rows(
            [(r.time, r.repo, r.kind, r.message) for r in rows],
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
        if key in self.status_feed.model.event_tab_keys:
            self._status_tabs[key].set_highlight_after(None)

    def _mark_status_read(self, key: str) -> None:
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
        self._report(gui_daemon.do_set_log(True), "Terminal log switched on (a new .log file).")

    def _mappings_visible(self) -> bool:
        """True when somebody can see the Mappings table (window not minimized, its tab selected)."""
        return not self._window_hidden() and self.notebook.select() == str(self.mappings_tab)

    def _refresh_mappings_if_visible(self) -> None:
        if self._mappings_visible():
            self.mappings_tab.refresh()

    def _on_tab_changed(self, _event: object = None) -> None:
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
        try:
            self._refresh_mappings_if_visible()
            if self.state() != "iconic" or self._tray_active():  # a tray icon still has news to deliver
                self._refresh_status_tabs()
        finally:
            self._tick_job = self.after(self._refresh_ms, self._tick)

    # ----- daemon status and control -----

    def _daemon_action_pending(self) -> bool:
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
            self._update_tray_icon()
        except Exception as exc:  # a status hiccup must never kill the GUI loop
            self.control_bar.status_label.configure(text=f"Daemon status unavailable: {exc}")
        finally:
            if self._daemon_job is not None:
                self.after_cancel(self._daemon_job)
            self._daemon_job = self.after(DAEMON_REFRESH_INTERVAL_MS, self._update_daemon_view)

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
        return self._stopping_since is not None and now - self._stopping_since < gui_daemon.STOPPING_TIMEOUT_SECONDS

    def _report(self, error: Optional[str], success_text: str) -> bool:
        """Show the outcome of a control action in the status bar; True when it worked."""
        self.set_status(error or success_text)
        self._update_daemon_view(force_control=True)  # we just changed something: read it back now
        return error is None

    def _on_start(self) -> None:
        if self._report(gui_daemon.do_start(), "Starting the daemon\u2026"):
            self._starting_since = self._gui_started_at = time.time()
            self._update_daemon_view()

    def _on_stop(self) -> None:
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
        pausing = not self._snapshot.paused
        self._report(
            gui_daemon.do_set_paused(pausing),
            "Pause requested; the countdown freezes." if pausing else "Resumed.",
        )

    def _on_poll_now(self) -> None:
        self._report(gui_daemon.do_poll_now(), "Poll requested; it runs within a second.")

    def _on_check_folders(self) -> None:
        self._report(
            gui_daemon.do_check_folders(),
            "Folder check requested; the counts in the Limit column update within a few seconds.",
        )

    def _on_log_toggle(self) -> None:
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
        SettingsDialog(self, self._after_settings_saved)

    def _open_doctor(self) -> None:
        DoctorDialog(self, self._refresh_doctor_attention)

    def _refresh_doctor_attention(self, report: Optional[dict] = None) -> None:
        """Highlight the Doctor button on a first run (missing files / login) or after a report with problems."""
        try:
            reasons = gui_doctor.first_run_reasons()
        except Exception:
            return
        self.control_bar.set_doctor_attention(reasons, gui_doctor.needs_attention(reasons, report))  # type: ignore[arg-type]

    def _after_settings_saved(self, message: str) -> None:
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
        return self._tray is not None and self._tray.active

    def _window_hidden(self) -> bool:
        """True while nobody can see the window: minimized or hidden in the tray."""
        return self.state() in ("iconic", "withdrawn")

    def _start_tray(self, config: dict) -> None:
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
        if event.widget is self:
            self.after_idle(self._hide_if_minimized)

    def _hide_if_minimized(self) -> None:
        try:
            if self._tray_active() and self.state() == "iconic":
                self.withdraw()  # gone from the taskbar; the tray icon brings it back
        except tk.TclError:
            pass

    def _show_from_tray(self) -> None:
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
        if not self._tray_active():
            return
        model = self.status_feed.model
        unread = model.unread("warnings")
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
            fresh = [row for row in warnings if row.id > self._notified_warning_id]
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

    def destroy(self) -> None:
        """Cancel pending timers first, so nothing fires into a window that is already gone."""
        for job in (self._tick_job, self._status_reset_job, self._daemon_job, self._verify_log_job, self._tray_job):
            if job is not None:
                self.after_cancel(job)
        self._tick_job = self._status_reset_job = self._daemon_job = self._verify_log_job = self._tray_job = None
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
        style = ttk.Style(self)
        for theme in ((preferred,) if preferred else ()) + ("vista", "winnative", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        self._configure_styles(style)

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
        style.map("Treeview.Heading", background=[("active", "#cbd7e6"), ("pressed", "#bccbdf")])
        # Explicit selection colors: tag backgrounds (striping) otherwise win over the theme's.
        style.map(
            "Treeview",
            background=[("selected", COLOR_SELECTED)],
            foreground=[("selected", "#ffffff")],
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="GHAADD GUI")
    parser.add_argument("--theme", help="ttk theme to use (default: native Windows theme; try 'clam')")
    args = parser.parse_args()
    MainWindow(theme=args.theme).mainloop()


if __name__ == "__main__":
    main()
