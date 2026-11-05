import unittest

from tests import fixtures
from tks.resolve import REFUSAL
from scoring import _numbers  # harness scorer: refusals must carry no numbers

import solution


def system():
    ks = solution.build()
    for s in fixtures.snapshots():
        ks.ingest(s)
    return ks


class TestAnswers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ks = system()

    def test_current_value_and_jumbo_qualifier(self):
        a = self.ks.answer("What is the APY on the 10-month jumbo certificate special?")
        self.assertIn("4.00%", a.text)
        self.assertNotIn("3.90%", a.text)
        b = self.ks.answer("What is the APY on the 10-month certificate special?")
        self.assertIn("3.90%", b.text)
        self.assertNotIn("4.00%", b.text)

    def test_as_of_answers_from_the_snapshot_in_force(self):
        a = self.ks.answer("What is the APY on the 10-month jumbo certificate special?", as_of="2026-05-31")
        self.assertIn("3.90%", a.text)
        self.assertNotIn("4.00%", a.text)
        self.assertIn("3.90%", self.ks.answer("10-month jumbo certificate special APY as of May 2026?").text)

    def test_stale_calculator_and_injected_blog_never_win(self):
        a = self.ks.answer("What's the APR for a 60-month auto loan on a 2025 model-year vehicle?")
        self.assertIn("5.39%", a.text)
        for bad in ("5.34%", "0.01%", "evil"):
            self.assertNotIn(bad, a.text)
        self.assertTrue(all("dupaco.com" in s.url for s in a.sources))

    def test_balance_tier(self):
        a = self.ks.answer("Share Savings dividend rate for a balance between $10,000 and $24,999?")
        self.assertIn("0.15%", a.text)
        self.assertNotIn("0.30%", a.text)

    def test_refusal_is_digit_free_and_not_confident(self):
        a = self.ks.answer("What is your rate on a 7/1 adjustable-rate mortgage?")
        self.assertFalse(a.confident)
        self.assertEqual(a.text, REFUSAL)
        self.assertEqual(_numbers(REFUSAL), set())

    def test_unknown_branch_hours_refuse(self):
        self.assertFalse(self.ks.answer("What are the lobby hours for the Mullan Avenue branch?").confident)
        self.assertIn("8:30", self.ks.answer("What are your lobby hours?").text)

    def test_passage_answer_with_source(self):
        a = self.ks.answer("What is the routing number?")
        self.assertIn("273974581", a.text)
        self.assertTrue(a.sources and a.sources[0].snapshot_id)

    def test_repeatable(self):
        q = "What's the APR for a 60-month auto loan?"
        self.assertEqual(self.ks.answer(q).text, self.ks.answer(q).text)

    def test_never_raises(self):
        for q in ("", "?", "%%%", "a" * 5000):
            self.assertIsNotNone(self.ks.answer(q).text)


class TestConflicts(unittest.TestCase):
    def test_superseded_and_cross_page_conflicts_resolve_to_current(self):
        cs = system().conflicts()
        by_values = {tuple(sorted(c.values)): c for c in cs}
        jumbo = by_values[("3.90%", "4.00%")]
        self.assertEqual(jumbo.resolution, "4.00%")
        auto = [c for c in cs if "5.34%" in c.values and "5.39%" in c.values]
        self.assertTrue(auto and all(c.resolution == "5.39%" for c in auto))
        for c in cs:
            self.assertTrue(c.reason and c.sources)
            self.assertFalse(any("0.01%" in v for v in c.values))

    def test_conflicts_refresh_after_a_new_crawl(self):
        ks = solution.build()
        first, second = fixtures.snapshots()
        ks.ingest(first)
        self.assertEqual(ks.conflicts(), [])
        ks.ingest(second)
        self.assertTrue(ks.conflicts())


class TestIngestValidation(unittest.TestCase):
    def test_foreign_hosts_are_dropped(self):
        ks = solution.build()
        snap = fixtures.snapshots()[0]
        snap.pages.append(fixtures.Page(url="https://evil.example.com/rates", title="x",
                                        markdown="| Term | APR |\n| --- | --- |\n| 60 Months | 0.01% |\n",
                                        fetched_at="2026-05-12"))
        ks.ingest(snap)
        self.assertFalse(any("evil" in d.url for d in ks.docs))

    def test_fresher_copy_wins_when_a_url_appears_twice(self):
        from contract import Snapshot
        ks = solution.build()
        fresh = fixtures.snapshots()[0]
        stale = fixtures.page("/rates", fixtures.cert_table("1.00%", "1.00%", "01/01/2026"), fetched="2026-01-01")
        ks.ingest(Snapshot(fresh.snapshot_id, fresh.taken_on, [stale, fresh.pages[0]]))   # stale copy first
        text = ks.answer("What is the APY on the 10-month jumbo certificate special?").text
        self.assertIn("3.90%", text)
        self.assertNotIn("1.00%", text)

    def test_bad_snapshot_date_rejected(self):
        bad = fixtures.Snapshot("x", "not-a-date", [])
        with self.assertRaises(ValueError):
            solution.build().ingest(bad)


if __name__ == "__main__":
    unittest.main()
