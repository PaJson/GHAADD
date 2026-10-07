"""Reading config.json: every setting has a safe default, junk values are replaced instead of crashing the daemon,
and the "restart needed" fingerprint changes only when a setting the daemon really uses changes.

No real config.json is read: every getter takes the config as an argument here.
Run from the project root: python -m unittest discover -s tests -t .
"""
import os
import unittest

from modules import config_manager as cm


class ConversionTests(unittest.TestCase):
    def test_booleans_accept_real_booleans_and_the_word_true(self) -> None:
        self.assertIs(cm._as_bool(True), True)
        self.assertIs(cm._as_bool(False, True), False)
        self.assertIs(cm._as_bool("True"), True)
        self.assertIs(cm._as_bool(" true "), True)
        self.assertIs(cm._as_bool("yes"), False)
        self.assertIs(cm._as_bool(None, True), True)  # unset: the default

    def test_integers_fall_back_to_the_default(self) -> None:
        self.assertEqual(cm._as_int("12", 5), 12)
        self.assertEqual(cm._as_int(7.9, 5), 7)
        self.assertEqual(cm._as_int("x", 5), 5)
        self.assertEqual(cm._as_int([], 5), 5)
        self.assertEqual(cm._as_int(None, 5), 5)

    def test_nested_lookup_survives_missing_or_wrong_sections(self) -> None:
        self.assertEqual(cm._get_nested({"a": {"b": 1}}, "a", "b"), 1)
        for config in ({}, {"a": 5}, {"a": []}, {"a": {"c": 1}}):
            self.assertIsNone(cm._get_nested(config, "a", "b"))


class NumberSettingTests(unittest.TestCase):
    def test_defaults_when_nothing_is_configured(self) -> None:
        self.assertEqual(cm.get_max_emails_to_process({}), 0)
        self.assertEqual(cm.get_destination_check_every_n_polls({}), 10)
        self.assertEqual(cm.get_recheck_intervals_minutes({}), cm.DEFAULT_RECHECK_INTERVALS_MINUTES)

    def test_negative_numbers_become_zero(self) -> None:
        self.assertEqual(cm.get_max_emails_to_process({"processing": {"max_emails_to_process": -4}}), 0)
        self.assertEqual(cm.get_destination_check_every_n_polls({"processing": {"destination_check_every_n_polls": -1}}), 0)

    def test_text_numbers_are_understood_and_nonsense_gives_the_default(self) -> None:
        self.assertEqual(cm.get_max_emails_to_process({"processing": {"max_emails_to_process": "25"}}), 25)
        self.assertEqual(cm.get_max_emails_to_process({"processing": {"max_emails_to_process": "many"}}), 0)

    def test_recheck_intervals_are_cleaned(self) -> None:
        config = {"processing": {"recheck_intervals_minutes": [5, "15", 5, 0, -3, "x", None, 60]}}
        self.assertEqual(cm.get_recheck_intervals_minutes(config), [5, 15, 60])

    def test_unusable_recheck_intervals_give_the_defaults(self) -> None:
        for value in ("5, 15", [], [0, -1, "x"], {"a": 1}, 5, None):
            with self.subTest(value=value):
                config = {"processing": {"recheck_intervals_minutes": value}}
                self.assertEqual(cm.get_recheck_intervals_minutes(config), cm.DEFAULT_RECHECK_INTERVALS_MINUTES)

    def test_the_defaults_cannot_be_changed_through_the_returned_list(self) -> None:
        cm.get_recheck_intervals_minutes({}).append(999)
        self.assertNotIn(999, cm.DEFAULT_RECHECK_INTERVALS_MINUTES)
        self.assertNotIn(999, cm.get_recheck_intervals_minutes({}))

    def test_state_persistence_is_on_unless_disabled(self) -> None:
        self.assertIs(cm.is_state_persistence_disabled({}), False)
        self.assertIs(cm.is_state_persistence_disabled({"state": {"disable_state_persistence": True}}), True)
        self.assertIs(cm.is_state_persistence_disabled({"state": {"disable_state_persistence": "false"}}), False)


class PollingTests(unittest.TestCase):
    def test_defaults(self) -> None:
        self.assertEqual(
            cm.get_polling_settings({}),
            {"enabled": False, "interval_seconds": 300, "jitter_min_seconds": 5, "jitter_max_seconds": 30},
        )

    def test_values_and_negative_clamping(self) -> None:
        config = {"polling": {"enabled": True, "interval_seconds": "120", "jitter_min_seconds": -5, "jitter_max_seconds": 9}}
        self.assertEqual(
            cm.get_polling_settings(config),
            {"enabled": True, "interval_seconds": 120, "jitter_min_seconds": 0, "jitter_max_seconds": 9},
        )


