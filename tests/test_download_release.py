"""The download engine against a faked GitHub: asset_downloader.download_release / download_all_assets.

No network is used. `FakeGitHub` answers the API calls (release JSON, commit hash, expanded assets page) and
the file downloads (HEAD and streamed GET), and records every request. Everything is stored inside temporary
folders (config.json, mapping.json and state.db are replaced for each test).
Run from the project root: python -m unittest discover -s tests -t .
"""
import contextlib
import io
import json
import os
import tempfile
import time
import types
import unittest
from email.utils import formatdate
from unittest import mock

import requests

from modules import asset_downloader, config_manager, db_manager, mapping_manager

REPO = "owner/app"
TAG = "v1.0"
BASE = "https://example.invalid"
LAST_MODIFIED_EPOCH = 1_700_000_000  # 2023-11-14, well before "now"


class FakeResponse:
    def __init__(self, status_code=200, body=b"", headers=None, json_data=None, text=None, fail_after=None):
        self.status_code = status_code
        self.content = body
        self.headers = headers or {}
        self._json = json_data
        self.text = text if text is not None else body.decode("utf-8", errors="replace")
        self.fail_after = fail_after  # raise this error while streaming

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")

    def iter_content(self, chunk_size=8192):
        if self.fail_after is not None:
            yield self.content[:2]
            raise self.fail_after
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start:start + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeGitHub:
    """Routes for one release. `files` maps a download URL to its bytes."""

    def __init__(self):
        self.release = {
            "tag_name": TAG,
            "name": "First release",
            "published_at": "2026-10-06T13:24:00Z",
            "prerelease": False,
            "html_url": f"{BASE}/{REPO}/releases/tag/{TAG}",
            "assets": [
                {"id": 1, "name": "app.zip", "browser_download_url": f"{BASE}/dl/app.zip", "size": 7,
                 "updated_at": "2026-10-06T13:00:00Z"},
                {"id": 2, "name": "app.exe", "browser_download_url": f"{BASE}/dl/app.exe", "size": 3,
                 "updated_at": "2026-10-06T13:00:00Z"},
            ],
            "zipball_url": f"{BASE}/dl/source.zip",
            "tarball_url": f"{BASE}/dl/source.tar.gz",
            "target_commitish": "main",
        }
        self.release_status = 200
        self.files = {
            f"{BASE}/dl/app.zip": b"payload",
            f"{BASE}/dl/app.exe": b"exe",
            f"{BASE}/dl/source.zip": b"srczip",
            f"{BASE}/dl/source.tar.gz": b"srctgz",
        }
        self.file_headers: dict[str, dict] = {}
        self.attestation_href: str | None = None
        self.get_errors: dict[str, list] = {}  # url -> errors to raise, one per attempt
        self.get_failures: dict[str, FakeResponse] = {}
        self.requests: list[tuple[str, str]] = []

    # --- API (requests.get) -------------------------------------------------------------
    def api_get(self, url, headers=None, timeout=None, **kwargs):
        self.requests.append(("GET", url))
        if "/releases/tags/" in url:
            if self.release_status == 200:
                return FakeResponse(200, json_data=self.release)
            return FakeResponse(self.release_status, text="nope")
        if "/commits/" in url:
            return FakeResponse(200, json_data={"sha": "abcdef1234567"})
        if "/releases/expanded_assets/" in url:
            html = ""
            if self.attestation_href:
                html = f'<a href="{self.attestation_href}">attestation</a>'
            return FakeResponse(200, body=html.encode())
        return FakeResponse(404)

    # --- Session ------------------------------------------------------------------------
    def make_session(self):
        github = self

        class Session:
            def __init__(self):
                self.headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def head(self, url, **kwargs):
                github.requests.append(("HEAD", url))
                if url not in github.files:
                    return FakeResponse(404)
                return FakeResponse(200, headers=github.headers_for(url))

            def get(self, url, **kwargs):
                github.requests.append(("GET", url))
                errors = github.get_errors.get(url)
                if errors:
                    raise errors.pop(0)
                if url in github.get_failures:
                    return github.get_failures[url]
                return FakeResponse(200, body=github.files[url], headers=github.headers_for(url))

        return Session()

    def headers_for(self, url):
        headers = {
            "Content-Length": str(len(self.files[url])),
            "Last-Modified": formatdate(LAST_MODIFIED_EPOCH, usegmt=True),
            "ETag": f'"etag-{url.rsplit("/", 1)[-1]}"',
        }
        headers.update(self.file_headers.get(url, {}))
        return {key: value for key, value in headers.items() if value is not None}  # None = leave the header out

    def module(self):
        return types.SimpleNamespace(
            get=self.api_get,
            Session=self.make_session,
            RequestException=requests.RequestException,
        )


class DownloadTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._temp_dir.cleanup)
        self.root = self._temp_dir.name
        self.downloads = os.path.join(self.root, "downloads")
        config_path = os.path.join(self.root, "config.json")
        mapping_path = os.path.join(self.root, "mapping.json")
        db_path = os.path.join(self.root, "state.db")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump({"paths": {"default_download_dir": self.downloads}}, handle)
        with open(mapping_path, "w", encoding="utf-8") as handle:
            json.dump({"repositories": []}, handle)
        self.github = FakeGitHub()
        self.dry_run = False
        self.sleeps: list[float] = []
        for target, name, value in (
            (db_manager, "get_state_db_path", lambda: db_path),
            (config_manager, "_config_file_path", lambda: config_path),
            (mapping_manager, "_mapping_file_path", lambda: mapping_path),
            (asset_downloader, "is_dry_run", lambda: self.dry_run),
            (asset_downloader, "requests", self.github.module()),
            (asset_downloader, "GITHUB_TOKEN", None),
            (asset_downloader.time, "sleep", self.sleeps.append),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    @property
    def release_dir(self) -> str:
        return os.path.join(
            self.downloads, "GHAADD", "Processing", "app (owner)", "Release",
            "2026-10-06_13-24, First release, v1.0, abcdef1",
        )

    def run_download(self, include_stats: bool = True, release_type: str | None = "Release"):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = asset_downloader.download_release(REPO, TAG, release_type, include_stats=include_stats)
        self.output = output.getvalue()
        return result

    def files_in_release_dir(self) -> list[str]:
        if not os.path.isdir(self.release_dir):
            return []
        return sorted(os.listdir(self.release_dir))

    def saved_state(self) -> dict:
        connection = db_manager.open_database()
        try:
            return db_manager.load_release_state(connection, f"{REPO}|{TAG}")
        finally:
            connection.close()


class GitHubLookupTests(DownloadTestCase):
    def test_release_data_is_returned_on_200(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            data = asset_downloader.get_release_data(REPO, TAG)
        self.assertEqual(data["tag_name"], TAG)

    def test_a_missing_tag_gives_none(self) -> None:
        self.github.release_status = 404
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertIsNone(asset_downloader.get_release_data(REPO, TAG))
        self.assertIn("not found yet", output.getvalue())

    def test_an_api_error_gives_none_and_is_reported(self) -> None:
        self.github.release_status = 500
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertIsNone(asset_downloader.get_release_data(REPO, TAG))
        self.assertIn("API Error (500)", output.getvalue())

    def test_the_token_is_sent_when_there_is_one(self) -> None:
        seen = {}

        def capture(url, headers=None, **kwargs):
            seen.update(headers or {})
            return FakeResponse(200, json_data={})

        module = types.SimpleNamespace(get=capture)
        with mock.patch.object(asset_downloader, "requests", module), \
                mock.patch.object(asset_downloader, "GITHUB_TOKEN", "secret"):
            asset_downloader.get_release_data(REPO, TAG)
        self.assertEqual(seen["Authorization"], "Bearer secret")

    def test_short_commit_hash(self) -> None:
        self.assertEqual(asset_downloader.get_short_commit_hash(REPO, TAG, {}), "abcdef1")

    def test_short_commit_hash_when_github_does_not_know_the_tag(self) -> None:
        module = types.SimpleNamespace(get=lambda *a, **k: FakeResponse(404))
        with mock.patch.object(asset_downloader, "requests", module):
            self.assertEqual(asset_downloader.get_short_commit_hash(REPO, TAG, {}), "unknown-commit")


class AttestationLookupTests(DownloadTestCase):
    def test_no_release_page_means_no_attestation(self) -> None:
        self.assertIsNone(asset_downloader.get_release_attestation_url({}, {}))

    def test_the_download_link_is_read_from_the_expanded_assets_page(self) -> None:
        self.github.attestation_href = "/owner/app/attestations/99/download"
        url = asset_downloader.get_release_attestation_url(self.github.release, {})
        self.assertEqual(url, "https://github.com/owner/app/attestations/99/download")
        self.assertIn(("GET", f"{BASE}/{REPO}/releases/expanded_assets/{TAG}"), self.github.requests)

    def test_a_page_without_a_link_gives_none(self) -> None:
        self.assertIsNone(asset_downloader.get_release_attestation_url(self.github.release, {}))


class ResultShapeTests(unittest.TestCase):
    def test_legacy_values(self) -> None:
        build = asset_downloader._build_download_result
        self.assertIs(build("SUCCESS", 1, 0, 1, False), True)
        self.assertEqual(build("SKIP", 0, 0, 0, False), "SKIP")
        self.assertIs(build("FAILED", 0, 0, 0, False), False)

    def test_detailed_payload(self) -> None:
        result = asset_downloader._build_download_result(
            "FAILED", 1, 2, 5, True, working_dir="x", skip_reason="why")
        self.assertEqual(result, {
            "status": "FAILED", "downloaded_count": 1, "skipped_count": 2, "total_items": 5,
            "skipped_items": [], "working_dir": "x", "skip_reason": "why",
        })


class SuccessfulDownloadTests(DownloadTestCase):
    def test_everything_is_downloaded_into_the_processing_folder(self) -> None:
        result = self.run_download()
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual((result["downloaded_count"], result["skipped_count"], result["total_items"]), (4, 0, 4))
        self.assertEqual(result["working_dir"], self.release_dir)
        self.assertEqual(
            self.files_in_release_dir(),
            sorted(["app.exe", "app.zip", "app-v1.0-Source_code (source).zip",
                    "app-v1.0-Source_code (source).tar.gz"]),
        )

    def test_the_file_content_and_the_remote_timestamp_are_kept(self) -> None:
        self.run_download()
        path = os.path.join(self.release_dir, "app.zip")
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"payload")
        self.assertEqual(int(os.path.getmtime(path)), LAST_MODIFIED_EPOCH)

    def test_no_part_files_are_left_behind(self) -> None:
        self.run_download()
        self.assertFalse([name for name in self.files_in_release_dir() if name.endswith(".part")])

    def test_the_legacy_return_value_is_true(self) -> None:
        self.assertIs(self.run_download(include_stats=False), True)

    def test_the_state_is_saved_for_every_item(self) -> None:
        self.run_download()
        state = self.saved_state()
        self.assertEqual(set(state), {"asset:1", "asset:2", "source:zipball", "source:tarball"})
        self.assertEqual(state["asset:1"]["size"], 7)
        self.assertEqual(state["asset:1"]["etag"], '"etag-app.zip"')

    def test_a_prerelease_goes_to_the_prerelease_folder_even_if_the_mail_said_release(self) -> None:
        self.github.release["prerelease"] = True
        result = self.run_download(release_type="Release")
        self.assertIn(os.sep + "Pre-release" + os.sep, result["working_dir"])

    def test_the_staging_folders_are_created_even_before_anything_is_downloaded(self) -> None:
        self.github.release_status = 404
        self.run_download()
        self.assertTrue(os.path.isdir(os.path.join(self.downloads, "GHAADD", "Processing")))

    def test_a_download_without_source_archives(self) -> None:
        del self.github.release["zipball_url"]
        del self.github.release["tarball_url"]
        result = self.run_download()
        self.assertEqual(result["total_items"], 2)

    def test_source_files_come_last(self) -> None:
        self.run_download()
        order = [url for method, url in self.github.requests if method == "GET" and "/dl/" in url]
        self.assertEqual([u.rsplit("/", 1)[-1] for u in order],
                         ["app.zip", "app.exe", "source.zip", "source.tar.gz"])

    def test_the_attestation_is_named_from_content_disposition(self) -> None:
        href = "/owner/app/attestations/99/download"
        full = "https://github.com" + href
        self.github.attestation_href = href
        self.github.files[full] = b"{}"
        self.github.file_headers[full] = {"Content-Disposition": 'attachment; filename="proof.sigstore"'}
        result = self.run_download()
        self.assertEqual(result["total_items"], 5)
        self.assertIn("proof.sigstore", self.files_in_release_dir())

    def test_an_attestation_without_a_name_gets_a_numbered_json_name(self) -> None:
        href = "/owner/app/attestations/99/download"
        full = "https://github.com" + href
        self.github.attestation_href = href
        self.github.files[full] = b"{}"
        self.run_download()
        self.assertIn("attestation-3.json", self.files_in_release_dir())

    def test_state_persistence_can_be_switched_off(self) -> None:
        with mock.patch.object(asset_downloader, "is_state_persistence_enabled", lambda: False):
            result = self.run_download()
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(self.saved_state(), {})


class SecondRunTests(DownloadTestCase):
    def test_unchanged_files_are_skipped_on_the_second_run(self) -> None:
        self.run_download()
        result = self.run_download()
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["downloaded_count"], 0)
        self.assertEqual(result["skipped_count"], 4)
        downloads = [url for method, url in self.github.requests if method == "GET" and "/dl/" in url]
        self.assertEqual(len(downloads), 4)  # the second run fetched nothing

    def test_a_changed_asset_is_fetched_again_and_the_source_archives_are_refreshed(self) -> None:
        self.run_download()
        self.github.files[f"{BASE}/dl/app.zip"] = b"payload-2"
        self.github.release["assets"][0]["size"] = 9
        self.github.release["assets"][0]["updated_at"] = "2026-10-07T00:00:00Z"
        self.github.file_headers[f"{BASE}/dl/app.zip"] = {"ETag": '"new"', "Last-Modified": formatdate(LAST_MODIFIED_EPOCH + 5000, usegmt=True)}
        result = self.run_download()
        self.assertEqual(result["status"], "SUCCESS")
        with open(os.path.join(self.release_dir, "app.zip"), "rb") as handle:
            self.assertEqual(handle.read(), b"payload-2")
        self.assertEqual(result["skipped_count"], 1)  # app.exe unchanged
        self.assertEqual(result["downloaded_count"], 3)  # app.zip + both source archives refreshed
        self.assertIn("Refreshing", self.output)

    def test_unchanged_source_archives_are_skipped_when_no_asset_changed(self) -> None:
        self.run_download()
        self.run_download()
        self.assertIn("no normal assets changed", self.output)

    def test_a_deleted_local_file_is_downloaded_again(self) -> None:
        self.run_download()
        os.remove(os.path.join(self.release_dir, "app.exe"))
        result = self.run_download()
        # app.exe plus both source archives, which are refreshed whenever a normal asset was fetched.
        self.assertEqual(result["downloaded_count"], 3)
        self.assertIn("app.exe", self.files_in_release_dir())


