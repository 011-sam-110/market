"""The niche board: rank niches, then products within them, at the best
identity tier each supports (design spec section 4 BOARD stage; contracts
section 2.13).

Rows are built from market_store.empty_row() so every ROW_FIELDS column is
present, the same pattern tests/test_metrics.py and tests/test_snapshots.py
use.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from phoneloop import board, catalog_ebay, fees
from phoneloop.knobs import Knobs
from phoneloop.market_store import MarketStore, empty_row
from phoneloop.snapshots import difference

# LEAF_GOOD is a REAL in-scope leaf (ebay_categories.json: "Mobile Phones &
# Smartphones", under department "Mobile Phones & Communication", id 15032)
# on purpose, not an arbitrary string. fees.FVF_BP_BY_TOP_LEVEL_NAME rates
# that department at 990bp, which differs from EVERYTHING_ELSE_BP (1290bp) --
# so a leaf id that only resolves through catalog_ebay.top_level_of() prices
# differently than one that falls to the fallback. LEAF_BAD/LEAF_WIN stay
# synthetic: nothing below exercises their fee rate, only their price/
# feedback/delivery bands and their snapshot dates.
LEAF_GOOD = "9355"        # in-band price, good feedback, fast delivery
LEAF_BAD = "200000"      # out-of-band price, poor feedback, slow delivery
LEAF_WIN = "300000"      # exists on two snapshot dates -> windowed STR

DAY_A = date(2026, 8, 20)
DAY_B = date(2026, 8, 21)
SEEN_A = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
SEEN_B = datetime(2026, 8, 21, 9, 0, tzinfo=UTC)


def _row(source_id: str, leaf: str, seller_id: str, seen_at: datetime, **over) -> dict:
    row = empty_row("ebay", source_id)
    row.update({
        "seen_at": seen_at, "category": "garden", "shard": f"leaf{leaf}",
        "title": f"Widget {source_id}", "price_pence": 3600, "total_pence": 4000,
        "currency": "GBP", "condition": "New", "available": True,
        "seller_id": seller_id, "seller_type": "business",
        "seller_feedback_bp": 9900, "seller_feedback_n": 500,
        "eta_days": 3, "leaf_category_id": leaf, "sold_qty": 12,
    })
    row.update(over)
    return row


@pytest.fixture
def knobs():
    return Knobs()


@pytest.fixture
def two_niche_store(tmp_path):
    """One snapshot date only -- every row in this store is 'lifetime'."""
    s = MarketStore(tmp_path)
    s.record([
        _row("g1", LEAF_GOOD, "sellerA", SEEN_A, total_pence=4000, eta_days=3,
             seller_feedback_bp=9900, seller_feedback_n=500),
        _row("g2", LEAF_GOOD, "sellerB", SEEN_A, total_pence=4200, eta_days=2,
             seller_feedback_bp=9950, seller_feedback_n=800),
        _row("b1", LEAF_BAD, "sellerC", SEEN_A, total_pence=9000, eta_days=10,
             seller_feedback_bp=8000, seller_feedback_n=10),
        _row("b2", LEAF_BAD, "sellerD", SEEN_A, total_pence=9500, eta_days=12,
             seller_feedback_bp=7500, seller_feedback_n=5),
    ])
    return s


class TestRankNiches:
    def test_good_niche_outranks_bad_niche(self, two_niche_store, knobs):
        rows = board.rank_niches(two_niche_store, knobs)
        assert [r.key for r in rows] == [LEAF_GOOD, LEAF_BAD]
        assert rows[0].score > rows[1].score

    def test_single_snapshot_is_lifetime_everywhere(self, two_niche_store, knobs):
        rows = board.rank_niches(two_niche_store, knobs)
        assert {r.str_source for r in rows} == {"lifetime"}

    def test_max_cost_comes_from_fees_not_reimplemented(self, two_niche_store, knobs):
        """board.py must not compute its own fee arithmetic -- cross-check
        every row's max_cost against a direct fees.max_cost() call on the
        same price_floor/price_median it reports, passed the SAME
        top_level_of mapping board.py itself threads through (contracts
        section 6.2, item 1) -- omitting it here would make this a
        same-fallback-on-both-sides tautology rather than a real check, since
        every leaf in this fixture would then price at EVERYTHING_ELSE_BP on
        both sides regardless of whether board.py's own wiring is intact."""
        top_level_of = catalog_ebay.top_level_of()
        rows = board.rank_niches(two_niche_store, knobs)
        for r in rows:
            if r.metrics.price_floor_pence is not None:
                expected = fees.max_cost(r.metrics.price_floor_pence, r.key, knobs,
                                         top_level_of=top_level_of)
                assert r.max_cost_at_floor_pence == expected
            if r.metrics.price_median_pence is not None:
                expected = fees.max_cost(r.metrics.price_median_pence, r.key, knobs,
                                         top_level_of=top_level_of)
                assert r.max_cost_at_median_pence == expected

    def test_max_cost_actually_uses_the_real_category_rate(self, two_niche_store, knobs):
        """Guard for the integration wiring itself (contracts section 6.2,
        item 1), not just internal consistency: LEAF_GOOD (real leaf 9355,
        "Mobile Phones & Smartphones") must price at its department's 990bp
        rate, NOT at EVERYTHING_ELSE_BP (1290bp) -- the fallback every leaf
        gets when board.py fails to pass a populated top_level_of into
        fees.max_cost. If board.py's `top_level_of = catalog_ebay.top_level_of()`
        wiring is ever dropped (reverted to `top_level_of=None`), this row's
        max_cost silently jumps to the fallback-rate figure and this test
        catches that directly by comparing against the fallback rate itself.
        (test_max_cost_comes_from_fees_not_reimplemented above also catches
        the same regression, since it threads the real top_level_of into its
        own expectation rather than recomputing board.py's fallback -- the
        two tests are watched red together below, not offered as
        alternatives.)"""
        rows = board.rank_niches(two_niche_store, knobs)
        good = next(r for r in rows if r.key == LEAF_GOOD)
        assert good.metrics.price_floor_pence is not None
        fallback_max_cost = fees.max_cost(good.metrics.price_floor_pence, LEAF_GOOD, knobs,
                                          top_level_of=None)
        assert good.max_cost_at_floor_pence != fallback_max_cost

    def test_limit_truncates(self, two_niche_store, knobs):
        rows = board.rank_niches(two_niche_store, knobs, limit=1)
        assert len(rows) == 1
        assert rows[0].key == LEAF_GOOD

    def test_niche_identity_tier_is_none(self, two_niche_store, knobs):
        rows = board.rank_niches(two_niche_store, knobs)
        assert all(r.identity_tier == "none" for r in rows)

    def test_competitors_is_distinct_sellers_in_the_niche(self, two_niche_store, knobs):
        rows = board.rank_niches(two_niche_store, knobs)
        saturation = {k.knob: k for r in rows for k in r.knobs if r.key == LEAF_GOOD}["saturation"]
        assert saturation.value == 2   # sellerA, sellerB


