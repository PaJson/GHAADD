"""The Gmail and GitHub test buttons (modules/connection_tests.py) against fake network clients.

No network is used. Run from the project root: python -m unittest discover -s tests -t .
"""
import socket
import unittest
from types import SimpleNamespace

import requests
from imapclient.exceptions import LoginError

from modules import connection_tests

FOLDERS = [
    ((b"\\HasNoChildren",), b"/", "INBOX"),
    ((b"\\Noselect", b"\\HasChildren"), b"/", "[Gmail]"),
    ((b"\\HasNoChildren",), b"/", "[Gmail]/All Mail"),
    ((b"\\HasNoChildren",), b"/", "GitHubNotifications"),
    ((b"\\HasNoChildren",), b"/", "archive"),
]


class FakeServer:
    def __init__(self, login_error=None, folders=FOLDERS, unread=(1, 2, 3), select_error=None):
        self.login_error, self.folders, self.unread, self.select_error = login_error, folders, unread, select_error
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        self.calls.append(("login", user, password))
        if self.login_error:
            raise self.login_error

    def list_folders(self):
        return self.folders

    def select_folder(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly))
        if self.select_error:
            raise self.select_error

    def search(self, criteria):
        self.calls.append(("search", criteria))
        return list(self.unread)


def factory(server=None, error=None, hosts=None):
    def make(host):
        if hosts is not None:
            hosts.append(host)
        if error:
            raise error
        return server
    return make


class GmailTests(unittest.TestCase):
    def test_a_good_login_lists_the_selectable_folders_sorted(self) -> None:
        server = FakeServer()
        result = connection_tests.check_gmail("me@gmail.com", "pw", "", factory(server))
        self.assertTrue(result.ok)
        self.assertEqual(result.folders, ["[Gmail]/All Mail", "archive", "GitHubNotifications", "INBOX"])
        self.assertNotIn("[Gmail]", result.folders)  # a container that cannot hold mail
        self.assertIsNone(result.folder_found)
        self.assertIn("4 folders", result.message)
        self.assertEqual(server.calls, [("login", "me@gmail.com", "pw")])

    def test_it_connects_to_gmail(self) -> None:
        hosts: list[str] = []
        connection_tests.check_gmail("me@gmail.com", "pw", "", factory(FakeServer(), hosts=hosts))
        self.assertEqual(hosts, ["imap.gmail.com"])

    def test_the_chosen_folder_is_checked_read_only_and_unread_mails_are_counted(self) -> None:
        server = FakeServer()
        result = connection_tests.check_gmail("me@gmail.com", "pw", "GitHubNotifications", factory(server))
        self.assertTrue(result.ok and result.folder_found)
        self.assertIn("3 unread", result.message)
        self.assertIn(("select", "GitHubNotifications", True), server.calls)  # never marks anything as seen

    def test_a_missing_folder_is_not_an_error_but_says_so_and_offers_the_list(self) -> None:
        server = FakeServer()
        result = connection_tests.check_gmail("me@gmail.com", "pw", "Nope", factory(server))
        self.assertTrue(result.ok)
        self.assertIs(result.folder_found, False)
        self.assertIn("no folder named \"Nope\"", result.message)
        self.assertIn("INBOX", result.folders)
        self.assertNotIn("select", [call[0] for call in server.calls])

    def test_a_rejected_login_explains_app_passwords_and_hides_the_password(self) -> None:
        error = LoginError("[AUTHENTICATIONFAILED] Invalid credentials for hunter2")
        result = connection_tests.check_gmail("me@gmail.com", "hunter2", "", factory(FakeServer(login_error=error)))
        self.assertFalse(result.ok)
        self.assertIn("app password", result.message)
        self.assertIn("2-step verification", result.message)
        self.assertNotIn("hunter2", result.message)

    def test_no_network(self) -> None:
        result = connection_tests.check_gmail("me@gmail.com", "pw", "", factory(error=OSError("getaddrinfo failed")))
        self.assertFalse(result.ok)
        self.assertIn("Could not reach imap.gmail.com", result.message)

    def test_a_timeout(self) -> None:
        result = connection_tests.check_gmail("me@gmail.com", "pw", "", factory(error=socket.timeout("timed out")))
        self.assertFalse(result.ok)
        self.assertIn("did not answer", result.message)

    def test_any_other_server_problem_is_reported(self) -> None:
        result = connection_tests.check_gmail("me@gmail.com", "pw", "", factory(FakeServer(login_error=RuntimeError("boom"))))
        self.assertEqual((result.ok, result.message), (False, "Gmail test failed: boom"))

    def test_an_empty_address_or_password_is_not_sent_anywhere(self) -> None:
        make = factory(error=AssertionError("must not connect"))
        self.assertFalse(connection_tests.check_gmail("", "pw", "", make).ok)
        self.assertFalse(connection_tests.check_gmail("me@gmail.com", "", "", make).ok)


def response(status, payload=None):
    return SimpleNamespace(status_code=status, json=lambda: payload)


def getter(result=None, error=None, seen=None):
    def get(url, headers=None, timeout=None):
        if seen is not None:
            seen.update(headers or {})
        if error:
            raise error
        return result
    return get


RATE = {"resources": {"core": {"limit": 5000, "remaining": 4990}}}


class GitHubTests(unittest.TestCase):
    def test_a_good_token(self) -> None:
        seen = {}
        result = connection_tests.check_github_token("ghp_x", getter(response(200, RATE), seen=seen))
        self.assertTrue(result.ok)
        self.assertIn("4990 of 5000", result.message)
        self.assertEqual(seen["Authorization"], "Bearer ghp_x")

    def test_no_token_is_fine_and_sends_no_authorization(self) -> None:
        seen = {}
        result = connection_tests.check_github_token("", getter(response(200, {"resources": {"core": {"limit": 60, "remaining": 58}}}), seen=seen))
        self.assertTrue(result.ok)
        self.assertIn("60 requests per hour", result.message)
        self.assertNotIn("Authorization", seen)

    def test_a_rejected_token(self) -> None:
        result = connection_tests.check_github_token("bad", getter(response(401)))
        self.assertEqual((result.ok, "rejected" in result.message), (False, True))

    def test_another_status(self) -> None:
        result = connection_tests.check_github_token("t", getter(response(503)))
        self.assertFalse(result.ok)
        self.assertIn("503", result.message)

    def test_no_network_and_the_token_is_not_echoed(self) -> None:
        result = connection_tests.check_github_token("ghp_secret", getter(error=requests.ConnectionError("failed for ghp_secret")))
        self.assertFalse(result.ok)
        self.assertNotIn("ghp_secret", result.message)

    def test_an_unreadable_answer_is_still_accepted(self) -> None:
        result = connection_tests.check_github_token("t", getter(response(200, {"unexpected": True})))
        self.assertTrue(result.ok)
        self.assertIn("could not be read", result.message)


if __name__ == "__main__":
    unittest.main()
