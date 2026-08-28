"""The item page carries a delivery estimate; the search row almost never does.

The `delivery` knob reads `eta_days` and had no way to populate. Measured
2026-08-28 across the 196 recorded item pages and the 31,206 stored search
rows:

    search rows with an eta_days      798 / 31,206   (2.6%)
    item pages with an estimate       195 /    196   (99.5%)

and ebay_item.py parsed none of them, so no amount of deepening could ever
move that knob.

Two wordings, both real, both taken verbatim from recordings:

    "Estimated between Tue, 1 Sep and Thu, 3 Sep to BN11AA"     180 (91.8%)
    "Estimated by Sat, 29 Aug to BN11AA. See details"            15  (7.7%)

The LATE end of a range is the answer, because the knob is a maximum
("max delivery days", knob 4). Taking the early end would let a 1-14 day
listing pass a 7-day threshold on the strength of its best case.

Pure, like ebay._eta_days: `today` is a parameter, never the clock, so a
fixture gives the same answer next month. Dates carry no year, so a target
already past is read as next year -- every recording lands in September, so
the rollover has no live example and is exercised against constructed dates
here rather than claimed as observed.

The estimate is to the VIEWER's postcode (BN11AA, this machine, Brighton).
It is a UK-buyer ETA, not a property of the listing.
"""

from __future__ import annotations

from datetime import date

from phoneloop.ebay_item import parse_item, to_row_updates

TODAY = date(2026, 8, 28)

# Verbatim from ~/.phoneloop/fixtures/ebay_item, 2026-08-28.
RANGE_TEXT = ("eBay item number: 377452038288\n"
              "Estimated between Tue, 1 Sep and Thu, 3 Sep to BN11AA\n")
SINGLE_TEXT = ("eBay item number: 125985175954\n"
               "Estimated by Sat, 29 Aug to BN11AA. See details\n")
NO_ETA_TEXT = "eBay item number: 126011493131\nRegistered as a business seller\n"


class TestEtaFromAnItemPage:
    def test_a_range_takes_the_LATE_end(self):
        item = parse_item(RANGE_TEXT, url="https://www.ebay.co.uk/itm/377452038288")
        assert item.eta_days_on(TODAY) == 6      # 28 Aug -> 3 Sep

    def test_a_single_date_is_read(self):
        item = parse_item(SINGLE_TEXT, url="https://www.ebay.co.uk/itm/125985175954")
        assert item.eta_days_on(TODAY) == 1      # 28 Aug -> 29 Aug

    def test_a_page_with_no_estimate_says_so(self):
        item = parse_item(NO_ETA_TEXT, url="https://www.ebay.co.uk/itm/126011493131")
        assert item.eta_days_on(TODAY) is None

    def test_without_a_today_it_refuses_to_guess_a_year(self):
        item = parse_item(RANGE_TEXT, url="https://www.ebay.co.uk/itm/377452038288")
        assert item.eta_days_on(None) is None


class TestTheYearRollover:
    """Dates carry no year. Constructed, not observed -- every recording is
    September, so December has no live example and is not claimed as one."""

    def test_a_january_date_in_december_is_next_year(self):
        text = "eBay item number: 1\nEstimated by Mon, 5 Jan to BN11AA"
        item = parse_item(text, url="https://www.ebay.co.uk/itm/1")
        assert item.eta_days_on(date(2026, 12, 28)) == 8

    def test_a_range_crossing_new_year_uses_the_late_end(self):
        text = ("eBay item number: 1\n"
                "Estimated between Tue, 30 Dec and Mon, 5 Jan to BN11AA")
        item = parse_item(text, url="https://www.ebay.co.uk/itm/1")
        assert item.eta_days_on(date(2026, 12, 28)) == 8


class TestItReachesTheRow:
    def test_eta_days_is_written_onto_the_row(self):
        item = parse_item(RANGE_TEXT, url="https://www.ebay.co.uk/itm/377452038288")
        assert to_row_updates(item, today=TODAY)["eta_days"] == 6

    def test_an_absent_eta_is_not_written_over_a_search_row_value(self):
        """The merge rule: only keys this parser has an opinion on. A search
        row that DID carry "Free delivery in 3 days" must keep it."""
        item = parse_item(NO_ETA_TEXT, url="https://www.ebay.co.uk/itm/126011493131")
        assert "eta_days" not in to_row_updates(item, today=TODAY)
