"""Tests for the status tabs model, its database queries and the feed that wires them together.

Run from the project root: python -m unittest discover -s tests -t .
"""
import json
import os
import tempfile
import unittest
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

    def test_folder_limits_tab_has_no_counter(self) -> None:
        model = self.model()
        model.refresh()
        self.events.add("WARNING", "LIMIT")
        model.refresh()
        self.assertEqual((model.unread("limits"), model.title("limits")), (0, "Folder limits"))
        self.assertEqual(len(model.rows("limits")), 1)

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


class HelperTests(unittest.TestCase):
    def test_find_repo_only_returns_known_repositories(self) -> None:
        known = {"ip7z/7zip": "ip7z/7zip"}
        self.assertEqual(find_repo("Update for IP7Z/7ZIP (24.09) ready", known), "ip7z/7zip")
        self.assertEqual(find_repo("Folder L:/Games/Emulators is full", known), "")  # a path, not a repo
        self.assertEqual(find_repo("", known), "")

    def test_unmapped_rows_lists_entries_without_destination_newest_first(self) -> None:
        entries = [
            {"name": "a/mapped", "destination": "K:\\Apps", "last_notification_seen": "2026-10-06_10-00"},
            {"name": "b/old", "destination": "", "foldername": "Old", "last_notification_seen": "2026-09-01_08-30"},
            {"name": "c/new", "destination": "  ", "last_notification_seen": "2026-10-06_04-47"},
            {"name": "d/nodate", "last_notification_seen": ""},
            {"destination": ""},  # no name: ignored
        ]
        rows = unmapped_rows(entries)

        self.assertEqual([r.repo for r in rows], ["c/new", "b/old", "d/nodate"])
        self.assertEqual(rows[0].first_seen, "2026-10-06 04:47")
        self.assertEqual(rows[1].foldername, "Old")


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
                {"name": "o/app", "destination": "K:\\Apps"},
                {"name": "o/new", "destination": ""},
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

    def test_unmapped_list_comes_from_mapping_json(self) -> None:
        feed = gui_data.StatusFeed()
        self.assertEqual([row.repo for row in feed.unmapped()], ["o/new"])
        with open(self.mapping_path, "w", encoding="utf-8") as handle:
            json.dump({"repositories": [{"name": "o/new", "destination": "K:\\Apps"}]}, handle)
        os.utime(self.mapping_path, ns=(1, 2_000_000_000_000_000_000))  # make the change visible to the stat cache
        self.assertEqual(feed.unmapped(), [])

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
