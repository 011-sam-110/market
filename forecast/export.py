"""The dashboard payload for the forecast record.

One file, `forecast.json`, written by scripts/export_forecast.py with the
same writer as the rest of the bundle (phoneloop.dashexport.write_json:
sorted keys, no NaN, atomic). The payload is the scorer's own report plus
the most recent forecasts. Nothing here computes a second version of a
score figure: the dashboard shows what score.build_report() returns.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from forecast.ledger import Ledger
from forecast.score import build_report

PAYLOAD_VERSION = 1
RECENT_LIMIT = 60


def recent_rows(ledger: Ledger, limit: int = RECENT_LIMIT) -> list[dict[str, Any]]:
    rows = list(ledger.conn.execute(
        "SELECT * FROM forecasts ORDER BY forecast_id DESC LIMIT ?", (limit,)))
    out = []
    for r in rows:
        try:
            evidence = json.loads(r["evidence_json"] or "[]")
        except ValueError:
            evidence = []
        out.append({
            "market_id": r["market_id"],
            "question": r["question"],
            "outcome_a": r["outcome_a"],
            "outcome_b": r["outcome_b"],
            "market_url": r["market_url"],
            "end_date": r["end_date"],
            "created_at": r["created_at"],
            "market_price": r["market_price"],
            "probability": r["probability"],
            "side": r["side"],
            "stake": r["stake"],
            "status": r["status"],
            "final_price_a": r["final_price_a"],
            "pnl": r["pnl"],
            "is_sports": bool(r["is_sports"]),
            "reddit_status": r["reddit_status"],
            "news_status": r["news_status"],
            "evidence": [{"kind": e.get("kind"), "url": e.get("url"),
                          "title": e.get("title"), "used": bool(e.get("used"))}
                         for e in evidence if isinstance(e, dict)],
        })
    return out


def build_payload(ledger: Ledger, now: datetime, *, git_sha: str = "unknown"
                  ) -> dict[str, Any]:
    n_total = int(ledger.conn.execute("SELECT count(*) FROM forecasts").fetchone()[0])
    recent = recent_rows(ledger)
    return {
        "payload_version": PAYLOAD_VERSION,
        "git_sha": git_sha,
        "report": build_report(ledger, now),
        "recent": recent,
        # Nothing is capped silently: the payload names the limit and the drop.
        "recent_limit": RECENT_LIMIT,
        "recent_dropped": max(0, n_total - len(recent)),
    }
