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
