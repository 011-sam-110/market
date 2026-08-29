"""A delta measured across a bounded "sold" reading is not a sale count.

`ebay.py` now records whether the page said "10,000+ sold" rather than
"10,000 sold" (see tests/test_sold_bound.py). This is the half that makes
that flag worth recording: `snapshots.difference` carries it onto each
ListingDelta, and `board._deltas_for_metrics` refuses the subtraction.

Why refusing is right. eBay buckets the bound, so a listing can read
"7,000+ sold" on one page view and a precise "616 sold" on another. Both are
true statements about the same listing; their difference is a rendering
change, not 6,384 sales. Because at least one side is a bound, the real
movement is anywhere from 0 upward -- which is exactly what "unknown" means.

This catches the case the negative-delta floor cannot. A bounded reading that
moves UPWARD looks like an ordinary sale once the "+" is gone, so the
existing `delta_sold >= 0` guard passes it straight through; the 82 listings
contributing 82% of the units in the 2026-08-28 -> 2026-08-29 diff are that
shape.

Split across two layers on purpose, matching the split already there for
negatives: `snapshots.difference()` REPORTS what was observed (including the
bound) and decides nothing, and `board` -- the consumer that turns deltas
into a sell-through figure -- decides what it can honestly use.

Rows written before the flag shipped carry no `sold_bound` key. Those read as
`None` (bound unknown) and are treated as unbounded, which is the behaviour
they already had; the flag can only help pairs swept after it landed.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from phoneloop import board
from phoneloop.market_store import MarketStore, empty_row
from phoneloop.snapshots import difference

DAY_A = date(2026, 8, 28)
DAY_B = date(2026, 8, 29)
SEEN_A = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)
SEEN_B = datetime(2026, 8, 29, 9, 0, tzinfo=UTC)
LEAF = "25863"


def _row(source_id: str, sold: int | None, seen_at: datetime,
         bound: bool | None) -> dict:
    """`bound=None` writes NO sold_bound key -- a row from before the flag."""
    extra = {"aspects": [], "watchers": 0, "ships_from": "", "stock_hint": ""}
    if bound is not None:
        extra["sold_bound"] = bound
    row = empty_row("ebay", source_id)
    row.update({
        "seen_at": seen_at, "category": "garden_patio",
        "shard": f"{LEAF}/15-55/p1", "title": f"Widget {source_id}",
        "price_pence": 2399, "total_pence": 2399, "currency": "GBP",
        "condition": "New", "available": True, "seller_id": f"s{source_id}",
        "leaf_category_id": LEAF, "sold_qty": sold,
        "url": f"https://www.ebay.co.uk/itm/{source_id}",
        "extra": json.dumps(extra, separators=(",", ":")),
    })
    return row


@pytest.fixture
def store(tmp_path):
    """`bounded_b` is item 285441712116's measured shape: 616 exact, then
    7,000+ bounded -- a +6,384 "sale" that never happened."""
    s = MarketStore(tmp_path)
    s.record([
        _row("bounded_b", 616, SEEN_A, False),
        _row("bounded_a", 7000, SEEN_A, True),
        _row("honest", 100, SEEN_A, False),
        _row("legacy", 100, SEEN_A, None),
    ])
    s.record([
        _row("bounded_b", 7000, SEEN_B, True),
        _row("bounded_a", 7050, SEEN_B, False),
        _row("honest", 104, SEEN_B, False),
        _row("legacy", 104, SEEN_B, None),
    ])
    return s


@pytest.fixture
def deltas(store):
    return board._deltas_for_metrics(difference(store, DAY_A, DAY_B))


class TestTheBoundIsCarried:
    def test_difference_reports_the_bound_on_each_side(self, store):
        ld = difference(store, DAY_A, DAY_B).listings["bounded_b"]
        assert (ld.sold_a_bound, ld.sold_b_bound) == (False, True)

    def test_a_row_written_before_the_flag_reads_as_unknown(self, store):
        ld = difference(store, DAY_A, DAY_B).listings["legacy"]
        assert ld.sold_a_bound is None and ld.sold_b_bound is None

    def test_the_raw_delta_is_still_reported(self, store):
        """difference() reports; it does not decide. The 6,384 is still
        visible to anyone who wants it."""
        ld = difference(store, DAY_A, DAY_B).listings["bounded_b"]
        assert ld.delta_sold == 6384


class TestTheMetricsPathRefusesABoundedDelta:
    def test_a_bound_on_the_later_side_is_unknown(self, deltas):
        assert deltas["bounded_b"][0] is None, (
            "616 -> '7,000+' was counted as 6,384 units sold")

    def test_a_bound_on_the_earlier_side_is_also_unknown(self, deltas):
        """'7,000+' then an exact 7,050 does not mean 50 sold -- the true
        starting point was anywhere at or above 7,000."""
        assert deltas["bounded_a"][0] is None

    def test_an_unbounded_pair_still_counts(self, deltas):
        assert deltas["honest"][0] == 4

    def test_a_legacy_pair_behaves_as_it_did_before(self, deltas):
        """No flag on either side -- treated as unbounded, exactly as these
        rows were treated before the flag existed. Not a regression."""
        assert deltas["legacy"][0] == 4


class TestItReachesTheSellThroughFigure:
    def test_the_bounded_listings_leave_the_denominator(self, store, deltas):
        from phoneloop import metrics
        rows = list(store.rows_on(DAY_B, source="ebay"))
        m = metrics.compute(rows, now=datetime(2026, 8, 29, 12, tzinfo=UTC),
                            deltas=deltas)
        assert m.str_window_denominator == 2, (
            "a refused pair must leave the denominator, not sit in it as a "
            "zero and drag the average down")
        # (4 + 4) / 2 = 4.0 units -> 40000bp
        assert m.str_window_bp == 40000
