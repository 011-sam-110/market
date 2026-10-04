"""The forecaster: one model call per market, at most HARD_DAILY_CALLS a day.

For each picked market the agent:

1. reads the market price (the first outcome's mid price) and the book edge,
2. collects Reddit hits and news headlines,
3. reserves a call slot in the ledger, then makes one DeepSeek call,
4. turns the answer into a paper stake at quarter-Kelly, capped at 6% of the
   free paper capital,
5. writes one forecast row.

The paper stake buys the side the agent thinks is underpriced, at the price a
buyer would pay (the best ask for the first outcome, one minus the best bid
for the second). Kelly runs on that fill price, so an edge smaller than the
spread gives no stake. Fees are not modelled.

No order is placed. There is no wallet and no Polymarket key in this package.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from forecast import HARD_DAILY_CALLS, KELLY_MULTIPLIER, STAKE_CAP
from forecast import evidence as ev
from forecast import gamma
from forecast.ledger import CallBudgetExhausted, Ledger, iso
from forecast.model import (PROMPT_VERSION, AnswerError, DeepSeek, ModelError,
                            build_prompt, parse_answer)


@dataclass(frozen=True)
class Stake:
    side: str  # "a", "b" or "none"
    fill_price: float | None
    fill_basis: str  # "book" or "mid" or "none"
    kelly_full: float
    fraction: float
    amount: float


def kelly_full(probability: float, price: float) -> float:
    """Full-Kelly fraction for a contract that pays 1, bought at `price`."""
    if not (0.0 < price < 1.0):
        return 0.0
    return (probability - price) / (1.0 - price)


def paper_stake(probability: float, market: gamma.Market, free_capital: float, *,
                multiplier: float = KELLY_MULTIPLIER, cap: float = STAKE_CAP) -> Stake:
    """Quarter-Kelly on the side the agent favours, capped at `cap` of free capital."""
    mid = market.price
    if probability > mid:
        side = "a"
        book = market.best_ask
        fill, basis = (book, "book") if book is not None and 0.0 < book < 1.0 else (mid, "mid")
        p_side = probability
    elif probability < mid:
        side = "b"
        book = market.best_bid
        fill, basis = ((1.0 - book), "book") if book is not None and 0.0 < book < 1.0 \
            else (1.0 - mid, "mid")
        p_side = 1.0 - probability
    else:
        return Stake("none", None, "none", 0.0, 0.0, 0.0)
    full = kelly_full(p_side, fill)
    if full <= 0.0 or free_capital <= 0.0:
        return Stake("none", fill, basis, full, 0.0, 0.0)
    fraction = min(multiplier * full, cap)
    amount = round(fraction * free_capital, 2)
    return Stake(side, fill, basis, full, fraction, amount)


@dataclass
class RunResult:
    written: int = 0
    calls_before: int = 0
    calls_after: int = 0
    cap: int = HARD_DAILY_CALLS
    failures: Counter = field(default_factory=Counter)
    selection_lines: list[str] = field(default_factory=list)
    examples: list[dict] = field(default_factory=list)
    stopped: str = ""

    def lines(self) -> list[str]:
        out = list(self.selection_lines)
        out.append(f"model calls today: {self.calls_after} of {self.cap} "
                   f"({self.calls_after - self.calls_before} this run)")
        out.append(f"paper forecasts written this run: {self.written}")
        for reason, n in sorted(self.failures.items()):
            out.append(f"  no forecast ({reason}): {n}")
        if self.stopped:
            out.append(f"stopped: {self.stopped}")
        return out


@dataclass
class Sources:
    prospector_bin: Path | None = ev.DEFAULT_PROSPECTOR_BIN
    prospector_db: Path | None = None
    news_db: Path | None = ev.DEFAULT_NEWS_DB


def gather(market: gamma.Market, sources: Sources, now: datetime,
           run: Callable | None = None) -> tuple[list[dict], str, str]:
    """Evidence items with prompt ids (R1.., N1..), and each source's status."""
    reddit = ev.reddit_evidence(
        market.question, binary=sources.prospector_bin, db=sources.prospector_db,
        **({"run": run} if run is not None else {}))
    news = ev.news_evidence(market.question, db=sources.news_db, now=now,
                            extra=[market.event_title])
    items: list[dict] = []
    for i, item in enumerate(reddit.items, 1):
        items.append({**item, "id": f"R{i}"})
    for i, item in enumerate(news.items, 1):
        items.append({**item, "id": f"N{i}"})
    return items, reddit.status, news.status