class NothingToDownloadTests(DownloadTestCase):
    def test_a_release_that_no_longer_exists_is_skipped(self) -> None:
        self.github.release_status = 404
        result = self.run_download()
        self.assertEqual(result["status"], "SKIP")
        self.assertEqual(result["skip_reason"], "release_not_found")

    def test_the_legacy_value_for_a_vanished_release(self) -> None:
        self.github.release_status = 404
        self.assertEqual(self.run_download(include_stats=False), "SKIP")

    def test_a_release_with_no_assets_and_no_source_fails_after_waiting(self) -> None:
        self.github.release["assets"] = []
        del self.github.release["zipball_url"]
        del self.github.release["tarball_url"]
        result = self.run_download()
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(self.sleeps, [10])
        self.assertIn("Timed out", self.output)


class FailureTests(DownloadTestCase):
    URL = f"{BASE}/dl/app.zip"

    def test_a_network_error_is_retried_and_then_succeeds(self) -> None:
        self.github.get_errors[self.URL] = [requests.ConnectionError("reset")]
        result = self.run_download()
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(self.sleeps, [2])
        self.assertIn("Retrying in 2s", self.output)

    def test_three_network_errors_in_a_row_fail_the_download(self) -> None:
        self.github.get_errors[self.URL] = [requests.Timeout("slow")] * 3
        result = self.run_download()
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(self.sleeps, [2, 4])
        self.assertEqual(result["working_dir"], self.release_dir)
        self.assertIn("after 3 attempts", self.output)

    def test_a_connection_lost_mid_file_removes_the_part_file(self) -> None:
        self.github.get_failures[self.URL] = FakeResponse(
            200, body=b"payload", headers={"Content-Length": "7"}, fail_after=requests.ConnectionError("cut"))
        result = self.run_download()
        self.assertEqual(result["status"], "FAILED")
        self.assertFalse([name for name in self.files_in_release_dir() if name.endswith(".part")])
        self.assertNotIn("app.zip", self.files_in_release_dir())

    def test_an_http_error_stops_at_once_without_retrying(self) -> None:
        self.github.get_failures[self.URL] = FakeResponse(403)
        result = self.run_download()
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(self.sleeps, [])
        self.assertIn("Request error", self.output)

    def test_a_write_error_fails_the_download(self) -> None:
        with mock.patch.object(asset_downloader.os, "replace", side_effect=OSError("disk full")):
            result = self.run_download()
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("File write error", self.output)
        self.assertFalse([name for name in self.files_in_release_dir() if name.endswith(".part")])

    def test_a_failed_head_request_does_not_stop_the_download(self) -> None:
        # The HEAD is only an optimisation: without it the file is simply downloaded.
        session_factory = self.github.make_session

        def session_with_failing_head():
            session = session_factory()

            def head(url, **kwargs):
                raise requests.ConnectionError("no head")

            session.head = head
            return session

        module = self.github.module()
        module.Session = session_with_failing_head
        with mock.patch.object(asset_downloader, "requests", module):
            result = self.run_download()
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["downloaded_count"], 4)

    def test_a_current_file_is_still_skipped_when_only_the_get_response_can_tell(self) -> None:
        self.run_download()
        session_factory = self.github.make_session

        def session_with_failing_head():
            session = session_factory()

            def head(url, **kwargs):
                raise requests.ConnectionError("no head")

            session.head = head
            return session

        module = self.github.module()
        module.Session = session_with_failing_head
        with mock.patch.object(asset_downloader, "requests", module):
            result = self.run_download()
        self.assertEqual(result["downloaded_count"], 0)
        self.assertEqual(result["skipped_count"], 4)

    def test_the_items_already_done_are_counted_when_a_later_one_fails(self) -> None:
        self.github.get_failures[f"{BASE}/dl/app.exe"] = FakeResponse(500)
        result = self.run_download()
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["downloaded_count"], 1)


