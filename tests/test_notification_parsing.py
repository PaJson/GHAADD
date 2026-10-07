"""Reading a GitHub notification email subject: which repository, tag and release type it names.

A wrong answer here sends files to the wrong place or skips a release silently, so the formats are pinned down.
No mailbox is contacted. Run from the project root: python -m unittest discover -s tests -t .
"""
import os
import unittest

# mailbox_listener refuses to import without credentials; these are never used to connect.
os.environ.setdefault("GMAIL_USER", "test@example.invalid")
os.environ.setdefault("GMAIL_APP_PASSWORD", "unused")

from modules.mailbox_listener import decode_email_subject, parse_github_subject  # noqa: E402


class ParseSubjectTests(unittest.TestCase):
    def test_a_release_with_a_title(self) -> None:
        self.assertEqual(
            parse_github_subject("[owner/repo] Release v1.0 - Big update"), ("owner/repo", "v1.0", "Release", False)
        )

    def test_a_pre_release(self) -> None:
        self.assertEqual(
            parse_github_subject("[owner/repo] Pre-release v1.0-rc1 - Candidate"),
            ("owner/repo", "v1.0-rc1", "Pre-release", False),
        )

    def test_the_tag_stops_at_the_first_separator_even_when_the_title_repeats_it(self) -> None:
        self.assertEqual(
            parse_github_subject("[a/b] Release v0.6.7 - v0.6.7 - v0.6.7"), ("a/b", "v0.6.7", "Release", False)
        )

    def test_extra_spaces_around_the_separator_are_fine(self) -> None:
        self.assertEqual(parse_github_subject("[a/b] Release   v1  - x"), ("a/b", "v1", "Release", False))

    def test_a_subject_without_a_title_uses_the_fallback_pattern(self) -> None:
        self.assertEqual(parse_github_subject("[a/b] Release nightly"), ("a/b", "nightly", "Release", True))
        self.assertEqual(parse_github_subject("[a/b] Pre-release nightly"), ("a/b", "nightly", "Pre-release", True))

    def test_the_keyword_is_not_case_sensitive(self) -> None:
        self.assertEqual(parse_github_subject("[a/b] release V1 - x")[:3], ("a/b", "V1", "Release"))
        self.assertEqual(parse_github_subject("[a/b] PRE-RELEASE V1 - x")[:3], ("a/b", "V1", "Pre-release"))

    def test_a_title_that_mentions_pre_release_does_not_change_the_type(self) -> None:
        # Regression: the type used to be looked for in the whole subject, title included.
        self.assertEqual(parse_github_subject("[a/b] Release v1 - Pre-release notes")[2], "Release")
        self.assertEqual(parse_github_subject("[a/b] Release v1 - Pre-release")[2], "Release")
        self.assertEqual(parse_github_subject("[a/b] Release v1 - the pre-release is out")[2], "Release")
        self.assertEqual(parse_github_subject("[a/b] Release pre-release-candidate")[2], "Release")  # fallback pattern

    def test_a_pre_release_with_pre_release_in_its_title_stays_a_pre_release(self) -> None:
        self.assertEqual(parse_github_subject("[a/b] Pre-release v1 - Pre-release notes")[2], "Pre-release")

    def test_tags_with_unusual_characters(self) -> None:
        self.assertEqual(parse_github_subject("[a/b] Release 🎉 v2 - big")[1], "🎉 v2")
        self.assertEqual(parse_github_subject("[a/b] Release release/2026.10 - x")[1], "release/2026.10")
        self.assertEqual(parse_github_subject("[a/b] Release v1.2.3+build.5 - x")[1], "v1.2.3+build.5")

    def test_repository_names_with_dots_dashes_and_underscores(self) -> None:
        self.assertEqual(parse_github_subject("[my-org/my.repo_2] Release v1 - x")[0], "my-org/my.repo_2")

    def test_subjects_that_are_not_releases_are_not_parsed(self) -> None:
        for subject in ("", "Hello", "[a/b] Something else v1", "[a/b] New issue", "Release v1", "[a/b]Release v1"):
            with self.subTest(subject=subject):
                self.assertEqual(parse_github_subject(subject), (None, None, None, False))


class DecodeSubjectTests(unittest.TestCase):
    def test_plain_text_is_unchanged(self) -> None:
        self.assertEqual(decode_email_subject("[a/b] Release v1 - x"), "[a/b] Release v1 - x")

    def test_empty_and_missing_subjects(self) -> None:
        self.assertEqual(decode_email_subject(None), "")
        self.assertEqual(decode_email_subject(""), "")

    def test_utf8_encoded_words(self) -> None:
        self.assertEqual(decode_email_subject("=?utf-8?q?=F0=9F=9A=80_Launch?="), "🚀 Launch")
        self.assertEqual(decode_email_subject("=?utf-8?b?w6TDtg==?= x"), "äö x")

    def test_other_charsets(self) -> None:
        self.assertEqual(decode_email_subject("=?iso-8859-1?q?caf=E9?="), "café")

    def test_a_decoded_subject_parses(self) -> None:
        subject = decode_email_subject("=?utf-8?q?[a/b]_Release_v1_-_=F0=9F=9A=80?=")
        self.assertEqual(parse_github_subject(subject), ("a/b", "v1", "Release", False))


if __name__ == "__main__":
    unittest.main()
