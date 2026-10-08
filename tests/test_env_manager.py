"""Writing the credentials to .env from the GUI (modules/env_manager.py): nothing else in the file may be lost.

Everything happens in a temporary folder. Run from the project root: python -m unittest discover -s tests -t .
"""
import os
import stat
import tempfile
import unittest
from unittest import mock

from dotenv import dotenv_values

from modules import env_manager


class EnvTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._temp_dir.cleanup)
        self.path = os.path.join(self._temp_dir.name, ".env")
        patcher = mock.patch.object(env_manager, "get_env_path", lambda: self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        saved = {key: os.environ.get(key) for key in env_manager.ENV_KEYS}
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v) for k, v in saved.items()])

    def write(self, text: str) -> None:
        with open(self.path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)

    def read(self) -> str:
        with open(self.path, encoding="utf-8", newline="") as handle:
            return handle.read()


class ReadTests(EnvTestCase):
    def test_a_missing_file_gives_empty_values(self) -> None:
        self.assertEqual(env_manager.read_values(), {"GMAIL_USER": "", "GMAIL_APP_PASSWORD": "", "GITHUB_PAT": ""})

    def test_values_are_read_including_quoted_ones(self) -> None:
        self.write('GMAIL_USER=me@gmail.com\nGMAIL_APP_PASSWORD="ab cd"\n# GITHUB_PAT=commented\n')
        values = env_manager.read_values()
        self.assertEqual((values["GMAIL_USER"], values["GMAIL_APP_PASSWORD"], values["GITHUB_PAT"]), ("me@gmail.com", "ab cd", ""))

    def test_summary_never_contains_the_secrets(self) -> None:
        self.write("GMAIL_USER=me@gmail.com\nGMAIL_APP_PASSWORD=topsecret\nGITHUB_PAT=ghp_secret\n")
        text = env_manager.summary()
        self.assertIn("me@gmail.com", text)
        self.assertIn("GitHub token: set", text)
        self.assertNotIn("topsecret", text)
        self.assertNotIn("ghp_secret", text)

    def test_summary_when_nothing_is_set(self) -> None:
        self.assertEqual(env_manager.summary(), "Gmail: not set   ·   GitHub token: not set")
        self.write("GMAIL_USER=me@gmail.com\n")
        self.assertIn("(no app password)", env_manager.summary())


