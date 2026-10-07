"""Shortcuts that launch GHAADD under its own name and icon.

Windows: two `.lnk` files (the GUI, and the background daemon) in the Start menu, or in a folder of your
choice (for example a launcher folder you keep). The GUI shortcut carries the same AppUserModelID the GUI
declares for itself (`app_info.APP_USER_MODEL_ID`), which is what lets Windows list the app as "GHAADD" with its
own icon instead of "Python". Linux: a `.desktop` launcher for the GUI. Nothing needs admin rights.

The PowerShell script and the .desktop text are built by pure functions; the runner is a parameter so the
tests never create real shortcuts.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Callable, Optional

from modules.app_info import APP_NAME, APP_USER_MODEL_ID
from modules.autostart import AutostartResult, main_script_path, run_hidden, windowless_python

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUI_SHORTCUT = f"{APP_NAME}.lnk"
DAEMON_SHORTCUT = f"{APP_NAME} daemon.lnk"
DESKTOP_FILE = "ghaadd.desktop"

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class ShortcutSpec:
    path: str
    target: str
    arguments: str
    workdir: str
    icon: str
    description: str
    app_id: str = ""


def _run(command: list[str]) -> "subprocess.CompletedProcess[str]":
    return run_hidden(command, timeout=120)


def gui_script_path() -> str:
    return os.path.join(_APP_DIR, "main_gui.py")


def icon_path(extension: str) -> str:
    return os.path.join(_APP_DIR, "assets", f"ghaadd.{extension}")


def start_menu_folder() -> str:
    """The per-user Start menu "Programs" folder (what Windows searches for app shortcuts)."""
    appdata = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs")


GUI_OPTION_MINIMIZED = "--minimized"
GUI_OPTION_START_DAEMON = "--start-daemon"


def gui_options(minimized: bool = False, start_daemon: bool = False) -> tuple[str, ...]:
    """The main_gui.py options a GUI shortcut should carry for the ticked startup choices."""
    return tuple(
        option for option, wanted in ((GUI_OPTION_MINIMIZED, minimized), (GUI_OPTION_START_DAEMON, start_daemon)) if wanted
    )


def _with_options(arguments: str, options: tuple[str, ...]) -> str:
    return " ".join([arguments, *options])


def _options_note(options: tuple[str, ...]) -> str:
    return f" The GUI shortcut starts with {' '.join(options)}." if options else ""


def windows_specs(directory: str, python: Optional[str] = None, options: tuple[str, ...] = ()) -> list[ShortcutSpec]:
    interpreter = windowless_python(python)
    icon = icon_path("ico")
    return [
        ShortcutSpec(
            os.path.join(directory, GUI_SHORTCUT), interpreter, _with_options(f'"{gui_script_path()}"', options), _APP_DIR,
            icon if os.path.isfile(icon) else "", "GHAADD: GitHub release downloader (window)", APP_USER_MODEL_ID,
        ),
        ShortcutSpec(
            os.path.join(directory, DAEMON_SHORTCUT), interpreter, f'"{main_script_path()}" --daemon-detached', _APP_DIR,
            icon if os.path.isfile(icon) else "", "Start the GHAADD polling daemon in the background (no window)",
        ),
    ]


def is_supported(platform: Optional[str] = None) -> bool:
    """True where shortcuts can be made (Windows, Linux); no probing."""
    system = platform if platform is not None else sys.platform
    return system == "win32" or system.startswith("linux")


def default_folder(platform: Optional[str] = None, home: Optional[str] = None) -> str:
    """Where the shortcuts go unless the user picks another folder (the Start menu / application menu)."""
    system = platform if platform is not None else sys.platform
    if system == "win32":
        return start_menu_folder()
    if system.startswith("linux"):
        return _applications_folder(home)
    return ""


# ----- PowerShell script (Windows) -----

_CSHARP = """\
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;

public static class GhaaddShortcut {
  [ComImport, Guid("00021401-0000-0000-C000-000000000046")] class CShellLink { }