class SkipDecisionTests(DownloadTestCase):
    """The ways a file already on disk can be recognised as current."""

    def test_a_changed_signature_with_the_same_etag_and_size_is_still_current(self) -> None:
        self.run_download()
        for asset in self.github.release["assets"]:
            asset["updated_at"] = "2026-10-09T00:00:00Z"  # the signature changes, the file's ETag does not
        result = self.run_download()
        self.assertEqual((result["downloaded_count"], result["skipped_count"]), (0, 4))

    def test_without_saved_state_the_size_and_the_timestamp_decide(self) -> None:
        with mock.patch.object(asset_downloader, "is_state_persistence_enabled", lambda: False):
            self.run_download()
            result = self.run_download()
        self.assertEqual((result["downloaded_count"], result["skipped_count"]), (0, 4))

    def test_a_file_of_another_size_is_downloaded_again_without_saved_state(self) -> None:
        with mock.patch.object(asset_downloader, "is_state_persistence_enabled", lambda: False):
            self.run_download()
            with open(os.path.join(self.release_dir, "app.exe"), "wb") as handle:
                handle.write(b"truncated-and-longer")
            result = self.run_download()
        self.assertIn("app.exe", self.files_in_release_dir())
        self.assertGreaterEqual(result["downloaded_count"], 1)
        with open(os.path.join(self.release_dir, "app.exe"), "rb") as handle:
            self.assertEqual(handle.read(), b"exe")


