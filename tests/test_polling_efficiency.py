"""Tests for the GUI's cheap polling: the stat cache, the snapshot reader and the queue-summary cache.

Run from the project root: python -m unittest discover -s tests -t .
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from modules import config_manager, daemon_control, db_manager, gui_daemon, gui_data
from modules.file_cache import StatCache, file_signature


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class StatCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.path = os.path.join(self._temp_dir.name, "data.txt")
        self.write("one")
        self.calls = 0
        self.clock = FakeClock()

    def write(self, text: str) -> None:
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def compute(self) -> str:
        self.calls += 1
        with open(self.path, encoding="utf-8") as handle:
            return handle.read()

    def cache(self, **kwargs) -> StatCache:
        return StatCache(lambda: [self.path], self.compute, clock=self.clock, **kwargs)

    def test_reuses_the_value_while_the_file_is_unchanged(self) -> None:
        cache = self.cache()
        self.assertEqual([cache.get(), cache.get(), cache.get()], ["one"] * 3)
        self.assertEqual(self.calls, 1)

    def test_recomputes_when_size_or_mtime_changes(self) -> None:
        cache = self.cache()
        cache.get()
        self.write("longer text")  # different size
        self.assertEqual(cache.get(), "longer text")
        self.assertEqual(self.calls, 2)

        info = os.stat(self.path)
        self.write("longer test")  # same size, then force a different mtime
        os.utime(self.path, ns=(info.st_atime_ns, info.st_mtime_ns + 5_000_000_000))
        self.assertEqual(cache.get(), "longer test")
        self.assertEqual(self.calls, 3)

    def test_max_age_forces_a_refresh_even_if_nothing_looks_changed(self) -> None:
        cache = self.cache(max_age=30.0)
        cache.get()
        self.clock.now += 29
        cache.get()
        self.assertEqual(self.calls, 1)
        self.clock.now += 2
        cache.get()
        self.assertEqual(self.calls, 2)

    def test_missing_file_is_cached_too_and_noticed_when_it_appears(self) -> None:
        os.remove(self.path)
        compute = mock.Mock(return_value="none")
        cache = StatCache(lambda: [self.path], compute, clock=self.clock)
        cache.get()
        cache.get()
        self.assertEqual(compute.call_count, 1)
        self.write("back")
        cache.get()
        self.assertEqual(compute.call_count, 2)

    def test_errors_are_not_cached(self) -> None:
        cache = StatCache(lambda: [self.path], mock.Mock(side_effect=[RuntimeError("boom"), "ok"]), clock=self.clock)
        with self.assertRaises(RuntimeError):
            cache.get()
        self.assertEqual(cache.get(), "ok")

    def test_a_different_path_is_a_cache_miss(self) -> None:
        other = os.path.join(self._temp_dir.name, "other.txt")
        with open(other, "w", encoding="utf-8") as handle:
            handle.write("two")
        current = {"path": self.path}

        def read_current() -> str:
            with open(current["path"], encoding="utf-8") as handle:
                return handle.read()

        cache = StatCache(lambda: [current["path"]], read_current, clock=self.clock)
        self.assertEqual(cache.get(), "one")
        current["path"] = other
        self.assertEqual(cache.get(), "two")

    def test_invalidate_forces_a_recompute(self) -> None:
        cache = self.cache()
        cache.get()
        cache.invalidate()
        cache.get()
        self.assertEqual(self.calls, 2)

    def test_signature_marks_missing_files(self) -> None:
        signature = file_signature([self.path, self.path + ".nope"])
        self.assertIsNotNone(signature[0])
        self.assertIsNone(signature[1])


class ConfigReadingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.config_path = os.path.join(self._temp_dir.name, "config.json")
        patcher = mock.patch.object(config_manager, "_config_file_path", lambda: self.config_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_strict_read_reports_a_broken_file_but_not_a_missing_one(self) -> None:
        self.assertEqual(config_manager.read_config_strict(), {})
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.write('{"polling": {"enabled": true,}')  # trailing comma, half-saved
        with self.assertRaises(config_manager.ConfigUnreadableError):
            config_manager.read_config_strict()
        self.assertEqual(config_manager.load_config(), {})  # the lenient reader still hides it

    def test_broken_config_never_shows_restart_needed(self) -> None:
        status = {
            "running": True, "pid": 7, "started_at": 1.0, "paused": False, "next_poll_at": None,
            "last_forced_poll_handled": None, "current_job": None, "config_fingerprint": "published",
        }
        control = {"paused": False, "poll_now_request": None, "log_override": None}
        reader = gui_daemon.SnapshotReader()
        with mock.patch.object(gui_daemon.daemon_lock, "get_daemon_status", return_value=status), \
                mock.patch.object(gui_daemon.daemon_control, "get_control_state", return_value=control):
            with open(self.config_path, "w", encoding="utf-8") as handle:
                json.dump({"polling": {"interval_seconds": 111}}, handle)
            self.assertTrue(reader.read().restart_needed)  # valid file that differs from "published"

            with open(self.config_path, "w", encoding="utf-8") as handle:
                handle.write("{ not json")
            self.assertFalse(reader.read().restart_needed)  # unknown, not "changed"

            with open(self.config_path, "w", encoding="utf-8") as handle:
                json.dump({"polling": {"interval_seconds": 111}}, handle)
            self.assertTrue(reader.read().restart_needed)  # fixed again


class SnapshotReaderTests(unittest.TestCase):
    STATUS = {
        "running": True, "pid": 7, "started_at": 1.0, "paused": False, "next_poll_at": 99.0,
        "last_forced_poll_handled": None, "current_job": None, "config_fingerprint": None,
    }

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.status = dict(self.STATUS)
        self.control = {"paused": False, "poll_now_request": None, "log_override": None}
        patches = (
            mock.patch.object(gui_daemon.daemon_lock, "get_daemon_status", side_effect=lambda: dict(self.status)),
            mock.patch.object(gui_daemon.daemon_control, "get_control_state", side_effect=lambda: dict(self.control)),
            mock.patch.object(gui_daemon.config_manager, "read_config_strict", return_value={}),
        )
        self.get_status, self.get_control, _ = (patch.start() for patch in patches)
        for patch in patches:
            self.addCleanup(patch.stop)
        self.reader = gui_daemon.SnapshotReader(control_interval=3.0, clock=self.clock)

    def test_control_table_is_read_only_every_few_seconds(self) -> None:
        for _ in range(3):  # three 1-second ticks: one read at the start
            self.reader.read()
            self.clock.now += 1
        self.assertEqual(self.get_control.call_count, 1)
        self.reader.read()  # 3 s after the first read
        self.assertEqual(self.get_control.call_count, 2)

    def test_forcing_reads_the_control_table_right_away(self) -> None:
        self.reader.read()
        self.reader.read(force_control=True)
        self.assertEqual(self.get_control.call_count, 2)

    def test_a_new_daemon_pid_forces_a_read(self) -> None:
        self.reader.read()
        self.status["pid"] = 8
        self.reader.read()
        self.assertEqual(self.get_control.call_count, 2)

    def test_stopped_daemon_costs_no_control_read_and_forgets_the_old_one(self) -> None:
        self.reader.read()
        self.status["running"] = False
        self.assertFalse(self.reader.read().running)
        self.status["running"] = True
        self.reader.read()
        self.assertEqual(self.get_control.call_count, 2)  # the restarted daemon is asked again

    def test_fresh_control_read_outranks_the_lagging_status_file(self) -> None:
        self.control["paused"] = True
        self.assertTrue(self.reader.read().paused)  # status file still says "not paused"
        self.control["paused"] = False
        self.assertFalse(self.reader.read(force_control=True).paused)

    def test_older_status_file_value_wins_between_control_reads(self) -> None:
        self.reader.read()  # control read at t=0: not paused
        self.clock.now += 2.5  # control value is now stale; someone paused via the CLI
        self.status["paused"] = True
        self.assertTrue(self.reader.read().paused)  # seen through the status file, no db read needed
        self.assertEqual(self.get_control.call_count, 1)

    def test_busy_database_keeps_the_last_control_values(self) -> None:
        self.control["log_override"] = True
        self.assertTrue(self.reader.read().log_on)
        self.clock.now += 5
        self.get_control.side_effect = __import__("sqlite3").OperationalError("database is locked")
        self.assertTrue(self.reader.read().log_on)  # still the last known override


class SummaryCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._temp_dir.cleanup)
        db_path = os.path.join(self._temp_dir.name, "state.db")
        patcher = mock.patch.object(db_manager, "get_state_db_path", lambda: db_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(gui_data._summaries_cache.invalidate)
        gui_data._summaries_cache.invalidate()

        # A long-lived connection like the daemon's: keeps the WAL file in place between commits.
        self.daemon_connection = db_manager.open_database()
        self.addCleanup(self.daemon_connection.close)

    def table(self):
        with mock.patch.object(gui_data.daemon_lock, "get_daemon_status", return_value={"current_job": None}):
            return gui_data.load_repo_table()

    def test_the_queue_is_queried_only_when_state_db_changed(self) -> None:
        real = db_manager.get_repo_job_summaries
        with mock.patch.object(db_manager, "get_repo_job_summaries", side_effect=real) as query:
            self.table()
            self.table()
            self.table()
            unchanged_calls = query.call_count

            db_manager.enqueue_job(self.daemon_connection, "o/new", "v1")
            self.daemon_connection.commit()
            self.table()
            self.table()
            changed_calls = query.call_count

        self.assertLessEqual(unchanged_calls, 2)  # the first load, plus at most one settling reload
        self.assertGreater(changed_calls, unchanged_calls)  # the daemon wrote: reloaded
        self.assertLessEqual(changed_calls, unchanged_calls + 2)

    def test_new_jobs_show_up_in_the_rows(self) -> None:
        db_manager.enqueue_job(self.daemon_connection, "o/first", "v1")
        self.daemon_connection.commit()
        with mock.patch.object(gui_data.mapping_manager, "load_mapping",
                               return_value={"repositories": [{"name": "o/first", "foldername": "First"}]}):
            before = self.table()
            db_manager.enqueue_job(self.daemon_connection, "o/first", "v2")
            self.daemon_connection.commit()
            after = self.table()

        self.assertEqual(before.rows[0].tag, "v1")
        self.assertEqual(after.rows[0].tag, "v2")


if __name__ == "__main__":
    unittest.main()
