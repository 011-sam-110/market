"""Tier 3 differences the newest date against a baseline 25 or more dates
older, not against last night.

Before this, `scripts/niche_board.py` and `phoneloop/population.py` always
differenced `dates[-2]` against `dates[-1]`. On a nightly schedule that is a
window of about 1 day, and `metrics.check` withholds any window shorter than
STR_WINDOW_DAYS x MIN_COMPARABLE_WINDOW_FRACTION (24.0 days). So sell-through
read "?" on every row however many nights the store held: all 40 rows on the
2026-10-04 board.

Why 25 dates and not 24: a snapshot date is a UTC day, and `metrics.check`
judges elapsed time. On the prod store, 2026-09-10 -> 2026-10-04 (24 dates)
measured a median of 23.998 days. `test_a_24_date_gap_can_measure_under_24_days`
below rebuilds that failure on a fixture.
"""

from __future__ import annotations

import importlib.util
import math
import os
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from phoneloop import board, metrics, population, population_html
from phoneloop.knobs import Knobs
from phoneloop.market_store import MarketStore, empty_row
from phoneloop.snapshots import (BASELINE_MIN_GAP_DAYS, describe_baseline,
                                 difference, pick_baseline)

# A real in-scope leaf ("Mobile Phones & Smartphones"), so the niche row
# survives the board script's catalogue filter.
LEAF = "9355"
NEWEST = date(2026, 10, 4)

