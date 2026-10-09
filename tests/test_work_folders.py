import os
import tempfile
import unittest
from unittest import mock

from modules import work_folders


class WorkFoldersTests(unittest.TestCase):
    def test_paths_follow_the_config(self) -> None:
        config = {"paths": {"default_download_dir": "base"}, "folders": {"ghaadd_root": "G", "partial": "Half"}}
        found = {item.key: item for item in work_folders.work_folders(config)}
        self.assertEqual(list(found), ["complete", "logs", "partial", "processing"])
        self.assertEqual(found["partial"].path, os.path.join(os.path.normpath("base"), "G", "Half"))
        self.assertEqual(found["logs"].label, "Logs")

    def test_count_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(work_folders.count_entries(tmp), 0)
            os.mkdir(os.path.join(tmp, "a"))
            open(os.path.join(tmp, "b.txt"), "w").close()
            self.assertEqual(work_folders.count_entries(tmp), 2)
            self.assertEqual(work_folders.count_entries(os.path.join(tmp, "missing")), 0)

    def test_button_text_hides_zero(self) -> None:
        self.assertEqual(work_folders.button_text("Partial", 0), "Partial")
        self.assertEqual(work_folders.button_text("Logs", 30), "Logs (30)")

    def test_cache_relists_only_after_a_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = {"paths": {"default_download_dir": tmp}}
            root = os.path.join(tmp, "GHAADD")
            os.makedirs(os.path.join(root, "Complete"))
            with mock.patch.object(work_folders.config_manager, "load_config", return_value=config), \
                    mock.patch.object(work_folders, "count_entries", wraps=work_folders.count_entries) as counter:
                cache = work_folders.folder_count_cache()
                self.assertEqual(cache.get()["complete"], 0)
                calls = counter.call_count
                cache.get()
                self.assertEqual(counter.call_count, calls)  # nothing changed: no new listing
                os.mkdir(os.path.join(root, "Complete", "repo"))
                os.utime(os.path.join(root, "Complete"), ns=(1, 1))  # force a visible mtime change on coarse clocks
                self.assertEqual(cache.get()["complete"], 1)

    def test_open_target_falls_back_to_existing_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(work_folders.open_target(tmp), os.path.normpath(tmp))
            self.assertEqual(work_folders.open_target(os.path.join(tmp, "no", "where")), os.path.normpath(tmp))


if __name__ == "__main__":
    unittest.main()
