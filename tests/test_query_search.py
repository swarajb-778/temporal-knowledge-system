import unittest

from tests import fixtures  # noqa: F401
from tks.query import parse
from tks.search import BM25, tokens


class TestParse(unittest.TestCase):
    def test_date_in_text_sets_as_of_and_is_removed_from_tokens(self):
        q = parse("What was the 10-month jumbo certificate special as of May 2026?")
        self.assertEqual(q.as_of, "2026-05-31")
        self.assertNotIn("may", q.toks)
        self.assertEqual(q.terms, {10})
        self.assertIn("jumbo", q.flags)

    def test_explicit_as_of_wins_and_may_the_verb_is_not_a_date(self):
        self.assertEqual(parse("rate as of June 2026", as_of="2026-09-01").as_of, "2026-09-01")
        self.assertIsNone(parse("May I see the auto loan rates?").as_of)

    def test_number_words_terms_years_amounts(self):
        q = parse("Thirty-year fixed mortgage for $100k on a 2025 model-year car over six months")
        self.assertEqual(q.terms, {360, 6})
        self.assertEqual(q.years, [2025])
        self.assertIn(100000.0, q.amounts)

    def test_intents(self):
        self.assertEqual(parse("What are the drive-thru hours?").hours_kind, "drive")
        self.assertEqual(parse("What are the drive-thru hours?").intent, "hours")
        self.assertEqual(parse("What link shows certificate rates?").intent, "url")
        self.assertEqual(parse("How many branches are there?").intent, "count")
        self.assertEqual(parse("What APRs do your cards have?").intent, "rate")
        self.assertEqual(parse("Do I pay a fee at other ATMs?").intent, "general")

    def test_branch_names_are_case_insensitive(self):
        for text in ("Lobby hours for the Mullan Avenue branch?", "LOBBY HOURS FOR THE MULLAN AVENUE BRANCH?",
                     "lobby hours at the branch in mullan avenue"):
            self.assertEqual(parse(text).branch, "mullan avenue", text)
        self.assertIsNone(parse("How many branches does Dupaco have?").branch)

    def test_intro_negation_and_segments(self):
        self.assertNotIn("intro", parse("Ignoring the intro offer, what is the Platinum Visa APR?").flags)
        self.assertNotIn("business", parse("personal and business credit card APRs").flags)


class TestSearch(unittest.TestCase):
    def test_tokens_normalize_synonyms_and_stopwords(self):
        self.assertEqual(tokens("What are your CD rates?"), ["certificate", "rate"])
        self.assertEqual(tokens("How many branches does Dupaco have"), ["branch"])
        self.assertEqual(tokens("recognized"), tokens("recognition"))

    def test_bm25_ties_break_on_id(self):
        ix = BM25()
        for _ in range(3):
            ix.add(["auto", "loan"])
        self.assertEqual([p for _, p in ix.search(["auto"])], [0, 1, 2])


if __name__ == "__main__":
    unittest.main()
