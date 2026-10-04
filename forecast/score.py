"""Settle resolved markets, and score the agent against the market price.

The score is the Brier score: the mean of (forecast - outcome) squared, over
resolved two-outcome markets. Lower is better. The agent's forecast is its
probability. The market's forecast is the price it showed when the agent
made its forecast. The record therefore asks one question: did the agent
beat the price it could see on the day?

This module follows the market record's rules:

- every figure prints the count it rests on;
- below WITHHOLD_BELOW resolved forecasts, every comparison figure is
  stamped WITHHELD. Its measured value still prints, marked "measured, not
  compared", because the record never hides a measurement;
- a market that resolves 50/50 is "void". It settles its paper stake and it
  stays out of the Brier score, because a 0.5 outcome is not a yes or a no.

Paper P&L buys at the fill price the agent recorded and pays 1, 0 or 0.5 per
share. It ignores fees.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import datetime
from typing import Any

from forecast import HARD_DAILY_CALLS, WITHHOLD_BELOW
from forecast import gamma
from forecast.ledger import Ledger

UNSETTLED_STATUSES = frozenset({"proposed", "disputed", "challenged"})


def settlement(market: gamma.Market) -> tuple[str, float] | None:
    """("resolved", 1.0 or 0.0) or ("void", 0.5), or None while still open."""
    if not market.closed or market.final_prices is None:
        return None
    if market.resolution_status and market.resolution_status.lower() in UNSETTLED_STATUSES:
        return None
    a, b = market.final_prices
    if abs(a + b - 1.0) > 1e-9:
        return None
    if a in (0.0, 1.0):
        return "resolved", a
    if a == 0.5:
        return "void", 0.5
    return None


def paper_pnl(side: str, stake: float, fill_price: float | None, final_price_a: float
              ) -> float:
    if side not in ("a", "b") or not stake or not fill_price:
        return 0.0
    payout = final_price_a if side == "a" else 1.0 - final_price_a
    shares = stake / fill_price
    return round(shares * payout - stake, 2)


def check_resolutions(ledger: Ledger, get_json: gamma.GetJSON, now: datetime, *,
                      sleep: Callable[[float], None] | None = None,
                      pause_s: float = 0.2) -> Counter:
    """Ask Gamma about every open forecast once. Returns counts by result."""
    counts: Counter = Counter()
    for row in ledger.open_forecasts():
        counts["checked"] += 1
        try:
            market = gamma.fetch_market(get_json, row["market_id"])
        except gamma.GammaError:
            counts["fetch failed"] += 1
            continue
        if market is None:
            counts["unreadable"] += 1
            continue
        settled = settlement(market)
        if settled is None:
            counts["still open"] += 1
        else:
            status, final_a = settled
            pnl = paper_pnl(row["side"], row["stake"], row["fill_price"], final_a)
            ledger.settle(row["forecast_id"], status=status, final_price_a=final_a,
                          pnl=pnl, settled_at=now)
            counts[status] += 1
        if sleep is not None:
            sleep(pause_s)
    return counts


def _figure(value: float | None, n: int, withheld: bool, note: str = "") -> dict:
    return {"value": value, "n": n, "withheld": withheld or n == 0 or value is None,
            "note": note}


def build_report(ledger: Ledger, now: datetime, *,
                 withhold_below: int = WITHHOLD_BELOW) -> dict[str, Any]:
    rows = ledger.all_forecasts()
    resolved = [r for r in rows if r["status"] == "resolved"]
    void = [r for r in rows if r["status"] == "void"]
    open_rows = [r for r in rows if r["status"] == "open"]
    settled = resolved + void
    n_resolved = len(resolved)
    withheld = n_resolved < withhold_below
    why = (f"{n_resolved} resolved forecasts. The comparison needs {withhold_below}."
           if withheld else "")

    agent_brier = market_brier = gap = None
    if n_resolved:
        agent_brier = sum((r["probability"] - r["final_price_a"]) ** 2
                          for r in resolved) / n_resolved
        market_brier = sum((r["market_price"] - r["final_price_a"]) ** 2
                           for r in resolved) / n_resolved
        gap = market_brier - agent_brier
    agent_closer = sum(1 for r in resolved
                       if abs(r["probability"] - r["final_price_a"])
                       < abs(r["market_price"] - r["final_price_a"]))

    staked_settled = [r for r in settled if r["stake"] > 0]
    pnl_total = round(sum(r["pnl"] or 0.0 for r in staked_settled), 2)
    staked_total = round(sum(r["stake"] for r in staked_settled), 2)
    wins = sum(1 for r in staked_settled if (r["pnl"] or 0) > 0)
    losses = sum(1 for r in staked_settled if (r["pnl"] or 0) < 0)
    flat = len(staked_settled) - wins - losses

    capital = ledger.paper_capital()
    open_stake = round(sum(r["stake"] for r in open_rows), 2)
    today = ledger.day_of(now)

    def has_hits(status: str, prefix: str) -> bool:
        if not status.startswith(prefix):
            return False
        try:
            return int(status[len(prefix):].split()[0]) > 0
        except (ValueError, IndexError):
            return False

    return {
        "generated_at": now,
        "ledger_path": str(ledger.path),
        "withhold_below": withhold_below,
        "counts": {
            "forecasts": len(rows),
            "open": len(open_rows),
            "resolved": n_resolved,
            "void": len(void),
            "staked": sum(1 for r in rows if r["stake"] > 0),
            "no_stake": sum(1 for r in rows if r["stake"] <= 0),
            "sports": sum(1 for r in rows if r["is_sports"]),
            "with_reddit": sum(1 for r in rows if has_hits(r["reddit_status"], "reddit: ")),
            "with_news": sum(1 for r in rows if has_hits(r["news_status"], "news: ")),
            "calls_today": ledger.calls_on(today),
            "calls_cap": HARD_DAILY_CALLS,
            "calls_day": today,
            "calls_total": int(ledger.conn.execute(
                "SELECT count(*) FROM model_calls").fetchone()[0]),
            "calls_ok_total": int(ledger.conn.execute(
                "SELECT count(*) FROM model_calls WHERE outcome = 'ok'").fetchone()[0]),
        },
        "brier": {
            "agent": _figure(agent_brier, n_resolved, withheld, why),
            "market": _figure(market_brier, n_resolved, withheld, why),
            "gap": _figure(gap, n_resolved, withheld,
                           why or "Market Brier minus agent Brier. Above 0 means the "
                                  "agent scored better."),
            "agent_closer": {"value": agent_closer, "n": n_resolved},
        },
        "paper": {
            "capital": capital,
            "open_stake": open_stake,
            "free": round(capital + pnl_total - open_stake, 2),
            "pnl": _figure(pnl_total if staked_settled else None, len(staked_settled),
                           withheld, why),
            "staked_settled": staked_total,
            "wins": wins,
            "losses": losses,
            "flat": flat,
            "fees_modelled": False,
        },
    }


def _reading(name: str, fig: dict, fmt: str, withhold_below: int) -> str:
    n = fig["n"]
    if fig["value"] is None:
        return f"{name:<14} WITHHELD (n={n}; needs {withhold_below} resolved)"
    value = format(fig["value"], fmt)
    if fig["withheld"]:
        return (f"{name:<14} WITHHELD (n={n}; needs {withhold_below} resolved); "
                f"{value} measured, not compared")
    return f"{name:<14} {value} (n={n})"


def report_lines(report: dict[str, Any]) -> list[str]:
    c = report["counts"]
    b = report["brier"]
    p = report["paper"]
    w = report["withhold_below"]
    lines = [
        f"paper forecast record ({report['ledger_path']})",
        f"forecasts: {c['forecasts']} (open {c['open']}, resolved {c['resolved']}, "
        f"void {c['void']})",
        f"  with a paper stake: {c['staked']}, with no stake: {c['no_stake']}",
        f"  sports: {c['sports']}, other: {c['forecasts'] - c['sports']}",
        f"  with Reddit hits: {c['with_reddit']} of {c['forecasts']}, "
        f"with news headlines: {c['with_news']} of {c['forecasts']}",
        f"model calls: {c['calls_today']} of {c['calls_cap']} on {c['calls_day']} (UTC), "
        f"{c['calls_total']} in all, {c['calls_ok_total']} answered",
        f"resolved forecasts: n={c['resolved']}. The comparison needs {w}.",
        _reading("agent Brier", b["agent"], ".4f", w),
        _reading("market Brier", b["market"], ".4f", w),
        _reading("Brier gap", b["gap"], "+.4f", w),
        (f"agent closer:  {b['agent_closer']['value']} of {b['agent_closer']['n']} "
         f"resolved"),
        _reading("paper P&L", p["pnl"], "+.2f", w),
        (f"  settled stakes: {p['pnl']['n']} (won {p['wins']}, lost {p['losses']}, "
         f"flat {p['flat']}), {p['staked_settled']:.2f} staked"),
        (f"paper capital: {p['capital']:.2f} start, {p['open_stake']:.2f} in open "
         f"stakes, {p['free']:.2f} free. Fees are not modelled."),
    ]
    return lines
