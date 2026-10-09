import os
import tempfile
import unittest

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

    def test_open_target_falls_back_to_existing_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(work_folders.open_target(tmp), os.path.normpath(tmp))
            self.assertEqual(work_folders.open_target(os.path.join(tmp, "no", "where")), os.path.normpath(tmp))


if __name__ == "__main__":
    unittest.main()
