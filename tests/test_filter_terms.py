"""The multi-word filter shared by every tab (words in any order, "quotes" keep a phrase together)."""
import unittest

from modules import gui_forms


class FilterTermsTests(unittest.TestCase):
    def test_words_are_split_and_case_folded(self) -> None:
        self.assertEqual(gui_forms.filter_terms("  Lichii   STAGING "), ["lichii", "staging"])

    def test_quotes_keep_a_phrase_together(self) -> None:
        self.assertEqual(gui_forms.filter_terms('"queue: due" Staging'), ["queue: due", "staging"])

    def test_an_unfinished_quote_is_a_phrase_to_the_end(self) -> None:
        self.assertEqual(gui_forms.filter_terms('a "b c'), ["a", "b c"])

    def test_empty_text_and_empty_quotes_give_no_terms(self) -> None:
        self.assertEqual(gui_forms.filter_terms(""), [])
        self.assertEqual(gui_forms.filter_terms('  "" '), [])

    def test_every_term_must_be_present_in_any_order(self) -> None:
        line = "Lichii release for staging"
        self.assertTrue(gui_forms.matches_terms(line, gui_forms.filter_terms("staging lichii")))
        self.assertFalse(gui_forms.matches_terms(line, gui_forms.filter_terms("lichii production")))
        self.assertFalse(gui_forms.matches_terms(line, gui_forms.filter_terms('"lichii staging"')))
        self.assertTrue(gui_forms.matches_terms(line, []))


if __name__ == "__main__":
    unittest.main()
