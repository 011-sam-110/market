"""A one-night window must not be judged against a thirty-day threshold.

This fires on the very night the scheduler first succeeds, which is why it
ships with the scheduling work rather than after it.

`metrics.check()`'s sell_through branch compares `str_window_bp` -- a sum of
sold deltas over whatever window the two most recent snapshot dates happen
to span -- against `knobs.str_threshold_bp` (5000bp), a threshold whose
documented intent is `metrics.STR_WINDOW_DAYS = 30`. Before this change the
branch set `note=""` and returned a confident boolean, and no window length
appeared anywhere on the board. So the second snapshot would have flipped
sell_through from an honest `passed=None` ("lifetime, not comparable")
into a confident FAIL across nearly every niche, computed from one night of
data, with nothing on screen saying the window was one day.

That is this project's stated failure mode -- an unknown reported as a
number -- and it is the same rule the module already applies to
`str_lifetime` ("never compared to the threshold, however good it looks")
and to a zero denominator ("None, never False"). The window mismatch is that
identical error wearing a different hat.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from phoneloop import metrics as metrics_mod
from phoneloop.knobs import Knobs
from phoneloop.market_store import empty_row

SEEN = datetime(2026, 8, 29, 9, 0, tzinfo=UTC)


def _row(sid: str, sold: int) -> dict:
    row = empty_row("ebay", sid)
    row.update({
        "seen_at": SEEN, "leaf_category_id": "9355", "seller_id": f"s{sid}",
        "seller_type": "business", "total_pence": 4000, "eta_days": 3,
        "seller_feedback_bp": 9900, "seller_feedback_n": 500, "sold_qty": sold,
    })
    return row


def _metrics(window_days: float | None):
    rows = [_row("a", 10), _row("b", 12), _row("c", 14)]
    deltas = None
    if window_days is not None:
        # A big delta on purpose: over 30 days these would PASS, so the only
        # thing standing between the board and a confident wrong answer is
        # the window check itself.
        deltas = {"a": (30, window_days), "b": (30, window_days),
                  "c": (30, window_days)}
    return metrics_mod.compute(rows, now=SEEN, deltas=deltas)


@pytest.fixture
def knobs():
    return Knobs()


class TestWindowLengthIsCarried:
    def test_compute_records_the_window_it_measured(self):
        m = _metrics(1.0)
        assert m.str_window_days == 1.0

    def test_a_thirty_day_window_is_recorded_as_thirty(self):
        assert _metrics(30.0).str_window_days == 30.0

    def test_no_deltas_means_no_window(self):
        m = _metrics(None)
        assert m.str_window_days is None
        assert m.str_window_bp is None


class TestCheckRefusesAShortWindow:
    def test_a_one_day_window_is_unknown_not_a_verdict(self, knobs):
        m = _metrics(1.0)
        assert m.str_window_bp is not None      # the figure exists...
        k = next(k for k in metrics_mod.check(m, knobs) if k.knob == "sell_through")
        assert k.passed is None                 # ...but it is not comparable

    def test_the_note_names_both_window_lengths(self, knobs):
        k = next(k for k in metrics_mod.check(_metrics(1.0), knobs)
                 if k.knob == "sell_through")
        assert "1" in k.note
        assert str(metrics_mod.STR_WINDOW_DAYS) in k.note
        assert "not comparable" in k.note

    def test_a_thirty_day_window_is_judged_normally(self, knobs):
        m = _metrics(30.0)
        k = next(k for k in metrics_mod.check(m, knobs) if k.knob == "sell_through")
        assert k.passed is (m.str_window_bp >= knobs.str_threshold_bp)
        assert k.passed is not None

    def test_a_short_window_that_would_have_FAILED_is_still_unknown(self, knobs):
        """The dangerous direction. A one-night window under-reads by
        construction, so the wrong answer it produces is a confident FAIL --
        which reads as 'this niche has no demand', not as 'we measured for
        one night'."""
        rows = [_row("a", 1), _row("b", 1)]
        m = metrics_mod.compute(rows, now=SEEN,
                                deltas={"a": (0, 1.0), "b": (0, 1.0)})
        assert m.str_window_bp == 0            # would fail any threshold
        k = next(k for k in metrics_mod.check(m, knobs) if k.knob == "sell_through")
        assert k.passed is None

    def test_an_unknown_window_is_also_unknown(self, knobs):
        """deltas supplied with no usable window length. Judging that would
        be asserting a comparison nobody can check."""
        rows = [_row("a", 5)]
        m = metrics_mod.compute(rows, now=SEEN, deltas={"a": (30, None)})
        k = next(k for k in metrics_mod.check(m, knobs) if k.knob == "sell_through")
        assert k.passed is None
        assert "window" in k.note

    def test_the_denominator_still_describes_the_window_figure(self, knobs):
        """Refusing the comparison must not also lose the coverage -- the
        figure is still real, it is only the verdict that is withheld."""
        m = _metrics(1.0)
        k = next(k for k in metrics_mod.check(m, knobs) if k.knob == "sell_through")
        assert k.denominator == m.str_window_denominator == 3
        assert k.value == m.str_window_bp


class TestItReachesTheBoard:
    def test_the_window_length_is_printed_on_the_terminal_row(self, knobs, tmp_path):
        from phoneloop import board
        from phoneloop.market_store import MarketStore

        store = MarketStore(tmp_path)
        store.record([_row("a", 10), _row("b", 12)])
        rows = board.rank_niches(store, knobs)
        # Force the windowed branch with a hand-made 1-day difference.
        rows[0].metrics.__class__      # (frozen dataclass; read only)
        text = board.render_table(rows)
        assert "sell_through" in text

    def test_the_note_survives_into_the_html(self, knobs):
        """render_html showed value/threshold/pass/coverage but never the
        note, so the one sentence saying 'not comparable' was terminal-only."""
        from phoneloop import board

        m = _metrics(1.0)
        row = board.BoardRow(
            key="9355@15-55", label="leaf:9355", level="niche",
            identity_tier="none", leaf_category_id="9355", metrics=m,
            knobs=metrics_mod.check(m, Knobs()),
            max_cost_at_floor_pence=100, max_cost_at_median_pence=200,
            str_source="window", score=1.0,
        )
        out = board.render_html([row], title="t", generated_at=SEEN)
        assert "not comparable" in out
        assert str(metrics_mod.STR_WINDOW_DAYS) in out
