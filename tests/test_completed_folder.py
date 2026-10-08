"""The Completed tab's "Name (folder)" column: the name shown and the folder a double-click opens.

Both come from the same mapping.json entry the Mappings tab uses, so they must agree with its "Open folder" button.
Run from the project root: python -m unittest discover -s tests -t .
"""
import os
import tempfile
import unittest

from modules import gui_forms


class FolderDisplayNameTests(unittest.TestCase):
    def test_the_entry_s_own_name_wins(self) -> None:
        self.assertEqual(gui_forms.folder_display_name("o/app", {"folder": "My App"}), "My App")

    def test_a_missing_or_blank_name_falls_back_to_the_default(self) -> None:
        self.assertEqual(gui_forms.folder_display_name("owner/app", {"folder": "  "}), "app (owner)")
        self.assertEqual(gui_forms.folder_display_name("owner/app", {}), "app (owner)")

    def test_an_unmapped_repository_has_no_name(self) -> None:
        self.assertEqual(gui_forms.folder_display_name("owner/app", None), "")


class OpenFolderForEntryTests(unittest.TestCase):
    def test_it_matches_what_the_open_folder_button_resolves(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root:
            target = os.path.join(root, "App", "Nightly")
            os.makedirs(target)
            entry = {"destination": root, "folder": "App", "subfolder": "Nightly"}
            self.assertEqual(gui_forms.open_folder_for_entry("o/app", entry), target)
            self.assertEqual(
                gui_forms.open_folder_for_entry("o/app", entry),
                gui_forms.resolve_open_folder("o/app", root, "App", "Nightly"),
            )

    def test_a_folder_not_created_yet_falls_back_to_its_nearest_parent(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root:
            entry = {"destination": root, "folder": "Never created"}
            self.assertEqual(gui_forms.open_folder_for_entry("o/app", entry), os.path.normpath(root))

    def test_nothing_to_open_without_an_entry_or_destination(self) -> None:
        self.assertIsNone(gui_forms.open_folder_for_entry("o/app", None))
        self.assertIsNone(gui_forms.open_folder_for_entry("o/app", {"destination": ""}))
        self.assertIsNone(gui_forms.open_folder_for_entry("o/app", {"destination": "/definitely/not/here"}))


if __name__ == "__main__":
    unittest.main()
