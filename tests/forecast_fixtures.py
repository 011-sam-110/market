"""Shared builders for the forecast tests. No network anywhere."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def raw_market(mid: str, *, question: str | None = None, outcomes=("Yes", "No"),
               prices=("0.40", "0.60"), liquidity=50_000.0, end_in=timedelta(days=5),
               neg_risk=False, event_id: str | None = None, recurrence: str | None = "daily",
               sports: str | None = None, closed=False, uma: str | None = None,
               best_bid: float | None = 0.39, best_ask: float | None = 0.41,
               description="Resolves Yes if the thing happens.") -> dict:
    end = NOW + end_in
    return {
        "id": mid,
        "question": question or f"Will thing {mid} happen?",
        "description": description,
        "outcomes": json.dumps(list(outcomes)),
        "outcomePrices": json.dumps(list(prices)),
        "liquidityNum": liquidity,
        "volumeNum": liquidity * 3,
        "endDate": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "negRisk": neg_risk,
        "closed": closed,
        "umaResolutionStatus": uma,
        "bestBid": best_bid,
        "bestAsk": best_ask,
        "slug": f"thing-{mid}",
        "sportsMarketType": sports,
        "events": [{
            "id": event_id or f"ev-{mid}",
            "title": f"Event {event_id or mid}",
            "series": [{"recurrence": recurrence}] if recurrence else [],
        }],
    }
