import unittest

from tests import fixtures  # noqa: F401  (puts submission/ on sys.path)
from tks.extract import (canon_url, clean_inline, hours_blocks, model_years, page_kind, parse_tables,
                         passages, published_date, terms, tier_range)

RATES_MD = """# Rates

Certificates

Special Certificates

| Certificate | Dividend Rate | Annual Percentage Yield (APY) | Min. to Earn |
| --- | --- | --- | --- |
| 10 Month Fixed Jumbo Certificate Special | 3.96% | 4.00%1 | $100,000.00 |
| 10 Month Fixed Certificate Special | 3.86% | 3.90%2 | $1,000.00 |

Rates are effective as of 09/01/2026 and are subject to change without notice.\\\\r\\\\n

Credit Cards

Platinum Visa

| Card Type | APR |
| --- | --- |
| Introductory Balance Transfer Rate | 2.90%1 |
| Platinum Visa Rate | 12.50% – 19.50%2 |
"""


class TestUrlsAndKinds(unittest.TestCase):
    def test_canon_url_drops_query_fragment_and_trailing_slash(self):
        self.assertEqual(canon_url("https://www.dupaco.com/Personal/Loans/?a=1#rates"),
                         "https://www.dupaco.com/personal/loans")
        self.assertEqual(canon_url("https://www.dupaco.com/"), "https://www.dupaco.com/")

    def test_page_kinds_rank_rate_sheets_over_stale_sources(self):
        self.assertEqual(page_kind("https://www.dupaco.com/rates"), "rate_sheet")
        self.assertEqual(page_kind("https://www.dupaco.com/personal/loans/auto-motorcycle-loans"), "product")
        self.assertEqual(page_kind("https://www.dupaco.com/calculators/vehicle-affordability"), "calculator")
        self.assertEqual(page_kind("https://www.dupaco.com/switch-and-earn"), "promo")
        self.assertEqual(page_kind("https://www.dupaco.com/learn/blog/x"), "blog")
        self.assertEqual(page_kind("https://www.dupaco.com/newsroom/x"), "news")
        self.assertEqual(page_kind("https://www.dupaco.com/newsroom/tag/x"), "listing")
        self.assertEqual(page_kind("https://www.dupaco.com/hubfs/a.pdf"), "file")


class TestTables(unittest.TestCase):
    def setUp(self):
        self.rows = parse_tables(RATES_MD)

    def test_rows_keep_caption_labels_and_effective_date(self):
        jumbo = self.rows[0]
        self.assertEqual(jumbo.caption, "Certificates Special Certificates")
        self.assertEqual(jumbo.labels, ["10 Month Fixed Jumbo Certificate Special"])
        self.assertEqual(jumbo.effective, "2026-09-01")

    def test_footnote_markers_are_stripped_from_values(self):
        self.assertEqual(self.rows[0].values, [("dividend", "3.96%"), ("apy", "4.00%")])
        self.assertEqual(self.rows[1].values, [("dividend", "3.86%"), ("apy", "3.90%")])

    def test_ranges_and_non_metric_columns(self):
        visa = self.rows[3]
        self.assertEqual(visa.values, [("apr", "12.50%–19.50%")])
        self.assertEqual(visa.caption, "Credit Cards Platinum Visa")
        self.assertNotIn("$100,000.00", " ".join(self.rows[0].labels))

    def test_tables_without_percent_values_yield_no_rows(self):
        self.assertEqual(parse_tables("| Category | Amount |\n| --- | --- |\n| Loan | $5,000 |\n"), [])


class TestTiers(unittest.TestCase):
    def test_tier_ranges(self):
        self.assertEqual(tier_range("$10,000 – $24,999.99"), (10000.0, 24999.99))
        self.assertEqual(tier_range("$25,000+"), (25000.0, float("inf")))
        self.assertEqual(tier_range("Purchase: 30-Year Fixed Conventional $100,000 or less"), (0.0, 100000.0))
        self.assertEqual(tier_range("$10,000-24,999"), (10000.0, 24999.0))
        self.assertIsNone(tier_range("60 Months"))

    def test_terms_and_model_years(self):
        self.assertEqual(terms("Purchase: 30-Year Fixed"), {360})
        self.assertEqual(terms("2024-27 Model Year 60 Months"), {60})
        self.assertEqual(model_years("2024-27 Model Year"), (2024, 2027))
        self.assertEqual(model_years("Model Year 2026-23"), (2023, 2026))
        self.assertEqual(model_years("2016 Model Year (and older)"), (0, 2016))


class TestHoursPassagesDates(unittest.TestCase):
    def test_hours_block(self):
        md = ("Branch hours are:\n\n**Lobby**\n\nMonday-Thursday: 8:30 a.m.-5 p.m.\n\nFriday: 8:30 a.m.-6 p.m.\n\n"
              "Sunday: Closed\n\n**Drive-up**\n\nMonday-Friday: 8 a.m.-6 p.m.\n\nSunday: ATM only\n\n**ABOUT DUPACO**\n")
        (lobby, drive), = hours_blocks(md)
        self.assertEqual(lobby, ("Monday-Thursday: 8:30 a.m.-5 p.m.", "Friday: 8:30 a.m.-6 p.m.", "Sunday: Closed"))
        self.assertEqual(drive, ("Monday-Friday: 8 a.m.-6 p.m.", "Sunday: ATM only"))

    def test_inline_markdown_is_flattened_but_links_kept(self):
        self.assertEqual(clean_inline("**Join** [online](https://join.dupaco.com/) ![x](https://a/b.png)"),
                         "Join online (https://join.dupaco.com/)")

    def test_passages_carry_heading_and_skip_images(self):
        ps = passages("## Join\n\n![img](https://x/y.png)\n\nApply at [join](https://join.dupaco.com/).\n")
        self.assertEqual(ps, [("Join", "Apply at join (https://join.dupaco.com/).")])

    def test_published_date(self):
        self.assertEqual(published_date("2 min read\n\nMarch 02, 2026\n\nBody"), "2026-03-02")
        self.assertIsNone(published_date("no date here"))


if __name__ == "__main__":
    unittest.main()
