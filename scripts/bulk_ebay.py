"""Enumerate eBay UK at scale, with location pinned server-side.

The earlier eBay work had a hole underneath it. Captures went through a
rotating residential gateway with no country targeting, so consecutive requests
left from Poland, then New York, then Albany. eBay reorders results by shipping
proximity to the requester, so a "UK search" answered from Poland is a
different, thinner, wronger population -- and nothing downstream could see it.
The UK share of a single run's queries swung from 79/88 to 28/84.

The fix is not to stop using the proxy. It is to stop letting the *requester's*
address decide the population: `LH_LocatedIn=1` asks eBay for UK-located items
explicitly, server-side, so egress geography no longer selects the pool.

That parameter was found by reading eBay's own filter links, after guessing
wrong. Worth recording so nobody re-guesses: `LH_PrefLoc=1` is NOT a UK
filter -- measured, it returns 55-63% UK, which is indistinguishable from no
filter at all -- and `LH_PrefLoc=3` is North America (240 of 261 rows
explicitly United States). `LH_LocatedIn=1` returns 97%.

The guard from `phoneloop.ebay` still runs on every page as a check on that claim
-- if the filter ever stops meaning what it means, the run says so instead of
quietly banking a foreign sample.

    python scripts/bulk_ebay.py --category phones
    python scripts/bulk_ebay.py --max-pages 400
    python scripts/bulk_ebay.py --direct          # no proxy; rate-limits fast
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote_plus, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phoneloop import catalog, config, proxies  # noqa: E402
from phoneloop.ebay import parse_rows, uk_share  # noqa: E402
from phoneloop.market_store import MarketStore  # noqa: E402
from phoneloop.parser import parse  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# _ipg=240 is the largest page eBay serves. LH_LocatedIn=1 is "UK only".
# _udlo/_udhi are the price band. _pgn is the page.
SEARCH = ("https://www.ebay.co.uk/sch/i.html?_nkw={q}&_ipg=240&_pgn={page}"
          "&LH_LocatedIn=1&_udlo={lo}&_udhi={hi}")

PAUSE_S = 4.0
NAV_TIMEOUT_MS = 75000
MAX_PAGE = 40                # eBay stops serving new results well before this
MIN_UK_SHARE_OK = 0.80       # with LH_LocatedIn=1 this should be near 1.0

_ROW_JS = """() => {
  const out = [];
  document.querySelectorAll('li').forEach(li => {
    const t = li.innerText || '';
    if (!/£/.test(t)) return;
    if (t.length < 45 || t.length > 700) return;
    const a = li.querySelector('a[href*="/itm/"]');
    if (!a) return;
    out.push({ text: t, href: a.href.split('?')[0] });
  });
  return out;
}"""


def item_id_from(url: str) -> str | None:
    """eBay's item number out of an /itm/ URL."""
    part = url.rstrip("/").split("/itm/")[-1]
    tail = part.split("/")[-1].split("?")[0]
    return tail if tail.isdigit() else None


def playwright_proxy():
    url = proxies.proxy_url()
    if not url:
        return None
    p = urlsplit(url)
    return {"server": f"http://{p.hostname}:{p.port}",
            "username": p.username, "password": p.password}


def rows_for(listings, hrefs, category: str, shard: str) -> list[dict]:
    out = []
    for listing in listings:
        item_id = item_id_from(listing.url)
        if item_id is None:
            continue
        spec = parse(listing.title)
        out.append({
            "source": "ebay",
            "source_id": item_id,
            "category": category,
            "shard": shard,
            "title": listing.title,
            "price_pence": listing.price_pence,
            # NULL, not zero, when eBay did not state delivery. A £150 phone
            # with unstated postage is not a £150 phone.
            "total_pence": listing.landed_pence,
            "currency": "GBP",
            "condition": listing.condition,
            "brand": "",
            "model": listing.catalogue_model or spec.model,
            "storage_gb": listing.storage_gb or spec.storage_gb,
            "grade": "",
            "kind": "ask",
            "available": True,
            "region": "GB" if listing.is_uk else "",
            "seller_id": "",
            "url": listing.url,
            "extra": {
                "watchers": listing.watchers,
                "ships_from": listing.ships_from,
                "aspects": list(listing.aspects),
                "is_used": listing.is_used,
                "delivery_pence": listing.delivery_pence,
            },
        })
    return out