# The snapshot dates the prod store on provenance-1 held on 2026-10-04,
# read from its date partitions (read-only). 2026-09-10 is exactly 24
# dates before the newest, and 2026-09-09 does not exist.
PROD_DATES_2026_10_04 = [date.fromisoformat(d) for d in (
    "2026-08-28", "2026-08-29", "2026-08-30", "2026-08-31", "2026-09-03",
    "2026-09-04", "2026-09-06", "2026-09-07", "2026-09-08", "2026-09-10",
    "2026-09-12", "2026-09-13", "2026-09-14", "2026-09-15", "2026-09-16",
    "2026-09-17", "2026-09-18", "2026-09-20", "2026-09-21", "2026-09-23",
    "2026-09-26", "2026-09-27", "2026-09-28", "2026-09-29", "2026-09-30",
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04")]

NIGHTLY_SCRIPT = (Path(__file__).resolve().parent.parent
                  / "scripts" / "niche_board.py")


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def _row(sid: str, seen_at: datetime, sold: int) -> dict:
    row = empty_row("ebay", sid)
    row.update({
        "seen_at": seen_at, "category": "phones", "shard": f"leaf{LEAF}",
        "title": f"Widget {sid}", "price_pence": 3600, "total_pence": 4000,
        "currency": "GBP", "condition": "New", "available": True,
        "seller_id": f"seller_{sid}", "seller_type": "business",
        "seller_feedback_bp": 9900, "seller_feedback_n": 500,
        "eta_days": 3, "leaf_category_id": LEAF, "sold_qty": sold,
    })
    return row


def _sell_through(rows: list[board.BoardRow]):
    assert len(rows) == 1, "the fixture holds one niche"
    return next(k for k in rows[0].knobs if k.knob == "sell_through")


@pytest.fixture
def knobs() -> Knobs:
    return Knobs()


@pytest.fixture
def long_store(tmp_path) -> MarketStore:
    """Four dates. The baseline (NEWEST - 25) is swept LATE in its day and
    the newest date EARLY in its day: the shortest elapsed time a 25-date
    gap can give. Three listings are seen on both dates."""
    s = MarketStore(tmp_path)
    old = NEWEST - timedelta(days=30)
    base = NEWEST - timedelta(days=BASELINE_MIN_GAP_DAYS)
    s.record([_row(f"s{i}", _at(old, 9), 5) for i in range(6)])
    s.record([_row(f"s{i}", _at(base, 23, 30), 10) for i in range(5)])
    s.record([_row(f"s{i}", _at(NEWEST - timedelta(days=1), 9), 12)
              for i in range(4)])
    s.record([_row("s0", _at(NEWEST, 0, 30), 30),
              _row("s1", _at(NEWEST, 0, 30), 31),
              _row("s2", _at(NEWEST, 0, 30), 32),
              _row("new1", _at(NEWEST, 0, 30), 1)])
    return s


@pytest.fixture
def nightly_only_store(tmp_path) -> MarketStore:
    """Two consecutive dates and nothing older: the fallback case."""
    s = MarketStore(tmp_path)
    s.record([_row(f"s{i}", _at(NEWEST - timedelta(days=1), 9), 10)
              for i in range(3)])
    s.record([_row(f"s{i}", _at(NEWEST, 9), 12) for i in range(3)])
    return s


# --- pick_baseline ------------------------------------------------------------

def test_the_gap_comes_from_the_metrics_constants():
    """25 = ceil(30 x 0.8) + 1. Read from metrics.py, so a change to the
    window rule moves the baseline with it."""
    expected = math.ceil(metrics.STR_WINDOW_DAYS
                         * metrics.MIN_COMPARABLE_WINDOW_FRACTION) + 1
    assert BASELINE_MIN_GAP_DAYS == expected == 25


def test_on_the_prod_dates_it_picks_2026_09_08():
    """The newest date 25 or more dates before 2026-10-04. 2026-09-10 is
    only 24 dates back and 2026-09-09 is missing, so 2026-09-08 (26)."""
    b = pick_baseline(PROD_DATES_2026_10_04)
    assert b is not None
    assert (b.date_a, b.date_b) == (date(2026, 9, 8), NEWEST)
    assert b.long_enough is True
    assert b.gap_days == 26


def test_it_picks_the_NEWEST_date_that_is_old_enough():
    dates = [NEWEST - timedelta(days=n) for n in (40, 31, 25, 24, 3, 1, 0)]
    b = pick_baseline(dates)
    assert b.date_a == NEWEST - timedelta(days=25)
    assert b.long_enough is True


def test_order_and_duplicates_do_not_matter():
    dates = [NEWEST, NEWEST - timedelta(days=26), NEWEST,
             NEWEST - timedelta(days=1)]
    b = pick_baseline(dates)
    assert (b.date_a, b.date_b) == (NEWEST - timedelta(days=26), NEWEST)


def test_fallback_is_the_two_newest_dates_marked_not_long_enough():
    dates = [NEWEST - timedelta(days=n) for n in (24, 10, 1, 0)]
    b = pick_baseline(dates)
    assert (b.date_a, b.date_b) == (NEWEST - timedelta(days=1), NEWEST)
    assert b.long_enough is False


def test_one_date_has_no_pair():
    assert pick_baseline([NEWEST]) is None
    assert pick_baseline([]) is None


# --- why 25 and not 24 ----------------------------------------------------------

def test_a_24_date_gap_can_measure_under_24_days(tmp_path, knobs):
    """The prod failure on a fixture: baseline swept at 23:30, newest at
    00:30, 24 dates apart. Elapsed is 23.04 days, so metrics.check withholds
    the verdict, and the board would read "not comparable" on a pair that a
    24-date rule calls long enough."""
    s = MarketStore(tmp_path)
    base = NEWEST - timedelta(days=24)
    s.record([_row(f"s{i}", _at(base, 23, 30), 10) for i in range(3)])
    s.record([_row(f"s{i}", _at(NEWEST, 0, 30), 30) for i in range(3)])

    assert pick_baseline(s.snapshot_dates()).long_enough is False
    diff = difference(s, base, NEWEST)
    k = _sell_through(board.rank_niches(s, knobs, difference=diff))
    assert k.passed is None
    assert "not comparable" in k.note


def test_a_25_date_gap_always_gives_a_verdict(long_store, knobs):
    """The same worst case one date further back: 24.04 days, so the
    sell-through knob reads PASS or FAIL, not "?"."""
    b = pick_baseline(long_store.snapshot_dates())
    assert b.long_enough is True
    diff = difference(long_store, b.date_a, b.date_b)
    assert min(ld.days for ld in diff.listings.values() if ld.known) > 24.0
    k = _sell_through(board.rank_niches(long_store, knobs, difference=diff))
    assert k.passed is not None
    assert "not comparable" not in k.note


# --- what the board prints --------------------------------------------------------

def _run_board(monkeypatch, capsys, data_dir: Path, *args: str) -> str:
    """Run scripts/niche_board.py's main() against `data_dir`, with no
    PHONELOOP_ setting from the machine leaking in."""
    for key in list(os.environ):
        if key.startswith("PHONELOOP_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PHONELOOP_DATA_DIR", str(data_dir))
    monkeypatch.setattr(sys, "argv", ["niche_board.py", *args])
    spec = importlib.util.spec_from_file_location("niche_board_under_test",
                                                  NIGHTLY_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main() == 0
    return capsys.readouterr().out


def test_board_prints_both_dates_and_the_overlap(long_store, tmp_path,
                                                monkeypatch, capsys):
    out = _run_board(monkeypatch, capsys, tmp_path)
    base = NEWEST - timedelta(days=BASELINE_MIN_GAP_DAYS)
    assert f"Tier 3 difference: {base} -> {NEWEST} (25-day gap)" in out
    assert "3 listings seen on both dates" in out
    assert "fallback" not in out
    # The niche row's sell-through now carries a verdict.
    st_lines = [ln for ln in out.splitlines() if "sell_through" in ln]
    assert st_lines, out
    assert all("not comparable" not in ln for ln in st_lines), st_lines
    assert all("[?]" not in ln for ln in st_lines), st_lines


def test_board_fallback_prints_not_comparable(nightly_only_store, tmp_path,
                                              monkeypatch, capsys):
    out = _run_board(monkeypatch, capsys, tmp_path)
    prev = NEWEST - timedelta(days=1)
    assert f"Tier 3 difference: {prev} -> {NEWEST} (1-day gap)" in out
    assert "fallback pair" in out
    assert "not comparable" in out
    assert "3 listings seen on both dates" in out
    st_lines = [ln for ln in out.splitlines() if "sell_through" in ln]
    assert st_lines and all("[?]" in ln for ln in st_lines), st_lines


def test_the_html_report_carries_the_tier3_lines(long_store, tmp_path,
                                                 monkeypatch, capsys):
    out = _run_board(monkeypatch, capsys, tmp_path, "--html")
    written = Path(out.strip().splitlines()[-1].removeprefix("wrote "))
    page = written.read_text(encoding="utf-8")
    base = NEWEST - timedelta(days=BASELINE_MIN_GAP_DAYS)
    assert f"Tier 3 difference: {base} -&gt; {NEWEST}" in page
    assert "3 listings seen on both dates" in page


def test_describe_baseline_counts_listings_seen_on_both(long_store):
    b = pick_baseline(long_store.snapshot_dates())
    diff = difference(long_store, b.date_a, b.date_b)
    assert diff.n_on_both == 3          # s0, s1, s2
    assert diff.n_listings == 6         # s0-s4 on the baseline, plus new1
    text = describe_baseline(b, diff)
    assert str(b.date_a) in text and str(b.date_b) in text


# --- the population page uses the same pair ---------------------------------------

def test_population_build_uses_the_same_baseline(long_store, tmp_path, knobs):
    b = population.build(tmp_path, knobs)
    base = NEWEST - timedelta(days=BASELINE_MIN_GAP_DAYS)
    assert (b.diff.date_a, b.diff.date_b) == (base, NEWEST)
    assert b.baseline.long_enough is True

    out = population_html.render(b)
    assert f"Window: {base} → {NEWEST} (25-day gap" in out


def test_population_fallback_is_labelled_not_comparable(nightly_only_store,
                                                        tmp_path, knobs):
    """The 1-day fallback used to render a confident PASS or FAIL in the
    sell-through panel, because the panel compared the figure to the
    threshold itself. It now shows the knob's own verdict."""
    b = population.build(tmp_path, knobs)
    assert b.baseline.long_enough is False
    payload, reason = population.panel_sell_through(b)
    assert reason is None
    assert all(r["passed"] is None for r in payload["rows"])

    out = population_html.render(b)
    assert "fallback: the two newest dates, not comparable" in out
    assert ">not comparable</span>" in out