  [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")]
  interface IPropertyStore {
    [PreserveSig] int GetCount(out uint count);
    [PreserveSig] int GetAt(uint index, out PropertyKey key);
    [PreserveSig] int GetValue(ref PropertyKey key, out PropVariant value);
    [PreserveSig] int SetValue(ref PropertyKey key, ref PropVariant value);
    [PreserveSig] int Commit();
  }

  [StructLayout(LayoutKind.Sequential, Pack = 4)] struct PropertyKey { public Guid FormatId; public uint PropertyId; }
  [StructLayout(LayoutKind.Explicit, Size = 24)] struct PropVariant {
    [FieldOffset(0)] public ushort Type;
    [FieldOffset(8)] public IntPtr Pointer;
  }

  static PropertyKey AppIdKey() {
    PropertyKey key = new PropertyKey();
    key.FormatId = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");
    key.PropertyId = 5;
    return key;
  }

  static void Check(int hr) { if (hr != 0) Marshal.ThrowExceptionForHR(hr); }

  public static void SetAppId(string path, string appId) {
    IPersistFile file = (IPersistFile)new CShellLink();
    file.Load(path, 2);
    IPropertyStore store = (IPropertyStore)file;
    PropertyKey key = AppIdKey();
    PropVariant value = new PropVariant();
    value.Type = 31;
    value.Pointer = Marshal.StringToCoTaskMemUni(appId);
    try {
      Check(store.SetValue(ref key, ref value));
      Check(store.Commit());
      file.Save(path, true);
    } finally {
      Marshal.FreeCoTaskMem(value.Pointer);
    }
  }
}
"""


def _ps(text: str) -> str:
    """A PowerShell single-quoted string literal."""
    return "'" + text.replace("'", "''") + "'"


def powershell_script(specs: list[ShortcutSpec]) -> str:
    lines = [
        "$ErrorActionPreference = 'Stop'",
        "Add-Type -TypeDefinition @'",
        _CSHARP.rstrip("\n"),
        "'@",
        "$shell = New-Object -ComObject WScript.Shell",
    ]
    for spec in specs:
        lines += [
            f"$link = $shell.CreateShortcut({_ps(spec.path)})",
            f"$link.TargetPath = {_ps(spec.target)}",
            f"$link.Arguments = {_ps(spec.arguments)}",
            f"$link.WorkingDirectory = {_ps(spec.workdir)}",
            f"$link.Description = {_ps(spec.description)}",
        ]
        if spec.icon:
            lines.append(f"$link.IconLocation = {_ps(spec.icon)}")
        lines.append("$link.Save()")
        if spec.app_id:
            lines.append(f"[GhaaddShortcut]::SetAppId({_ps(spec.path)}, {_ps(spec.app_id)})")
    return "\r\n".join(lines) + "\r\n"


# ----- create / remove -----


def create_shortcuts(
    directory: Optional[str] = None,
    runner: Runner = _run,
    platform: Optional[str] = None,
    python: Optional[str] = None,
    home: Optional[str] = None,
    options: tuple[str, ...] = (),
) -> AutostartResult:
    """Make the shortcuts; `options` (see `gui_options`) are added to the GUI shortcut's command line."""
    system = platform if platform is not None else sys.platform
    if system == "win32":
        return _create_windows(directory or start_menu_folder(), runner, python, options)
    if system.startswith("linux"):
        return _create_linux(directory, python, home, options)
    return AutostartResult(False, f"Shortcuts are not supported on this system ({system}).")


def remove_shortcuts(
    directory: Optional[str] = None, platform: Optional[str] = None, home: Optional[str] = None
) -> AutostartResult:
    system = platform if platform is not None else sys.platform
    if system == "win32":
        paths = [os.path.join(directory or start_menu_folder(), name) for name in (GUI_SHORTCUT, DAEMON_SHORTCUT)]
    elif system.startswith("linux"):
        paths = [os.path.join(directory or _applications_folder(home), DESKTOP_FILE)]
    else:
        return AutostartResult(False, f"Shortcuts are not supported on this system ({system}).")
    removed = []
    for path in paths:
        try:
            os.remove(path)
            removed.append(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            return AutostartResult(False, f"Could not remove {path}: {exc}")
    return AutostartResult(True, "Removed: " + ", ".join(removed) if removed else "There were no shortcuts to remove.")


def _create_windows(directory: str, runner: Runner, python: Optional[str], options: tuple[str, ...] = ()) -> AutostartResult:
    specs = windows_specs(directory, python, options)
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        return AutostartResult(False, f"Could not create {directory}: {exc}")
    handle, script_path = tempfile.mkstemp(suffix=".ps1", prefix="ghaadd_shortcuts_")
    try:
        with os.fdopen(handle, "w", encoding="utf-8-sig", newline="") as file:  # PowerShell 5 needs the BOM for non-ASCII paths
            file.write(powershell_script(specs))
        result = runner(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script_path])
    except (OSError, subprocess.SubprocessError) as exc:
        return AutostartResult(False, f"Could not run PowerShell: {exc}")
    finally:
        try:
            os.remove(script_path)
        except OSError:
            pass
    if result.returncode != 0:
        return AutostartResult(False, f"Creating the shortcuts failed: {(result.stderr or result.stdout).strip()}")
    names = " and ".join(f'"{os.path.basename(spec.path)}"' for spec in specs)
    return AutostartResult(True, f"Created {names} in {directory}." + _options_note(options))


# ----- Linux (.desktop) -----


def _applications_folder(home: Optional[str] = None) -> str:
    return os.path.join(home or os.path.expanduser("~"), ".local", "share", "applications")


def desktop_entry_text(python: str, script: str, icon: str, workdir: str, options: tuple[str, ...] = ()) -> str:
    def quote(text: str) -> str:
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`") + '"'

    lines = [
        "[Desktop Entry]",
        "Type=Application",
        f"Name={APP_NAME}",
        "Comment=GitHub release downloader",
        _with_options(f"Exec={quote(python)} {quote(script)}".replace("%", "%%"), options),
        f"Path={workdir}",
        "Terminal=false",
        "Categories=Utility;Network;",
        "StartupWMClass=" + APP_NAME.lower(),
    ]
    if icon:
        lines.append(f"Icon={icon}")
    return "\n".join(lines) + "\n"


def _create_linux(
    directory: Optional[str], python: Optional[str], home: Optional[str], options: tuple[str, ...] = ()
) -> AutostartResult:
    folder = directory or _applications_folder(home)
    png = icon_path("png")
    text = desktop_entry_text(
        python or sys.executable, gui_script_path(), png if os.path.isfile(png) else "", _APP_DIR, options
    )
    path = os.path.join(folder, DESKTOP_FILE)
    try:
        os.makedirs(folder, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as file:
            file.write(text)
        os.chmod(path, 0o755)
    except OSError as exc:
        return AutostartResult(False, f"Could not write {path}: {exc}")
    return AutostartResult(True, f'Created "{DESKTOP_FILE}" in {folder}; "{APP_NAME}" now shows in your application menu.' + _options_note(options))
