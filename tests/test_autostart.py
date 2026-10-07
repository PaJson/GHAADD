"""Autostart at login: the systemd unit text, the Windows Run entry and the install/uninstall/status flow.

Everything runs against a temp home folder, a fake systemctl and a fake registry; nothing real changes.
Run from the project root: python -m unittest discover -s tests -t .
"""
import os
import plistlib
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from modules import autostart, cli_commands


class FakeSystemctl:
    def __init__(self, fail_on: str = "", enabled: str = "enabled", active: str = "active") -> None:
        self.calls: list[list[str]] = []
        self.fail_on = fail_on
        self.enabled, self.active = enabled, active

    def __call__(self, command: list[str]) -> "subprocess.CompletedProcess[str]":
        self.calls.append(command)
        verb = command[2]
        if verb == self.fail_on:
            return subprocess.CompletedProcess(command, 1, "", "Failed to connect to bus")
        output = {"is-enabled": self.enabled, "is-active": self.active}.get(verb, "")
        return subprocess.CompletedProcess(command, 0, output + "\n", "")


class FakeSchtasks:
    """Pretends to be schtasks.exe: remembers the one task and the XML it was created from."""

    def __init__(self, fail_create: bool = False) -> None:
        self.installed = False
        self.xml = ""
        self.calls: list[list[str]] = []
        self.fail_create = fail_create

    def __call__(self, command: list[str]) -> "subprocess.CompletedProcess[str]":
        self.calls.append(command)
        verb = command[1]
        if verb == "/Create":
            if self.fail_create:
                return subprocess.CompletedProcess(command, 1, "", "ERROR: Access is denied.")
            with open(command[command.index("/XML") + 1], encoding="utf-16") as handle:
                self.xml = handle.read()
            self.installed = True
        elif verb == "/Delete":
            self.installed = False
        elif verb == "/Query" and not self.installed:
            return subprocess.CompletedProcess(command, 1, "", "ERROR: The system cannot find the file specified.")
        return subprocess.CompletedProcess(command, 0, "", "")


class FakeRegistry:
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ, KEY_WRITE, REG_SZ = 1, 2, 1

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def _handle(self):
        values = self.values

        class Handle:
            def __enter__(self):
                return values

            def __exit__(self, *_exc):
                return False

        return Handle()

    def CreateKeyEx(self, _root, path, _reserved, _access):
        assert path == autostart.WINDOWS_RUN_KEY
        return self._handle()

    def OpenKey(self, _root, path, _reserved, _access):
        assert path == autostart.WINDOWS_RUN_KEY
        return self._handle()

    def SetValueEx(self, key, name, _reserved, _kind, value):
        key[name] = value

    def QueryValueEx(self, key, name):
        if name not in key:
            raise FileNotFoundError(name)
        return key[name], self.REG_SZ

    def DeleteValue(self, key, name):
        if name not in key:
            raise FileNotFoundError(name)
        del key[name]


