"""CeX parsing tests.

The page text below is copied verbatim from a live CeX sell-search on
2026-08-27, not invented. A fixture written from imagination would test the
parser against the page shape I assumed, which is the one thing it must not do.
"""

from __future__ import annotations

import pytest

from phoneloop.cex import (
    CexBox,
    best_cash,
    parse_box_name,
    parse_sell_page,
    to_pence,
)

LIVE_PAGE = """Menu
Sign in
Basket
Buy from CeX
Great
Based on 522386 Reviews
Make money selling to CeX

Find the product you'd like to trade-in for cash or voucher

Home
Search
CeX (UK) : Stock search
By Category
iPhone 12(183)
By Cash Price
to
Capacity
Grade
Main Colour
Manufacturer
Network
OS
Phone Model

Search Results

"iphone 12 128gb"

183 results

Sort By:
Relevance
iPhone 12
Apple iPhone 12 128GB Black, Unlocked B

CASH
£110.00

CeXVOUCHER
£142.00

Sell
iPhone 12
Apple iPhone 12 128GB Blue, Unlocked C

CASH
£85.00

CeXVOUCHER
£111.00

Sell
iPhone 12
Apple iPhone 12 128GB Blue, 3 B

CASH
£95.00

CeXVOUCHER
£120.00

Sell
"""


class TestMoney:
    def test_parses_pounds_and_pence_to_integer_pence(self):
        assert to_pence("£110.00") == 11000
        assert to_pence("CASH £1,250.50") == 125050

    def test_returns_none_when_there_is_no_number(self):
        assert to_pence("CeXVOUCHER") is None
        assert to_pence("") is None


class TestBoxName:
    def test_splits_a_real_box_name(self):
        parts = parse_box_name("Apple iPhone 12 128GB Black, Unlocked B")
        assert parts == {
            "make": "apple",
            "model": "iphone 12",
            "storage_gb": 128,
            "colour": "Black",
            "network": "Unlocked",
            "grade": "B",
        }

    def test_terabyte_storage_is_normalised_to_gb(self):
        parts = parse_box_name("Apple iPhone 15 Pro Max 1TB Natural Titanium, Unlocked A")
        assert parts["storage_gb"] == 1024
        assert parts["grade"] == "A"

    def test_carrier_locked_network_is_preserved(self):
        assert parse_box_name("Apple iPhone 12 128GB Blue, 3 B")["network"] == "3"

    def test_unmatched_name_returns_empty_rather_than_guessing(self):
        # CeX sells a lot that is not a phone. A half-parsed box priced into a
        # bucket is worse than one that was skipped.
        assert parse_box_name("The Legend of Zelda: Tears of the Kingdom") == {}
        assert parse_box_name("") == {}


class TestSellPage:
    def test_finds_every_priced_box(self):
        boxes = parse_sell_page(LIVE_PAGE)
        assert len(boxes) == 3

    def test_reads_cash_and_voucher_against_the_right_box(self):
        boxes = parse_sell_page(LIVE_PAGE)
        by_name = {b.box_name: b for b in boxes}
        black_b = by_name["Apple iPhone 12 128GB Black, Unlocked B"]
        assert black_b.cash_pence == 11000
        assert black_b.voucher_pence == 14200
        assert black_b.grade == "B"
        assert black_b.storage_gb == 128

    def test_the_category_heading_is_not_mistaken_for_a_box_name(self):
        # "iPhone 12" sits directly above the box name in the rendered page.
        # Picking it up would produce a box with no storage and no grade.
        assert all(b.box_name != "iPhone 12" for b in parse_sell_page(LIVE_PAGE))

    def test_empty_page_yields_nothing_rather_than_raising(self):
        assert parse_sell_page("") == []