def fetch_page(page, query: str, lo: int, hi: int, pgn: int) -> list[dict]:
    url = SEARCH.format(q=quote_plus(query), page=pgn, lo=lo, hi=hi)
    page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="load")
    page.wait_for_timeout(2500)
    body = page.inner_text("body")
    # A captcha and an empty market look identical downstream unless they are
    # separated here.
    if "Security measure" in body or "splashui/captcha" in page.url:
        raise RuntimeError("captcha")
    if "Something went wrong" in body or page.title().startswith("Error Page"):
        raise RuntimeError("error page - rate limited")
    return page.evaluate(_ROW_JS)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--category", action="append")
    ap.add_argument("--max-pages", type=int, default=0,
                    help="stop after this many page loads (0 = unbounded)")
    ap.add_argument("--target", type=int, default=0)
    ap.add_argument("--direct", action="store_true",
                    help="no proxy; expect rate limiting within ~10 requests")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--checkpoint", type=Path, default=None)
    ap.add_argument("--fresh", action="store_true")
    args = ap.parse_args()

    cfg = config.load()
    store = MarketStore(cfg.DATA_DIR, max_bytes=cfg.MAX_STORE_BYTES)

    cp_path = args.checkpoint or (cfg.DATA_DIR / "bulk_ebay_checkpoint.json")
    checkpoint: dict = {}
    if cp_path.exists() and not args.fresh:
        checkpoint = json.loads(cp_path.read_text(encoding="utf-8"))
        print(f"resuming: {len(checkpoint.get('done', {}))} shards done")
    done = checkpoint.setdefault("done", {})

    slugs = args.category or [c.slug for c in catalog.CATEGORIES]
    proxy = None if args.direct else playwright_proxy()
    print(f"egress: {'DIRECT' if proxy is None else 'proxy (UK pinned server-side)'}")

    from playwright.sync_api import sync_playwright

    pages_loaded = 0
    total_unique: set[str] = set()
    started = time.time()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless, channel="chrome",
                                     proxy=proxy)
        page = browser.new_page(locale="en-GB")
        try:
            for slug in slugs:
                cat = catalog.BY_SLUG.get(slug)
                if cat is None:
                    print(f"unknown category {slug!r}")
                    continue
                print(f"\n=== {slug} — {len(cat.ebay_queries)} queries ===")
                cat_unique: set[str] = set()

                for query in cat.ebay_queries:
                    for pgn in range(1, MAX_PAGE + 1):
                        key = f"{slug}/{query}/{cat.price_from}-{cat.price_to}/{pgn}"
                        if key in done:
                            continue
                        if args.max_pages and pages_loaded >= args.max_pages:
                            print("   page budget spent")
                            raise SystemExit(_finish(
                                store, cp_path, checkpoint, total_unique, started))
                        try:
                            raw = fetch_page(page, query, cat.price_from,
                                             cat.price_to, pgn)
                            pages_loaded += 1
                        except Exception as exc:            # noqa: BLE001
                            print(f"   {query[:24]:<24} p{pgn:<3} FAILED "
                                  f"{str(exc)[:44]}")
                            time.sleep(PAUSE_S * 4)
                            break

                        listings = parse_rows(raw)
                        if not listings:
                            print(f"   {query[:24]:<24} p{pgn:<3} empty - end of results")
                            done[key] = 0
                            break

                        share = uk_share(listings)
                        if share < MIN_UK_SHARE_OK:
                            # LH_LocatedIn=1 is supposed to make this impossible.
                            # If it stops being true, say so rather than bank
                            # the sample.
                            print(f"   {query[:24]:<24} p{pgn:<3} SKIPPED - "
                                  f"only {share:.0%} UK despite LH_LocatedIn=1")
                            done[key] = 0
                            continue

                        rows = rows_for(listings, raw, slug, f"{query}/p{pgn}")
                        written = store.record(rows)
                        ids = {r["source_id"] for r in rows}
                        new = len(ids - cat_unique)
                        cat_unique |= ids
                        total_unique |= ids
                        done[key] = len(ids)
                        print(f"   {query[:24]:<24} p{pgn:<3} {len(rows):>3} rows "
                              f"{new:>3} new  {share:.0%} UK  (+{written} stored)")

                        # eBay serves the same page forever past the real end;
                        # zero new ids twice running means we are there.
                        if new == 0:
                            break
                        time.sleep(PAUSE_S + random.random() * 2)

                    cp_path.parent.mkdir(parents=True, exist_ok=True)
                    cp_path.write_text(json.dumps(checkpoint), encoding="utf-8")
                    if args.target and len(cat_unique) >= args.target:
                        print(f"   target {args.target} reached for {slug}")
                        break
                print(f"--- {slug}: {len(cat_unique):,} unique")
        finally:
            browser.close()

    return _finish(store, cp_path, checkpoint, total_unique, started)


def _finish(store, cp_path, checkpoint, total_unique, started) -> int:
    cp_path.parent.mkdir(parents=True, exist_ok=True)
    cp_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    print(f"\nswept {len(total_unique):,} unique eBay listings in "
          f"{(time.time() - started) / 60:.1f} min")
    print(f"store now holds {store.total_listings('ebay'):,} distinct ebay listings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
