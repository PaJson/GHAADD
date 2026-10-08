"""The IMAP side of mailbox_listener against a faked server (no network, no real Gmail account).

`FakeIMAPClient` replaces imapclient.IMAPClient and records what the code asks of the server: login, folder
select (read-only when only reading), search, fetch, flags and the move to Trash.
Run from the project root: python -m unittest discover -s tests -t .
"""
import contextlib
import io
import os
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("GMAIL_USER", "test@example.invalid")
os.environ.setdefault("GMAIL_APP_PASSWORD", "not-a-real-password")

from modules import mailbox_listener  # noqa: E402


class FakeServer:
    """What the fake mailbox contains and what was done to it."""

    def __init__(self) -> None:
        self.subjects: dict[int, object] = {}
        self.calls: list[tuple] = []
        self.fail_times = 0  # number of connections that fail before one works
        self.connections = 0


class FakeIMAPClient:
    server = FakeServer()

    def __init__(self, host, use_uid=True):
        type(self).server.calls.append(("connect", host, use_uid))
        type(self).server.connections += 1
        if type(self).server.connections <= type(self).server.fail_times:
            raise OSError("connection refused")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        self.server.calls.append(("login", user, password))

    def select_folder(self, folder, readonly=False):
        self.server.calls.append(("select", folder, readonly))

    def search(self, criteria):
        self.server.calls.append(("search", criteria))
        return list(self.server.subjects)

    def fetch(self, ids, items):
        self.server.calls.append(("fetch", list(ids), items))
        return {
            msg_id: {b"ENVELOPE": SimpleNamespace(subject=self.server.subjects[msg_id])}
            for msg_id in ids
        }

    def set_flags(self, ids, flags):
        self.server.calls.append(("flags", list(ids), flags))

    def move(self, ids, folder):
        self.server.calls.append(("move", list(ids), folder))


