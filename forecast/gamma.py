"""Read open markets from Polymarket's public Gamma API, and pick the ones to forecast.

Read only. The Gamma API needs no key and no wallet, and this module sends
nothing but GET requests to it. There is no order code anywhere in this
package: GB is close-only on Polymarket, and the forecaster is paper only.

The selection rules, in the order `select()` applies them:

1. Two outcomes only. The forecast is one probability for the FIRST outcome.
2. No negRisk market. A negRisk market is one leg of a multi-outcome event,
   so its price is not a free two-outcome price.
3. Liquidity at or above a floor (default $10,000), so the market price is a
   real benchmark and not a thin book.
4. Price of the first outcome inside [0.05, 0.95]. A market at 0.99 scores
   the same for everybody and teaches nothing.
5. End date between 24 hours and 30 days ahead. Sooner is a market already
   in play. Later makes the record slow to reach its n.
6. No sub-daily series (hourly, 4h, 15m, 5m). Those are price-tick coin
   flips, and they would fill the record with noise.
7. One market per event, and never a market or an event the ledger already
   holds. Twenty spreads on one match would be one forecast twenty times.
8. Sports and other markets alternate, each ordered by liquidity, so the
   record does not become a tennis record.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

GAMMA_ROOT = "https://gamma-api.polymarket.com"
USER_AGENT = "market-paper-forecaster/1 (read-only research; no orders)"
PAGE_SIZE = 100
# Gamma refuses an offset above 2,000 with a 422 ("use /markets/keyset").
MAX_OFFSET = 2000
TIMEOUT_S = 60

LIQUIDITY_FLOOR = 10_000.0
PRICE_LOW = 0.05
PRICE_HIGH = 0.95
MIN_HORIZON = timedelta(hours=24)
MAX_HORIZON = timedelta(days=30)
SUB_DAILY = frozenset({"hourly", "4h", "15m", "5m", "1m", "30m", "minutely"})

GetJSON = Callable[[str], object]


class GammaError(RuntimeError):
    """Gamma did not answer with JSON. The caller records it and goes on."""


def urllib_get_json(url: str) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise GammaError(f"HTTP {exc.code} for {url}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise GammaError(f"{type(exc).__name__} for {url}") from exc


@dataclass(frozen=True)
class Market:
    id: str
    question: str
    description: str
    outcomes: tuple[str, str]
    price: float  # the first outcome's mid price, 0..1
    best_bid: float | None  # for the first outcome
    best_ask: float | None
    liquidity: float
    volume: float
    end_date: datetime | None
    event_id: str | None
    event_title: str
    series_recurrence: str | None
    sports_type: str | None
    slug: str
    neg_risk: bool
    closed: bool
    resolution_status: str | None
    final_prices: tuple[float, float] | None  # set only when closed

    @property
    def url(self) -> str:
        return f"https://polymarket.com/market/{self.slug}" if self.slug else ""

    @property
    def is_sports(self) -> bool:
        return bool(self.sports_type)


def _float(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number


def _json_list(value: object) -> list | None:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return None
        return parsed if isinstance(parsed, list) else None
    return None


def _when(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_market(raw: object) -> tuple[Market | None, str]:
    """One Gamma market, or None and the reason it cannot be read as two outcomes."""
    if not isinstance(raw, dict):
        return None, "not an object"
    outcomes = _json_list(raw.get("outcomes"))
    prices = _json_list(raw.get("outcomePrices"))
    if not outcomes or len(outcomes) != 2:
        return None, "not two outcomes"
    if not prices or len(prices) != 2:
        return None, "no two prices"
    parsed_prices = [_float(p) for p in prices]
    if parsed_prices[0] is None or parsed_prices[1] is None:
        return None, "prices unreadable"
    events = raw.get("events") if isinstance(raw.get("events"), list) else []
    event = events[0] if events and isinstance(events[0], dict) else {}
    series = event.get("series") if isinstance(event.get("series"), list) else []
    first_series = series[0] if series and isinstance(series[0], dict) else {}
    closed = bool(raw.get("closed"))
    market = Market(
        id=str(raw.get("id", "")),
        question=str(raw.get("question") or ""),
        description=str(raw.get("description") or ""),
        outcomes=(str(outcomes[0]), str(outcomes[1])),
        price=float(parsed_prices[0]),
        best_bid=_float(raw.get("bestBid")),
        best_ask=_float(raw.get("bestAsk")),
        liquidity=_float(raw.get("liquidityNum")) or _float(raw.get("liquidity")) or 0.0,
        volume=_float(raw.get("volumeNum")) or _float(raw.get("volume")) or 0.0,
        end_date=_when(raw.get("endDate")),
        event_id=str(event["id"]) if event.get("id") is not None else None,
        event_title=str(event.get("title") or ""),
        series_recurrence=(str(first_series["recurrence"]).lower()
                           if first_series.get("recurrence") else None),
        sports_type=str(raw["sportsMarketType"]) if raw.get("sportsMarketType") else None,
        slug=str(raw.get("slug") or ""),
        neg_risk=bool(raw.get("negRisk")),
        closed=closed,
        resolution_status=(str(raw["umaResolutionStatus"])
                           if raw.get("umaResolutionStatus") else None),
        final_prices=((float(parsed_prices[0]), float(parsed_prices[1]))
                      if closed else None),
    )
    if not market.id:
        return None, "no id"
    return market, ""


def exclusion(market: Market, now: datetime, *, liquidity_floor: float = LIQUIDITY_FLOOR
              ) -> str | None:
    """Why this market is not a candidate, or None when it is one."""
    if market.closed:
        return "closed"
    if market.neg_risk:
        return "negRisk"
    if market.liquidity < liquidity_floor:
        return "liquidity below floor"
    if not (PRICE_LOW <= market.price <= PRICE_HIGH):
        return "price outside 0.05-0.95"
    if market.end_date is None:
        return "no end date"
    if market.end_date < now + MIN_HORIZON:
        return "ends within 24h"
    if market.end_date > now + MAX_HORIZON:
        return "ends after 30 days"
    if market.series_recurrence in SUB_DAILY:
        return "sub-daily series"
    return None


@dataclass
class Selection:
    picked: list[Market]
    read: int  # rows Gamma returned
    unreadable: Counter  # reason -> count, rows that are not two-outcome markets
    excluded: Counter  # reason -> count, two-outcome markets that failed a rule
    eligible: int  # two-outcome markets that passed every rule

    def lines(self) -> list[str]:
        out = [f"gamma: read {self.read} open markets"]
        for reason, n in sorted(self.unreadable.items()):
            out.append(f"  not usable ({reason}): {n}")
        for reason, n in sorted(self.excluded.items()):
            out.append(f"  excluded ({reason}): {n}")
        out.append(f"  eligible: {self.eligible}")
        out.append(f"  picked: {len(self.picked)}")
        return out


def select(raw_markets: Iterable[object], now: datetime, *, limit: int,
           known_markets: set[str], known_events: set[str],
           liquidity_floor: float = LIQUIDITY_FLOOR) -> Selection:
    """Apply the rules in the module docstring. Every drop is counted by reason."""
    unreadable: Counter = Counter()
    excluded: Counter = Counter()
    eligible: list[Market] = []
    read = 0
    seen_ids: set[str] = set()
    for raw in raw_markets:
        read += 1
        market, why = parse_market(raw)
        if market is None:
            unreadable[why] += 1
            continue
        if market.id in seen_ids:
            excluded["duplicate row"] += 1
            continue
        seen_ids.add(market.id)
        reason = exclusion(market, now, liquidity_floor=liquidity_floor)
        if reason is None and market.id in known_markets:
            reason = "already forecast"
        if reason is None and market.event_id and market.event_id in known_events:
            reason = "event already forecast"
        if reason:
            excluded[reason] += 1
            continue
        eligible.append(market)

    # One per event: the most liquid market of each event stands for it.
    eligible.sort(key=lambda m: (-m.liquidity, m.id))
    per_event: list[Market] = []
    events_taken: set[str] = set()
    for market in eligible:
        key = market.event_id or f"market:{market.id}"
        if key in events_taken:
            excluded["another market of the same event"] += 1
            continue
        events_taken.add(key)
        per_event.append(market)

    sports = [m for m in per_event if m.is_sports]
    other = [m for m in per_event if not m.is_sports]
    picked: list[Market] = []
    while len(picked) < limit and (sports or other):
        for queue in (other, sports):
            if queue and len(picked) < limit:
                picked.append(queue.pop(0))
    return Selection(picked=picked, read=read, unreadable=unreadable,
                     excluded=excluded, eligible=len(per_event))


def open_markets_url(now: datetime, offset: int, *,
                     liquidity_floor: float = LIQUIDITY_FLOOR) -> str:
    params = {
        "closed": "false",
        "active": "true",
        "limit": str(PAGE_SIZE),
        "offset": str(offset),
        "liquidity_num_min": f"{liquidity_floor:.0f}",
        "end_date_min": (now + MIN_HORIZON).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_date_max": (now + MAX_HORIZON).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "order": "liquidityNum",
        "ascending": "false",
    }
    return f"{GAMMA_ROOT}/markets?{urllib.parse.urlencode(params)}"


def fetch_open(get_json: GetJSON, now: datetime, *,
               liquidity_floor: float = LIQUIDITY_FLOOR,
               pause_s: float = 0.2, sleep: Callable[[float], None] = time.sleep
               ) -> list[object]:
    """Every page Gamma will serve for the window, up to its offset limit.

    The server-side filters only narrow the download. `select()` applies the
    same rules again, so a filter Gamma ignores cannot let a market through.
    """
    rows: list[object] = []
    offset = 0
    while offset <= MAX_OFFSET:
        page = get_json(open_markets_url(now, offset, liquidity_floor=liquidity_floor))
        if not isinstance(page, list):
            raise GammaError(f"page at offset {offset} is not a list")
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        sleep(pause_s)
    return rows


def fetch_market(get_json: GetJSON, market_id: str) -> Market | None:
    """The current state of one market, for the resolution check."""
    raw = get_json(f"{GAMMA_ROOT}/markets/{urllib.parse.quote(market_id)}")
    market, _why = parse_market(raw)
    return market