class UnitTextTests(unittest.TestCase):
    def test_the_unit_runs_the_poll_command_and_restarts_on_failure(self) -> None:
        text = autostart.systemd_unit_text("/usr/bin/python3", "/opt/ghaadd/main.py", "/opt/ghaadd")
        self.assertIn('ExecStart="/usr/bin/python3" "/opt/ghaadd/main.py" --daemon', text)
        self.assertIn('WorkingDirectory="/opt/ghaadd"', text)
        self.assertIn("Restart=on-failure", text)  # a clean exit (the GUI's Stop) is not restarted
        self.assertIn("WantedBy=default.target", text)

    def test_paths_with_spaces_and_percent_signs_are_quoted(self) -> None:
        text = autostart.systemd_unit_text("/home/me/my env/bin/python", "/home/me/100%/main.py", "/home/me/100%")
        self.assertIn('"/home/me/my env/bin/python"', text)
        self.assertIn("100%%", text)

    def test_the_windows_command_uses_the_windowless_python(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            python = os.path.join(folder, "python.exe")
            windowless = os.path.join(folder, "pythonw.exe")
            for path in (python, windowless):
                open(path, "w").close()
            command = autostart.windows_run_command(python, "C:\\app\\main.py")
        self.assertEqual(command, f'"{windowless}" "C:\\app\\main.py" --daemon-detached')

    def test_without_pythonw_the_interpreter_itself_is_used(self) -> None:
        self.assertEqual(autostart.windowless_python(os.path.join("nowhere", "python.exe")), os.path.join("nowhere", "python.exe"))


class LinuxTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._temp.cleanup)
        self.home = self._temp.name

    def test_install_writes_the_unit_and_enables_it(self) -> None:
        systemctl = FakeSystemctl()
        result = autostart.install_autostart(systemctl, platform="linux", home=self.home, python="/usr/bin/python3")
        self.assertTrue(result.ok, result.message)
        path = autostart.unit_path(self.home)
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding="utf-8", newline="") as handle:
            self.assertNotIn("\r", handle.read())  # LF only
        self.assertEqual([call[2] for call in systemctl.calls], ["daemon-reload", "enable"])
        self.assertIn("--now", systemctl.calls[1])
        self.assertIn("enable-linger", result.message)

    def test_install_reports_a_missing_user_systemd(self) -> None:
        result = autostart.install_autostart(FakeSystemctl(fail_on="daemon-reload"), platform="linux", home=self.home)
        self.assertFalse(result.ok)
        self.assertIn("systemd is not available", result.message)

    def test_install_reports_a_failed_enable(self) -> None:
        result = autostart.install_autostart(FakeSystemctl(fail_on="enable"), platform="linux", home=self.home)
        self.assertFalse(result.ok)
        self.assertIn("enabling it failed", result.message)

    def test_a_missing_systemctl_is_reported_not_raised(self) -> None:
        def missing(_command):
            raise FileNotFoundError("systemctl")

        result = autostart.install_autostart(missing, platform="linux", home=self.home)
        self.assertFalse(result.ok)

    def test_uninstall_disables_and_removes_the_unit(self) -> None:
        autostart.install_autostart(FakeSystemctl(), platform="linux", home=self.home)
        systemctl = FakeSystemctl()
        result = autostart.uninstall_autostart(systemctl, platform="linux", home=self.home)
        self.assertTrue(result.ok, result.message)
        self.assertFalse(os.path.exists(autostart.unit_path(self.home)))
        self.assertEqual([call[2] for call in systemctl.calls], ["disable", "daemon-reload"])

    def test_uninstall_when_nothing_is_installed_is_fine(self) -> None:
        systemctl = FakeSystemctl()
        result = autostart.uninstall_autostart(systemctl, platform="linux", home=self.home)
        self.assertTrue(result.ok)
        self.assertEqual(systemctl.calls, [])

    def test_status(self) -> None:
        self.assertEqual(autostart.autostart_status(FakeSystemctl(), platform="linux", home=self.home).installed, False)
        autostart.install_autostart(FakeSystemctl(), platform="linux", home=self.home)
        status = autostart.autostart_status(FakeSystemctl(), platform="linux", home=self.home)
        self.assertTrue(status.installed)
        self.assertIn("enabled, active", status.detail)
        disabled = autostart.autostart_status(FakeSystemctl(enabled="disabled", active="inactive"), platform="linux", home=self.home)
        self.assertFalse(disabled.installed)


class WindowsTests(unittest.TestCase):
    def test_install_status_uninstall(self) -> None:
        registry, schtasks = FakeRegistry(), FakeSchtasks()
        self.assertFalse(autostart.autostart_status(schtasks, registry, "win32").installed)
        result = autostart.install_autostart(schtasks, registry, "win32", python="C:\\py\\python.exe", mode="runkey")
        self.assertTrue(result.ok, result.message)
        self.assertIn("--daemon-detached", registry.values[autostart.RUN_VALUE_NAME])
        self.assertFalse(schtasks.installed)  # runkey mode touches no task
        status = autostart.autostart_status(schtasks, registry, "win32")
        self.assertTrue(status.installed)
        self.assertTrue(autostart.uninstall_autostart(schtasks, registry, "win32").ok)
        self.assertEqual(registry.values, {})
        self.assertIn("not installed", autostart.uninstall_autostart(schtasks, registry, "win32").message)

    def test_install_twice_keeps_one_entry(self) -> None:
        registry, schtasks = FakeRegistry(), FakeSchtasks()
        autostart.install_autostart(schtasks, registry, "win32", mode="runkey")
        autostart.install_autostart(schtasks, registry, "win32", mode="runkey")
        self.assertEqual(list(registry.values), [autostart.RUN_VALUE_NAME])


class TaskSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry, self.schtasks = FakeRegistry(), FakeSchtasks()

    def install(self, trigger: str = "logon"):
        return autostart.install_autostart(
            self.schtasks, self.registry, "win32", python="C:\\py\\python.exe", mode="task", trigger=trigger
        )

    def test_the_task_runs_the_daemon_itself_and_restarts_after_a_failure(self) -> None:
        result = self.install()
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.registry.values, {})  # no Run key in task mode
        root = ET.fromstring(self.schtasks.xml.split("?>", 1)[1])  # well-formed XML
        text = self.schtasks.xml
        self.assertIn("--daemon", text)
        self.assertNotIn("--daemon-detached", text)  # a launcher that exits at once could not be restarted
        self.assertIn("<RestartOnFailure>", text)
        self.assertIn("<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>", text)  # never stopped for running long
        self.assertIn("<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>", text)
        self.assertIn("<LogonType>InteractiveToken</LogonType>", text)  # no stored password, no admin rights
        self.assertEqual(root.tag.rsplit("}", 1)[-1], "Task")

    def test_the_logon_trigger_is_optional(self) -> None:
        self.install("logon")
        self.assertIn("<LogonTrigger>", self.schtasks.xml)
        self.install("manual")
        self.assertNotIn("<Triggers>", self.schtasks.xml)
        self.assertIn("only when you start it", self.install("manual").message)

    def test_a_refused_task_is_reported(self) -> None:
        result = autostart.install_autostart(
            FakeSchtasks(fail_create=True), self.registry, "win32", mode="task"
        )
        self.assertFalse(result.ok)
        self.assertIn("Access is denied", result.message)

    def test_status_and_uninstall_cover_both_kinds(self) -> None:
        self.install()
        autostart.install_autostart(self.schtasks, self.registry, "win32", mode="runkey")  # and the Run key as well
        status = autostart.autostart_status(self.schtasks, self.registry, "win32")
        self.assertTrue(status.installed)
        self.assertIn("Task Scheduler: task", status.detail)
        self.assertIn("Run key: installed", status.detail)
        result = autostart.uninstall_autostart(self.schtasks, self.registry, "win32")
        self.assertTrue(result.ok, result.message)
        self.assertFalse(self.schtasks.installed)
        self.assertEqual(self.registry.values, {})
        self.assertFalse(autostart.autostart_status(self.schtasks, self.registry, "win32").installed)

    def test_the_user_and_paths_are_escaped_in_the_xml(self) -> None:
        text = autostart.task_xml("C:\\a&b\\pythonw.exe", '"C:\\a&b\\main.py" --poll', "C:\\a&b", "PC\\R&D")
        ET.fromstring(text.split("?>", 1)[1])
        self.assertIn("a&amp;b", text)

    def test_auto_mode_uses_the_task_and_falls_back_to_the_run_key(self) -> None:
        result = autostart.install_autostart(self.schtasks, self.registry, "win32")
        self.assertTrue(result.ok, result.message)
        self.assertTrue(self.schtasks.installed)
        self.assertEqual(self.registry.values, {})
        registry, refusing = FakeRegistry(), FakeSchtasks(fail_create=True)
        result = autostart.install_autostart(refusing, registry, "win32")
        self.assertTrue(result.ok, result.message)
        self.assertIn("Falling back to the Run key", result.message)
        self.assertIn(autostart.RUN_VALUE_NAME, registry.values)
        strict = autostart.install_autostart(FakeSchtasks(fail_create=True), FakeRegistry(), "win32", mode="task")
        self.assertFalse(strict.ok)  # an explicit task request does not quietly become something else

    def test_task_mode_is_not_for_linux_and_bad_choices_are_refused(self) -> None:
        self.assertFalse(autostart.install_autostart(platform="linux", mode="task").ok)
        self.assertFalse(autostart.install_autostart(platform="win32", mode="service").ok)
        self.assertFalse(autostart.install_autostart(platform="win32", mode="task", trigger="boot").ok)


class MacTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._temp.cleanup)
        self.home = self._temp.name
        self.calls: list[list[str]] = []

    def runner(self, command: list[str]) -> "subprocess.CompletedProcess[str]":
        self.calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    def test_the_plist_starts_at_login_and_restarts_only_after_a_failure(self) -> None:
        data = plistlib.loads(autostart.launchd_plist_bytes("/usr/bin/python3", "/opt/g/main.py", "/opt/g"))
        self.assertEqual(data["ProgramArguments"], ["/usr/bin/python3", "/opt/g/main.py", "--daemon"])
        self.assertTrue(data["RunAtLoad"])
        self.assertEqual(data["KeepAlive"], {"SuccessfulExit": False})  # a clean Stop stays stopped
        self.assertEqual(data["Label"], autostart.LAUNCHD_LABEL)

    def test_install_status_uninstall(self) -> None:
        result = autostart.install_autostart(self.runner, platform="darwin", home=self.home, uid=501)
        self.assertTrue(result.ok, result.message)
        self.assertTrue(os.path.isfile(autostart.plist_path(self.home)))
        self.assertEqual(self.calls[-1], ["launchctl", "bootstrap", "gui/501", autostart.plist_path(self.home)])
        status = autostart.autostart_status(self.runner, platform="darwin", home=self.home, uid=501)
        self.assertTrue(status.installed)
        self.assertIn("loaded", status.detail)
        self.assertTrue(autostart.uninstall_autostart(self.runner, platform="darwin", home=self.home, uid=501).ok)
        self.assertFalse(os.path.exists(autostart.plist_path(self.home)))
        self.assertFalse(autostart.autostart_status(self.runner, platform="darwin", home=self.home, uid=501).installed)

    def test_a_failed_load_is_reported(self) -> None:
        def failing(command):
            return subprocess.CompletedProcess(command, 5 if command[1] == "bootstrap" else 0, "", "Bootstrap failed: 5")

        result = autostart.install_autostart(failing, platform="darwin", home=self.home, uid=501)
        self.assertFalse(result.ok)
        self.assertIn("loading it failed", result.message)

    def test_windows_choices_are_refused_on_a_mac(self) -> None:
        self.assertFalse(autostart.install_autostart(self.runner, platform="darwin", home=self.home, mode="task").ok)


class HiddenRunTests(unittest.TestCase):
    def test_helper_programs_get_no_console_window_on_windows(self) -> None:
        with mock.patch.object(autostart.sys, "platform", "win32"), mock.patch.object(autostart.subprocess, "run") as run:
            autostart.run_hidden(["schtasks", "/Query"])
        self.assertEqual(run.call_args.kwargs["creationflags"], autostart.CREATE_NO_WINDOW)

    def test_other_systems_use_no_special_flags(self) -> None:
        with mock.patch.object(autostart.sys, "platform", "linux"), mock.patch.object(autostart.subprocess, "run") as run:
            autostart.run_hidden(["systemctl", "--user", "is-active", "x"])
        self.assertEqual(run.call_args.kwargs["creationflags"], 0)


class SupportTests(unittest.TestCase):
    def test_support_is_known_without_probing_the_system(self) -> None:
        for system in ("win32", "linux", "linux2", "darwin"):
            self.assertTrue(autostart.is_supported(system), system)
        self.assertFalse(autostart.is_supported("freebsd13"))


class OtherSystemTests(unittest.TestCase):
    def test_an_unsupported_system_says_so(self) -> None:
        self.assertFalse(autostart.install_autostart(platform="freebsd13").ok)
        self.assertFalse(autostart.uninstall_autostart(platform="freebsd13").ok)
        status = autostart.autostart_status(platform="freebsd13")
        self.assertFalse(status.supported)


class CommandLineTests(unittest.TestCase):
    def test_the_flags_are_known(self) -> None:
        for flag, name in (
            ("--install-autostart", "install_autostart"),
            ("--uninstall-autostart", "uninstall_autostart"),
            ("--autostart-status", "autostart_status"),
            ("--daemon-detached", "daemon_detached"),
        ):
            parsed = cli_commands.parse_cli_args([flag], "x")
            self.assertTrue(getattr(parsed, name), flag)

    def test_daemon_detached_does_nothing_while_a_daemon_runs(self) -> None:
        parsed = cli_commands.parse_cli_args(["--daemon-detached"], "x")
        with mock.patch.object(cli_commands, "is_daemon_running", lambda: True), mock.patch(
            "modules.daemon_launcher.start_daemon"
        ) as start:
            self.assertTrue(cli_commands.handle_cli_command(parsed, lambda: None))
        start.assert_not_called()

    def test_daemon_detached_starts_one_when_none_runs(self) -> None:
        parsed = cli_commands.parse_cli_args(["--daemon-detached"], "x")
        with mock.patch.object(cli_commands, "is_daemon_running", lambda: False), mock.patch(
            "modules.daemon_launcher.start_daemon", return_value=4242
        ) as start:
            self.assertTrue(cli_commands.handle_cli_command(parsed, lambda: None))
        start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
