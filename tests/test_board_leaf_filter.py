"""Downstream consumers filter niches by LEAF, so the niche key changing
shape must not break them.

Regression, 2026-08-28. Making the niche key "leaf x price band" changed it
from "9355" to "9355@15-55". Two consumers compare that key against raw leaf
ids:

    scripts/niche_board.py   [r for r in niche_rows if r.key in known_leaves]
    board.rank_products      the `niches` argument, matched against
                             row["leaf_category_id"]

Both silently matched nothing. The board printed "(no rows)" for every niche
and every product over a store holding 29,602 real listings, and the whole
suite stayed green because nothing exercised the two together. "(no rows)"
reads as "no opportunities today", not as "the filter is broken", which is
the failure mode this project keeps hitting.

The fix is that a BoardRow carries its leaf explicitly, and consumers filter
on that rather than parsing it back out of the key.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from phoneloop import board
from phoneloop.knobs import Knobs
from phoneloop.market_store import MarketStore, empty_row

LEAF = "9355"          # a real in-scope leaf, as tests/test_board.py uses
BAND = "15-55"
SEEN = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)


def _row(source_id: str, seller_id: str, *, banded: bool) -> dict:
    row = empty_row("ebay", source_id)
    row.update({
        "seen_at": SEEN, "category": "mobile_phone_accessories",
        # the real shapes: banded is what the sweep writes now, unbanded is
        # what the 29,602 rows already in the store carry
        "shard": f"{LEAF}/{BAND}/p1" if banded else f"{LEAF}/p1",
        "title": f"Widget {source_id}", "price_pence": 3600,
        "total_pence": 4000, "currency": "GBP", "condition": "New",
        "available": True, "seller_id": seller_id, "seller_type": "business",
        "seller_feedback_bp": 9900, "seller_feedback_n": 500,
        "eta_days": 3, "leaf_category_id": LEAF, "sold_qty": 12,
    })
    return row


@pytest.fixture
def banded_store(tmp_path):
    store = MarketStore(tmp_path)
    store.record([_row(f"b{i}", f"seller{i}", banded=True) for i in range(6)])
    return store


@pytest.fixture
def unbanded_store(tmp_path):
    store = MarketStore(tmp_path)
    store.record([_row(f"u{i}", f"seller{i}", banded=False) for i in range(6)])
    return store


class TestBoardRowKnowsItsLeaf:
    def test_a_banded_niche_still_reports_its_leaf(self, banded_store):
        (row,) = board.rank_niches(banded_store, Knobs())
        assert row.leaf_category_id == LEAF
        assert row.key != LEAF, "the key is expected to carry the band"

    def test_an_unbanded_niche_reports_the_same_leaf(self, unbanded_store):
        (row,) = board.rank_niches(unbanded_store, Knobs())
        assert row.leaf_category_id == LEAF


class TestFilteringByKnownLeavesSurvivesTheBandedKey:
    """What scripts/niche_board.py does with the result."""

    def test_a_banded_niche_is_not_filtered_out(self, banded_store):
        rows = board.rank_niches(banded_store, Knobs())
        kept = [r for r in rows if r.leaf_category_id in {LEAF}]
        assert len(kept) == 1, "filtering by known leaf dropped a real niche"

    def test_products_are_found_inside_a_banded_niche(self, banded_store):
        niches = board.rank_niches(banded_store, Knobs())
        products = board.rank_products(
            banded_store, Knobs(), [r.leaf_category_id for r in niches])
        assert products, "no products found inside a banded niche"
