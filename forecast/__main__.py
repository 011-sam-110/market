"""Command line for the paper forecaster.

    python -m forecast daily      settle, forecast (at most 20 calls), report
    python -m forecast score      settle resolved markets, then report
    python -m forecast report     report only (no network)
    python -m forecast markets    show what today's pick would be (no model call)

Common flags: --db (default ~/.forecast/ledger.sqlite3, or FORECAST_DB).
`daily` exits 2 when there is no DeepSeek key, after it settles and reports.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from forecast import HARD_DAILY_CALLS
from forecast import agent, gamma, model, score
from forecast import evidence as ev
from forecast.ledger import Ledger, utc_now


def _sources(args: argparse.Namespace) -> agent.Sources:
    return agent.Sources(
        prospector_bin=Path(args.prospector_bin).expanduser() if args.prospector_bin else None,
        prospector_db=ev.find_prospector_db(args.prospector_db),
        news_db=Path(args.news_db).expanduser() if args.news_db else None,
    )


def _settle_and_report(ledger: Ledger, *, check: bool) -> None:
    now = utc_now()
    if check:
        counts = score.check_resolutions(ledger, gamma.urllib_get_json, now,
                                         sleep=time.sleep)
        done = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        print(f"resolution check: {done or 'no open forecasts'}")
    for line in score.report_lines(score.build_report(ledger, now)):
        print(line)


def _forecast(ledger: Ledger, args: argparse.Namespace) -> int:
    now = utc_now()
    cap = max(0, min(args.max_calls, HARD_DAILY_CALLS))
    left = max(0, cap - ledger.calls_on(ledger.day_of(now)))
    print(f"model calls left today: {left} of {cap} (hard cap {HARD_DAILY_CALLS})")
    if left == 0:
        return 0
    key = model.load_key(key_file=args.key_file)
    if not key:
        print("no DeepSeek key: set DEEPSEEK_API_KEY or pass --key-file. "
              "No forecast was made.")
        return 2
    try:
        selection = agent.pick_markets(ledger, gamma.urllib_get_json, now, limit=left,
                                       liquidity_floor=args.liquidity_floor)
    except gamma.GammaError as exc:
        print(f"gamma: no market list ({exc}). No forecast was made.")
        return 1
    client = model.DeepSeek(key, model=args.model)
    result = agent.forecast_markets(ledger, client, selection.picked, _sources(args), now,
                                    cap=cap, clock=utc_now)
    result.selection_lines = selection.lines()
    for line in result.lines():
        print(line)
    return 0


def _markets(ledger: Ledger, args: argparse.Namespace) -> int:
    now = utc_now()
    selection = agent.pick_markets(ledger, gamma.urllib_get_json, now,
                                   limit=args.max_calls, liquidity_floor=args.liquidity_floor)
    for line in selection.lines():
        print(line)
    sources = _sources(args)
    for market in selection.picked:
        items, reddit_status, news_status = agent.gather(market, sources, now)
        print(f"- {market.id} [{'sports' if market.is_sports else 'other'}] "
              f"{market.question} | {market.outcomes[0]} at {market.price:.3f} | "
              f"liquidity {market.liquidity:,.0f} | ends {market.end_date:%Y-%m-%d}")
        print(f"    {reddit_status}; {news_status}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m forecast", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["daily", "score", "report", "markets"])
    parser.add_argument("--db", default=None, help="ledger path (outside the repo)")
    parser.add_argument("--max-calls", type=int,
                        default=int(os.environ.get("FORECAST_MAX_CALLS", HARD_DAILY_CALLS)),
                        help=f"model calls per UTC day, clamped to {HARD_DAILY_CALLS}")
    parser.add_argument("--liquidity-floor", type=float, default=gamma.LIQUIDITY_FLOOR)
    parser.add_argument("--model", default=os.environ.get("FORECAST_MODEL",
                                                          model.DEFAULT_MODEL))
    parser.add_argument("--key-file", default=os.environ.get("FORECAST_KEY_FILE"),
                        help="KEY=VALUE file; only its DEEPSEEK_API_KEY line is read")
    parser.add_argument("--prospector-bin", default=os.environ.get(
        "FORECAST_PROSPECTOR_BIN", str(ev.DEFAULT_PROSPECTOR_BIN)))
    parser.add_argument("--prospector-db", default=os.environ.get("FORECAST_PROSPECTOR_DB"))
    parser.add_argument("--news-db", default=os.environ.get(
        "FORECAST_NEWS_DB", str(ev.DEFAULT_NEWS_DB)))
    parser.add_argument("--no-check", action="store_true",
                        help="score: skip the Gamma resolution check")
    args = parser.parse_args(argv)

    with Ledger(args.db) as ledger:
        print(f"ledger: {ledger.path}")
        if args.command == "report":
            _settle_and_report(ledger, check=False)
            return 0
        if args.command == "score":
            _settle_and_report(ledger, check=not args.no_check)
            return 0
        if args.command == "markets":
            return _markets(ledger, args)
        # daily: settle first, so the free capital includes yesterday's results.
        run_id = ledger.start_run("daily", utc_now())
        counts = score.check_resolutions(ledger, gamma.urllib_get_json, utc_now(),
                                         sleep=time.sleep)
        print("resolution check: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
                                      or "no open forecasts"))
        code = _forecast(ledger, args)
        report = score.build_report(ledger, utc_now())
        for line in score.report_lines(report):
            print(line)
        ledger.finish_run(run_id, {"exit": code, "counts": report["counts"],
                                   "resolution_check": dict(counts)})
        return code


if __name__ == "__main__":
    sys.exit(main())
