"""Price-table lookups.

The two behave differently on a capacity miss and it is easy to get backwards,
which is the reason this moved out of the board script into something testable.
"""

from __future__ import annotations

from phoneloop.pricing import cex_cash, coverage, ebay_ceiling, load_table

CEX = {
    "iphone 13": {"model": "iphone-13", "storage_gb": None,
                  "cash_pence": {"A": 17300, "B": 12300, "C": 10300}},
    "iphone 14 pro max 128gb": {"model": "iphone-14-pro-max", "storage_gb": 128,
                                "cash_pence": {"A": 25500, "B": 20600}},
    "iphone 15 pro": {"model": "iphone-15-pro", "storage_gb": None,
                      "cash_pence": {"A": None, "B": 21700}},
}

EBAY = {
    "iphone 13": {"model": "iphone-13", "storage_gb": None, "ceiling_pence": 16000},
    "iphone 14 pro max 128gb": {"model": "iphone-14-pro-max", "storage_gb": 128,
                                "ceiling_pence": 42600},
}


class TestCex:
    def test_an_exact_capacity_match(self):
        assert cex_cash(CEX, "iphone-14-pro-max", 128) == (20600, True)

    def test_the_grade_is_selectable(self):
        assert cex_cash(CEX, "iphone-13", None, grade="A")[0] == 17300

    def test_it_falls_back_to_the_model_wide_figure(self):
        """That figure is the cheapest capacity CeX stocks, so it errs low --
        and a low exit only ever makes a buy look worse, never better."""
        assert cex_cash(CEX, "iphone-13", 512) == (12300, False)

    def test_a_missing_grade_is_none_not_a_substitute(self):
        """CeX genuinely lists no grade A for some handsets. Quietly serving
        the B price would overstate a pristine unit's exit."""
        assert cex_cash(CEX, "iphone-15-pro", None, grade="A")[0] is None

    def test_an_unknown_model_is_none(self):
        assert cex_cash(CEX, "pixel-9", 128) == (None, False)


class TestEbay:
    def test_an_exact_capacity_match(self):
        assert ebay_ceiling(EBAY, "iphone-14-pro-max", 128) == (42600, True)

    def test_a_capacity_miss_is_marked_inexact(self):
        assert ebay_ceiling(EBAY, "iphone-13", 256) == (16000, False)

    def test_two_unknowns_agreeing_is_not_an_exact_match(self):
        """A listing with no stated capacity, priced off a capacity-mixed
        median, is the widest uncertainty on the board -- and it looks like the
        cleanest row, which is why it must not be reported as exact."""
        assert ebay_ceiling(EBAY, "iphone-13", None) == (16000, False)

    def test_an_unknown_model_is_none(self):
        assert ebay_ceiling(EBAY, "pixel-9", 128) == (None, False)


class TestTables:
    def test_a_missing_file_is_an_empty_table(self, tmp_path):
        assert load_table(tmp_path / "nope.json") == {}

    def test_a_malformed_file_raises_rather_than_reading_as_empty(self, tmp_path):
        """An empty table shows on the board as "nothing is priceable", which
        is a market conclusion, not a file error."""
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        try:
            load_table(bad)
        except ValueError:
            return
        raise AssertionError("a broken table must not read as an empty one")

    def test_coverage_lists_priceable_models(self):
        assert coverage(CEX) == {"iphone-13", "iphone-14-pro-max", "iphone-15-pro"}