class WriteTests(EnvTestCase):
    def test_a_new_file_is_created(self) -> None:
        self.assertTrue(env_manager.update_values({"GMAIL_USER": "me@gmail.com", "GMAIL_APP_PASSWORD": "abcdefgh"}))
        self.assertEqual(self.read(), "GMAIL_USER=me@gmail.com\nGMAIL_APP_PASSWORD=abcdefgh\n")

    def test_other_lines_comments_and_order_are_kept(self) -> None:
        self.write("# my notes\nOTHER=1\nGMAIL_USER=old@gmail.com\n\n# token below\nGITHUB_PAT=old\nLAST=2\n")
        env_manager.update_values({"GMAIL_USER": "new@gmail.com"})
        self.assertEqual(
            self.read(), "# my notes\nOTHER=1\nGMAIL_USER=new@gmail.com\n\n# token below\nGITHUB_PAT=old\nLAST=2\n"
        )

    def test_only_the_given_keys_are_touched(self) -> None:
        self.write("GMAIL_USER=a@b.c\nGMAIL_APP_PASSWORD=keepme\n")
        env_manager.update_values({"GITHUB_PAT": "ghp_new"})
        values = dotenv_values(self.path)
        self.assertEqual((values["GMAIL_APP_PASSWORD"], values["GITHUB_PAT"]), ("keepme", "ghp_new"))

    def test_an_empty_value_removes_the_line(self) -> None:
        self.write("GMAIL_USER=a@b.c\nGITHUB_PAT=old\n")
        env_manager.update_values({"GITHUB_PAT": ""})
        self.assertEqual(self.read(), "GMAIL_USER=a@b.c\n")
        env_manager.update_values({"GITHUB_PAT": None})  # already gone: nothing to do
        self.assertEqual(self.read(), "GMAIL_USER=a@b.c\n")

    def test_duplicates_collapse_to_the_new_value(self) -> None:
        self.write("GMAIL_USER=one@x.y\nGMAIL_USER=two@x.y\n")
        env_manager.update_values({"GMAIL_USER": "three@x.y"})
        self.assertEqual(self.read(), "GMAIL_USER=three@x.y\n")

    def test_export_prefix_and_spaces_around_the_equals_sign_are_recognised(self) -> None:
        self.write("export GMAIL_USER = old@x.y\n")
        env_manager.update_values({"GMAIL_USER": "new@x.y"})
        self.assertEqual(self.read(), "GMAIL_USER=new@x.y\n")

    def test_no_change_does_not_rewrite_the_file(self) -> None:
        self.write("GMAIL_USER=a@b.c\n")
        self.assertFalse(env_manager.update_values({"GMAIL_USER": "a@b.c"}))

    def test_windows_line_endings_are_kept(self) -> None:
        self.write("GMAIL_USER=a@b.c\r\nOTHER=1\r\n")
        env_manager.update_values({"GITHUB_PAT": "ghp_x"})
        self.assertEqual(self.read(), "GMAIL_USER=a@b.c\r\nOTHER=1\r\nGITHUB_PAT=ghp_x\r\n")

    def test_a_file_without_a_final_newline_gets_one(self) -> None:
        self.write("GMAIL_USER=a@b.c")
        env_manager.update_values({"GITHUB_PAT": "x"})
        self.assertEqual(self.read(), "GMAIL_USER=a@b.c\nGITHUB_PAT=x\n")

    def test_values_that_need_quotes_survive_a_round_trip(self) -> None:
        for secret in ('pa ss#word', 'with "quotes" inside', "back\\slash", "dollar$sign", "equals=sign and space", "ünï©ode"):
            env_manager.update_values({"GMAIL_APP_PASSWORD": secret})
            self.assertEqual(env_manager.read_values()["GMAIL_APP_PASSWORD"], secret, secret)

    def test_plain_values_are_written_without_quotes(self) -> None:
        env_manager.update_values({"GITHUB_PAT": "ghp_AbC123"})
        self.assertEqual(self.read(), "GITHUB_PAT=ghp_AbC123\n")

    def test_the_running_process_follows(self) -> None:
        env_manager.update_values({"GMAIL_USER": "me@x.y"})
        self.assertEqual(os.environ["GMAIL_USER"], "me@x.y")
        env_manager.update_values({"GMAIL_USER": ""})
        self.assertNotIn("GMAIL_USER", os.environ)

    def test_anything_but_a_credential_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            env_manager.update_values({"PATH": "x"})
        self.assertFalse(os.path.exists(self.path))

    def test_no_temporary_files_are_left_behind(self) -> None:
        env_manager.update_values({"GMAIL_USER": "a@b.c"})
        self.assertEqual(sorted(name for name in os.listdir(os.path.dirname(self.path)) if "tmp" in name), [])

    @unittest.skipIf(os.name == "nt", "file permissions are POSIX only")
    def test_the_file_is_private_to_its_owner(self) -> None:
        env_manager.update_values({"GMAIL_USER": "a@b.c"})
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_a_failed_write_keeps_the_old_file(self) -> None:
        self.write("GMAIL_USER=old@x.y\n")
        with mock.patch.object(env_manager.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                env_manager.update_values({"GMAIL_USER": "new@x.y"})
        self.assertEqual(self.read(), "GMAIL_USER=old@x.y\n")
        self.assertNotIn("new@x.y", os.environ.get("GMAIL_USER", ""))

    def test_a_busy_lock_is_reported(self) -> None:
        from filelock import FileLock

        with FileLock(self.path + ".lock"):
            with mock.patch.object(env_manager, "_LOCK_TIMEOUT_SECONDS", 0.2):
                with self.assertRaises(env_manager.EnvLockTimeout):
                    env_manager.update_values({"GMAIL_USER": "a@b.c"})

    def test_a_briefly_locked_file_is_retried_on_windows(self) -> None:
        real_replace = os.replace
        calls = {"n": 0}

        def flaky(source, target):
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError("in use")
            return real_replace(source, target)

        with mock.patch.object(env_manager.os, "replace", flaky), mock.patch.object(env_manager.time, "sleep", lambda s: None):
            env_manager.update_values({"GMAIL_USER": "a@b.c"})
        self.assertEqual(calls["n"], 3)
        self.assertEqual(self.read(), "GMAIL_USER=a@b.c\n")


class ValidationTests(unittest.TestCase):
    def test_a_good_login(self) -> None:
        self.assertEqual(env_manager.validate("me@gmail.com", "abcd efgh ijkl mnop", ""), [])
        self.assertEqual(env_manager.validate("me@gmail.com", "pw", "ghp_token"), [])

    def test_the_required_values(self) -> None:
        self.assertEqual(len(env_manager.validate("", "", "")), 2)
        self.assertIn("name@gmail.com", env_manager.validate("not an address", "pw", "")[0])
        self.assertIn("app password", env_manager.validate("me@gmail.com", "   ", "")[0])

    def test_a_token_with_spaces_is_refused_but_an_empty_one_is_fine(self) -> None:
        self.assertEqual(len(env_manager.validate("me@gmail.com", "pw", "ghp a b")), 1)
        self.assertEqual(env_manager.validate("me@gmail.com", "pw", "  "), [])

    def test_the_spaces_of_an_app_password_are_dropped(self) -> None:
        self.assertEqual(env_manager.normalize_password(" abcd efgh\tijkl mnop "), "abcdefghijklmnop")

    def test_mask(self) -> None:
        self.assertEqual(env_manager.mask(""), "")
        self.assertEqual(env_manager.mask("abc"), "•••")
        self.assertEqual(len(env_manager.mask("x" * 50)), 12)  # the length of a long secret is not revealed either


class HelpLinkTests(unittest.TestCase):
    def test_every_link_is_a_labelled_https_address(self) -> None:
        self.assertGreaterEqual(len(env_manager.HELP_LINKS), 4)
        for label, url in env_manager.HELP_LINKS:
            self.assertTrue(label.strip(), url)
            self.assertTrue(url.startswith("https://"), url)
        self.assertEqual(len({url for _label, url in env_manager.HELP_LINKS}), len(env_manager.HELP_LINKS))


class FingerprintTests(EnvTestCase):
    def test_it_changes_with_the_credentials_and_hides_them(self) -> None:
        empty = env_manager.credentials_fingerprint()
        env_manager.update_values({"GMAIL_USER": "me@x.y", "GMAIL_APP_PASSWORD": "secretpw"})
        with_login = env_manager.credentials_fingerprint()
        self.assertNotEqual(empty, with_login)
        self.assertNotIn("secretpw", with_login)
        env_manager.update_values({"GMAIL_APP_PASSWORD": "otherpw"})
        self.assertNotEqual(with_login, env_manager.credentials_fingerprint())

    def test_a_changed_login_makes_the_daemon_settings_fingerprint_differ(self) -> None:
        # The GUI compares this fingerprint with the one a running daemon published: a new login needs a restart.
        from modules import config_manager

        config = {"mailbox": {"folder": "GitHubNotifications"}}
        before = config_manager.get_config_fingerprint(config)
        env_manager.update_values({"GMAIL_APP_PASSWORD": "newpassword"})
        self.assertNotEqual(before, config_manager.get_config_fingerprint(config))
        same = config_manager.get_config_fingerprint(config)
        self.write(self.read() + "# just a comment\n")  # unrelated edits to .env change nothing
        self.assertEqual(same, config_manager.get_config_fingerprint(config))

    def test_it_ignores_unrelated_lines(self) -> None:
        self.write("GMAIL_USER=a@b.c\n")
        before = env_manager.credentials_fingerprint()
        self.write("# note\nGMAIL_USER=a@b.c\nOTHER=1\n")
        self.assertEqual(before, env_manager.credentials_fingerprint())


if __name__ == "__main__":
    unittest.main()