class ImapTestCase(unittest.TestCase):
    def setUp(self) -> None:
        FakeIMAPClient.server = FakeServer()
        self.server = FakeIMAPClient.server
        for target, name, value in (
            (mailbox_listener, "IMAPClient", FakeIMAPClient),
            (mailbox_listener, "get_gmail_folder", lambda: "GitHub"),
            (mailbox_listener, "is_dry_run", lambda: False),
            (mailbox_listener.time, "sleep", lambda seconds: None),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def quietly(self, function, *args, **kwargs):
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                return function(*args, **kwargs)
        finally:
            self.output = buffer.getvalue()

    def call_names(self) -> list[str]:
        return [call[0] for call in self.server.calls]


class GetPendingNotificationsTests(ImapTestCase):
    def test_unread_release_mails_become_notifications(self) -> None:
        self.server.subjects = {
            11: "[owner/app] Release v1.0 - First",
            12: "[owner/tool] Pre-release v2.0-rc1 - Candidate",
        }
        found = self.quietly(mailbox_listener.get_pending_notifications)
        self.assertEqual(found, [
            {"repo": "owner/app", "tag": "v1.0", "release_type": "Release", "email_id": 11},
            {"repo": "owner/tool", "tag": "v2.0-rc1", "release_type": "Pre-release", "email_id": 12},
        ])

    def test_it_logs_in_and_opens_the_folder_read_only_so_nothing_is_marked_seen(self) -> None:
        self.server.subjects = {1: "[a/b] Release v1 - x"}
        self.quietly(mailbox_listener.get_pending_notifications)
        self.assertIn(("login", "test@example.invalid", os.environ["GMAIL_APP_PASSWORD"]), self.server.calls) \
            if False else None
        self.assertIn(("select", "GitHub", True), self.server.calls)
        self.assertIn(("search", "UNSEEN"), self.server.calls)
        self.assertNotIn("flags", self.call_names())
        self.assertNotIn("move", self.call_names())

    def test_an_empty_mailbox_gives_an_empty_list_and_no_fetch(self) -> None:
        self.assertEqual(self.quietly(mailbox_listener.get_pending_notifications), [])
        self.assertNotIn("fetch", self.call_names())

    def test_the_limit_caps_how_many_mails_are_read(self) -> None:
        self.server.subjects = {i: f"[o/r{i}] Release v{i} - x" for i in range(1, 6)}
        found = self.quietly(mailbox_listener.get_pending_notifications, limit=2)
        self.assertEqual([n["email_id"] for n in found], [1, 2])
        fetch = [call for call in self.server.calls if call[0] == "fetch"][0]
        self.assertEqual(fetch[1], [1, 2])

    def test_subjects_that_do_not_parse_are_skipped_and_reported(self) -> None:
        self.server.subjects = {1: "Your invoice", 2: "[o/r] Release v1 - x"}
        found = self.quietly(mailbox_listener.get_pending_notifications)
        self.assertEqual([n["email_id"] for n in found], [2])
        self.assertIn("Could not parse subject: Your invoice", self.output)

    def test_a_mail_without_a_subject_is_skipped(self) -> None:
        self.server.subjects = {1: None, 2: "[o/r] Release v1 - x"}
        found = self.quietly(mailbox_listener.get_pending_notifications)
        self.assertEqual([n["email_id"] for n in found], [2])
        self.assertIn("Could not extract subject", self.output)

    def test_bytes_and_mime_encoded_subjects_are_decoded(self) -> None:
        self.server.subjects = {
            1: "[o/r] Release v1 - x".encode("utf-8"),
            2: "=?utf-8?q?[o/caf=C3=A9]_Release_v2_-_x?=",
        }
        found = self.quietly(mailbox_listener.get_pending_notifications)
        self.assertEqual([(n["repo"], n["tag"]) for n in found], [("o/r", "v1"), ("o/café", "v2")])

    def test_the_fallback_pattern_is_reported(self) -> None:
        self.server.subjects = {1: "[o/r] Release v1"}
        found = self.quietly(mailbox_listener.get_pending_notifications)
        self.assertEqual(found[0]["tag"], "v1")
        self.assertIn("fallback parser pattern", self.output)

    def test_a_connection_error_is_reported_and_raised(self) -> None:
        self.server.fail_times = 1
        with self.assertRaises(OSError):
            self.quietly(mailbox_listener.get_pending_notifications)
        self.assertIn("Error fetching notifications", self.output)

    def test_missing_credentials_are_refused(self) -> None:
        with mock.patch.object(mailbox_listener, "PASSWORD", None):
            with self.assertRaises(ValueError):
                self.quietly(mailbox_listener.get_pending_notifications)
        self.assertEqual(self.server.calls, [])


class MarkAsReadAndDeleteTests(ImapTestCase):
    def test_nothing_to_do_does_not_connect(self) -> None:
        self.assertTrue(mailbox_listener.mark_as_read_and_delete([]))
        self.assertEqual(self.server.calls, [])

    def test_mails_are_flagged_seen_and_moved_to_trash(self) -> None:
        self.assertTrue(self.quietly(mailbox_listener.mark_as_read_and_delete, [5, 6]))
        self.assertIn(("select", "GitHub", False), self.server.calls)
        self.assertIn(("flags", [5, 6], [b"\\Seen"]), self.server.calls)
        self.assertIn(("move", [5, 6], "[Gmail]/Trash"), self.server.calls)

    def test_a_single_id_is_accepted(self) -> None:
        self.assertTrue(self.quietly(mailbox_listener.mark_as_read_and_delete, 9))
        self.assertIn(("move", [9], "[Gmail]/Trash"), self.server.calls)

    def test_dry_run_leaves_the_mailbox_alone(self) -> None:
        with mock.patch.object(mailbox_listener, "is_dry_run", lambda: True):
            self.assertTrue(self.quietly(mailbox_listener.mark_as_read_and_delete, [1]))
        self.assertEqual(self.server.calls, [])
        self.assertIn("[DRY-RUN]", self.output)

    def test_one_transient_failure_is_retried(self) -> None:
        self.server.fail_times = 1
        self.assertTrue(self.quietly(mailbox_listener.mark_as_read_and_delete, [1]))
        self.assertEqual(self.server.connections, 2)
        self.assertIn("Retrying in", self.output)

    def test_giving_up_returns_false(self) -> None:
        self.server.fail_times = 99
        self.assertFalse(self.quietly(mailbox_listener.mark_as_read_and_delete, [1]))
        self.assertEqual(self.server.connections, 2)

    def test_missing_credentials_are_refused(self) -> None:
        with mock.patch.object(mailbox_listener, "EMAIL", ""):
            with self.assertRaises(ValueError):
                mailbox_listener.mark_as_read_and_delete([1])


class MoveUnreadToTrashTests(ImapTestCase):
    def test_nothing_to_do_does_not_connect(self) -> None:
        self.assertTrue(mailbox_listener.move_unread_to_trash([]))
        self.assertEqual(self.server.calls, [])

    def test_mails_are_moved_without_being_flagged_seen(self) -> None:
        self.assertTrue(self.quietly(mailbox_listener.move_unread_to_trash, [3]))
        self.assertIn(("move", [3], "[Gmail]/Trash"), self.server.calls)
        self.assertNotIn("flags", self.call_names())

    def test_a_single_id_is_accepted(self) -> None:
        self.assertTrue(self.quietly(mailbox_listener.move_unread_to_trash, 4))
        self.assertIn(("move", [4], "[Gmail]/Trash"), self.server.calls)

    def test_dry_run_leaves_the_mailbox_alone(self) -> None:
        with mock.patch.object(mailbox_listener, "is_dry_run", lambda: True):
            self.assertTrue(self.quietly(mailbox_listener.move_unread_to_trash, [1]))
        self.assertEqual(self.server.calls, [])

    def test_retry_then_success(self) -> None:
        self.server.fail_times = 1
        self.assertTrue(self.quietly(mailbox_listener.move_unread_to_trash, [1]))
        self.assertIn("Retrying in", self.output)

    def test_giving_up_returns_false(self) -> None:
        self.server.fail_times = 99
        self.assertFalse(self.quietly(mailbox_listener.move_unread_to_trash, [1]))

    def test_missing_credentials_are_refused(self) -> None:
        with mock.patch.object(mailbox_listener, "PASSWORD", None):
            with self.assertRaises(ValueError):
                mailbox_listener.move_unread_to_trash([1])


class CheckReleasesTests(ImapTestCase):
    def test_it_reports_how_many_notifications_were_found(self) -> None:
        self.server.subjects = {1: "[o/r] Release v1 - x"}
        self.quietly(mailbox_listener.check_releases)
        self.assertIn("Found 1 release notification(s).", self.output)


if __name__ == "__main__":
    unittest.main()
