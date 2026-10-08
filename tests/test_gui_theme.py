import unittest

from modules import config_manager, gui_theme


class GuiThemeTests(unittest.TestCase):
    def test_palettes_have_the_same_keys(self):
        self.assertEqual(set(gui_theme.LIGHT), set(gui_theme.DARK))

    def test_color_swaps_map_the_other_palette_to_this_one(self):
        swaps = gui_theme.color_swaps(True)
        self.assertEqual(swaps[gui_theme.LIGHT["stripe"].lower()], gui_theme.DARK["stripe"])
        self.assertEqual(gui_theme.color_swaps(False)[gui_theme.DARK["muted"].lower()], gui_theme.LIGHT["muted"])

    def test_button_names_the_mode_it_switches_to(self):
        self.assertIn("Light", gui_theme.button_text(True))
        self.assertIn("Dark", gui_theme.button_text(False))

    def test_dark_mode_setting_defaults_to_off_and_ignores_non_booleans(self):
        self.assertFalse(config_manager.get_gui_dark_mode({}))
        self.assertTrue(config_manager.get_gui_dark_mode({"gui": {"dark_mode": True}}))
        self.assertFalse(config_manager.get_gui_dark_mode({"gui": {"dark_mode": "yes"}}))


if __name__ == "__main__":
    unittest.main()
