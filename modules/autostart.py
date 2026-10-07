"""Start the polling daemon automatically when the user logs in.

Linux: a systemd *user* unit (`~/.config/systemd/user/ghaadd.service`, no root needed). systemd also
restarts the daemon if it crashes; a normal stop (the GUI's Stop button) is respected.
Windows, two ways (`mode`): "task" (Task Scheduler: the task runs the daemon itself without a window and
restarts it after a failure; trigger "logon" or "manual" = no trigger, you start it with `schtasks /Run`) or
"runkey" (a per-user entry in the registry's Run key that starts the daemon through `main.py --daemon-detached`
at login). Neither needs admin rights.

Nothing here touches the daemon itself, `state.db`, `mapping.json` or `config.json`. The subprocess
runner, the registry module and the platform are parameters so the tests never change the real system.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Any, Callable, Optional
from xml.sax.saxutils import escape

SERVICE_NAME = "ghaadd.service"
RUN_VALUE_NAME = "GHAADD"
TASK_NAME = "GHAADD"
MODE_RUNKEY = "runkey"
MODE_TASK = "task"
TRIGGER_LOGON = "logon"
TRIGGER_MANUAL = "manual"
WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class AutostartResult:
    ok: bool
    message: str


@dataclass(frozen=True)
class AutostartStatus:
    supported: bool
    installed: bool
    detail: str


def _run(command: list[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)


def _platform(platform: Optional[str]) -> str:
    return platform if platform is not None else sys.platform


# ----- paths and texts (pure) -----


def main_script_path() -> str:
    return os.path.join(_APP_DIR, "main.py")


def windowless_python(executable: Optional[str] = None) -> str:
    """pythonw.exe next to python.exe when it exists (no console window), else the interpreter itself."""
    executable = executable or sys.executable
    directory, name = os.path.split(executable)
    if name.lower() == "python.exe":
        candidate = os.path.join(directory, "pythonw.exe")
        if os.path.isfile(candidate):
            return candidate
    return executable


def unit_path(home: Optional[str] = None) -> str:
    return os.path.join(home or os.path.expanduser("~"), ".config", "systemd", "user", SERVICE_NAME)


def _systemd_quote(text: str) -> str:
    """Quote one ExecStart word: double quotes around it, and % doubled (systemd expands %-specifiers)."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def systemd_unit_text(python: str, script: str, workdir: str) -> str:
    return (
        "[Unit]\n"
        "Description=GHAADD GitHub release downloader (polling daemon)\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={_systemd_quote(workdir)}\n"
        f"ExecStart={_systemd_quote(python)} {_systemd_quote(script)} --poll\n"
        "Environment=PYTHONIOENCODING=utf-8\n"
        "Restart=on-failure\n"
        "RestartSec=60\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def windows_run_command(python: Optional[str] = None, script: Optional[str] = None) -> str:
    return f'"{windowless_python(python)}" "{script or main_script_path()}" --daemon-detached'


# ----- install / uninstall / status -----


def install_autostart(
    runner: Runner = _run,
    registry: Any = None,
    platform: Optional[str] = None,
    home: Optional[str] = None,
    python: Optional[str] = None,
    mode: str = MODE_RUNKEY,
    trigger: str = TRIGGER_LOGON,
) -> AutostartResult:
    system = _platform(platform)
    if mode not in (MODE_RUNKEY, MODE_TASK) or trigger not in (TRIGGER_LOGON, TRIGGER_MANUAL):
        return AutostartResult(False, f"Unknown autostart mode or trigger ({mode}, {trigger}).")
    if system == "win32":
        if mode == MODE_TASK:
            return _install_task(runner, trigger, python)
        return _install_windows(registry, python)
    if system.startswith("linux"):
        if mode == MODE_TASK:
            return AutostartResult(False, "Task Scheduler is a Windows feature; on Linux the systemd user unit is used.")
        return _install_linux(runner, home, python)
    return AutostartResult(False, f"Autostart is not supported on this system ({system}).")


def uninstall_autostart(
    runner: Runner = _run, registry: Any = None, platform: Optional[str] = None, home: Optional[str] = None
) -> AutostartResult:
    system = _platform(platform)
    if system == "win32":
        task = _uninstall_task(runner)
        run_key = _uninstall_windows(registry)
        if not (task.ok and run_key.ok):
            return AutostartResult(False, " ".join(part.message for part in (task, run_key) if not part.ok))
        removed = [part.message for part in (task, run_key) if "not installed" not in part.message]
        return AutostartResult(True, " ".join(removed) if removed else "Autostart was not installed.")
    if system.startswith("linux"):
        return _uninstall_linux(runner, home)
    return AutostartResult(False, f"Autostart is not supported on this system ({system}).")


def autostart_status(
    runner: Runner = _run, registry: Any = None, platform: Optional[str] = None, home: Optional[str] = None
) -> AutostartStatus:
    system = _platform(platform)
    if system == "win32":
        task, run_key = _status_task(runner), _status_windows(registry)
        details = [f"{name}: {part.detail}" for name, part in (("Task Scheduler", task), ("Run key", run_key))]
        return AutostartStatus(task.supported or run_key.supported, task.installed or run_key.installed, "; ".join(details))
    if system.startswith("linux"):
        return _status_linux(runner, home)
    return AutostartStatus(False, False, f"not supported on {system}")


# ----- Linux (systemd user unit) -----


def _systemctl(runner: Runner, *arguments: str) -> "subprocess.CompletedProcess[str]":
    return runner(["systemctl", "--user", *arguments])


def _install_linux(runner: Runner, home: Optional[str], python: Optional[str]) -> AutostartResult:
    path = unit_path(home)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(systemd_unit_text(python or sys.executable, main_script_path(), _APP_DIR))
    except OSError as exc:
        return AutostartResult(False, f"Could not write {path}: {exc}")
    try:
        reload_result = _systemctl(runner, "daemon-reload")
        if reload_result.returncode != 0:
            return AutostartResult(False, f"Wrote {path}, but systemd is not available to this user: {_error_text(reload_result)}")
        enable_result = _systemctl(runner, "enable", "--now", SERVICE_NAME)
    except (OSError, subprocess.SubprocessError) as exc:
        return AutostartResult(False, f"Wrote {path}, but systemctl could not run: {exc}")
    if enable_result.returncode != 0:
        return AutostartResult(False, f"Wrote {path}, but enabling it failed: {_error_text(enable_result)}")
    return AutostartResult(
        True,
        f"Installed {path} and started the daemon. It now starts when you log in. "
        "To have it start at boot even without a login, run once: loginctl enable-linger $USER",
    )


def _uninstall_linux(runner: Runner, home: Optional[str]) -> AutostartResult:
    path = unit_path(home)
    if not os.path.isfile(path):
        return AutostartResult(True, "Autostart was not installed.")
    problems = []
    try:
        result = _systemctl(runner, "disable", "--now", SERVICE_NAME)
        if result.returncode != 0:
            problems.append(_error_text(result))
    except (OSError, subprocess.SubprocessError) as exc:
        problems.append(str(exc))
    try:
        os.remove(path)
    except OSError as exc:
        return AutostartResult(False, f"Could not remove {path}: {exc}")
    try:
        _systemctl(runner, "daemon-reload")
    except (OSError, subprocess.SubprocessError):
        pass
    note = f" (systemctl said: {'; '.join(problems)})" if problems else ""
    return AutostartResult(True, f"Removed {path}; the daemon no longer starts at login{note}.")


def _status_linux(runner: Runner, home: Optional[str]) -> AutostartStatus:
    path = unit_path(home)
    if not os.path.isfile(path):
        return AutostartStatus(True, False, "not installed")
    try:
        enabled = _systemctl(runner, "is-enabled", SERVICE_NAME).stdout.strip() or "unknown"
        active = _systemctl(runner, "is-active", SERVICE_NAME).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return AutostartStatus(True, True, f"unit file present ({path}); systemctl is not available")
    return AutostartStatus(True, enabled == "enabled", f"{enabled}, {active} ({path})")


def _error_text(result: "subprocess.CompletedProcess[str]") -> str:
    return (result.stderr or result.stdout or f"exit code {result.returncode}").strip()


# ----- Windows (Run key) -----


def _winreg(registry: Any) -> Any:
    if registry is not None:
        return registry
    import winreg

    return winreg


def _install_windows(registry: Any, python: Optional[str]) -> AutostartResult:
    command = windows_run_command(python)
    try:
        reg = _winreg(registry)
        with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY, 0, reg.KEY_WRITE) as key:
            reg.SetValueEx(key, RUN_VALUE_NAME, 0, reg.REG_SZ, command)
    except (OSError, ImportError) as exc:
        return AutostartResult(False, f"Could not write the Windows Run entry: {exc}")
    return AutostartResult(
        True,
        "Installed. The daemon starts (without a window) the next time you log in to Windows. "
        "To start it now, use the GUI's Start button or: python main.py --daemon-detached",
    )