class TestBestCash:
    def test_picks_the_unlocked_box_at_the_requested_grade(self):
        boxes = parse_sell_page(LIVE_PAGE)
        assert best_cash(boxes, "B") == 11000
        assert best_cash(boxes, "C") == 8500

    def test_carrier_locked_stock_is_excluded(self):
        # The Three-locked box is £95 at grade B. If it leaked into the answer
        # it would understate an unlocked handset by a grade.
        locked_only = [b for b in parse_sell_page(LIVE_PAGE) if not b.unlocked]
        assert locked_only, "fixture should contain a carrier-locked box"
        assert best_cash(locked_only, "B") is None

    def test_returns_none_when_no_box_matches(self):
        assert best_cash([], "B") is None
        assert best_cash(parse_sell_page(LIVE_PAGE), "A") is None

    def test_prefers_the_conservative_price_when_colours_differ(self):
        boxes = [
            CexBox("Apple iPhone 13 128GB Pink, Unlocked B", 20000, 0,
                   network="Unlocked", grade="B"),
            CexBox("Apple iPhone 13 128GB Black, Unlocked B", 18500, 0,
                   network="Unlocked", grade="B"),
        ]
        # The lower price is the one that will not overstate a margin.
        assert best_cash(boxes, "B") == 18500


@pytest.mark.parametrize("grade,expected", [("B", 11000), ("C", 8500)])
def test_grades_are_read_independently(grade, expected):
    assert best_cash(parse_sell_page(LIVE_PAGE), grade) == expected


class TestVariantContamination:
    """A CeX search for "iphone 17" also returns the 17 Pro, Pro Max and Air.

    best_cash takes a minimum, so without a model filter it mixes four
    different handsets. The live tell was a grade ordering that cannot exist:
    the unfiltered run returned A=34000, B=32100, C=36100 for the iPhone 17,
    and a grade C is never worth more than a grade A of the same phone. With
    the filter the same query returns 40500 / 38300 / 36100.
    """

    def _box(self, model, grade, pence, network="Unlocked"):
        return CexBox(
            box_name=f"Apple {model} 128GB Black, {network} {grade}",
            cash_pence=pence, voucher_pence=pence + 1000,
            make="apple", model=model, storage_gb=128,
            colour="Black", network=network, grade=grade,
        )

    def test_a_pro_does_not_price_the_base_model(self):
        boxes = [
            self._box("iPhone 17", "B", 38300),
            self._box("iPhone 17 Pro", "B", 51300),
            self._box("iPhone 17 Pro Max", "B", 54000),
        ]
        assert best_cash(boxes, "B", model="iphone-17") == 38300
        assert best_cash(boxes, "B", model="iphone-17-pro") == 51300

    def test_grades_stay_ordered_once_filtered(self):
        """The property that exposed the bug, pinned as a property."""
        boxes = [
            self._box("iPhone 17", "A", 40500),
            self._box("iPhone 17", "B", 38300),
            self._box("iPhone 17", "C", 36100),
            self._box("iPhone 17 Pro Max", "C", 30000),   # the contaminant
        ]
        a = best_cash(boxes, "A", model="iphone-17")
        b = best_cash(boxes, "B", model="iphone-17")
        c = best_cash(boxes, "C", model="iphone-17")
        assert a > b > c, f"grades out of order: A={a} B={b} C={c}"

    def test_without_a_filter_the_contaminant_wins(self):
        """Documents the old behaviour so the fix cannot be quietly reverted."""
        boxes = [
            self._box("iPhone 17", "C", 36100),
            self._box("iPhone 17 Pro Max", "C", 30000),
        ]
        assert best_cash(boxes, "C") == 30000
        assert best_cash(boxes, "C", model="iphone-17") == 36100

    def test_a_locked_box_is_still_excluded(self):
        boxes = [self._box("iPhone 17", "B", 20000, network="EE")]
        assert best_cash(boxes, "B", model="iphone-17") is None

    def test_an_unmatched_model_is_none_not_a_fallback(self):
        boxes = [self._box("iPhone 17 Pro", "B", 51300)]
        assert best_cash(boxes, "B", model="iphone-17") is None
