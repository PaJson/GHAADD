"""Tray support: what gets announced, the icon's state, the config switches and the failed-job feed.

No window and no real tray icon is created; the picture test is skipped when Pillow is not installed.
Run from the project root: python -m unittest discover -s tests -t .
"""
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from typing import Optional
from unittest import mock

from modules import config_manager, db_manager, gui_data, gui_tray


def row(repo: str, message: str) -> SimpleNamespace:
    return SimpleNamespace(repo=repo, message=message)


def text_of(notice: Optional[gui_tray.Notice]) -> str:
    """The notice's message; a missing notice fails the test right here instead of at an attribute access."""
    assert notice is not None, "a notice was expected"
    return notice.message


class NoticeTests(unittest.TestCase):
    def test_nothing_new_gives_no_notice(self) -> None:
        for make in (gui_tray.warnings_notice, gui_tray.failed_jobs_notice, gui_tray.unmapped_notice, gui_tray.limit_notice):
            self.assertIsNone(make([]))

    def test_one_warning_shows_its_text(self) -> None:
        notice = gui_tray.warnings_notice([row("o/app", "Sanity check: 3 files, was 5")])
        self.assertIn("o/app", text_of(notice))
        self.assertIn("Sanity check", text_of(notice))

    def test_several_warnings_are_counted_and_name_a_few_repositories(self) -> None:
        rows = [row(f"o/app{i}", "x") for i in range(5)]
        notice = gui_tray.warnings_notice(rows)
        self.assertIn("5 new warnings", text_of(notice))
        self.assertIn("(+2 more)", text_of(notice))  # three are named, two are not

    def test_a_failed_job_names_repository_and_tag(self) -> None:
        notice = gui_tray.failed_jobs_notice([{"repo": "o/app", "tag": "v2"}])
        self.assertIn("o/app v2", text_of(notice))
        self.assertIn("2 jobs failed", text_of(gui_tray.failed_jobs_notice([{"repo": "a/x"}, {"repo": "b/y"}])))

    def test_unmapped_and_limit_notices(self) -> None:
        self.assertIn("o/new", text_of(gui_tray.unmapped_notice(["o/new"])))
        self.assertIn("2 repositories", text_of(gui_tray.unmapped_notice(["a/x", "b/y"])))
        self.assertIn("o/app", text_of(gui_tray.limit_notice(["o/app"])))
        self.assertIn("2 repositories", text_of(gui_tray.limit_notice(["a/x", "b/y"])))

    def test_a_stopped_daemon_notice_says_nothing_is_downloaded(self) -> None:
        self.assertIn("Nothing is downloaded", text_of(gui_tray.daemon_stopped_notice()))


class NewItemTrackerTests(unittest.TestCase):
    def test_what_is_there_at_the_first_look_is_not_news(self) -> None:
        tracker = gui_tray.NewItemTracker()
        self.assertEqual(tracker.new(["a/x", "b/y"]), [])

    def test_later_additions_are_reported_once(self) -> None:
        tracker = gui_tray.NewItemTracker()
        tracker.new(["a/x"])
        self.assertEqual(tracker.new(["a/x", "c/z"]), ["c/z"])
        self.assertEqual(tracker.new(["a/x", "c/z"]), [])

    def test_an_item_that_goes_away_and_returns_is_news_again(self) -> None:
        tracker = gui_tray.NewItemTracker()
        tracker.new(["a/x"])
        tracker.new([])
        self.assertEqual(tracker.new(["a/x"]), ["a/x"])


