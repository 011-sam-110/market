"""scripts/bulk_ebay.py's `rows_to_deepen` -- the store-side half of Tier 2
(design spec section 4, contracts section 6.2's DEEPEN gap; integration
review 2026-08-28, blocking finding #2).

Pure over `MarketStore` -- no browser, no harness -- so it is tested
directly rather than through a live or replayed run, the same reasoning
scripts/bulk_ebay.py's own module docstring gives for why the rest of the
script (browser-driven) has no test module of its own. Rows are built from
`market_store.empty_row()`, the same literal-row pattern tests/test_board.py,
tests/test_metrics.py and tests/test_snapshots.py already use -- not a claim
that these are captured recordings, just row-shaped test data.

Imported via `importlib` because `scripts/` is not a package (no
`__init__.py`, matching every other script in this project) -- the module's
own `sys.path.insert(...)` bootstrap only matters when it runs as
`__main__`, which `exec_module` does not trigger.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path

from phoneloop.market_store import MarketStore, empty_row

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "bulk_ebay.py"
_spec = importlib.util.spec_from_file_location("bulk_ebay_under_test", _SCRIPT)
bulk_ebay = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = bulk_ebay
_spec.loader.exec_module(bulk_ebay)


def _row(source_id: str, **over) -> dict:
    row = empty_row("ebay", source_id)
    row.update({"url": f"https://www.ebay.co.uk/itm/{source_id}",
               "title": f"Widget {source_id}", "price_pence": 3600})
    row.update(over)
    return row


class TestRowsToDeepen:
    def test_picks_rows_with_a_url_and_no_seller_type_yet(self, tmp_path):
        store = MarketStore(tmp_path)
        store.record([_row("1"), _row("2", seller_type="business")])
        candidates = bulk_ebay.rows_to_deepen(store, limit=10)
        assert [r["source_id"] for r in candidates] == ["1"]

    def test_a_row_with_no_url_is_never_a_candidate(self, tmp_path):
        """No item page can be found for a listing with no url -- attempting
        one would mean guessing a URL, which this module never does."""
        store = MarketStore(tmp_path)
        store.record([_row("1", url=None)])
        assert bulk_ebay.rows_to_deepen(store, limit=10) == []

    def test_respects_the_limit(self, tmp_path):
        store = MarketStore(tmp_path)
        store.record([_row(str(i)) for i in range(5)])
        assert len(bulk_ebay.rows_to_deepen(store, limit=2)) == 2

    def test_empty_store_returns_no_candidates_not_an_error(self, tmp_path):
        store = MarketStore(tmp_path)
        assert bulk_ebay.rows_to_deepen(store, limit=10) == []

    def test_reads_the_latest_snapshot_date_only(self, tmp_path):
        """eBay is an always-snapshot source (market_store.py) -- an older
        snapshot date's rows for a listing that has since been re-swept must
        not resurface as a deepen candidate just because an older row of
        theirs still lacks seller_type; only the CURRENT population (the
        board's own reading, phoneloop.board._latest_rows) is a candidate."""
        store = MarketStore(tmp_path)
        old = _row("1", seen_at=datetime(2026, 1, 1, tzinfo=UTC))
        store.record([old])
        new = _row("1", seller_type="business", seen_at=datetime(2026, 8, 20, tzinfo=UTC))
        store.record([new])
        assert bulk_ebay.rows_to_deepen(store, limit=10) == []
