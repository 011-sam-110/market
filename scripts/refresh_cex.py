"""Build the CeX price table for the handsets we are actually seeing.

CeX sits behind a Cloudflare rule that refuses scripted clients outright --
plain requests and Chrome-TLS impersonation (curl_cffi) both return 403 on the
whole webuy.io domain, and their Algolia key is not valid against Algolia's
public hosts. A real browser is the only way in, so this drives Playwright.

And it has to be a *visible* one. Measured on 2026-08-27, all three tried:

    headless chromium      403, Cloudflare interstitial
    headless real Chrome   403, Cloudflare interstitial
    headed real Chrome     200, prices render

So this script opens a window. That is a genuine operational constraint rather
than a setting worth tuning: scheduling it unattended on a headless box will
silently return zero boxes for every query, which reads exactly like "CeX has no
stock". --headless is offered only to re-test that claim, and warns when used.

That cost is fine because the shape of the need is daily, not per-listing. CeX
prices move slowly; the loop wants a cached table, joined by model and storage.

Queries are derived from the observation store rather than hardcoded, so the
table covers what the collector is really finding instead of what I guessed it
would find.

    python scripts/refresh_cex.py               # from the store, default cap
    python scripts/refresh_cex.py --query "iphone 13 256gb"
    python scripts/refresh_cex.py --limit 40
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phoneloop import config  # noqa: E402
from phoneloop.cex import best_cash, parse_sell_page  # noqa: E402
from phoneloop.parser import parse  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SELL_URL = "https://uk.webuy.com/sell/search?stext={}"
GRADES = ("A", "B", "C")
PAUSE_S = 3.0          # between queries; CeX is not the one under study here
NAV_TIMEOUT_MS = 45000


def queries_from_store(cfg, limit: int) -> list[tuple[str, int | None]]:
    """Distinct (model, storage) pairs the collector has actually observed.

    Ordered by how often each appears, so a capped run spends its budget on the
    handsets that matter rather than the long tail.
    """
    import duckdb

    files = glob.glob(str(cfg.DATA_DIR / "observations" / "**" / "*.parquet"),
                      recursive=True)
    if not files:
        return []

    con = duckdb.connect()
    rows = con.execute(f"SELECT title FROM read_parquet({files!r})").fetchall()

    counts: Counter[tuple[str, int | None]] = Counter()
    for (title,) in rows:
        spec = parse(title or "")
        if not spec.model or spec.disqualifiers:
            continue
        counts[(spec.model, spec.storage_gb)] += 1

    return [pair for pair, _ in counts.most_common(limit)]


def query_text(model: str, storage_gb: int | None) -> str:
    """Turn a parser slug into something CeX search understands.

    The parser emits `iphone-14-pro-max`; CeX wants `iphone 14 pro max`. Leaving
    the hyphens in returns zero boxes for every query -- silently, since "no
    stock" and "wrong query" look identical in the results.
    """
    words = model.replace("-", " ").strip()
    return f"{words} {storage_gb}gb" if storage_gb else words


def fetch(page, text: str) -> list:
    page.goto(SELL_URL.format(text.replace(" ", "%20")),
              timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
    # Prices arrive from a client-side search call, so waiting on the network
    # settling is what makes this deterministic rather than a race.
    try:
        page.wait_for_selector("text=CASH", timeout=20000)
    except Exception:                            # noqa: BLE001
        # No prices rendered. Distinguish the two reasons, because a
        # Cloudflare block that reads as "no stock" is how a price table
        # ends up quietly empty.
        body = page.inner_text("body")
        if "security verification" in body.lower() or "Ray ID" in body:
            raise RuntimeError("blocked by Cloudflare - needs a headed browser")
        return []
    return parse_sell_page(page.inner_text("body"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--query", action="append", help="explicit query, repeatable")
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--headless", action="store_true",
                    help="re-test the Cloudflare block; returns 0 boxes as of 2026-08-27")
    args = ap.parse_args()

    cfg = config.load()
    out = args.out or (cfg.DATA_DIR / "cex_prices.json")

    if args.query:
        pairs = [(q, None) for q in args.query]
        texts = [q for q in args.query]
    else:
        pairs = queries_from_store(cfg, args.limit)
        texts = [query_text(m, s) for m, s in pairs]
        if not pairs:
            print("no observations yet - run 'python -m phoneloop collect' first")
            return 1

    print(f"refreshing CeX prices for {len(texts)} queries -> {out}")

    from playwright.sync_api import sync_playwright

    table: dict[str, dict] = {}
    with sync_playwright() as pw:
        if args.headless:
            print("warning: headless is blocked by Cloudflare - expect 0 boxes")
        # channel="chrome" is the real installed browser, not the bundled
        # Chromium build; the interstitial tells them apart.
        browser = pw.chromium.launch(headless=args.headless, channel="chrome")
        page = browser.new_page(locale="en-GB")
        for (pair, text) in zip(pairs, texts):
            try:
                boxes = fetch(page, text)
            except Exception as exc:              # noqa: BLE001
                print(f"  {text:<34} FAILED {type(exc).__name__}: {str(exc)[:50]}")
                continue

            cash = {g: best_cash(boxes, g) for g in GRADES}
            unlocked = [b for b in boxes if b.unlocked]
            table[text] = {
                "model": pair[0] if not args.query else None,
                "storage_gb": pair[1] if not args.query else None,
                "cash_pence": cash,
                "boxes_seen": len(boxes),
                "unlocked_seen": len(unlocked),
                "refreshed": datetime.now(UTC).isoformat(),
            }
            shown = "  ".join(
                f"{g}=£{v/100:.0f}" if v else f"{g}=-" for g, v in cash.items()
            )
            print(f"  {text:<34} {len(boxes):>3} boxes  {shown}")
            time.sleep(PAUSE_S)
        browser.close()

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(table, indent=2), encoding="utf-8")
    priced = sum(1 for v in table.values() if any(v["cash_pence"].values()))
    print(f"\n{priced}/{len(table)} queries returned a usable cash price")
    print(f"written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
