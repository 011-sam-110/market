"""Reddit text leaves the ledger after 7 days.

Prospector keeps Reddit content for 7 days, and Reddit's Data API terms
require content deleted on Reddit to leave every store. Prospector re-reads
its own store for deletions. The ledger cannot, so it keeps Reddit TEXT no
longer than Prospector keeps it, and then removes it.

What the ledger keeps for a Reddit hit after 7 days: the permalink (`url`),
`subreddit`, `score`, `created_utc`, the prompt id and the `used` flag. What
it removes: `title` and `quote`. News evidence is not touched.

A Reddit entry is stale when EITHER clock passes 7 days:
- the forecast row was written more than 7 days ago, or
- the Reddit post itself (`created_utc`) is more than 7 days old.
An entry whose dates cannot be read counts as stale.

The same rule runs in three places: when the agent writes a row, at the
start of every `forecast daily` (and `forecast prune-evidence`), and in the
dashboard export, which never carries a Reddit quote at all.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone

REDDIT_TEXT_DAYS = 7
REDDIT_TEXT_FIELDS = ("title", "quote")


def _parse_stored(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_created(value: object) -> datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except ValueError:
            return _parse_stored(value.strip())
    return None


def reddit_text_expired(item: dict, stored_at: object, now: datetime) -> bool:
    """True when this entry is a Reddit hit whose text must not be kept."""
    if item.get("kind") != "reddit":
        return False
    cutoff = now - timedelta(days=REDDIT_TEXT_DAYS)
    stored = _parse_stored(stored_at) if not isinstance(stored_at, datetime) else stored_at
    if stored is None or stored <= cutoff:
        return True
    created = _parse_created(item.get("created_utc"))
    if created is not None and created <= cutoff:
        return True
    return False


def strip_reddit_text(items: list, stored_at: object, now: datetime) -> tuple[list, int]:
    """The entries with stale Reddit text removed, and how many entries changed."""
    out: list = []
    changed = 0
    for item in items:
        if isinstance(item, dict) and reddit_text_expired(item, stored_at, now) \
                and any(field in item for field in REDDIT_TEXT_FIELDS):
            out.append({k: v for k, v in item.items() if k not in REDDIT_TEXT_FIELDS})
            changed += 1
        else:
            out.append(item)
    return out, changed


def prune_ledger(ledger, now: datetime) -> Counter:
    """Remove stale Reddit text from every forecast row. Returns counts."""
    counts: Counter = Counter()
    rows = list(ledger.conn.execute(
        "SELECT forecast_id, created_at, evidence_json FROM forecasts"))
    updates: list[tuple[str, int]] = []
    for forecast_id, created_at, evidence_json in rows:
        counts["rows checked"] += 1
        try:
            items = json.loads(evidence_json or "[]")
        except ValueError:
            counts["rows unreadable"] += 1
            continue
        if not isinstance(items, list):
            counts["rows unreadable"] += 1
            continue
        kept, changed = strip_reddit_text(items, created_at, now)
        if changed:
            counts["rows changed"] += 1
            counts["reddit entries stripped"] += changed
            updates.append((json.dumps(kept, ensure_ascii=False, sort_keys=True),
                            forecast_id))
    if updates:
        ledger.replace_evidence(updates)
    return counts


def prune_line(counts: Counter) -> str:
    return (f"reddit text prune ({REDDIT_TEXT_DAYS} days): "
            f"{counts.get('rows checked', 0)} rows checked, "
            f"{counts.get('rows changed', 0)} changed, "
            f"{counts.get('reddit entries stripped', 0)} entries stripped"
            + (f", {counts['rows unreadable']} unreadable"
               if counts.get("rows unreadable") else ""))