def _uninstall_windows(registry: Any) -> AutostartResult:
    try:
        reg = _winreg(registry)
        with reg.OpenKey(reg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY, 0, reg.KEY_WRITE) as key:
            reg.DeleteValue(key, RUN_VALUE_NAME)
    except FileNotFoundError:
        return AutostartResult(True, "Autostart was not installed.")
    except (OSError, ImportError) as exc:
        return AutostartResult(False, f"Could not remove the Windows Run entry: {exc}")
    return AutostartResult(True, "Removed. The daemon no longer starts when you log in (a running daemon is not stopped).")


def _status_windows(registry: Any) -> AutostartStatus:
    try:
        reg = _winreg(registry)
        with reg.OpenKey(reg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY, 0, reg.KEY_READ) as key:
            value, _kind = reg.QueryValueEx(key, RUN_VALUE_NAME)
    except FileNotFoundError:
        return AutostartStatus(True, False, "not installed")
    except (OSError, ImportError) as exc:
        return AutostartStatus(False, False, f"cannot read the Windows Run key: {exc}")
    return AutostartStatus(True, True, f"installed ({value})")


# ----- Windows (Task Scheduler) -----


def task_xml(command: str, arguments: str, workdir: str, user: str, trigger: str = TRIGGER_LOGON) -> str:
    """The task definition: runs the daemon itself, restarts it after a failure (not after a clean stop)."""
    triggers = ""
    if trigger == TRIGGER_LOGON:
        triggers = (
            "  <Triggers>\n    <LogonTrigger>\n      <Enabled>true</Enabled>\n"
            f"      <UserId>{escape(user)}</UserId>\n    </LogonTrigger>\n  </Triggers>\n"
        )
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n    <Description>GHAADD polling daemon (GitHub release downloader)</Description>\n  </RegistrationInfo>\n"
        f"{triggers}"
        '  <Principals>\n    <Principal id="Author">\n'
        f"      <UserId>{escape(user)}</UserId>\n"
        "      <LogonType>InteractiveToken</LogonType>\n      <RunLevel>LeastPrivilege</RunLevel>\n"
        "    </Principal>\n  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
        "    <RestartOnFailure>\n      <Interval>PT1M</Interval>\n      <Count>999</Count>\n    </RestartOnFailure>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n    <Exec>\n'
        f"      <Command>{escape(command)}</Command>\n"
        f"      <Arguments>{escape(arguments)}</Arguments>\n"
        f"      <WorkingDirectory>{escape(workdir)}</WorkingDirectory>\n"
        "    </Exec>\n  </Actions>\n"
        "</Task>\n"
    )


