"""Tier 2 has a tiny budget, so it must be spent where the board reads it.

rows_to_deepen took `candidates[:limit]` -- the store's own row order. With
29,602 candidates and a limit in the low hundreds, the deepened rows scatter
across the population, and the board's business knobs (min_margin,
price_band, delivery) still read `?` at denom=0 cov=0% afterwards. That is
exactly what happened on 2026-08-28: 99 item pages fetched, no knob moved.

The rows the board actually reports are the ones that group into a PRODUCT --
several sellers listing the same item. Deepening those first means the same
budget lands on rows a product row will read, instead of on 99 singletons
nobody looks at.

Ordering only. Nothing here widens the budget, drops the seller_type filter,
or lets a run return fewer candidates than before -- a store where no group
forms must still deepen, or a bad grouping would silently starve Tier 2.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest

from phoneloop.market_store import MarketStore, empty_row

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "bulk_ebay.py"
_spec = importlib.util.spec_from_file_location("bulk_ebay_deepen_targeting", _SCRIPT)
bulk_ebay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bulk_ebay)

LEAF = "9355"
SEEN = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)

# A real repeated-product shape, measured in hub 139956: one item, many
# sellers, tight price band. This is what a board product row is made of.
GROUPED_TITLE = "Bike Storage Shed for 1-4 Bikes Portable Waterproof Bike Tent 220x150x185cm"
SECOND_TITLE = "Heavy Duty Waterproof Garden Patio Furniture Cover Rattan Table Set Protector"


def _row(source_id: str, title: str, seller: str, *, deepened: bool = False) -> dict:
    row = empty_row("ebay", source_id)
    row.update({
        "seen_at": SEEN, "category": "garden_patio",
        "shard": f"{LEAF}/15-55/p1", "title": title,
        "price_pence": 3696, "total_pence": 4000, "currency": "GBP",
        "condition": "New", "available": True, "seller_id": seller,
        "url": f"https://www.ebay.co.uk/itm/{source_id}",
        "leaf_category_id": LEAF, "sold_qty": 3,
        "seller_type": "business" if deepened else None,
    })
    return row


@pytest.fixture
def mixed_store(tmp_path):
    """Twenty unrelated singletons, THEN six listings of one real product.

    The singletons are written first on purpose. Written the other way round
    the naive `candidates[:limit]` slice picks the product group by accident
    of row order and every assertion below passes against the unfixed code --
    a test that proves nothing. This ordering makes the naive slice return
    singletons, so only real prioritisation can satisfy the assertions.
    """
    store = MarketStore(tmp_path)
    rows = [_row(f"s{i}", f"Unrelated Oddment Number {i} Spare Part", f"solo{i}")
            for i in range(20)]
    rows += [_row(f"g{i}", GROUPED_TITLE, f"seller{i}") for i in range(6)]
    store.record(rows)
    return store


@pytest.fixture
def two_group_store(tmp_path):
    """A big product group and a smaller one, in the same leaf."""
    store = MarketStore(tmp_path)
    rows = [_row(f"big{i}", GROUPED_TITLE, f"seller{i}") for i in range(10)]
    rows += [_row(f"sml{i}", SECOND_TITLE, f"other{i}") for i in range(4)]
    store.record(rows)
    return store


class TestGroupedRowsAreDeepenedFirst:
    def test_a_small_budget_is_spent_on_the_product_not_the_singletons(
            self, mixed_store):
        picked = bulk_ebay.rows_to_deepen(mixed_store, limit=6)
        assert {r["source_id"] for r in picked} == {f"g{i}" for i in range(6)}

    def test_the_limit_is_still_respected(self, mixed_store):
        assert len(bulk_ebay.rows_to_deepen(mixed_store, limit=4)) == 4

    def test_singletons_are_still_reachable_with_a_bigger_budget(
            self, mixed_store):
        picked = bulk_ebay.rows_to_deepen(mixed_store, limit=26)
        assert len(picked) == 26, "prioritising must not drop the tail"


class TestTheBudgetSpreadsAcrossGroups:
    """Sorting purely by group size empties the budget into one product.

    Measured live 2026-08-28: a 40-page budget sorted by size went entirely
    into the single largest group (48 listings of "Vintage 1990s Old Retro
    mobile Phone Untested Spares Or Repair"). All 40 came back
    seller_type='private', so NO business knob moved -- 40 page loads, zero
    board coverage gained. Deep coverage of one product is worth less than
    one observation each of forty, because a product row needs only a single
    business listing before min_margin / price_band / delivery can read.
    """

    def test_a_budget_smaller_than_a_group_still_reaches_other_groups(
            self, two_group_store):
        picked = bulk_ebay.rows_to_deepen(two_group_store, limit=4)
        titles = {r["title"] for r in picked}
        assert len(titles) == 2, (
            "the whole budget went into one product group; a second group "
            "went untouched")

    def test_every_group_is_touched_before_any_is_finished(
            self, two_group_store):
        picked = bulk_ebay.rows_to_deepen(two_group_store, limit=2)
        assert len({r["title"] for r in picked}) == 2


class TestTheOldGuaranteesHold:
    def test_an_already_deepened_row_is_never_a_candidate(self, tmp_path):
        store = MarketStore(tmp_path)
        store.record([_row(f"g{i}", GROUPED_TITLE, f"seller{i}",
                           deepened=(i < 3)) for i in range(6)])
        picked = bulk_ebay.rows_to_deepen(store, limit=10)
        assert {r["source_id"] for r in picked} == {"g3", "g4", "g5"}

    def test_a_row_with_no_url_is_never_a_candidate(self, tmp_path):
        store = MarketStore(tmp_path)
        r = _row("nourl", GROUPED_TITLE, "seller0")
        r["url"] = None
        store.record([r])
        assert bulk_ebay.rows_to_deepen(store, limit=10) == []

    def test_a_store_where_nothing_groups_still_deepens(self, tmp_path):
        """The starvation guard: bad grouping must not zero out Tier 2."""
        store = MarketStore(tmp_path)
        store.record([_row(f"s{i}", f"Wholly Distinct Item {i} XYZ", f"solo{i}")
                      for i in range(8)])
        assert len(bulk_ebay.rows_to_deepen(store, limit=5)) == 5

    def test_an_empty_store_yields_nothing(self, tmp_path):
        assert bulk_ebay.rows_to_deepen(MarketStore(tmp_path), limit=5) == []
