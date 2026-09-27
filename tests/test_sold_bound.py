""""10,000+ sold" is a lower bound. Storing it as 10000 makes it a figure.

`ebay._SOLD_RE` matches `([\\d,]+)\\+?\\s*sold` and keeps only the digits.
The comment above it records the choice -- "the '+' means 'at least this
many' and is dropped; the digits are kept exactly" -- and as a way to read
one page that is fine. What it did not anticipate is Tier 3, which SUBTRACTS
two of these readings and calls the result units sold.

Two bounded readings do not subtract. eBay buckets the bound, so the same
listing can render "7,000+ sold" on one page view and a precise "616 sold"
on another, and the difference between them is a rendering change, not 6,384
sales. Measured on ~/.phoneloop/prod for 2026-08-28: `sold_qty` is exactly
10000 on 60 rows, exactly 2000 on 6, exactly 1000 on 5 -- bucket values, not
coincidences. Within that single sweep, 30 of 328 twice-observed listings
disagree with themselves about sold_qty, worst spread 6,384.

`board._deltas_for_metrics` already floors the damage by dropping NEGATIVE
deltas, but a bounded reading that moves UPWARD is indistinguishable from a
real sale once the "+" is gone. That is what this restores: the bound is
recorded, so a delta computed across one can be refused rather than guessed
at.

The bound is kept ALONGSIDE the digits, never instead of them. "10,000+
sold" still reports 10000 -- it is a true lower bound and the best number
available for a niche-level read. Only the SUBTRACTION is refused.

Rows written before this change carry no flag and are read as unbounded,
which is the pre-existing behaviour and not a regression: the flag can only
help pairs where at least one side was swept after it shipped.
"""

from __future__ import annotations

import json

from phoneloop.ebay import parse_row, to_row

URL = "https://www.ebay.co.uk/itm/285441712116"

# Verbatim shapes measured live 2026-08-27/28.
BOUNDED = ("Heavy Duty Waterproof Outdoor Furniture Cover Garden Patio\n"
           "Opens in a new window or tab\nBrand new\n£6.99\nBuy It Now\n"
           "Free postage\n7,000+ sold\nseller99 99.8% positive (10.7K)")
EXACT = ("Heavy Duty Waterproof Outdoor Furniture Cover Garden Patio\n"
         "Opens in a new window or tab\nBrand new\n£23.99\nBuy It Now\n"
         "Free postage\n616 sold\nseller99 99.8% positive (10.7K)")
NO_SOLD = ("Heavy Duty Waterproof Outdoor Furniture Cover Garden Patio\n"
           "Opens in a new window or tab\nBrand new\n£23.99\nBuy It Now\n"
           "Free postage\nseller99 99.8% positive (10.7K)")


class TestTheDigitsAreStillKept:
    """The bound must not cost us the number."""

    def test_a_bounded_count_still_reports_its_digits(self):
        assert parse_row(BOUNDED, URL).sold_qty == 7000

    def test_an_exact_count_is_unchanged(self):
        assert parse_row(EXACT, URL).sold_qty == 616

    def test_no_sold_line_is_still_unknown_never_zero(self):
        assert parse_row(NO_SOLD, URL).sold_qty is None


class TestTheBoundIsRecorded:
    def test_a_plus_marks_the_count_as_a_lower_bound(self):
        assert parse_row(BOUNDED, URL).sold_is_bound is True

    def test_an_exact_count_is_not_a_bound(self):
        assert parse_row(EXACT, URL).sold_is_bound is False

    def test_an_absent_count_is_not_a_bound(self):
        assert parse_row(NO_SOLD, URL).sold_is_bound is False


class TestItSurvivesOntoTheRow:
    def _extra(self, text):
        row = to_row(parse_row(text, URL), category="garden_patio",
                     shard="25863/15-55/p1", leaf_category_id="25863")
        return json.loads(row["extra"])

    def test_the_bound_reaches_the_stored_row(self):
        assert self._extra(BOUNDED)["sold_bound"] is True

    def test_an_exact_row_records_it_as_false(self):
        """Present and false, not absent -- absent is how a row written
        before this change reads, and the two must stay distinguishable."""
        assert self._extra(EXACT)["sold_bound"] is False

    def test_the_rest_of_extra_is_untouched(self):
        extra = self._extra(BOUNDED)
        for key in ("aspects", "watchers", "ships_from", "stock_hint"):
            assert key in extra, f"{key} was dropped from extra"


class TestACommaIsNotACount:
    """`([\d,]+)` also matched a LONE comma, so a row whose text said
    ", sold" anywhere -- "Brand new, sold as seen" -- captured "," and
    `int("")` raised. The raise killed the whole sweep mid-run: the Windows
    nightlies of 13, 14, 15 and 18 Sep 2026 and the first provenance-1 night
    (27 Sep, after 22 good pages) all died on this one traceback."""

    TITLE_COMMA = ("Bedding Set Double Brand new, sold as seen\n"
                   "Opens in a new window or tab\nBrand new\n£19.99\n"
                   "Buy It Now\nFree postage\nseller99 99.8% positive (10.7K)")

    def test_a_comma_before_sold_does_not_raise(self):
        assert parse_row(self.TITLE_COMMA, URL).sold_qty is None

    def test_a_number_then_a_comma_is_not_a_count(self):
        """"Pack of 2, sold as seen" is a title, not two sales."""
        text = self.TITLE_COMMA.replace("Brand new, sold", "Pack of 2, sold")
        assert parse_row(text, URL).sold_qty is None

    def test_real_counts_still_parse(self):
        for shown, qty in (("616 sold", 616), ("7,000+ sold", 7000),
                           ("12345 sold", 12345), ("1,234,567 sold", 1234567)):
            assert parse_row(EXACT.replace("616 sold", shown), URL).sold_qty == qty, shown
