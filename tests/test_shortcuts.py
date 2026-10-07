"""The GHAADD shortcuts: the PowerShell script, the Linux launcher and the create/remove flow.

A fake PowerShell stands in for the real one, so no shortcut is ever made outside a temp folder.
Run from the project root: python -m unittest discover -s tests -t .
"""
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from modules import app_info, cli_commands, shortcuts


class FakePowerShell:
    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.script = ""
        self.command: list[str] = []
        self.returncode, self.stderr = returncode, stderr

    def __call__(self, command: list[str]) -> "subprocess.CompletedProcess[str]":
        self.command = command
        with open(command[-1], encoding="utf-8-sig") as handle:
            self.script = handle.read()
        return subprocess.CompletedProcess(command, self.returncode, "", self.stderr)


class WindowsScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = os.path.join(tempfile.gettempdir(), "ghaadd shortcuts's test")

    def test_the_gui_shortcut_carries_the_apps_identity_and_the_daemon_one_does_not(self) -> None:
        gui, daemon = shortcuts.windows_specs(self.directory, "C:\\py\\python.exe")
        self.assertEqual(gui.app_id, app_info.APP_USER_MODEL_ID)
        self.assertEqual(daemon.app_id, "")
        self.assertTrue(gui.arguments.endswith('main_gui.py"'))
        self.assertTrue(daemon.arguments.endswith("--daemon-detached"))
        self.assertEqual(os.path.basename(gui.path), "GHAADD.lnk")
        self.assertEqual(os.path.basename(daemon.path), "GHAADD daemon.lnk")

    def test_the_script_sets_target_icon_and_identity_and_survives_quotes_in_paths(self) -> None:
        script = shortcuts.powershell_script(shortcuts.windows_specs(self.directory))
        self.assertIn("$link.TargetPath = '", script)
        self.assertIn("[GhaaddShortcut]::SetAppId(", script)
        self.assertIn(f"'{app_info.APP_USER_MODEL_ID}'", script)
        self.assertIn("shortcuts''s test", script)  # a single quote in a path is doubled inside PowerShell quotes
        self.assertEqual(script.count("SetAppId("), 2)  # the definition's call site plus one use: the GUI shortcut only
        self.assertIn("\r\n", script)

    def test_the_identity_is_set_once_per_shortcut_that_has_one(self) -> None:
        script = shortcuts.powershell_script(shortcuts.windows_specs(self.directory))
        self.assertEqual(script.count("[GhaaddShortcut]::SetAppId("), 1)

    def test_create_runs_the_script_in_the_chosen_folder(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            target = os.path.join(folder, "launchers")
            powershell = FakePowerShell()
            result = shortcuts.create_shortcuts(target, powershell, platform="win32")
            self.assertTrue(result.ok, result.message)
            self.assertTrue(os.path.isdir(target))
            self.assertIn("GHAADD.lnk", result.message)
            self.assertIn("-File", powershell.command)
            self.assertIn(target.replace("'", "''"), powershell.script)
            self.assertFalse(os.path.exists(powershell.command[-1]))  # the temporary script is removed

    def test_a_failing_powershell_is_reported(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            result = shortcuts.create_shortcuts(folder, FakePowerShell(1, "Add-Type failed"), platform="win32")
        self.assertFalse(result.ok)
        self.assertIn("Add-Type failed", result.message)

    def test_remove_deletes_only_our_two_files(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            for name in ("GHAADD.lnk", "GHAADD daemon.lnk", "Other.lnk"):
                open(os.path.join(folder, name), "w").close()
            result = shortcuts.remove_shortcuts(folder, platform="win32")
            self.assertTrue(result.ok, result.message)
            self.assertEqual(os.listdir(folder), ["Other.lnk"])
            self.assertIn("no shortcuts", shortcuts.remove_shortcuts(folder, platform="win32").message)

    def test_the_default_folder_is_the_per_user_start_menu(self) -> None:
        with mock.patch.dict(os.environ, {"APPDATA": "C:\\Users\\me\\AppData\\Roaming"}):
            self.assertTrue(shortcuts.start_menu_folder().endswith(os.path.join("Windows", "Start Menu", "Programs")))


class LinuxTests(unittest.TestCase):
    def test_the_launcher_text(self) -> None:
        text = shortcuts.desktop_entry_text("/usr/bin/python3", "/opt/g/main_gui.py", "/opt/g/assets/ghaadd.png", "/opt/g")
        self.assertIn("Name=GHAADD", text)
        self.assertIn('Exec="/usr/bin/python3" "/opt/g/main_gui.py"', text)
        self.assertIn("Icon=/opt/g/assets/ghaadd.png", text)
        self.assertIn("Terminal=false", text)

    def test_create_and_remove(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as home:
            result = shortcuts.create_shortcuts(platform="linux", home=home, python="/usr/bin/python3")
            self.assertTrue(result.ok, result.message)
            path = os.path.join(home, ".local", "share", "applications", "ghaadd.desktop")
            with open(path, encoding="utf-8", newline="") as handle:
                self.assertNotIn("\r", handle.read())
            self.assertTrue(shortcuts.remove_shortcuts(platform="linux", home=home).ok)
            self.assertFalse(os.path.exists(path))

    def test_other_systems_are_refused(self) -> None:
        self.assertFalse(shortcuts.create_shortcuts(platform="darwin").ok)
        self.assertFalse(shortcuts.remove_shortcuts(platform="darwin").ok)


class SettingsDialogSupportTests(unittest.TestCase):
    def test_which_systems_can_make_shortcuts(self) -> None:
        for system in ("win32", "linux", "linux2"):
            self.assertTrue(shortcuts.is_supported(system), system)
        for system in ("darwin", "freebsd13"):
            self.assertFalse(shortcuts.is_supported(system), system)

    def test_the_folder_the_picker_starts_in(self) -> None:
        with mock.patch.dict(os.environ, {"APPDATA": "C:\\Users\\me\\AppData\\Roaming"}):
            self.assertTrue(shortcuts.default_folder("win32").endswith(os.path.join("Start Menu", "Programs")))
        self.assertEqual(
            shortcuts.default_folder("linux", home="/home/me"),
            os.path.join("/home/me", ".local", "share", "applications"),
        )
        self.assertEqual(shortcuts.default_folder("darwin"), "")


class CommandLineTests(unittest.TestCase):
    def test_the_flags(self) -> None:
        parsed = cli_commands.parse_cli_args(["--create-shortcuts", "--shortcut-dir", "X"], "x")
        self.assertTrue(parsed.create_shortcuts)
        self.assertEqual(parsed.shortcut_dir, "X")
        self.assertTrue(cli_commands.parse_cli_args(["--remove-shortcuts"], "x").remove_shortcuts)

    def test_dispatch_passes_the_folder_on(self) -> None:
        parsed = cli_commands.parse_cli_args(["--create-shortcuts", "--shortcut-dir", "X"], "x")
        with mock.patch("modules.shortcuts.create_shortcuts", return_value=shortcuts.AutostartResult(True, "done")) as create:
            self.assertTrue(cli_commands.handle_cli_command(parsed, lambda: None))
        create.assert_called_once_with("X", options=())

    def test_the_gui_options_reach_the_shortcut(self) -> None:
        parsed = cli_commands.parse_cli_args(["--create-shortcuts", "--shortcut-minimized", "--shortcut-start-daemon"], "x")
        with mock.patch("modules.shortcuts.create_shortcuts", return_value=shortcuts.AutostartResult(True, "done")) as create:
            cli_commands.handle_cli_command(parsed, lambda: None)
        create.assert_called_once_with(None, options=("--minimized", "--start-daemon"))

    def test_the_gui_options_are_rejected_without_create_shortcuts(self) -> None:
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            cli_commands.parse_cli_args(["--shortcut-minimized"], "x")


class GuiOptionTests(unittest.TestCase):
    def test_only_the_ticked_choices_become_options(self) -> None:
        self.assertEqual(shortcuts.gui_options(), ())
        self.assertEqual(shortcuts.gui_options(minimized=True), ("--minimized",))
        self.assertEqual(shortcuts.gui_options(start_daemon=True), ("--start-daemon",))
        self.assertEqual(shortcuts.gui_options(True, True), ("--minimized", "--start-daemon"))

    def test_windows_only_the_gui_shortcut_gets_them(self) -> None:
        gui, daemon = shortcuts.windows_specs("C:\\x", "C:\\py\\python.exe", ("--minimized", "--start-daemon"))
        self.assertTrue(gui.arguments.endswith('main_gui.py" --minimized --start-daemon'))
        self.assertNotIn("--minimized", daemon.arguments)
        plain, _ = shortcuts.windows_specs("C:\\x", "C:\\py\\python.exe")
        self.assertTrue(plain.arguments.endswith('main_gui.py"'))  # none ticked: exactly as before

    def test_the_created_message_says_what_the_shortcut_does(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            with_options = shortcuts.create_shortcuts(folder, FakePowerShell(), platform="win32", options=("--minimized",))
            without = shortcuts.create_shortcuts(folder, FakePowerShell(), platform="win32")
        self.assertIn("starts with --minimized", with_options.message)
        self.assertNotIn("starts with", without.message)

    def test_linux_launcher_gets_them_on_the_exec_line(self) -> None:
        text = shortcuts.desktop_entry_text("/usr/bin/python3", "/opt/g/main_gui.py", "", "/opt/g", ("--minimized",))
        self.assertIn('Exec="/usr/bin/python3" "/opt/g/main_gui.py" --minimized', text)
        plain = shortcuts.desktop_entry_text("/usr/bin/python3", "/opt/g/main_gui.py", "", "/opt/g")
        self.assertIn('Exec="/usr/bin/python3" "/opt/g/main_gui.py"\n', plain)


if __name__ == "__main__":
    unittest.main()