class IconStateTests(unittest.TestCase):
    def test_state_follows_the_daemon(self) -> None:
        self.assertEqual(gui_tray.icon_state(False, False), gui_tray.STATE_STOPPED)
        self.assertEqual(gui_tray.icon_state(False, True), gui_tray.STATE_STOPPED)  # paused means nothing without a daemon
        self.assertEqual(gui_tray.icon_state(True, True), gui_tray.STATE_PAUSED)
        self.assertEqual(gui_tray.icon_state(True, False), gui_tray.STATE_RUNNING)

    def test_attention_needs_something_unread(self) -> None:
        self.assertFalse(gui_tray.needs_attention(0, 0, 0))
        self.assertTrue(gui_tray.needs_attention(1, 0))
        self.assertTrue(gui_tray.needs_attention(0, 2))
        self.assertTrue(gui_tray.needs_attention(0, 0, 1))

    def test_tooltip_texts(self) -> None:
        self.assertEqual(gui_tray.tooltip_text("GHAADD", gui_tray.STATE_RUNNING, 0, 0), "GHAADD: running")
        self.assertEqual(gui_tray.tooltip_text("GHAADD", gui_tray.STATE_STOPPED, 0, 0), "GHAADD: daemon not running")
        text = gui_tray.tooltip_text("GHAADD", gui_tray.STATE_PAUSED, 1, 3)
        self.assertEqual(text, "GHAADD: paused, 1 unread warning, 3 unmapped")
        self.assertIn("2 unread warnings", gui_tray.tooltip_text("GHAADD", gui_tray.STATE_RUNNING, 2, 0))

    @unittest.skipUnless(gui_tray.tray_available(), "pystray and Pillow are not installed")
    def test_every_state_can_be_drawn(self) -> None:
        for state in (gui_tray.STATE_RUNNING, gui_tray.STATE_PAUSED, gui_tray.STATE_STOPPED):
            for attention in (False, True):
                image = gui_tray.draw_icon(state, attention)
                self.assertEqual(image.size, (64, 64))

    @unittest.skipUnless(gui_tray.tray_available(), "pystray and Pillow are not installed")
    def test_a_missing_icon_file_falls_back_to_the_drawn_icon(self) -> None:
        image = gui_tray.draw_icon(gui_tray.STATE_RUNNING, False, base_image_path=os.path.join("no", "such.png"))
        self.assertEqual(image.size, (64, 64))


class TrayIconWithoutTrayTests(unittest.TestCase):
    def test_an_icon_that_was_never_started_ignores_everything(self) -> None:
        icon = gui_tray.TrayIcon("GHAADD", lambda: "Pause")
        self.assertFalse(icon.active)
        icon.update(gui_tray.STATE_RUNNING, True, "tip")
        icon.notify(gui_tray.Notice("t", "m"))
        icon.stop()
        self.assertEqual(icon.pending_actions(), [])

    def test_a_missing_tray_library_means_no_tray(self) -> None:
        with mock.patch.dict(sys.modules, {"pystray": None}):
            self.assertFalse(gui_tray.tray_available())
            self.assertFalse(gui_tray.TrayIcon("GHAADD", lambda: "Pause").start())


class FakeRegistry:
    """Just enough of winreg to see what would be written."""

    HKEY_CURRENT_USER = "HKCU"
    KEY_READ, KEY_WRITE, REG_SZ = 1, 2, 1

    def __init__(self, fail: bool = False) -> None:
        self.keys: dict[str, dict[str, str]] = {}
        self.writes = 0
        self.fail = fail

    def CreateKeyEx(self, root, path, _reserved, _access):
        if self.fail:
            raise OSError("denied")
        values = self.keys.setdefault(path, {})
        registry = self

        class Handle:
            def __enter__(self):
                return (path, values)

            def __exit__(self, *_exc):
                return False

        return Handle()

    def QueryValueEx(self, key, name):
        if name not in key[1]:
            raise OSError("missing")
        return key[1][name], self.REG_SZ

    def SetValueEx(self, key, name, _reserved, _kind, value):
        key[1][name] = value
        self.writes += 1