def forecast_markets(ledger: Ledger, client: DeepSeek, picked: list[gamma.Market],
                     sources: Sources, now: datetime, *, cap: int = HARD_DAILY_CALLS,
                     run: Callable | None = None, clock: Callable[[], datetime] | None = None,
                     log: Callable[[str], None] = print) -> RunResult:
    clock = clock or (lambda: now)
    cap = max(0, min(cap, HARD_DAILY_CALLS))
    result = RunResult(cap=cap)
    result.calls_before = ledger.calls_on(ledger.day_of(now))
    for market in picked:
        if ledger.calls_on(ledger.day_of(clock())) >= cap:
            result.stopped = f"{cap} of {cap} model calls used today (UTC)"
            break
        items, reddit_status, news_status = gather(market, sources, now, run)
        system, user = build_prompt(market, items, now)
        try:
            slot = ledger.reserve_call(clock(), market.id, client.model, cap=cap)
        except CallBudgetExhausted as exc:
            result.stopped = str(exc)
            break
        try:
            completion = client.complete(system, user)
        except ModelError as exc:
            ledger.finish_call(slot.call_id, f"error: {exc}")
            result.failures[f"model error: {str(exc)[:60]}"] += 1
            log(f"  {market.id}: model error ({exc})")
            continue
        try:
            probability, reasoning, used = parse_answer(completion.content)
        except AnswerError as exc:
            ledger.finish_call(slot.call_id, f"bad answer: {exc}", model=completion.model,
                               prompt_tokens=completion.prompt_tokens,
                               completion_tokens=completion.completion_tokens)
            result.failures[f"bad answer: {exc}"] += 1
            log(f"  {market.id}: bad answer ({exc})")
            continue
        ledger.finish_call(slot.call_id, "ok", model=completion.model,
                           prompt_tokens=completion.prompt_tokens,
                           completion_tokens=completion.completion_tokens)
        used_set = set(used)
        evidence_rows = [{**item, "used": item["id"] in used_set} for item in items]
        free = ledger.free_capital()
        stake = paper_stake(probability, market, free)
        row = {
            "market_id": market.id,
            "event_id": market.event_id,
            "event_title": market.event_title,
            "question": market.question,
            "outcome_a": market.outcomes[0],
            "outcome_b": market.outcomes[1],
            "market_url": market.url,
            "end_date": iso(market.end_date) if market.end_date else None,
            "liquidity": market.liquidity,
            "volume": market.volume,
            "is_sports": int(market.is_sports),
            "market_price": market.price,
            "best_bid": market.best_bid,
            "best_ask": market.best_ask,
            "price_read_at": iso(now),
            "probability": probability,
            "reasoning": reasoning,
            "evidence_json": evidence_rows,
            "reddit_status": reddit_status,
            "news_status": news_status,
            "side": stake.side,
            "fill_price": stake.fill_price,
            "fill_basis": stake.fill_basis,
            "kelly_full": stake.kelly_full,
            "stake_fraction": stake.fraction,
            "stake": stake.amount,
            "free_capital": free,
            "model": completion.model,
            "prompt_version": PROMPT_VERSION,
            "call_id": slot.call_id,
            "created_at": iso(clock()),
        }
        ledger.record_forecast(row)
        result.written += 1
        if len(result.examples) < 3:
            result.examples.append(row)
        log(f"  {market.id}: p={probability:.3f} vs price {market.price:.3f}, "
            f"stake {stake.amount:.2f} on {stake.side} "
            f"({len(evidence_rows)} evidence items)")
    result.calls_after = ledger.calls_on(ledger.day_of(now))
    return result


def pick_markets(ledger: Ledger, get_json: gamma.GetJSON, now: datetime, *, limit: int,
                 liquidity_floor: float = gamma.LIQUIDITY_FLOOR) -> gamma.Selection:
    rows = gamma.fetch_open(get_json, now, liquidity_floor=liquidity_floor)
    return gamma.select(rows, now, limit=limit, known_markets=ledger.known_markets(),
                        known_events=ledger.known_events(),
                        liquidity_floor=liquidity_floor)
