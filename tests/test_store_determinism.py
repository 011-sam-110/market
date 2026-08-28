"""The same store, read twice, must give the same answer.

rows_on() had no ORDER BY, so DuckDB's parallel parquet scan returned rows in
whatever order the threads finished. Nothing downstream noticed until it hit
identity.cluster(), whose greedy single-pass assignment depends on the order
it sees rows in: each row joins the FIRST existing cluster it scores against,
so a different order draws different cluster boundaries.

Measured on the real 29,602-listing store, three consecutive runs of the same
grouping over the same static data:

    groups= 26824  singletons= 25041
    groups= 26827  singletons= 25048
    groups= 26825  singletons= 25046

That breaks the premise the whole harness is built on -- "a diff in the output
is a diff caused by the code". It also means any product count on the board is
unreproducible, and a figure quoted from one run cannot be checked against the
next.

The trap was hidden by a comment that was true in isolation. cluster() says
"the same input list always yields the same ids -- deterministic". That is
correct ABOUT THE FUNCTION and false about the system, because its input list
was never in a fixed order. A guarantee is only as strong as its input.

Fixed at the source rather than by sorting in each caller: rows_on() orders by
source_id, so every consumer -- board, deepen targeting, the viewer, anything
later -- inherits reproducibility instead of each having to remember.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from phoneloop import board
from phoneloop.market_store import MarketStore, empty_row

DAY = date(2026, 8, 28)
LEAF = "9355"


def _row(source_id: str, title: str, seller: str) -> dict:
    row = empty_row("ebay", source_id)
    row.update({
        "seen_at": datetime(2026, 8, 28, 9, 0, tzinfo=UTC),
        "category": "garden_patio", "shard": f"{LEAF}/15-55/p1",
        "title": title, "price_pence": 3600, "total_pence": 4000,
        "currency": "GBP", "condition": "New", "available": True,
        "seller_id": seller, "url": f"https://www.ebay.co.uk/itm/{source_id}",
        "leaf_category_id": LEAF, "sold_qty": 3,
    })
    return row


@pytest.fixture
def busy_store(tmp_path):
    """Enough rows, written across several batches, that an unordered scan
    has a real chance of returning them differently between reads."""
    store = MarketStore(tmp_path)
    titles = [
        "Bike Storage Shed for 1-4 Bikes Portable Waterproof Bike Tent 220x150x185cm",
        "Heavy Duty Waterproof Garden Patio Furniture Cover Rattan Table Protector",
        "3x3M Waterproof Gazebo Top Replacement Canopy Sunshade Pavilion Roof Cover",
    ]
    for batch in range(6):
        rows = []
        for t_i, title in enumerate(titles):
            for i in range(8):
                sid = f"b{batch}t{t_i}i{i}"
                rows.append(_row(sid, title, f"seller{batch}{t_i}{i}"))
            rows.append(_row(f"b{batch}t{t_i}solo",
                             f"Unrelated Oddment {batch}{t_i} Spare Part XYZ",
                             f"solo{batch}{t_i}"))
        store.record(rows)
    return store


class TestRowsOnIsOrdered:
    def test_two_reads_return_the_same_order(self, busy_store):
        a = [r["source_id"] for r in busy_store.rows_on(DAY, source="ebay")]
        b = [r["source_id"] for r in busy_store.rows_on(DAY, source="ebay")]
        assert a == b

    def test_the_order_is_actually_sorted_not_merely_repeatable(self, busy_store):
        ids = [r["source_id"] for r in busy_store.rows_on(DAY, source="ebay")]
        assert ids == sorted(ids), (
            "a stable-but-arbitrary order still changes when files are "
            "rewritten or compacted; sort so it survives that too")

    def test_it_still_returns_every_listing_once(self, busy_store):
        ids = [r["source_id"] for r in busy_store.rows_on(DAY, source="ebay")]
        assert len(ids) == len(set(ids)) == 6 * 3 * 9


class TestGroupingIsReproducible:
    """The failure this was actually costing us."""

    def _group_counts(self, store):
        rows = store.rows_on(DAY, source="ebay")
        by_leaf = {}
        for r in rows:
            by_leaf.setdefault(r["leaf_category_id"], []).append(r)
        total = singles = 0
        for leaf_rows in by_leaf.values():
            groups, _ = board.group_products_in_leaf(leaf_rows)
            total += len(groups)
            singles += sum(1 for v in groups.values() if len(v) == 1)
        return total, singles

    def test_three_runs_agree(self, busy_store):
        counts = {self._group_counts(busy_store) for _ in range(3)}
        assert len(counts) == 1, f"grouping varied across runs: {counts}"

    def test_the_same_listing_lands_in_the_same_product_twice(self, busy_store):
        def keyed(store):
            rows = [r for r in store.rows_on(DAY, source="ebay")
                    if r["leaf_category_id"] == LEAF]
            groups, _ = board.group_products_in_leaf(rows)
            return {r["source_id"]: k for k, rs in groups.items() for r in rs}
        assert keyed(busy_store) == keyed(busy_store)
