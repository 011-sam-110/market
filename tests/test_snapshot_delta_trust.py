"""A falling "sold" counter must not reach the sell-through metric as a sale.

`snapshots.difference()` deliberately records a negative delta rather than
clamping it, and says so: "a relist resets eBay's lifetime counter, so
sold(day B) < sold(day A) is a real, if awkward, observation... a caller that
wants to treat a negative delta as a relist artefact can filter it out
itself, but difference() does not decide that on the caller's behalf."

That contract is right and is left alone. The bug is that the caller it
delegates to never filtered. `board._deltas_for_metrics` reshapes
`Difference.listings` for `metrics.compute` and passes `delta_sold` through
verbatim, negatives included, where it is summed into `str_window_bp` -- the
one figure knob 2 is allowed to be judged on.

What that costs, measured on ~/.phoneloop/prod for 2026-08-28 -> 2026-08-29:

    listings with a known delta          4,752
    NEGATIVE deltas                        251
    most negative                       -7,468   (item 221186778005, whose
                                                  title AND price are
                                                  identical on both days:
                                                  9,274 -> 1,806)

The relist explanation does not cover these. The same instability appears
INSIDE a single sweep, where no time passed in which anything could sell or
be relisted: of 328 listings observed twice on 2026-08-28, 30 disagree with
themselves about `sold_qty`, worst spread 6,384. Two measured causes, neither
repairable at this layer:

  * A BOUND STORED AS A FIGURE -- `ebay._SOLD_RE` matches "10,000+ sold" and
    keeps only the digits, so a lower bound becomes an exact count. The tell:
    `sold_qty` is exactly 10000 on 60 rows.
  * VARIANT SWITCHING -- a multi-variant listing renders whichever variant
    eBay chose for that page view, moving `sold_qty` and `price_pence`
    together on one source_id (13 listings within the one day).

For the question `str_window_bp` asks -- "units sold in this window" -- a
negative delta is uninformative whatever its cause: a relist means the
window's sales are unrecoverable, and an artefact means there was never a
measurement. Either way the honest value is None, which `metrics.compute`
already skips. So the filter belongs here, in the caller, exactly where
snapshots.py said it did.

This is a floor on the damage, not a repair. An inflated POSITIVE delta from
the same two causes is indistinguishable from a real sale at this layer and
still gets through; fixing that needs the parser to keep the "+".
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from phoneloop import board
from phoneloop.market_store import MarketStore, empty_row
from phoneloop.snapshots import difference

DAY_A = date(2026, 8, 28)
DAY_B = date(2026, 8, 29)
SEEN_A = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)
SEEN_B = datetime(2026, 8, 29, 9, 0, tzinfo=UTC)
LEAF = "26677"


def _row(source_id: str, sold: int | None, seen_at: datetime, **over) -> dict:
    row = empty_row("ebay", source_id)
    row.update({
        "seen_at": seen_at, "category": "garden_patio",
        "shard": f"{LEAF}/15-55/p1", "title": f"Widget {source_id}",
        "price_pence": 3490, "total_pence": 3490, "currency": "GBP",
        "condition": "New", "available": True, "seller_id": f"s{source_id}",
        "leaf_category_id": LEAF, "sold_qty": sold,
        "url": f"https://www.ebay.co.uk/itm/{source_id}",
    })
    row.update(over)
    return row


@pytest.fixture
def store(tmp_path):
    """`faller` is item 221186778005's real shape: 9,274 then 1,806, with an
    identical price on both days."""
    s = MarketStore(tmp_path)
    s.record([
        _row("faller", 9274, SEEN_A),
        _row("riser", 100, SEEN_A),
        _row("steady", 50, SEEN_A),
    ])
    s.record([
        _row("faller", 1806, SEEN_B),
        _row("riser", 103, SEEN_B),
        _row("steady", 50, SEEN_B),
    ])
    return s


@pytest.fixture
def deltas(store):
    return board._deltas_for_metrics(difference(store, DAY_A, DAY_B))


class TestTheMeasurementLayerIsUnchanged:
    """snapshots.difference keeps its documented contract."""

    def test_difference_still_records_the_raw_negative(self, store):
        ld = difference(store, DAY_A, DAY_B).listings["faller"]
        assert ld.delta_sold == -7468
        assert ld.known is True


class TestTheMetricsPathRefusesIt:
    def test_a_negative_delta_reaches_metrics_as_unknown(self, deltas):
        delta_sold, _days = deltas["faller"]
        assert delta_sold is None, (
            f"a counter that fell 9274 -> 1806 reached the sell-through "
            f"metric as {delta_sold} units sold")

    def test_a_rising_counter_is_untouched(self, deltas):
        assert deltas["riser"][0] == 3

    def test_an_unchanged_counter_stays_a_known_zero(self, deltas):
        """Zero sales is a fact, not an unknown. Dropping quiet listings
        would shrink the denominator and flatter whatever remained."""
        assert deltas["steady"][0] == 0

    def test_the_elapsed_days_are_preserved_for_the_rejected_row(self, deltas):
        """Only the sold figure is unknown; the window is still measured."""
        assert deltas["faller"][1] == pytest.approx(1.0)


class TestItReachesTheSellThroughFigure:
    def test_the_niche_average_is_not_dragged_negative(self, store, deltas):
        """What a board row actually reads. Averaged over the two rows that
        remain measurable, 3 and 0 -> 1.5 units, i.e. 15000bp. With the
        faller folded in it would be (-7468+3+0)/3 -- a large negative
        sell-through, which is not a thing that can exist."""
        from phoneloop import metrics
        rows = [r for r in store.rows_on(DAY_B, source="ebay")]
        m = metrics.compute(rows, now=datetime(2026, 8, 29, 12, tzinfo=UTC),
                            deltas=deltas)
        assert m.str_window_bp is not None
        assert m.str_window_bp > 0, (
            f"sell-through came out at {m.str_window_bp}bp")
        assert m.str_window_denominator == 2, (
            "the rejected listing must leave the denominator too, not sit "
            "in it as a zero")
