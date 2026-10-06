"""GHAADD GUI (Tkinter).

Phase 4, step 2: the Mappings tab and the Settings dialog work on real data
(mapping.json via mapping_manager, config.json via config_manager, queue state
via gui_data/db_manager). The control bar, Live log and status tabs are still
static placeholders (steps 3-5). The widgets here are a thin view: parsing,
validation, ordering and data loading live in the toolkit-independent modules
(gui_forms, gui_data, repo_overview).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Literal, Optional

from modules import config_manager, gui_daemon, gui_data, gui_forms, gui_state, mapping_manager
from modules.app_info import APP_NAME, __version__

Anchor = Literal["nw", "n", "ne", "w", "center", "e", "sw", "s", "se"]

APP_TITLE = f"{APP_NAME} {__version__}"
# The app icon is looked up here: ghaadd.ico (preferred on Windows) or ghaadd.png.
ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
WINDOWS_APP_ID = "GHAADD.GUI"  # own taskbar identity, so the taskbar shows our icon instead of Python's
REFRESH_INTERVAL_MS = 3000
STATUS_MESSAGE_MS = 6000
DAEMON_REFRESH_INTERVAL_MS = 1000
RESTART_HINT = (
    "Settings changed since the daemon started.\n"
    "Click to restart it: the job in progress finishes, the daemon stops,\n"
    "then starts again with the new settings."
)
DEFAULT_STATUS_TEXT = "config.json · mapping.json · state.db"

COLOR_ERROR = "#b3261e"
COLOR_WARNING = "#9a6700"
COLOR_MUTED = "#666666"
COLOR_STRIPE = "#f2f5f9"  # every second table row
COLOR_HEADER_BG = "#dde5ef"  # table column headers
COLOR_HEADER_FG = "#1f2d3d"
COLOR_SELECTED = "#0078d4"

# Row icon per status (first column). Paused is a mapping.json flag and wins over runtime state.
STATUS_ICONS = {
    "Running": "▶",
    "Queued": "⏳",
    "Waiting": "◷",
    "Idle": "○",
    "Paused": "⏸",
    "Failed": "✖",
}

# Hover text for the status icon in the first column.
STATUS_HINTS = {
    "Running": "A job is being processed now.",
    "Queued": "A check is due and waiting its turn.",
    "Waiting": "The next recheck is scheduled for later.",
    "Idle": "Nothing pending.",
    "Paused": "This repository is paused in mapping.json.",
    "Failed": "The last job failed.",
}

STATUS_LEGEND = "Status\n" + "\n".join(
    f"{icon}  {name}: {STATUS_HINTS[name]}" for name, icon in STATUS_ICONS.items()
)

# Placeholder content for tabs that get real data in steps 4-5.
DUMMY_LOG = (
    "[12:00:01] Polling mailbox...\n"
    "[12:00:03] 1 new notification(s)\n"
    "[12:00:04] Queued ip7z/7zip 24.09\n"
    "[12:00:09] Completed ip7z/7zip 24.09\n"
)
STATUS_TABS = ["Warnings (3)", "Completed (12)", "Folder limits", "Unmapped (1)"]
STATUS_COLUMNS = ("time", "repo", "message")


def pick_directory(parent: tk.Misc, variable: tk.Variable) -> None:
    """Let the user choose a folder and store it (normalized) in `variable`."""
    current = str(variable.get()).strip()
    chosen = filedialog.askdirectory(
        parent=parent, initialdir=current if os.path.isdir(current) else None, title="Select folder"
    )
    if chosen:
        variable.set(os.path.normpath(chosen))


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
        window.wm_geometry(f"+{x_root + 14}+{y_root + 18}")
        tk.Label(
            window, text=text, justify="left", background="#ffffe1", foreground="#000000",
            relief="solid", borderwidth=1, padx=6, pady=3,
        ).pack()
        self._window = window


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
        self.detailed_log = tk.BooleanVar(value=False)
        self.log_check = ttk.Checkbutton(buttons, text="Detailed log", variable=self.detailed_log)
        self.restart_button = ttk.Button(buttons, text="\u21bb Restart")
        self.settings_button = ttk.Button(buttons, text="Settings\u2026")
        self._restart_tip = Tooltip(self.restart_button)
        self.restart_button.bind("<Enter>", self._show_restart_tip)
        self.restart_button.bind("<Leave>", lambda _event: self._restart_tip.hide())
        self.restart_button.bind("<ButtonPress>", lambda _event: self._restart_tip.hide())

        for column, widget in enumerate(
            (self.start_button, self.stop_button, self.pause_button, self.poll_button)
        ):
            widget.grid(row=0, column=column, padx=(0, 6))
        self.log_check.grid(row=0, column=4, padx=(6, 12))
        self.restart_button.grid(row=0, column=5, padx=(0, 6))
        self.settings_button.grid(row=0, column=6)
        self.apply_view(gui_daemon.build_view(gui_daemon.DaemonSnapshot(), 0.0))

    def _show_restart_tip(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        self._restart_tip.schedule(RESTART_HINT, event.x_root, event.y_root)

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
        name_entry.focus_set()
        self.grab_set()

    def _add(self) -> None:
        result = gui_forms.build_new_repo(self.name_var.get(), self.dest_var.get())
        if not result.ok:
            self.message.configure(text=" ".join(result.errors))
            return
        repo = result.changes["name"]
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
        ("foldername", "Name", 150, "w"),
        ("repo", "Repository", 150, "w"),
        ("destination", "Destination", 150, "w"),
        ("tag", "Latest tag", 100, "w"),
        ("last_check", "Last check", 125, "w"),
        ("step", "Recheck", 65, "center"),
        ("next_check", "Next check", 125, "w"),
        ("files", "Files", 70, "center"),
        ("limit", "Limit", 50, "center"),
    )
    STRETCH_COLUMNS = ("foldername", "repo", "destination", "tag")
    SHOW_FILTERS = ("All", "Active", "Paused", "Has pending")
    # Editor fields per column (top to bottom): (key, label, kind). kinds: entry, readonly, spin, dest.
    FORM_COLUMNS = (
        (
            ("name", "Name (owner/repo)", "readonly"),
            ("foldername", "Folder name", "entry"),
            ("destination", "Destination", "dest"),
            ("subfolder", "Subfolder", "entry"),
        ),
        (
            ("release_folders", "Release folders", "entry"),
            ("limit", "Limit", "spin"),
            ("recheck", "Recheck (minutes, empty = default)", "entry"),
            ("default_recheck", "Default recheck", "readonly"),
        ),
        (
            ("skiplist", "Skiplist", "entry"),
            ("last_seen", "Last notification", "readonly"),
            ("last_finalized", "Last finalized", "readonly"),
        ),
    )
    # Form values the user can change (the rest is read-only display).
    EDITABLE_KEYS = (
        "foldername", "destination", "subfolder", "release_folders", "limit", "recheck", "skiplist", "paused",
    )

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
        bar.columnconfigure(0, weight=1)
        self.filter_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self.filter_var).grid(row=0, column=0, sticky="ew")
        self.filter_var.trace_add("write", lambda *_: self._render_rows())
        self.show_var = tk.StringVar(value=self.SHOW_FILTERS[0])
        show_box = ttk.Combobox(bar, textvariable=self.show_var, state="readonly", width=12, values=self.SHOW_FILTERS)
        show_box.grid(row=0, column=1, padx=(8, 0))
        show_box.bind("<<ComboboxSelected>>", lambda _event: self._render_rows())
        self.add_button = ttk.Button(bar, text="Add repository…", command=self._add_repository)
        self.remove_button = ttk.Button(bar, text="Remove", command=self._remove_repository)
        self.add_button.grid(row=0, column=2, padx=(8, 6))
        self.remove_button.grid(row=0, column=3)

    def _build_table(self) -> None:
        self.table_frame = ttk.Frame(self)
        self.table_frame.columnconfigure(0, weight=1)
        self.table_frame.rowconfigure(0, weight=1)
        keys = [column[0] for column in self.TABLE_COLUMNS]
        self.tree = ttk.Treeview(self.table_frame, columns=keys, show="headings", selectmode="browse")
        for key, title, width, anchor in self.TABLE_COLUMNS:
            self.tree.heading(key, text=title, anchor=anchor)
            self.tree.column(key, width=width, minwidth=30 if key == "icon" else 40, anchor=anchor, stretch=key in self.STRETCH_COLUMNS)
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(self.table_frame, orient="vertical", command=self.tree.yview)
        self._xscroll = ttk.Scrollbar(self.table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=self._set_xscroll)
        yscroll.grid(row=0, column=1, sticky="ns")
        self._xscroll.grid(row=1, column=0, sticky="ew")
        self.tree.tag_configure("odd", background=COLOR_STRIPE)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        # Re-fit the "..." truncation after the user drags a column or the window is resized.
        self.tree.bind("<ButtonRelease-1>", lambda _event: self._schedule_refit())
        self.tree.bind("<Configure>", lambda _event: self._schedule_refit())
        self.tree.bind("<Motion>", self._on_motion)
        self.tree.bind("<Leave>", lambda _event: self._hide_tip())
        self.tree.bind("<ButtonPress>", lambda _event: self._hide_tip())
        self.tree.bind("<MouseWheel>", lambda _event: self._hide_tip())

    def _build_form(self) -> None:
        self.form_frame = ttk.LabelFrame(self, text="Selected repository", padding=10)
        for column in range(len(self.FORM_COLUMNS)):
            self.form_frame.columnconfigure(column, weight=1, uniform="form")

        self.vars: dict[str, tk.Variable] = {
            key: tk.StringVar() for column in self.FORM_COLUMNS for key, _label, _kind in column
        }
        self.vars["paused"] = tk.BooleanVar()
        for key in self.EDITABLE_KEYS:
            self.vars[key].trace_add("write", lambda *_: self._on_form_edited())

        self.form_widgets: list[ttk.Widget] = []
        for column, fields in enumerate(self.FORM_COLUMNS):
            pad = (0, 0) if column == len(self.FORM_COLUMNS) - 1 else (0, 14)
            for row, (key, label, kind) in enumerate(fields):
                # Label and input sit directly in the form grid (no wrapper frame per field: fewer
                # widgets to lay out and repaint while the window is resized).
                ttk.Label(self.form_frame, text=label).grid(row=row * 2, column=column, sticky="w", padx=pad)
                if kind == "dest":
                    self._add_dest_widgets(row * 2 + 1, column, pad)
                    continue
                if kind == "spin":
                    widget: ttk.Widget = ttk.Spinbox(self.form_frame, from_=0, to=999, width=6, textvariable=self.vars[key])
                    sticky = "w"
                else:
                    widget = ttk.Entry(
                        self.form_frame, textvariable=self.vars[key], state="readonly" if kind == "readonly" else "normal"
                    )
                    sticky = "ew"
                widget.grid(row=row * 2 + 1, column=column, sticky=sticky, padx=pad, pady=(0, 6))
                if kind != "readonly":
                    self.form_widgets.append(widget)

        # Fourth row: validation message (left) and Paused + Revert/Save (last column).
        self.message_label = ttk.Label(self.form_frame, text="", foreground=COLOR_ERROR, wraplength=700)
        self.message_label.grid(row=8, column=0, columnspan=2, sticky="w")
        actions = ttk.Frame(self.form_frame)
        actions.grid(row=8, column=2, sticky="ew", pady=(8, 0))
        actions.columnconfigure(0, weight=1)
        self.paused_check = ttk.Checkbutton(actions, text="Paused", variable=self.vars["paused"])
        self.revert_button = ttk.Button(actions, text="Revert", command=self._revert)
        self.save_button = ttk.Button(actions, text="Save", command=self._save)
        self.paused_check.grid(row=0, column=0, sticky="w")
        self.revert_button.grid(row=0, column=1, padx=(0, 6))
        self.save_button.grid(row=0, column=2)
        self.form_widgets.append(self.paused_check)

    def _add_dest_widgets(self, row: int, column: int, pad: tuple[int, int]) -> None:
        holder = ttk.Frame(self.form_frame)
        holder.grid(row=row, column=column, sticky="ew", padx=pad, pady=(0, 6))
        holder.columnconfigure(0, weight=1)
        entry = ttk.Entry(holder, textvariable=self.vars["destination"])
        entry.grid(row=0, column=0, sticky="ew")
        self.browse_button = ttk.Button(
            holder, text="Browse…", width=9, command=lambda: pick_directory(self, self.vars["destination"])
        )
        self.browse_button.grid(row=0, column=1, padx=(6, 0))
        self.form_widgets.extend([entry, self.browse_button])

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

    def _tree_font(self) -> tkfont.Font:
        spec = ttk.Style(self).lookup("Treeview", "font") or "TkDefaultFont"
        try:
            return tkfont.Font(root=self, font=spec)
        except tk.TclError:
            return tkfont.nametofont("TkDefaultFont")

    @staticmethod
    def _cell_text(row: Any, key: str) -> str:
        return STATUS_ICONS.get(row.status, "") if key == "icon" else str(getattr(row, key))

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
            self._tooltip.schedule(f"{row.status}: {STATUS_HINTS.get(row.status, '')}", event.x_root, event.y_root)
            return
        full = self._cell_text(row, key)
        if self._cell_display(row, key) != full:  # only text that is cut off gets a tooltip
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
        if needle and needle not in f"{row.foldername} {row.repo} {row.destination}".casefold():
            return False
        show = self.show_var.get()
        if show == "Active":
            return row.status != "Paused"
        if show == "Paused":
            return row.status == "Paused"
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
            "foldername": str(entry.get("foldername") or ""),
            "destination": str(entry.get("destination") or ""),
            "subfolder": str(entry.get("subfolder") or ""),
            "release_folders": gui_forms.format_list(entry.get("limit_release_type_folders")),
            "limit": str(entry.get("limit", "")),
            "recheck": gui_forms.format_list(entry.get("recheck_intervals_minutes")),
            "skiplist": gui_forms.format_list(entry.get("skiplist")),
            "last_seen": str(entry.get("last_notification_seen") or ""),
            "last_finalized": str(entry.get("last_finalized") or ""),
            "paused": entry.get("paused") is True,
        }

    def _load_form(self, repo: Optional[str]) -> None:
        """Fill the editor from the loaded entry (or clear it when repo is None)."""
        self._loading = True
        try:
            values = self._entry_to_form(repo) if repo else {}
            for key, var in self.vars.items():
                if key == "default_recheck":
                    continue
                var.set(values.get(key, False if key == "paused" else ""))
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


class LiveLogTab(ttk.Frame):
    """Raw log tail with follow control (static until step 4)."""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, padding=10)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(self)
        toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        toolbar.columnconfigure(2, weight=1)
        self.follow_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(toolbar, text="Follow", variable=self.follow_var).grid(row=0, column=0)
        self.filter_var = tk.StringVar()
        ttk.Entry(toolbar, textvariable=self.filter_var, width=28).grid(row=0, column=1, padx=8)
        self.file_label = ttk.Label(toolbar, text="20261006_120000.log", foreground=COLOR_MUTED)
        self.file_label.grid(row=0, column=2, sticky="e", padx=(0, 8))
        ttk.Button(toolbar, text="Copy").grid(row=0, column=3, padx=(0, 6))
        ttk.Button(toolbar, text="Open folder").grid(row=0, column=4)

        self.text = tk.Text(self, wrap="none", height=10, font=("Consolas", 10), state="normal")
        self.text.insert("1.0", DUMMY_LOG)
        self.text.configure(state="disabled")
        self.text.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        scroll.grid(row=1, column=1, sticky="ns")


class StatusTab(ttk.Frame):
    """Filtered structured-event list (static until step 5)."""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, padding=10)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(self, columns=STATUS_COLUMNS, show="headings")
        for column, title, width in (("time", "Time", 150), ("repo", "Repository", 240), ("message", "Message", 420)):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.grid(row=0, column=1, sticky="ns")

        bottom = ttk.Frame(self)
        bottom.grid(row=1, column=0, columnspan=2, sticky="e", pady=(8, 0))
        self.mark_read_button = ttk.Button(bottom, text="Mark all read")
        self.mark_read_button.grid(row=0, column=0)

        self.tree.insert("", "end", values=("2026-10-06 12:00:09", "ip7z/7zip", "Example row"))


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
        row(processing, 0, "Default recheck (minutes)", ttk.Entry(processing, textvariable=self.vars["recheck"], width=28))
        row(processing, 1, "Max emails per poll (0 = all)", spin(processing, "max_emails", 0, 9999))
        row(processing, 2, "Destination check every N polls (0 = off)", spin(processing, "dest_check", 0, 999))

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
        row(paths, 0, "Default download dir", dir_frame)
        row(paths, 1, "Terminal log on at startup", ttk.Checkbutton(paths, variable=self.vars["log_enabled"]))
        row(paths, 2, "Max log file (MB, 0 = no rollover)", spin(paths, "log_max_mb", 0, 1000))
        row(paths, 3, "Keep log files (0 = all)", spin(paths, "log_keep", 0, 9999))

        ttk.Label(body, text="Changes apply the next time the daemon starts.", foreground=COLOR_MUTED).grid(
            row=3, column=0, sticky="w", pady=(10, 0)
        )
        self.message = ttk.Label(body, text="", foreground=COLOR_ERROR, wraplength=440)
        self.message.grid(row=4, column=0, sticky="w", pady=(6, 0))
        buttons = ttk.Frame(body)
        buttons.grid(row=5, column=0, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(buttons, text="Save", command=self._save).grid(row=0, column=1)

        self.bind("<Escape>", lambda _event: self.destroy())
        self.grab_set()

    def _save(self) -> None:
        result = gui_forms.build_settings_changes({key: var.get() for key, var in self.vars.items()})
        if not result.ok:
            self.message.configure(text="\n".join(result.errors))
            return
        try:
            changed = config_manager.set_config_values(result.changes)
        except (config_manager.ConfigLockTimeout, config_manager.ConfigUnreadableError, OSError) as exc:
            self.message.configure(text=describe_error(exc))
            return
        text = "Settings saved; they apply when the daemon next starts." if changed else "No settings changed."
        if result.warnings:
            text += " " + " ".join(result.warnings)
        self.destroy()
        self._on_saved(text)


class MainWindow(tk.Tk):
    DEFAULT_SIZE = "1280x720"
    MIN_SIZE = (900, 520)

    def __init__(self, theme: Optional[str] = None) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self._icon_image: Optional[tk.PhotoImage] = None  # keep a reference or Tk drops the icon
        self._set_icon()
        self.minsize(*self.MIN_SIZE)
        self._restore_window_state()
        self._normal_state: Optional[gui_state.WindowState] = None  # last size/position while not maximized
        self.bind("<Configure>", self._remember_normal_state)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._apply_theme(theme)
        self._status_reset_job: Optional[str] = None

        self._snapshot = gui_daemon.DaemonSnapshot()
        self._reader = gui_daemon.SnapshotReader()
        self._starting_since: Optional[float] = None  # set by Start/Stop until the daemon appears/disappears
        self._stopping_since: Optional[float] = None
        self._restart_pending = False  # a restart was requested: start again once the daemon has exited

        self.control_bar = ControlBar(self)
        self.control_bar.pack(fill="x")
        self.control_bar.settings_button.configure(command=self._open_settings)
        self.control_bar.start_button.configure(command=self._on_start)
        self.control_bar.stop_button.configure(command=self._on_stop)
        self.control_bar.pause_button.configure(command=self._on_pause)
        self.control_bar.poll_button.configure(command=self._on_poll_now)
        self.control_bar.log_check.configure(command=self._on_log_toggle)
        self.control_bar.restart_button.configure(command=self._on_restart)
        ttk.Separator(self).pack(fill="x")

        self.status_bar = ttk.Label(self, text=DEFAULT_STATUS_TEXT, anchor="w", padding=(10, 3))
        self.status_bar.pack(fill="x", side="bottom")

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=8)

        self.mappings_tab = MappingsTab(self.notebook, self.set_status)
        self.log_tab = LiveLogTab(self.notebook)
        self.notebook.add(self.mappings_tab, text="Mappings")
        self.notebook.add(self.log_tab, text="Live log")
        for title in STATUS_TABS:
            self.notebook.add(StatusTab(self.notebook), text=title)

        self.notebook.bind("<<NotebookTabChanged>>", lambda _event: self._refresh_mappings_if_visible())
        self.bind("<Map>", self._on_map)
        self._tick_job: Optional[str] = self.after(REFRESH_INTERVAL_MS, self._tick)
        self._daemon_job: Optional[str] = None
        self._update_daemon_view()

    def set_status(self, text: str) -> None:
        """Show a message in the status bar for a few seconds, then restore the default."""
        self.status_bar.configure(text=text)
        if self._status_reset_job is not None:
            self.after_cancel(self._status_reset_job)
        self._status_reset_job = self.after(STATUS_MESSAGE_MS, self._reset_status)

    def _reset_status(self) -> None:
        self._status_reset_job = None
        self.status_bar.configure(text=DEFAULT_STATUS_TEXT)

    def _mappings_visible(self) -> bool:
        """True when somebody can see the Mappings table (window not minimized, its tab selected)."""
        return self.state() != "iconic" and self.notebook.select() == str(self.mappings_tab)

    def _refresh_mappings_if_visible(self) -> None:
        if self._mappings_visible():
            self.mappings_tab.refresh()

    def _on_map(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        """The window was restored: catch up immediately instead of waiting for the next tick."""
        if event.widget is self:
            self._refresh_mappings_if_visible()
            self._update_daemon_view()

    def _tick(self) -> None:
        try:
            self._refresh_mappings_if_visible()
        finally:
            self._tick_job = self.after(REFRESH_INTERVAL_MS, self._tick)

    # ----- daemon status and control -----

    def _daemon_action_pending(self) -> bool:
        return bool(self._restart_pending or self._starting_since or self._stopping_since)

    def _update_daemon_view(self, force_control: bool = False) -> None:
        """Read what the daemon publishes, update the control bar, and schedule the next look.

        A minimized window is not looked at, so it is left alone (unless a Start/Stop/Restart is
        in flight, which must keep running); the first look after restoring happens at once.
        """
        try:
            if self.state() == "iconic" and not self._daemon_action_pending() and not force_control:
                return
            self._snapshot = self._reader.read(force_control=force_control)
            now = time.time()
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
                        self._starting_since = now
            self.control_bar.apply_view(
                gui_daemon.build_view(self._snapshot, now, self._starting_since, self._stopping_since)
            )
        except Exception as exc:  # a status hiccup must never kill the GUI loop
            self.control_bar.status_label.configure(text=f"Daemon status unavailable: {exc}")
        finally:
            if self._daemon_job is not None:
                self.after_cancel(self._daemon_job)
            self._daemon_job = self.after(DAEMON_REFRESH_INTERVAL_MS, self._update_daemon_view)

    def _is_stopping(self, now: float) -> bool:
        return self._stopping_since is not None and now - self._stopping_since < gui_daemon.STOPPING_TIMEOUT_SECONDS

    def _report(self, error: Optional[str], success_text: str) -> bool:
        """Show the outcome of a control action in the status bar; True when it worked."""
        self.set_status(error or success_text)
        self._update_daemon_view(force_control=True)  # we just changed something: read it back now
        return error is None

    def _on_start(self) -> None:
        if self._report(gui_daemon.do_start(), "Starting the daemon\u2026"):
            self._starting_since = time.time()
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

    def _on_log_toggle(self) -> None:
        wanted = bool(self.control_bar.detailed_log.get())  # the click has already flipped the box
        if not self._report(
            gui_daemon.do_set_log(wanted),
            "Detailed log switched on (a new .log file)." if wanted else "Detailed log switched off.",
        ):
            self.control_bar.detailed_log.set(self._snapshot.log_on)  # show the real state again

    def _open_settings(self) -> None:
        SettingsDialog(self, self._after_settings_saved)

    def _after_settings_saved(self, message: str) -> None:
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
        for job in (self._tick_job, self._status_reset_job, self._daemon_job):
            if job is not None:
                self.after_cancel(job)
        self._tick_job = self._status_reset_job = self._daemon_job = None
        self.mappings_tab.shutdown()
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
