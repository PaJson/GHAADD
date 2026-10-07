"""The tooltip texts (modules/gui_tooltips.py) cover everything the GUI looks up.

Run from the project root: python -m unittest discover -s tests -t .
"""
import unittest

from modules import gui_tooltips

try:
    import main_gui
except ImportError:  # no Tk on this machine
    main_gui = None


@unittest.skipIf(main_gui is None, "tkinter is not available")
class TooltipCoverageTests(unittest.TestCase):
    def test_every_editor_field_and_button_has_help(self) -> None:
        layout = main_gui.MappingsTab.FORM_LAYOUT
        keys = {cell[0] for column in layout for row in column for cell in row}
        keys |= {"active", "shared_destination", "open_folder", "github"}
        self.assertEqual(keys - set(gui_tooltips.FIELD_HELP), set())

    def test_every_table_column_has_help_except_the_status_legend_column(self) -> None:
        keys = {column[0] for column in main_gui.MappingsTab.TABLE_COLUMNS} - {"icon"}
        self.assertEqual(keys - set(gui_tooltips.COLUMN_HELP), set())

    def test_every_status_has_a_hint(self) -> None:
        self.assertEqual(set(main_gui.STATUS_ICONS), set(gui_tooltips.STATUS_HINTS))

    def test_the_legend_lists_every_status(self) -> None:
        for name in main_gui.STATUS_ICONS:
            self.assertIn(name, main_gui.STATUS_LEGEND)


class TooltipTextTests(unittest.TestCase):
    def test_no_text_is_empty(self) -> None:
        for table in (gui_tooltips.FIELD_HELP, gui_tooltips.COLUMN_HELP, gui_tooltips.STATUS_HINTS, gui_tooltips.CONTROL_HELP):
            for key, text in table.items():
                self.assertTrue(text.strip(), key)

    def test_the_limit_note_template_fills_in(self) -> None:
        text = gui_tooltips.LIMIT_NOTE.format(count=12, allowed=15, when="2026-10-07 10:00:00")
        self.assertEqual(text, "12 of 15 allowed folders (counted 2026-10-07 10:00:00)")


if __name__ == "__main__":
    unittest.main()