class FolderSettingTests(unittest.TestCase):
    def folders(self, **names):
        return cm.get_folder_settings({"folders": names})

    def test_defaults(self) -> None:
        self.assertEqual(
            cm.get_folder_settings({}),
            {"ghaadd_root": "GHAADD", "processing": "Processing", "complete": "Complete", "partial": "Partial", "logs": "Logs"},
        )

    def test_a_configured_name_is_used(self) -> None:
        self.assertEqual(self.folders(complete="Done")["complete"], "Done")

    def test_a_name_can_never_point_somewhere_else(self) -> None:
        # These names are joined to the download folder; they must stay a single, harmless folder name.
        self.assertEqual(self.folders(complete="a/b")["complete"], "a_b")
        self.assertEqual(self.folders(complete="a\\b")["complete"], "a_b")
        self.assertEqual(self.folders(complete="/Done/")["complete"], "Done")
        for empty in ("..", ".", "...", "", "   ", "/", "\\"):
            with self.subTest(name=empty):
                self.assertEqual(self.folders(complete=empty)["complete"], "Complete")
        for tricky in ("../..", "..\\..\\Windows", "a/../../b", "/etc/passwd"):  # whatever comes out is one plain folder name
            with self.subTest(name=tricky):
                result = self.folders(complete=tricky)["complete"]
                self.assertFalse(any(sep in result for sep in "/\\"), result)  # no separator: it cannot leave its folder

    def test_non_text_names_give_the_default(self) -> None:
        self.assertEqual(self.folders(complete=5)["complete"], "Complete")
        self.assertEqual(cm.get_folder_settings({"folders": "nope"})["complete"], "Complete")


class PathSettingTests(unittest.TestCase):
    def test_the_download_dir_is_used_as_written(self) -> None:
        self.assertEqual(cm.get_default_download_dir({"paths": {"default_download_dir": "/data/dl"}}), "/data/dl")

    def test_a_missing_download_dir_falls_back_to_the_users_downloads(self) -> None:
        expected = os.path.join(os.path.expanduser("~"), "Downloads")
        for config in ({}, {"paths": {"default_download_dir": ""}}, {"paths": {"default_download_dir": "  "}}, {"paths": {"default_download_dir": 5}}):
            with self.subTest(config=config):
                self.assertEqual(cm.get_default_download_dir(config), expected)

    @unittest.skipUnless(os.name == "nt", "drive-root shorthand only exists on Windows")
    def test_a_bare_drive_letter_means_the_drive_root(self) -> None:
        self.assertEqual(cm.get_default_download_dir({"paths": {"default_download_dir": "D:"}}), "D:\\")
        self.assertEqual(cm.get_all_download_dirs({"paths": {"default_download_dir": "D:"}}), ["D:\\"])

    def test_all_download_dirs_has_the_default_once(self) -> None:
        self.assertEqual(cm.get_all_download_dirs({"paths": {"default_download_dir": "/data/dl"}}), ["/data/dl"])
        self.assertEqual(cm.get_download_dir_for_release("o/app", "Release", {"paths": {"default_download_dir": "/data/dl"}}), "/data/dl")

    def test_the_mailbox_folder(self) -> None:
        self.assertEqual(cm.get_gmail_folder({}), "GitHubNotifications")
        self.assertEqual(cm.get_gmail_folder({"mailbox": {"folder": "  Releases "}}), "Releases")
        self.assertEqual(cm.get_gmail_folder({"mailbox": {"folder": "  "}}), "GitHubNotifications")
        self.assertEqual(cm.get_gmail_folder({"mailbox": {"folder": 5}}), "GitHubNotifications")


class TerminalLogTests(unittest.TestCase):
    def test_defaults(self) -> None:
        settings = cm.get_terminal_log_settings({"paths": {"default_download_dir": "/data/dl"}})
        self.assertEqual((settings["enabled"], settings["max_file_mb"], settings["keep_files"]), (False, 10, 30))
        self.assertEqual(settings["directory"], os.path.join("/data/dl", "GHAADD", "Logs"))

    def test_values_and_clamping(self) -> None:
        config = {"terminal_log": {"enabled": True, "max_file_mb": -1, "keep_files": "5"}, "folders": {"logs": "Transcripts"}}
        settings = cm.get_terminal_log_settings({"paths": {"default_download_dir": "/data/dl"}, **config})
        self.assertEqual((settings["enabled"], settings["max_file_mb"], settings["keep_files"]), (True, 0, 5))
        self.assertTrue(settings["directory"].endswith("Transcripts"))