def _windows_user() -> str:
    domain, name = os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME", "")
    return f"{domain}\\{name}" if domain and name else name


def _install_task(runner: Runner, trigger: str, python: Optional[str]) -> AutostartResult:
    xml = task_xml(windowless_python(python), f'"{main_script_path()}" --poll', _APP_DIR, _windows_user(), trigger)
    handle, path = tempfile.mkstemp(suffix=".xml", prefix="ghaadd_task_")
    try:
        with os.fdopen(handle, "w", encoding="utf-16", newline="") as file:  # schtasks wants UTF-16 with a BOM
            file.write(xml)
        result = runner(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", path, "/F"])
    except (OSError, subprocess.SubprocessError) as exc:
        return AutostartResult(False, f"Could not create the scheduled task: {exc}")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    if result.returncode != 0:
        return AutostartResult(False, f"Could not create the scheduled task: {_error_text(result)}")
    when = "the next time you log in to Windows" if trigger == TRIGGER_LOGON else "only when you start it"
    return AutostartResult(
        True,
        f'Scheduled task "{TASK_NAME}" created; it starts the daemon (without a window) {when}, and restarts it after a failure. '
        f"Start it now with: schtasks /Run /TN {TASK_NAME}  (or from Task Scheduler). A clean Stop is not restarted.",
    )


def _uninstall_task(runner: Runner) -> AutostartResult:
    try:
        if runner(["schtasks", "/Query", "/TN", TASK_NAME]).returncode != 0:
            return AutostartResult(True, "Scheduled task not installed.")
        result = runner(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    except (OSError, subprocess.SubprocessError) as exc:
        return AutostartResult(False, f"Could not remove the scheduled task: {exc}")
    if result.returncode != 0:
        return AutostartResult(False, f"Could not remove the scheduled task: {_error_text(result)}")
    return AutostartResult(True, f'Scheduled task "{TASK_NAME}" removed (a running daemon is left running).')


def _status_task(runner: Runner) -> AutostartStatus:
    try:
        result = runner(["schtasks", "/Query", "/TN", TASK_NAME])
    except (OSError, subprocess.SubprocessError):
        return AutostartStatus(False, False, "schtasks is not available")
    if result.returncode != 0:
        return AutostartStatus(True, False, "not installed")
    return AutostartStatus(True, True, f'task "{TASK_NAME}" installed')
