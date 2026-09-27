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


class TestTheCircuitBreaker:
    """A deepen budget of several thousand runs for hours unattended.

    Each item-page failure is caught and skipped, which is right for an item
    eBay happens to refuse and wrong for an eBay that has started refusing
    everything. These pin the difference.

    `offline=True` keeps a browser from opening, and `recorded` is replaced
    by a direct call to the producer so the harness does not intercept. That
    leaves `fetch_item` as the single failure injection point, which is the
    same path a live block takes.
    """

    def _stub(self, monkeypatch, fetch):
        monkeypatch.setattr(bulk_ebay, "recorded",
                            lambda _c, _k, _n, produce, **_kw: produce())
        monkeypatch.setattr(bulk_ebay, "fetch_item", fetch)
        monkeypatch.setattr(bulk_ebay.ebay_item, "parse_item",
                            lambda *a, **k: object())
        monkeypatch.setattr(bulk_ebay.ebay_item, "to_row_updates",
                            lambda *a, **k: {"seller_type": "business"})

    def _store(self, tmp_path, n):
        store = MarketStore(tmp_path)
        store.record([_row(str(i)) for i in range(n)])
        return store

    def _run(self, store, tmp_path, limit):
        return bulk_ebay.run_deepen(
            store, bulk_ebay.Cassette(tmp_path / "fixtures"),
            offline=True, limit=limit, headless=True, direct=True)

    def test_a_sustained_block_stops_the_run(self, tmp_path, monkeypatch, capsys):
        """Without this, a 5,000-item budget spends hours fetching nothing
        and reports "deepened 0/5000" at the end of it."""
        calls = {"n": 0}

        def fetch(_page, _url):
            calls["n"] += 1
            raise RuntimeError("Something went wrong on our end")

        self._stub(monkeypatch, fetch)
        store = self._store(tmp_path, 200)
        code = self._run(store, tmp_path, 200)
        out = capsys.readouterr().out

        assert calls["n"] == bulk_ebay.MAX_CONSECUTIVE_FAILURES
        assert "STOPPED EARLY" in out
        assert "25 attempted and refused, 175 never attempted" in out
        assert code == 0, (
            "a partial deepen must not abort the rest of the nightly "
            "pipeline -- the rows it did deepen are real and already stored")

    def test_scattered_failures_do_not_stop_it(self, tmp_path, monkeypatch, capsys):
        """One refused item is normal. The breaker must not fire on noise,
        or a single awkward listing would end the night's budget."""
        calls = {"n": 0}

        def fetch(_page, url):
            calls["n"] += 1
            if calls["n"] % 5 == 0:
                raise RuntimeError("Something went wrong on our end")
            return {"text": f"item {url}", "html": "", "url": url}

        self._stub(monkeypatch, fetch)
        store = self._store(tmp_path, 60)
        self._run(store, tmp_path, 60)
        out = capsys.readouterr().out

        assert calls["n"] == 60, "every candidate must still be attempted"
        assert "STOPPED EARLY" not in out
        assert "deepened 48/60" in out

    def test_a_success_resets_the_streak(self, tmp_path, monkeypatch, capsys):
        """24 failures, one success, then failures again is not a sustained
        block until the second streak completes. A counter that never reset
        would have stopped during the first."""
        cap = bulk_ebay.MAX_CONSECUTIVE_FAILURES
        calls = {"n": 0}

        def fetch(_page, url):
            calls["n"] += 1
            if calls["n"] == cap:          # one success just before the cap
                return {"text": f"item {url}", "html": "", "url": url}
            raise RuntimeError("Something went wrong on our end")

        self._stub(monkeypatch, fetch)
        store = self._store(tmp_path, 200)
        self._run(store, tmp_path, 200)
        out = capsys.readouterr().out

        # 24 failures, 1 success, then a full fresh streak of 25.
        assert calls["n"] == (cap - 1) + 1 + cap
        assert "STOPPED EARLY" in out
        assert "deepened 1/200" in out


class TestTheTimeBudget:
    """Deepen runs before the board and the publish step. A deepen that
    outlives the night takes both with it -- which is what the Windows
    task's 3h kill did on most nights after 2026-09-08."""

    _stub = TestTheCircuitBreaker._stub
    _store = TestTheCircuitBreaker._store

    @staticmethod
    def _clock(step_s):
        """A clock that moves step_s seconds every time it is read."""
        t = {"now": 0.0}

        def read():
            t["now"] += step_s
            return t["now"]
        return read

    def _ok(self, calls):
        def fetch(_page, url):
            calls["n"] += 1
            return {"text": f"item {url}", "html": "", "url": url}
        return fetch

    def test_the_budget_stops_the_run_cleanly(self, tmp_path, monkeypatch, capsys):
        calls = {"n": 0}
        self._stub(monkeypatch, self._ok(calls))
        store = self._store(tmp_path, 50)
        # Deadline is set at t=60 to 60+300=360; each loop check moves the
        # clock 60 s, so checks read 120..360 and the fifth one stops it.
        code = bulk_ebay.run_deepen(
            store, bulk_ebay.Cassette(tmp_path / "fixtures"),
            offline=True, limit=50, headless=True, direct=True,
            max_seconds=300, clock=self._clock(60))
        out = capsys.readouterr().out

        assert calls["n"] == 4
        assert "STOPPED EARLY: the 5-minute deepen budget ran out" in out
        assert "deepened 4/50" in out
        assert "46 never attempted" in out
        assert code == 0, (
            "a spent budget must not abort the nightly chain -- the board "
            "and the publish step are the reason the budget exists")

    def test_no_budget_means_no_limit(self, tmp_path, monkeypatch, capsys):
        """0 is the default, and must keep the old behaviour exactly."""
        calls = {"n": 0}
        self._stub(monkeypatch, self._ok(calls))
        store = self._store(tmp_path, 30)
        bulk_ebay.run_deepen(
            store, bulk_ebay.Cassette(tmp_path / "fixtures"),
            offline=True, limit=30, headless=True, direct=True,
            clock=self._clock(10_000))
        out = capsys.readouterr().out

        assert calls["n"] == 30
        assert "STOPPED EARLY" not in out