class GuiTimingTests(unittest.TestCase):
    def refresh(self, value):
        return cm.get_gui_refresh_seconds({"gui": {"refresh_seconds": value}})

    def message(self, value):
        return cm.get_gui_status_message_seconds({"gui": {"status_message_seconds": value}})

    def test_defaults(self) -> None:
        self.assertEqual((cm.get_gui_refresh_seconds({}), cm.get_gui_status_message_seconds({})), (3.0, 6.0))
        self.assertEqual(cm.get_gui_refresh_seconds({"gui": {"window": {"width": 900}}}), 3.0)  # other gui keys do not matter

    def test_configured_values_are_used_as_numbers_or_text(self) -> None:
        self.assertEqual((self.refresh(10), self.message(12)), (10.0, 12.0))
        self.assertEqual((self.refresh(1.5), self.refresh("5"), self.message("2.5")), (1.5, 5.0, 2.5))

    def test_values_are_kept_inside_their_limits(self) -> None:
        self.assertEqual((self.refresh(0), self.refresh(-5), self.refresh(0.2)), (1.0, 1.0, 1.0))  # never a busy loop
        self.assertEqual((self.refresh(100000), self.message(100000)), (60.0, 60.0))
        self.assertEqual((self.message(0), self.message(1)), (2.0, 2.0))

    def test_unusable_values_give_the_defaults(self) -> None:
        for bad in ("fast", "", None, True, [3], {"a": 1}, float("nan")):
            with self.subTest(value=bad):
                self.assertEqual((self.refresh(bad), self.message(bad)), (3.0, 6.0))
        self.assertEqual(cm.get_gui_refresh_seconds({"gui": "text"}), 3.0)


class FingerprintTests(unittest.TestCase):
    BASE = {
        "polling": {"enabled": True, "interval_seconds": 300},
        "processing": {"recheck_intervals_minutes": [5, 15], "max_emails_to_process": 0},
        "paths": {"default_download_dir": "/data/dl"},
        "terminal_log": {"enabled": False},
    }

    def fingerprint(self, **sections) -> str:
        config = {**self.BASE}
        for key, value in sections.items():
            config[key] = {**config.get(key, {}), **value} if isinstance(value, dict) else value
        return cm.get_config_fingerprint(config)

    def test_it_is_stable(self) -> None:
        self.assertEqual(self.fingerprint(), self.fingerprint())
        self.assertEqual(len(self.fingerprint()), 16)

    def test_settings_the_daemon_uses_change_it(self) -> None:
        base = self.fingerprint()
        for change in (
            {"polling": {"interval_seconds": 120}},
            {"processing": {"recheck_intervals_minutes": [5, 15, 60]}},
            {"processing": {"max_emails_to_process": 3}},
            {"processing": {"destination_check_every_n_polls": 0}},
            {"paths": {"default_download_dir": "/other"}},
            {"terminal_log": {"enabled": True}},
            {"folders": {"complete": "Done"}},
            {"mailbox": {"folder": "Other"}},
            {"state": {"disable_state_persistence": True}},
        ):
            with self.subTest(change=change):
                self.assertNotEqual(self.fingerprint(**change), base)

    def test_things_the_daemon_ignores_do_not_change_it(self) -> None:
        base = self.fingerprint()
        self.assertEqual(self.fingerprint(gui={"window": {"width": 900}}), base)  # the GUI's remembered window size
        self.assertEqual(self.fingerprint(gui={"refresh_seconds": 10, "status_message_seconds": 20}), base)  # GUI timing
        self.assertEqual(self.fingerprint(**{"polling": {"interval_seconds": "300"}}), base)  # same value, written as text
        self.assertEqual(self.fingerprint(**{"processing": {"recheck_intervals_minutes": [5, 15, 5, 0]}}), base)  # same effective list
        self.assertEqual(self.fingerprint(**{"processing": {"destination_check_every_n_polls": 10}}), base)  # an explicit default
        self.assertEqual(self.fingerprint(**{"processing": {"default_limit": 20}}), base)  # read when an entry is created, no restart needed


if __name__ == "__main__":
    unittest.main()
