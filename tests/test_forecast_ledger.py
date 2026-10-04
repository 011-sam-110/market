"""The ledger: outside the repo, and a call cap that survives a restart."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from forecast import HARD_DAILY_CALLS, PAPER_CAPITAL
from forecast.ledger import (REPO_ROOT, CallBudgetExhausted, Ledger, LedgerPathError,
                             resolve_path)
from tests.forecast_fixtures import NOW


def test_hard_cap_is_twenty():
    assert HARD_DAILY_CALLS == 20


def test_a_path_inside_the_repo_is_refused():
    with pytest.raises(LedgerPathError):
        resolve_path(REPO_ROOT / "ledger.sqlite3")
    with pytest.raises(LedgerPathError):
        resolve_path(REPO_ROOT / "dashboard" / "x.sqlite3")


def test_cap_holds_and_a_bigger_request_is_clamped(tmp_path):
    with Ledger(tmp_path / "l.sqlite3") as ledger:
        for i in range(HARD_DAILY_CALLS):
            slot = ledger.reserve_call(NOW, f"m{i}", "deepseek-flash", cap=500)
            assert slot.cap == HARD_DAILY_CALLS
        with pytest.raises(CallBudgetExhausted):
            ledger.reserve_call(NOW, "m-extra", "deepseek-flash", cap=500)
        assert ledger.calls_on(ledger.day_of(NOW)) == HARD_DAILY_CALLS


def test_the_count_persists_across_a_restart(tmp_path):
    path = tmp_path / "l.sqlite3"
    with Ledger(path) as ledger:
        for i in range(15):
            ledger.reserve_call(NOW, f"m{i}", "deepseek-flash")
    with Ledger(path) as ledger:
        for i in range(5):
            ledger.reserve_call(NOW, f"n{i}", "deepseek-flash")
        with pytest.raises(CallBudgetExhausted):
            ledger.reserve_call(NOW, "n-extra", "deepseek-flash")


def test_a_lower_cap_is_honoured(tmp_path):
    with Ledger(tmp_path / "l.sqlite3") as ledger:
        ledger.reserve_call(NOW, "a", "m", cap=2)
        ledger.reserve_call(NOW, "b", "m", cap=2)
        with pytest.raises(CallBudgetExhausted):
            ledger.reserve_call(NOW, "c", "m", cap=2)


def test_the_count_starts_again_on_the_next_utc_day(tmp_path):
    with Ledger(tmp_path / "l.sqlite3") as ledger:
        for i in range(HARD_DAILY_CALLS):
            ledger.reserve_call(NOW, f"m{i}", "m")
        slot = ledger.reserve_call(NOW + timedelta(days=1), "next", "m")
        assert slot.used_after == 1


def test_the_schema_trigger_refuses_a_raw_insert_past_the_cap(tmp_path):
    """Code that skips reserve_call() still cannot pass the cap."""
    with Ledger(tmp_path / "l.sqlite3") as ledger:
        for i in range(HARD_DAILY_CALLS):
            ledger.conn.execute(
                "INSERT INTO model_calls(day, reserved_at) VALUES ('2026-10-04', 'x')")
        with pytest.raises(sqlite3.IntegrityError, match="cap"):
            ledger.conn.execute(
                "INSERT INTO model_calls(day, reserved_at) VALUES ('2026-10-04', 'x')")


def test_free_capital_counts_open_stakes_and_settled_pnl(tmp_path):
    with Ledger(tmp_path / "l.sqlite3") as ledger:
        assert ledger.free_capital() == PAPER_CAPITAL
        base = {"question": "q", "outcome_a": "Yes", "outcome_b": "No",
                "market_price": 0.4, "price_read_at": "t", "probability": 0.5,
                "reddit_status": "r", "news_status": "n", "side": "a",
                "kelly_full": 0.1, "stake_fraction": 0.025, "free_capital": 1000.0,
                "model": "m", "prompt_version": "v", "created_at": "t"}
        first = ledger.record_forecast({**base, "market_id": "1", "stake": 30.0})
        ledger.record_forecast({**base, "market_id": "2", "stake": 20.0})
        assert ledger.free_capital() == pytest.approx(PAPER_CAPITAL - 50.0)
        ledger.settle(first, status="resolved", final_price_a=1.0, pnl=45.0, settled_at=NOW)
        assert ledger.free_capital() == pytest.approx(PAPER_CAPITAL + 45.0 - 20.0)