class WindowsIdentityTests(unittest.TestCase):
    def test_name_and_icon_are_registered_once(self) -> None:
        registry = FakeRegistry()
        icon = os.path.abspath(__file__)  # any file that exists
        self.assertTrue(gui_tray.register_windows_identity("GHAADD.GUI", "GHAADD", icon, registry))
        (path, values), = registry.keys.items()
        self.assertTrue(path.endswith("AppUserModelId\\GHAADD.GUI"))
        self.assertEqual(values, {"DisplayName": "GHAADD", "IconUri": icon})
        writes = registry.writes
        gui_tray.register_windows_identity("GHAADD.GUI", "GHAADD", icon, registry)
        self.assertEqual(registry.writes, writes)  # unchanged values are not written again

    def test_a_missing_icon_file_only_sets_the_name(self) -> None:
        registry = FakeRegistry()
        gui_tray.register_windows_identity("GHAADD.GUI", "GHAADD", os.path.join("no", "such.ico"), registry)
        self.assertEqual(list(registry.keys.values()), [{"DisplayName": "GHAADD"}])

    def test_a_registry_that_refuses_is_not_an_error(self) -> None:
        self.assertFalse(gui_tray.register_windows_identity("GHAADD.GUI", "GHAADD", "", FakeRegistry(fail=True)))


class TraySettingsTests(unittest.TestCase):
    def test_defaults(self) -> None:
        self.assertTrue(config_manager.get_gui_tray_enabled({}))
        self.assertTrue(config_manager.get_gui_notifications_enabled({}))
        self.assertFalse(config_manager.get_gui_close_to_tray({}))

    def test_minimize_default_depends_on_the_platform(self) -> None:
        with mock.patch.object(sys, "platform", "win32"):
            self.assertTrue(config_manager.get_gui_minimize_to_tray({}))
        with mock.patch.object(sys, "platform", "linux"):
            self.assertFalse(config_manager.get_gui_minimize_to_tray({}))
            self.assertTrue(config_manager.get_gui_minimize_to_tray({"gui": {"minimize_to_tray": True}}))

    def test_configured_values_and_unusable_ones(self) -> None:
        config = {"gui": {"tray": False, "notifications": False, "close_to_tray": True}}
        self.assertFalse(config_manager.get_gui_tray_enabled(config))
        self.assertFalse(config_manager.get_gui_notifications_enabled(config))
        self.assertTrue(config_manager.get_gui_close_to_tray(config))
        junk = {"gui": {"tray": "no", "notifications": 0, "close_to_tray": None}}
        self.assertTrue(config_manager.get_gui_tray_enabled(junk))  # only a real true/false counts
        self.assertTrue(config_manager.get_gui_notifications_enabled(junk))
        self.assertFalse(config_manager.get_gui_close_to_tray(junk))

    def test_the_gui_section_does_not_restart_the_daemon(self) -> None:
        base = {"processing": {"default_limit": 10}}
        changed = {**base, "gui": {"tray": False}}
        self.assertEqual(
            config_manager.get_config_fingerprint(base), config_manager.get_config_fingerprint(changed)
        )


class FailedJobFeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._temp_dir.cleanup)
        db_path = os.path.join(self._temp_dir.name, "state.db")
        patcher = mock.patch.object(db_manager, "get_state_db_path", lambda: db_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.connection = db_manager.open_database()
        self.addCleanup(self.connection.close)

    def fail_job(self, repo: str, tag: str) -> int:
        job_id = db_manager.enqueue_job(self.connection, repo, tag, "Release", 0.0, None)
        db_manager.mark_job_failed(self.connection, job_id)
        self.connection.commit()
        return job_id

    def test_only_failed_jobs_after_the_cursor_are_returned(self) -> None:
        failed = self.fail_job("o/app", "v1")
        ok = db_manager.enqueue_job(self.connection, "o/ok", "v1", "Release", 0.0, None)
        self.connection.commit()
        found = db_manager.get_failed_jobs_since(self.connection, 0)
        self.assertEqual([job["id"] for job in found], [failed])
        self.assertNotIn(ok, [job["id"] for job in found])
        self.assertEqual(db_manager.get_failed_jobs_since(self.connection, 4_000_000_000), [])

    def test_the_feed_reports_a_failure_once(self) -> None:
        class Store:
            def load(self):
                return {}

            def save(self, seen):
                pass

        feed = gui_data.StatusFeed(store=Store())
        feed._failed_cursor = 0.0  # as if the GUI had started long ago
        failed = self.fail_job("o/app", "v1")
        feed.refresh()
        first = feed.take_failed_jobs()
        self.assertEqual([job["id"] for job in first], [failed])
        self.assertEqual(feed.take_failed_jobs(), [])
        feed._signature = None  # force another look at the database
        feed.refresh()
        self.assertEqual(feed.take_failed_jobs(), [])  # the same job is not announced again
        second = self.fail_job("o/other", "v3")
        feed._signature = None
        feed.refresh()
        self.assertEqual([job["id"] for job in feed.take_failed_jobs()], [second])


class ToastTests(unittest.TestCase):
    def test_the_script_uses_the_app_id_and_quotes_the_texts(self) -> None:
        script = gui_tray.windows_toast_script("GHAADD.GUI", "It's a title", "o/app: 3 <new> & files")
        self.assertIn("CreateToastNotifier('GHAADD.GUI')", script)
        self.assertIn("It''s a title", script)  # a single quote is doubled inside PowerShell quotes
        self.assertIn("CreateTextNode('o/app: 3 <new> & files')", script)  # text nodes, so no XML escaping is needed

    def test_success_and_failure_are_reported_without_raising(self) -> None:
        notice = gui_tray.Notice("t", "m")
        seen = []

        def runner(command):
            seen.append(command)
            return SimpleNamespace(returncode=0)

        self.assertTrue(gui_tray.send_windows_toast("GHAADD.GUI", notice, runner))
        self.assertIn("-EncodedCommand", seen[0])
        self.assertFalse(gui_tray.send_windows_toast("GHAADD.GUI", notice, lambda command: SimpleNamespace(returncode=1)))

        def broken(command):
            raise OSError("no powershell")

        self.assertFalse(gui_tray.send_windows_toast("GHAADD.GUI", notice, broken))

    def test_a_failed_toast_falls_back_to_the_tray_balloon(self) -> None:
        icon = gui_tray.TrayIcon("GHAADD", lambda: "Pause", toast_app_id="GHAADD.GUI")
        shown = []
        icon._icon = SimpleNamespace(notify=lambda message, title: shown.append((title, message)))
        with mock.patch.object(gui_tray, "send_windows_toast", return_value=False):
            icon._toast_or_balloon(gui_tray.Notice("t", "m"))
        self.assertEqual(shown, [("t", "m")])
        shown.clear()
        with mock.patch.object(gui_tray, "send_windows_toast", return_value=True):
            icon._toast_or_balloon(gui_tray.Notice("t", "m"))
        self.assertEqual(shown, [])


class MenuStateTests(unittest.TestCase):
    def test_nothing_to_poll_or_pause_without_a_daemon(self) -> None:
        state = gui_tray.MenuState(window_visible=True, daemon_running=False, daemon_paused=False)
        self.assertTrue(state.can_start)
        self.assertFalse(state.can_poll)
        self.assertFalse(state.can_pause)

    def test_a_running_daemon_can_be_polled_and_paused(self) -> None:
        state = gui_tray.MenuState(True, True, False)
        self.assertFalse(state.can_start)
        self.assertTrue(state.can_poll)
        self.assertTrue(state.can_pause)

    def test_a_paused_daemon_can_be_resumed_and_still_polled_once(self) -> None:
        state = gui_tray.MenuState(True, True, True)
        self.assertTrue(state.can_poll)
        self.assertTrue(state.can_pause)

    def test_the_first_entry_follows_the_window(self) -> None:
        self.assertEqual(gui_tray.toggle_label("GHAADD", True), "Hide GHAADD")
        self.assertEqual(gui_tray.toggle_label("GHAADD", False), "Show GHAADD")

    def test_setting_the_state_without_an_icon_is_harmless(self) -> None:
        gui_tray.TrayIcon("GHAADD", lambda: "Pause").set_menu_state(gui_tray.MenuState(False, True, False))


if __name__ == "__main__":
    unittest.main()
