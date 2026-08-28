"""Tier 3 differencing: sold_qty(today) - sold_qty(previous snapshot), per
listing, summed per product and per niche (design spec section 4;
contracts section 2.11).

Every row is real-shaped: built from market_store.empty_row() and the
ROW_FIELDS columns the shared row actually carries (contracts section 1.1),
not a hand-picked subset -- the same pattern tests/test_metrics.py uses.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from phoneloop.market_store import MarketStore, empty_row
from phoneloop.snapshots import Difference, ListingDelta, difference

DAY_A = date(2026, 8, 20)
DAY_B = date(2026, 8, 21)
LEAF = "139971"

SEEN_A = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
SEEN_B = datetime(2026, 8, 21, 9, 0, tzinfo=UTC)


def _row(source_id: str, sold_qty: int | None, seen_at: datetime, **over) -> dict:
    row = empty_row("ebay", source_id)
    row.update({
        "seen_at": seen_at, "category": "garden", "shard": "leaf139971:1500-5500",
        "title": f"Widget {source_id}", "price_pence": 1999, "total_pence": 2199,
        "currency": "GBP", "condition": "New", "available": True,
        "seller_id": "seller1", "sold_qty": sold_qty, "leaf_category_id": LEAF,
    })
    row.update(over)
    return row


@pytest.fixture
def store(tmp_path):
    s = MarketStore(tmp_path)
    # Day A: listings 1, 2, 3 exist.
    s.record([
        _row("1", 10, SEEN_A),
        _row("2", 5, SEEN_A),
        _row("3", 20, SEEN_A),   # gone by day B
    ])
    # Day B: listings 1, 2, 4 exist. eBay always-writes, so "2" is written
    # again even though its sold_qty did not move (market_store.py's
    # ALWAYS_SNAPSHOT_SOURCES semantics -- that is the whole point: an
    # unchanged sold count on day B is itself the observation Tier 3 needs).
    s.record([
        _row("1", 14, SEEN_B),   # +4
        _row("2", 5, SEEN_B),    # +0, unchanged but still a real day-B row
        _row("4", 2, SEEN_B),    # new on day B, absent on day A
    ])
    return s


class TestPerListingDelta:
    def test_positive_delta(self, store):
        d = difference(store, DAY_A, DAY_B)
        ld = d.listings["1"]
        assert ld.sold_a == 10 and ld.sold_b == 14
        assert ld.delta_sold == 4
        assert ld.known is True
        assert ld.days == pytest.approx(1.0)   # SEEN_B - SEEN_A, both present

    def test_zero_delta_is_known_not_dropped(self, store):
        d = difference(store, DAY_A, DAY_B)
        ld = d.listings["2"]
        assert ld.delta_sold == 0
        assert ld.known is True

    def test_missing_on_day_b_is_unknown_not_zero(self, store):
        """Listing 3 sold on day A but never appears on day B -- sold out,
        ended, or just not re-swept are indistinguishable from the store, so
        this MUST be None/False, never a delta of -20 or 0."""
        d = difference(store, DAY_A, DAY_B)
        ld = d.listings["3"]
        assert ld.sold_a == 20
        assert ld.sold_b is None
        assert ld.delta_sold is None
        assert ld.known is False

    def test_missing_on_day_a_is_unknown_not_zero(self, store):
        """Listing 4 is new on day B -- there is no day-A baseline, so its
        delta is unknown, not "all of sold_b is new demand"."""
        d = difference(store, DAY_A, DAY_B)
        ld = d.listings["4"]
        assert ld.sold_a is None
        assert ld.sold_b == 2
        assert ld.delta_sold is None
        assert ld.known is False

    def test_null_sold_qty_on_one_side_is_unknown(self, store):
        store.record([_row("5", None, SEEN_A)])   # present, sold_qty unknown
        store.record([_row("5", 3, SEEN_B)])
        d = difference(store, DAY_A, DAY_B)
        ld = d.listings["5"]
        assert ld.delta_sold is None
        assert ld.known is False

    def test_negative_delta_is_recorded_not_clamped(self, store):
        """A relist resets eBay's lifetime counter -- sold_b < sold_a is a
        real observation, not an error this module silently corrects."""
        store.record([_row("6", 50, SEEN_A)])
        store.record([_row("6", 3, SEEN_B)])   # relist reset the counter
        d = difference(store, DAY_A, DAY_B)
        ld = d.listings["6"]
        assert ld.delta_sold == -47
        assert ld.known is True


class TestDifferenceAggregate:
    def test_n_listings_and_coverage(self, store):
        d = difference(store, DAY_A, DAY_B)
        # 4 distinct source_ids across both days: 1, 2, 3, 4.
        assert d.n_listings == 4
        assert d.n_known == 2          # only 1 and 2 have both sides known
        assert d.coverage == pytest.approx(0.5)

    def test_returns_a_difference_with_the_dates_normalised(self, store):
        d = difference(store, "2026-08-20", "2026-08-21")
        assert d.date_a == DAY_A
        assert d.date_b == DAY_B


class TestByProductByNiche:
    def test_by_product_sums_only_known_deltas(self, store):
        d = difference(store, DAY_A, DAY_B)
        key_of = {"1": "prod:a", "2": "prod:a", "3": "prod:b", "4": "prod:c"}.get
        result = d.by_product(key_of)
        # prod:a = listing 1 (+4, known) + listing 2 (+0, known) = 4
        assert result["prod:a"] == (4, 1)
        # prod:b's only member (listing 3) is unknown -> the whole key is
        # absent, not present with a zero sum.
        assert "prod:b" not in result
        # prod:c's only member (listing 4) is unknown too.
        assert "prod:c" not in result

    def test_by_niche_sums_only_known_deltas(self, store):
        d = difference(store, DAY_A, DAY_B)
        result = d.by_niche(lambda sid: LEAF)
        assert result[LEAF] == (4, 1)   # listings 3 and 4 excluded (unknown)

    def test_key_of_returning_none_excludes_the_listing(self, store):
        d = difference(store, DAY_A, DAY_B)
        result = d.by_product(lambda sid: None if sid == "2" else "prod:x")
        assert result["prod:x"] == (4, 1)   # only listing 1 contributed
