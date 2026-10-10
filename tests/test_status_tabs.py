"""Tests for the status tabs model, its database queries and the feed that wires them together.

Run from the project root: python -m unittest discover -s tests -t .
"""
import json
import os
import tempfile
import unittest
from contextlib import closing
from unittest import mock

from modules import config_manager, db_manager, gui_data, mapping_manager, status_tabs
from modules.status_tabs import StatusTabsModel, TabDef, find_repo, format_title, unmapped_rows


class MemoryStore:
    def __init__(self, initial=None) -> None:
        self.data = dict(initial or {})
        self.saves = 0

    def load(self):
        return dict(self.data)

    def save(self, seen) -> None:
        self.data = dict(seen)
        self.saves += 1


class FakeEvents:
    """An in-memory lifecycle_events table honouring the fetch contract (newest first, id > after)."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.fetches = 0

    def add(self, event_type, category=None, repo=None, tag=None, message="m", created_at=1_000_000.0) -> int:
        event_id = len(self.events) + 1
        self.events.append(dict(id=event_id, event_type=event_type, category=category, repo=repo,
                                tag=tag, message=message, created_at=created_at))
        return event_id

    def fetch(self, tab: TabDef, after_id, limit):
        self.fetches += 1
        matching = [
            e for e in self.events
            if e["event_type"] in tab.event_types
            and (tab.categories is None or e["category"] in tab.categories)
            and e["category"] not in tab.exclude_categories
            and (after_id is None or e["id"] > after_id)
        ]
        return sorted(matching, key=lambda e: -e["id"])[:limit]

    def max_id(self) -> int:
        return len(self.events)


class ModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.events = FakeEvents()
        self.store = MemoryStore()

    def model(self, **kwargs) -> StatusTabsModel:
        return StatusTabsModel(status_tabs.TAB_DEFS, self.events.fetch, self.events.max_id, self.store, **kwargs)

    def test_titles_show_unread_counts_only_when_there_are_some(self) -> None:
        self.assertEqual(format_title("Warnings", 0), "Warnings")
        self.assertEqual(format_title("Warnings", 3), "Warnings (3)")

    def test_folder_limit_tab_shows_only_the_newest_warning_per_repository(self) -> None:
        known = {"o/app": "o/app", "o/lib": "o/lib"}
        for count in (30, 31, 32):
            self.events.add("WARNING", "LIMIT", message=f"Folder limit warning: o/app currently has {count} folder(s)")
        self.events.add("WARNING", "LIMIT", message="Folder limit warning: o/lib currently has 12 folder(s)")
        model = self.model(known_repos=lambda: known)
        model.refresh()
        self.events.add("WARNING", "LIMIT", message="Folder limit warning: o/lib currently has 13 folder(s)")
        model.refresh()

        rows = model.rows("limits")
        self.assertEqual([(r.repo, r.earlier) for r in rows], [("o/lib", 1), ("o/app", 2)])  # newest first
        self.assertIn("32 folder", next(r.message for r in rows if r.repo == "o/app"))
        self.assertEqual(rows[1].kind, "Limit (+2 earlier)")
        warnings_tab = self.model(known_repos=lambda: known)
        warnings_tab.refresh()
        self.assertEqual(warnings_tab.rows("warnings"), [])  # other tabs keep every row

        model.drop_repo("limits", "o/app")
        self.assertEqual([r.repo for r in model.rows("limits")], ["o/lib"])

    def test_a_fresh_gui_starts_with_everything_read(self) -> None:
        for _ in range(5):
            self.events.add("WARNING", "API")
            self.events.add("COMPLETED_MOVE", tag="v1")
        model = self.model()
        model.refresh()

        self.assertEqual([model.unread(k) for k in ("warnings", "completed")], [0, 0])
        self.assertEqual(model.title("warnings"), "Warnings")
        self.assertEqual(len(model.rows("warnings")), 5)  # the history is still there to look at

    def test_new_events_become_unread_and_opening_the_tab_marks_them_read(self) -> None:
        model = self.model()
        model.refresh()
        self.events.add("WARNING", "API")
        self.events.add("WARNING", "SANITY_CHECK")
        self.events.add("COMPLETED_MOVE", tag="v2")

        changed = model.refresh()

        self.assertEqual(changed, {"warnings", "completed"})
        self.assertEqual((model.unread("warnings"), model.unread("completed")), (2, 1))
        self.assertEqual((model.title("warnings"), model.title("completed")), ("Warnings (2)", "Completed (1)"))
        self.assertTrue(model.mark_read("warnings"))
        self.assertEqual((model.title("warnings"), model.title("completed")), ("Warnings", "Completed (1)"))
        self.assertFalse(model.mark_read("warnings"))  # nothing new to mark

        self.events.add("WARNING", "API")  # a later one counts again: "Warnings (1)"
        model.refresh()
        self.assertEqual(model.title("warnings"), "Warnings (1)")

    def test_the_unread_rows_are_the_ones_after_the_read_mark(self) -> None:
        model = self.model()
        model.refresh()
        self.events.add("WARNING", "API", message="a")
        self.events.add("WARNING", "SANITY_CHECK", message="b")
        self.events.add("PARTIAL_MOVE", message="c")
        model.refresh()
        self.assertEqual([row.message for row in model.unread_rows("warnings")], ["c", "b", "a"])  # newest first
        self.assertEqual(model.unread("warnings"), len(model.unread_rows("warnings")))
        model.mark_read("warnings")
        self.assertEqual(model.unread_rows("warnings"), [])
        self.events.add("WARNING", "MAILBOX", message="d")
        model.refresh()
        self.assertEqual([row.kind for row in model.unread_rows("warnings")], ["MAILBOX"])

    def test_a_tab_without_a_counter_has_no_unread_rows(self) -> None:
        model = self.model()
        model.refresh()
        self.events.add("WARNING", "LIMIT", message="x")
        model.refresh()
        self.assertEqual(model.unread_rows("limits"), [])  # Folder limits has no unread counter
        self.assertEqual(model.unread("limits"), 0)

    def test_read_marks_survive_a_restart(self) -> None:
        model = self.model()
        model.refresh()
        self.events.add("WARNING", "API")
        self.events.add("WARNING", "API")
        model.refresh()
        model.mark_read("warnings")
        self.events.add("WARNING", "API")

        restarted = self.model()  # same store, as if the GUI was closed and reopened
        restarted.refresh()

        self.assertEqual(restarted.seen_id("warnings"), 2)  # the stored read mark was kept
        self.assertEqual(restarted.title("warnings"), "Warnings (1)")  # so only the later event is unread

    def test_tabs_filter_by_type_and_category(self) -> None:
        self.events.add("WARNING", "LIMIT", message="over limit")
        self.events.add("WARNING", "API", message="api trouble")
        self.events.add("PARTIAL_MOVE", message="moved to Partial")
        self.events.add("COMPLETED_MOVE", tag="v1", message="done")
        self.events.add("CYCLE_SUMMARY", message="summary")
        model = self.model()
        model.refresh()

        self.assertEqual([r.message for r in model.rows("warnings")], ["moved to Partial", "api trouble"])
        self.assertEqual([r.message for r in model.rows("limits")], ["over limit"])
        self.assertEqual([r.message for r in model.rows("completed")], ["done"])

    def test_folder_limits_title_counts_the_repositories_over_their_limit_not_unread_rows(self) -> None:
        model = self.model()
        model.refresh()
        self.assertEqual(model.title("limits"), "Folder limits")
        self.events.add("WARNING", "LIMIT", repo="a/one", message="a/one has 12 release folders (limit 10)")
        self.events.add("WARNING", "LIMIT", repo="b/two", message="b/two has 11 release folders (limit 10)")
        model.refresh()
        self.assertEqual((model.unread("limits"), model.title("limits")), (0, "Folder limits (2)"))
        self.events.add("WARNING", "LIMIT", repo="a/one", message="a/one has 13 release folders (limit 10)")
        model.refresh()
        self.assertEqual(model.title("limits"), "Folder limits (2)")  # an already flagged repository stays one
        self.events.add("WARNING", "LIMIT", repo="c/three", message="c/three has 11 release folders (limit 10)")
        model.refresh()
        self.assertEqual(model.title("limits"), "Folder limits (3)")  # a new one counts
        model.mark_read("limits")  # looking at the tab changes nothing: it stays until the folders are fixed
        self.assertEqual(model.title("limits"), "Folder limits (3)")
        model.clear("limits")  # cleared (or fixed): the number goes away
        self.assertEqual(model.title("limits"), "Folder limits")

    def test_only_the_limits_tab_counts_rows(self) -> None:
        self.assertEqual([tab.key for tab in status_tabs.TAB_DEFS if tab.count_rows], ["limits"])

    def test_only_new_rows_are_fetched_and_the_list_is_capped(self) -> None:
        model = self.model(limit=3)
        for number in range(5):
            self.events.add("WARNING", "API", message=f"w{number}")
        model.refresh()
        self.assertEqual([r.message for r in model.rows("warnings")], ["w4", "w3", "w2"])

        self.events.add("WARNING", "API", message="w5")
        self.events.add("WARNING", "API", message="w6")
        model.refresh()
        self.assertEqual([r.message for r in model.rows("warnings")], ["w6", "w5", "w4"])

    def test_row_columns_and_repo_lookup(self) -> None:
        self.events.add("WARNING", "API", message="Could not resolve current commit hash for Owner/Repo @ v1.", created_at=1_700_000_000.0)
        self.events.add("COMPLETED_MOVE", repo="o/app", tag="v9", message="Completed [o/app v9]")
        self.events.add("WARNING", "LIMIT", message="Folder limit warning: nobody/else has 31 folder(s)")
        model = self.model(known_repos=lambda: {"owner/repo": "Owner/Repo", "o/app": "o/app"})
        model.refresh()

        warning = model.rows("warnings")[0]
        self.assertEqual((warning.kind, warning.repo), ("API", "Owner/Repo"))  # repo found in the text
        self.assertRegex(warning.time, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        completed = model.rows("completed")[0]
        self.assertEqual((completed.kind, completed.repo), ("v9", "o/app"))  # Type column = tag
        limit = model.rows("limits")[0]
        self.assertEqual((limit.kind, limit.repo), ("Limit", ""))  # unknown repo: left empty

    def test_a_reset_event_table_resets_the_read_marks(self) -> None:
        self.store.data = {"warnings": 5000, "completed": 5000, "limits": 5000}  # ids from an old database
        self.events.add("WARNING", "API")
        model = self.model()
        model.refresh()
        self.assertEqual(model.seen_id("warnings"), 1)
        self.events.add("WARNING", "API")
        model.refresh()
        self.assertEqual(model.unread("warnings"), 1)

    def test_marks_are_saved_only_when_they_change(self) -> None:
        model = self.model()
        model.refresh()
        saves_after_start = self.store.saves
        model.refresh()
        model.refresh()
        self.assertEqual(self.store.saves, saves_after_start)


class ReleaseTypeTests(unittest.TestCase):
    def test_the_release_type_is_read_from_the_destination_path(self) -> None:
        path = r"L:\Gaming\Emu\@GitHub\Pre-release\2026-10-06_13-24, NESd nightly, nightly, 2a91d3e"
        self.assertEqual(status_tabs.release_type_from_path(path), "Pre-release")
        self.assertEqual(status_tabs.release_type_from_path(path.replace("Pre-release", "Release")), "Release")
        self.assertEqual(status_tabs.release_type_from_path("K:/Apps/@GitHub/release/2026-10-06_13-24, x"), "Release")
        for odd in ("", None, r"K:\Apps\Release\notes", r"K:\Release Candidates\x"):
            self.assertEqual(status_tabs.release_type_from_path(odd), "", odd)

    def test_markers(self) -> None:
        from modules.repo_overview import format_tag, release_marker
        self.assertEqual((release_marker("Pre-release"), release_marker("Release"), release_marker(None)), ("(P)", "(R)", ""))
        self.assertEqual(format_tag("v1", "Pre-release"), "(P) v1")
        self.assertEqual(format_tag("v1", "Release"), "(R) v1")
        self.assertEqual(format_tag("v1", None), "v1")
        self.assertEqual(format_tag(None, "Release"), "")

    def test_completed_rows_carry_the_marker_and_the_tag(self) -> None:
        events = FakeEvents()
        events.add("COMPLETED_MOVE", tag="nightly", message="m")
        events.events[0]["destination_path"] = r"L:\@GitHub\Pre-release\2026-10-06_13-24, x"
        model = StatusTabsModel(status_tabs.TAB_DEFS, events.fetch, events.max_id, MemoryStore())
        model.refresh()
        events.add("COMPLETED_MOVE", tag="v2", message="m")
        events.events[1]["destination_path"] = r"L:\@GitHub\Release\2026-10-07_13-24, x"
        model.refresh()
        self.assertEqual([r.kind for r in model.rows("completed")], ["(R) v2", "(P) nightly"])  # newest first


class HelperTests(unittest.TestCase):
    def test_find_repo_only_returns_known_repositories(self) -> None:
        known = {"ip7z/7zip": "ip7z/7zip"}
        self.assertEqual(find_repo("Update for IP7Z/7ZIP (24.09) ready", known), "ip7z/7zip")
        self.assertEqual(find_repo("Folder L:/Games/Emulators is full", known), "")  # a path, not a repo
        self.assertEqual(find_repo("", known), "")

    def test_unmapped_rows_lists_entries_without_destination_newest_first(self) -> None:
        entries = [
            {"repository": "a/mapped", "destination": "K:\\Apps", "last_notification": "2026-10-06_10-00"},
            {"repository": "b/old", "destination": "", "folder": "Old", "last_notification": "2026-09-01_08-30"},
            {"repository": "c/new", "destination": "  ", "last_notification": "2026-10-06_04-47"},
            {"repository": "d/nodate", "last_notification": ""},
            {"destination": ""},  # no name: ignored
        ]
        rows = unmapped_rows(entries)

        self.assertEqual([r.repo for r in rows], ["c/new", "b/old", "d/nodate"])
        self.assertEqual(rows[0].first_seen, "2026-10-06 04:47")
        self.assertEqual(rows[1].folder, "Old")


class DatabaseQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._temp_dir.cleanup)
        db_path = os.path.join(self._temp_dir.name, "state.db")
        patcher = mock.patch.object(db_manager, "get_state_db_path", lambda: db_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.connection = db_manager.open_database()
        self.addCleanup(self.connection.close)

    def add(self, event_type, category=None, message="m") -> None:
        db_manager.insert_lifecycle_event(self.connection, event_type, message, category=category)

    def test_filters_orders_and_limits(self) -> None:
        self.add("WARNING", "API", "first")
        self.add("WARNING", "LIMIT", "limit")
        self.add("COMPLETED_MOVE", None, "done")
        self.add("WARNING", None, "uncategorised")
        self.add("WARNING", "API", "last")

        everything = db_manager.get_events_for_tab(self.connection, ("WARNING",), exclude_categories=("LIMIT",))
        self.assertEqual([e["message"] for e in everything], ["last", "uncategorised", "first"])  # newest first, NULL kept

        only_limit = db_manager.get_events_for_tab(self.connection, ("WARNING",), categories=("LIMIT",))
        self.assertEqual([e["message"] for e in only_limit], ["limit"])

        newer = db_manager.get_events_for_tab(self.connection, ("WARNING",), after_id=everything[-1]["id"])
        self.assertEqual(len(newer), 3)
        self.assertEqual(len(db_manager.get_events_for_tab(self.connection, ("WARNING",), limit=2)), 2)
        self.assertEqual(db_manager.get_events_for_tab(self.connection, ("WARNING",), after_id=db_manager.get_max_event_id(self.connection)), [])

    def test_purge_removes_exactly_what_the_tab_lists(self) -> None:
        self.add("WARNING", "API")
        self.add("WARNING", "MAPPING")
        self.add("PARTIAL_MOVE")
        self.add("WARNING", "LIMIT")
        self.add("COMPLETED_MOVE")
        warnings = (("WARNING", "PARTIAL_MOVE"), None, ("LIMIT",))

        self.assertEqual(db_manager.purge_events_for_tab(self.connection, *warnings, dry_run=True), 3)
        self.assertEqual(db_manager.get_max_event_id(self.connection), 5)  # dry run deleted nothing
        self.assertEqual(db_manager.purge_events_for_tab(self.connection, *warnings), 3)

        left = db_manager.get_events_for_tab(self.connection, ("WARNING", "COMPLETED_MOVE", "PARTIAL_MOVE"))
        self.assertEqual({(e["event_type"], e["category"]) for e in left}, {("WARNING", "LIMIT"), ("COMPLETED_MOVE", None)})

    def test_purge_for_one_repository_matches_its_own_warnings_only(self) -> None:
        def limit(repo):
            self.add("WARNING", "LIMIT", f"Folder limit warning: {repo} currently has 30 folder(s) in 'x' (limit=25).")
        limit("o/my_app")
        limit("O/MY_APP")
        limit("o/myXapp")  # "_" must not act as a wildcard
        limit("o/my_app2")  # a longer name is a different repository
        self.add("WARNING", "API", "Could not resolve o/my_app")

        self.assertEqual(db_manager.purge_limit_warnings_for_repo(self.connection, "o/my_app", dry_run=True), 2)
        self.assertEqual(db_manager.purge_limit_warnings_for_repo(self.connection, "o/my_app"), 2)
        left = [e["message"] for e in db_manager.get_events_for_tab(self.connection, ("WARNING",))]
        self.assertEqual(len(left), 3)
        self.assertEqual(db_manager.get_repos_with_limit_warnings(self.connection), {"o/myxapp", "o/my_app2"})

    def test_max_event_id(self) -> None:
        self.assertEqual(db_manager.get_max_event_id(self.connection), 0)
        self.add("WARNING", "API")
        self.assertEqual(db_manager.get_max_event_id(self.connection), 1)


class FeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._temp_dir.cleanup)
        folder = self._temp_dir.name
        db_path = os.path.join(folder, "state.db")
        self.config_path = os.path.join(folder, "config.json")
        self.mapping_path = os.path.join(folder, "mapping.json")
        for target, name, value in (
            (db_manager, "get_state_db_path", lambda: db_path),
            (config_manager, "_config_file_path", lambda: self.config_path),
            (mapping_manager, "_mapping_file_path", lambda: self.mapping_path),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        with open(self.mapping_path, "w", encoding="utf-8") as handle:
            json.dump({"repositories": [
                {"repository": "o/app", "destination": "K:\\Apps"},
                {"repository": "o/new", "destination": ""},
            ]}, handle)
        self.daemon = db_manager.open_database()  # stays open, like the daemon's connection
        self.addCleanup(self.daemon.close)

    def event(self, event_type, category=None, message="m", repo=None) -> None:
        db_manager.insert_lifecycle_event(self.daemon, event_type, message, category=category, repo=repo)

    def test_events_flow_from_state_db_to_tab_titles(self) -> None:
        self.event("WARNING", "API", "old warning")
        feed = gui_data.StatusFeed()
        feed.refresh()
        self.assertEqual(feed.model.title("warnings"), "Warnings")  # existing history starts as read

        self.event("WARNING", "API", "Could not resolve commit for o/app")
        self.event("COMPLETED_MOVE", message="Completed [o/app v1]", repo="o/app")
        self.assertEqual(feed.refresh(), {"warnings", "completed"})

        self.assertEqual((feed.model.title("warnings"), feed.model.title("completed")), ("Warnings (1)", "Completed (1)"))
        self.assertEqual(feed.model.rows("warnings")[0].repo, "o/app")

    def test_read_marks_are_stored_in_config_json_and_survive_a_restart(self) -> None:
        feed = gui_data.StatusFeed()
        feed.refresh()
        self.event("WARNING", "API", "w")
        feed.refresh()
        feed.model.mark_read("warnings")

        with open(self.config_path, encoding="utf-8") as handle:
            stored = json.load(handle)["gui"]["status_tabs"]
        self.assertEqual(stored["warnings"], 1)

        self.event("WARNING", "API", "after")
        reopened = gui_data.StatusFeed()
        reopened.refresh()
        self.assertEqual(reopened.model.title("warnings"), "Warnings (1)")

    def test_an_unchanged_database_is_not_queried_again(self) -> None:
        feed = gui_data.StatusFeed()
        feed.refresh()
        with mock.patch.object(db_manager, "get_events_for_tab", wraps=db_manager.get_events_for_tab) as query:
            feed.refresh()
            feed.refresh()
            feed.refresh()
            unchanged = query.call_count
            self.event("WARNING", "API", "new")
            feed.refresh()
        self.assertLessEqual(unchanged, 3 * 3)  # at most one settling pass (3 event tabs)
        self.assertGreater(query.call_count, unchanged)

    def test_clearing_a_tab_empties_only_that_tab_and_new_events_still_arrive(self) -> None:
        self.event("WARNING", "API", "w")
        self.event("WARNING", "LIMIT", "l")
        self.event("COMPLETED_MOVE", message="c", repo="o/app")
        feed = gui_data.StatusFeed()
        feed.refresh()
        self.assertEqual(feed.count_tab("warnings"), 1)

        self.assertEqual(feed.clear_tab("warnings"), 1)
        self.assertEqual(feed.model.rows("warnings"), [])
        self.assertEqual(len(feed.model.rows("limits")), 1)
        self.assertEqual(len(feed.model.rows("completed")), 1)

        self.event("WARNING", "API", "after clearing")
        self.assertIn("warnings", feed.refresh())
        self.assertEqual([row.message for row in feed.model.rows("warnings")], ["after clearing"])
        self.assertEqual(feed.model.title("warnings"), "Warnings (1)")  # ids are never reused, so it is unread

    def test_unmapped_list_comes_from_mapping_json(self) -> None:
        feed = gui_data.StatusFeed()
        self.assertEqual([row.repo for row in feed.unmapped()], ["o/new"])
        with open(self.mapping_path, "w", encoding="utf-8") as handle:
            json.dump({"repositories": [{"repository": "o/new", "destination": "K:\\Apps"}]}, handle)
        os.utime(self.mapping_path, ns=(1, 2_000_000_000_000_000_000))  # make the change visible to the stat cache
        self.assertEqual(feed.unmapped(), [])

    def test_events_deleted_elsewhere_disappear_from_the_tabs(self) -> None:
        self.event("WARNING", "LIMIT", "Folder limit warning: o/app currently has 30 folder(s)")
        self.event("WARNING", "LIMIT", "Folder limit warning: o/app currently has 31 folder(s)")
        self.event("WARNING", "API", "keep me")
        feed = gui_data.StatusFeed()
        feed.refresh()
        self.assertEqual(len(feed.model.rows("limits")), 1)

        with closing(db_manager.open_database()) as other:  # the daemon cleaning up after the folder shrank
            db_manager.purge_limit_warnings_for_repo(other, "o/app")
        self.assertEqual(feed.refresh(), {"limits"})
        self.assertEqual(feed.model.rows("limits"), [])
        self.assertEqual(len(feed.model.rows("warnings")), 1)

    def test_a_config_that_cannot_be_saved_does_not_break_the_feed(self) -> None:
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.write("{ broken")
        feed = gui_data.StatusFeed()
        feed.refresh()  # the baseline save fails silently
        self.event("WARNING", "API", "w")
        feed.refresh()
        self.assertTrue(feed.model.mark_read("warnings"))  # still works in memory


if __name__ == "__main__":
    unittest.main()
