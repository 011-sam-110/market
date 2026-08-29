"""The review page's "leaves" denominators must be counted, not typed in.

Two figures on the population page divided by a hardcoded `67`:

    population_html.py:765   _of(s["leaves"], 67, "leaves swept")
    population_html.py:1209  _of(p["leaves_touched"], 67, "leaves touched")

`67` is not the size of anything this project sweeps. The in-scope catalogue
(`catalog_ebay.top_level_of()`) holds 43 leaves and the 2026-08-28 pass swept
all 43 of them, so the constant is wrong in the direction that flatters the
page: a seller present in every leaf we look at renders as "43 / 67 leaves
swept" -- 64%, read as a trader with partial reach, when the truthful figure
is 100% and that seller is in the whole market.

That is worse than a missing number. A denominator's entire job here is to
say how much of the whole a part represents (module rule: "a figure never
travels without the denominator it was computed over"), and a denominator
nobody counted quietly rescales every share on the panel.

These tests pin the denominator to the data. They are deliberately written
against a store whose leaf count is NEITHER 43 nor 67, so neither the old
constant nor a new constant swapped in for it can pass: the only way through
is to count what is actually there.
"""

from __future__ import annotations


from datetime import UTC, date, datetime

import pytest

from phoneloop import population, population_html
from phoneloop.knobs import Knobs
from phoneloop.market_store import MarketStore, empty_row

SEEN = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
DAY = date(2026, 8, 20)

# Three leaves, all of them real members of the in-scope catalogue, chosen so
# the expected denominator (3) collides with no constant in the codebase.
LEAVES = ("139971", "20518", "11700")


def _row(source_id: str, leaf: str, seller_id: str, **over) -> dict:
    row = empty_row("ebay", source_id)
    row.update({
        "seen_at": SEEN, "category": "garden_patio",
        "shard": f"{leaf}/15-55/p1", "title": f"Widget {source_id}",
        "price_pence": 3600, "total_pence": 4000, "currency": "GBP",
        "condition": "New", "available": True, "region": "GB",
        "seller_id": seller_id, "seller_type": "business",
        "seller_feedback_bp": 9900, "seller_feedback_n": 500, "eta_days": 3,
        "leaf_category_id": leaf, "sold_qty": 12, "promoted": False,
        "origin_country": "GB",
        "url": f"https://www.ebay.co.uk/itm/{source_id}",
        "extra": '{"aspects":[],"watchers":0,"ships_from":"","stock_hint":""}',
    })
    row.update(over)
    return row


@pytest.fixture
def three_leaf_store(tmp_path):
    """One seller in all three swept leaves; two sellers in one leaf each.

    `omni` is the case the bug distorts: it is in 3 of the 3 leaves that
    exist, so any honest rendering says 3/3, and the buggy one says 3/67.
    """
    s = MarketStore(tmp_path)
    rows = []
    for i, leaf in enumerate(LEAVES):
        rows.append(_row(f"omni{i}", leaf, "omni"))
        rows.append(_row(f"local{i}", leaf, f"local{i}"))
    s.record(rows)
    return s


@pytest.fixture
def build(three_leaf_store, tmp_path):
    # `three_leaf_store` writes into tmp_path; build() reads the directory.
    return population.build(tmp_path, Knobs())


class TestTheBreadthDenominatorIsCounted:
    def test_the_seller_in_every_leaf_is_shown_over_the_leaves_that_exist(
            self, build):
        """3 leaves swept, a seller in all 3 -> the denominator is 3."""
        payload, reason = population.panel_seller_footprint(build)
        assert reason is None, f"panel did not render: {reason}"
        omni = next(s for s in payload["by_breadth"] if s["seller_id"] == "omni")
        assert omni["leaves"] == 3
        assert payload["leaves_swept"] == 3, (
            "the footprint panel must carry the leaf count it divides by, "
            "rather than the renderer supplying a constant")

    def test_the_number_67_appears_nowhere_in_the_rendered_page(self, build):
        """The literal, wherever it is reached from. `_of` renders as
        "n=3 of 67 leaves swept", so that is the string to look for -- an
        assertion written against a format the page does not use would pass
        before and after the fix and prove nothing."""
        html = population_html.render(build)
        assert "of 67" not in html, "a hardcoded 67 reached the page"

    def test_a_seller_in_every_leaf_reads_as_the_whole_market(self, build):
        """The user-visible symptom: 3 of 3, never 3 of something larger."""
        html = population_html.render(build)
        assert "n=3 of 3 leaves swept" in html, (
            "the seller present in every swept leaf is not shown as covering "
            "all of them")


class TestTheDenominatorFollowsTheData:
    def test_a_two_leaf_store_says_two(self, tmp_path):
        """Change the data, the denominator must change with it -- this is
        what a constant swapped for another constant cannot do."""
        s = MarketStore(tmp_path)
        s.record([_row("x0", LEAVES[0], "omni"), _row("x1", LEAVES[1], "omni")])
        payload, reason = population.panel_seller_footprint(
            population.build(tmp_path, Knobs()))
        assert reason is None
        assert payload["leaves_swept"] == 2