class HeaderFallbackTests(DownloadTestCase):
    def test_a_garbled_last_modified_falls_back_to_the_publish_time(self) -> None:
        self.github.file_headers[f"{BASE}/dl/app.zip"] = {"Last-Modified": "not a date"}
        self.run_download()
        published = 1_791_293_040  # 2026-10-06T13:24:00Z
        self.assertEqual(int(os.path.getmtime(os.path.join(self.release_dir, "app.zip"))), published)

    def test_a_missing_last_modified_falls_back_to_the_publish_time(self) -> None:
        self.github.file_headers[f"{BASE}/dl/app.zip"] = {"Last-Modified": None}
        self.run_download()
        self.assertEqual(int(os.path.getmtime(os.path.join(self.release_dir, "app.zip"))), 1_791_293_040)

    def test_a_release_without_a_publish_date_uses_the_current_time(self) -> None:
        del self.github.release["published_at"]
        self.github.file_headers[f"{BASE}/dl/app.zip"] = {"Last-Modified": None}
        before = time.time()
        result = self.run_download()
        self.assertEqual(result["status"], "SUCCESS")
        self.assertGreaterEqual(os.path.getmtime(os.path.join(result["working_dir"], "app.zip")), before - 2)
        self.assertIn("unknown-date", result["working_dir"])

    def test_an_attestation_with_an_unreadable_content_disposition_gets_a_numbered_name(self) -> None:
        href = "/owner/app/attestations/99/download"
        full = "https://github.com" + href
        self.github.attestation_href = href
        self.github.files[full] = b"{}"
        self.github.file_headers[full] = {"Content-Disposition": "attachment; filename="}
        self.run_download()
        self.assertIn("attestation-3.json", self.files_in_release_dir())

    def test_the_token_is_sent_with_the_api_calls_of_a_download(self) -> None:
        seen = []
        original = self.github.api_get

        def spy(url, headers=None, **kwargs):
            seen.append((headers or {}).get("Authorization"))
            return original(url, headers=headers, **kwargs)

        module = self.github.module()
        module.get = spy
        with mock.patch.object(asset_downloader, "requests", module), mock.patch.object(asset_downloader, "GITHUB_TOKEN", "tok"):
            result = self.run_download()
        self.assertEqual(result["status"], "SUCCESS")
        self.assertTrue(seen and all(value == "Bearer tok" for value in seen))

    def test_a_request_error_in_the_middle_of_a_file_removes_the_part_file(self) -> None:
        self.github.get_failures[f"{BASE}/dl/app.zip"] = FakeResponse(
            200, body=b"payload", headers={"Content-Length": "7"}, fail_after=requests.HTTPError("boom"))
        result = self.run_download()
        self.assertEqual(result["status"], "FAILED")
        self.assertFalse([name for name in self.files_in_release_dir() if name.endswith(".part")])


class DryRunTests(DownloadTestCase):
    def test_a_dry_run_fetches_no_bytes_and_creates_no_folder(self) -> None:
        self.dry_run = True
        result = self.run_download()
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["downloaded_count"], 4)
        self.assertFalse(os.path.exists(self.release_dir))
        self.assertEqual(self.saved_state(), {})
        downloads = [url for method, url in self.github.requests if method == "GET" and "/dl/" in url]
        self.assertEqual(downloads, [])
        self.assertIn("[DRY-RUN]", self.output)


if __name__ == "__main__":
    unittest.main()