class TestStrSourceWindow:
    def test_two_snapshots_gives_window_source(self, tmp_path, knobs):
        s = MarketStore(tmp_path)
        s.record([_row("w1", LEAF_WIN, "sellerE", SEEN_A, sold_qty=5)])
        s.record([_row("w1", LEAF_WIN, "sellerE", SEEN_B, sold_qty=9)])
        diff = difference(s, DAY_A, DAY_B)

        rows = board.rank_niches(s, knobs, difference=diff)
        row = next(r for r in rows if r.key == LEAF_WIN)
        assert row.str_source == "window"
        assert row.metrics.str_window_bp is not None

    def test_no_difference_passed_gives_lifetime_even_with_two_dates(self, tmp_path, knobs):
        """A caller that has two snapshot dates but does not pass a
        Difference in still gets the honest 'lifetime' label -- str_source
        reflects what was actually supplied, not what could theoretically
        be computed."""
        s = MarketStore(tmp_path)
        s.record([_row("w1", LEAF_WIN, "sellerE", SEEN_A, sold_qty=5)])
        s.record([_row("w1", LEAF_WIN, "sellerE", SEEN_B, sold_qty=9)])

        rows = board.rank_niches(s, knobs)   # no difference=
        row = next(r for r in rows if r.key == LEAF_WIN)
        assert row.str_source == "lifetime"


class TestRankProducts:
    @pytest.fixture
    def product_store(self, tmp_path):
        s = MarketStore(tmp_path)
        s.record([
            # Two listings of the SAME catalogued product (shared epid),
            # from two different sellers -- one product, two competitors.
            _row("p1", LEAF_GOOD, "sellerA", SEEN_A, epid="EPID1",
                identity_tier="catalogued"),
            _row("p2", LEAF_GOOD, "sellerB", SEEN_A, epid="EPID1",
                identity_tier="catalogued"),
            # An unrelated listing with no epid/gtin/title-cluster kin --
            # tier "none", excluded from every product group.
            _row("p3", LEAF_GOOD, "sellerC", SEEN_A, epid=None, title=None,
                leaf_category_id=None),
        ])
        return s

    def test_groups_by_catalogued_epid(self, product_store, knobs):
        rows = board.rank_products(product_store, knobs, [LEAF_GOOD])
        assert len(rows) == 1
        row = rows[0]
        assert row.key == "epid:EPID1"
        assert row.identity_tier == "catalogued"
        assert row.level == "product"

    def test_competitors_is_distinct_sellers_on_that_product(self, product_store, knobs):
        rows = board.rank_products(product_store, knobs, [LEAF_GOOD])
        saturation = next(k for k in rows[0].knobs if k.knob == "saturation")
        assert saturation.value == 2   # sellerA, sellerB

    def test_niches_not_requested_are_excluded(self, product_store, knobs):
        rows = board.rank_products(product_store, knobs, [LEAF_BAD])
        assert rows == []


class TestRenderTable:
    def test_contains_every_knob_and_the_lifetime_note(self, two_niche_store, knobs):
        rows = board.rank_niches(two_niche_store, knobs)
        text = board.render_table(rows)
        for knob_name in ("min_margin", "sell_through", "price_band",
                          "delivery", "seller_quality", "saturation"):
            assert knob_name in text
        assert "lifetime" in text


class TestRenderHtml:
    def test_self_contained_no_external_resources(self, two_niche_store, knobs):
        rows = board.rank_niches(two_niche_store, knobs)
        out = board.render_html(rows, title="test board",
                                generated_at=datetime(2026, 8, 27, tzinfo=UTC))
        assert "http://" not in out
        assert "https://" not in out
        assert "<style>" in out   # CSS is inline, not linked

    def test_lifetime_row_visibly_says_not_comparable(self, two_niche_store, knobs):
        rows = board.rank_niches(two_niche_store, knobs)
        out = board.render_html(rows, title="test board",
                                generated_at=datetime(2026, 8, 27, tzinfo=UTC))
        assert "not comparable" in out


class TestWriteReport:
    def test_writes_under_data_dir_reports(self, two_niche_store, knobs, tmp_path):
        rows = board.rank_niches(two_niche_store, knobs)
        path = board.write_report(rows, tmp_path)
        assert path.exists()
        assert path.parent.name == "reports"
        assert path.parent.parent == tmp_path
        assert "http://" not in path.read_text(encoding="utf-8")
