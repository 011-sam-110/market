"""Build the eBay retail-ceiling table for handsets we are seeing.

Access, measured rather than assumed (2026-08-27):

    scripted client (requests / curl_cffi)   403 on live search
    headed Chrome, direct                    200, then rate-limited within ~10 requests
    headed Chrome + residential proxy        200, reliable
    SOLD / completed, every client tried     captcha -> sign-in wall

Sold prices are therefore out of scope on purpose. Reaching them means solving a
captcha while signed in, and eBay suspends accounts for automated access -- the
account at risk is the one you need in order to sell. Asking prices still give a
retail ceiling, and the listing count gives a saturation signal the arbitrage
side has been missing entirely.

Be unhurried with this. eBay rate-limits harder than Vinted and it is a browser
per request; PAUSE_S exists to be respected, not tuned down.

    python scripts/refresh_ebay.py --query "iphone 13 128gb"
    python scripts/refresh_ebay.py --limit 8
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
from urllib.parse import quote_plus, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phoneloop import config, harness, proxies  # noqa: E402
from phoneloop.harness import Cassette, recorded  # noqa: E402
from phoneloop.ebay import (  # noqa: E402
    looks_geolocated_abroad,
    parse_rows,
    retail_ceiling,
    saturation,
    uk_share,
)
from phoneloop.parser import parse  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SEARCH_URL = "https://www.ebay.co.uk/sch/i.html?_nkw={}&_ipg=60"
PAUSE_S = 6.0
NAV_TIMEOUT_MS = 75000

# Pull every result row that carries a price and links to an item. Class names
# on eBay's search page change often; the item link and a price are the two
# things that have not.
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


def playwright_proxy():
    url = proxies.proxy_url()
    if not url:
        return None
    parts = urlsplit(url)
    return {"server": f"http://{parts.hostname}:{parts.port}",
            "username": parts.username, "password": parts.password}


def queries_from_store(cfg, limit: int) -> list[tuple[str, int | None]]:
    """(model, storage) pairs the collector has actually observed, by frequency."""
    import duckdb

    files = glob.glob(str(cfg.DATA_DIR / "observations" / "**" / "*.parquet"),
                      recursive=True)
    if not files:
        return []
    rows = duckdb.connect().execute(
        f"SELECT title FROM read_parquet({files!r})").fetchall()
    counts: Counter[tuple[str, int | None]] = Counter()
    for (title,) in rows:
        spec = parse(title or "")
        if not spec.model or spec.disqualifiers:
            continue
        counts[(spec.model, spec.storage_gb)] += 1
    return [pair for pair, _ in counts.most_common(limit)]


def query_text(model: str, storage_gb: int | None) -> str:
    words = model.replace("-", " ").strip()
    return f"{words} {storage_gb}gb" if storage_gb else words


def scrape_rows(page, text: str) -> list[dict]:
    """The raw result rows, straight off a live page.

    Returns the untouched rows rather than parsed listings, because those are
    what gets recorded: the whole point of the fixture is to iterate on
    parse_row() offline, and a fixture of already-parsed objects could not
    exercise the parser at all.
    """
    page.goto(SEARCH_URL.format(quote_plus(text)),
              timeout=NAV_TIMEOUT_MS, wait_until="load")
    page.wait_for_timeout(6000)
    body = page.inner_text("body")
    # Separate "eBay refused" from "nothing matched". Conflating them is how a
    # ceiling table ends up quietly empty and gets read as a quiet market.
    if "Security measure" in body or "splashui/captcha" in page.url:
        raise RuntimeError("captcha - eBay is challenging this session")
    if "Something went wrong" in body or page.title().startswith("Error Page"):
        raise RuntimeError("eBay error page - likely rate limited, back off")
    return page.evaluate(_ROW_JS)


def fetch(cassette, page, text: str) -> list:
    """Rows for one query, replayed from the cassette or scraped live.

    In replay mode `page` is None and no browser is ever launched, which is
    what makes the ceiling logic developable without a marketplace.
    """
    raw = recorded(cassette, "ebay", text, lambda: scrape_rows(page, text))
    return parse_rows(raw)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--query", action="append")
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--root", type=Path, default=harness.DEFAULT_ROOT)
    ap.add_argument("--headless", action="store_true",
                    help="expect a 403; eBay wants a real browser")
    args = ap.parse_args()

    cfg = config.load()
    out = args.out or (cfg.DATA_DIR / "ebay_ceilings.json")

    if args.query:
        pairs = [(q, None) for q in args.query]
        texts = list(args.query)
    else:
        pairs = queries_from_store(cfg, args.limit)
        texts = [query_text(m, s) for m, s in pairs]
        if not pairs:
            print("no observations yet - run 'python -m phoneloop collect' first")
            return 1

    proxy = playwright_proxy()
    print(f"eBay ceilings for {len(texts)} queries -> {out}")
    print(f"egress: {'proxy ' + proxies.redact(proxies.proxy_url()) if proxy else 'DIRECT (expect rate limiting)'}")

    import contextlib

    cassette = Cassette(args.root)
    offline = harness.mode() == harness.REPLAY
    print(f"harness: {harness.mode()}"
          + (f", {len(cassette.keys('ebay'))} ebay recordings" if offline else ""))

    table: dict[str, dict] = {}
    with contextlib.ExitStack() as stack:
        page = None
        if not offline:
            from playwright.sync_api import sync_playwright
            pw = stack.enter_context(sync_playwright())
            browser = pw.chromium.launch(headless=args.headless,
                                         channel="chrome", proxy=proxy)
            stack.callback(browser.close)
            page = browser.new_page(locale="en-GB")

        for (model, storage), text in zip(pairs, texts):
            try:
                listings = fetch(cassette, page, text)
            except Exception as exc:                     # noqa: BLE001
                print(f"  {text:<30} FAILED {str(exc)[:56]}")
                if not offline:
                    time.sleep(PAUSE_S * 3)
                continue

            # Narrow to the exact model using eBay's own catalogue line. A
            # search for "iphone 13" also returns the mini, the Pro and the
            # Pro Max, and pooling them yields a median that describes no
            # actual handset.
            # An explicit --query must still resolve to a slug or the entry
            # can never match a listing. Same failure the CeX table had.
            want = model if not args.query else parse(text).model
            storage = storage if not args.query else parse(text).storage_gb
            if want is None:
                print(f"  {text:<30} SKIPPED - no model slug from that query")
                continue
            # A pool served to a non-UK address is not a UK market sample.
            # Skipping it leaves the previous good price in the merged table,
            # which is the right outcome: no number beats a wrong one.
            if looks_geolocated_abroad(listings):
                print(f"  {text:<30} SKIPPED - only {uk_share(listings):.0%} UK, "
                      "served abroad")
                continue

            ceiling = retail_ceiling(listings, storage_gb=storage, model=want)
            sat = saturation(listings)
            table[text] = {
                "model": want,
                "storage_gb": storage,
                "ceiling_pence": ceiling.median_pence if ceiling else None,
                "mad_pence": ceiling.mad_pence if ceiling else None,
                "samples": ceiling.samples if ceiling else 0,
                "variant_excluded": ceiling.variant_excluded if ceiling else 0,
                "saturation": sat,
                "refreshed": datetime.now(UTC).isoformat(),
            }
            shown = (f"median £{ceiling.median_pence/100:.0f} "
                     f"(n={ceiling.samples}, -{ceiling.variant_excluded} variant)"
                     if ceiling else "too few UK comps after filtering")
            print(f"  {text:<30} {sat['live_listings']:>3} live "
                  f"({sat['overseas_listings']:>2} overseas)  {shown}")
            if not offline:
                time.sleep(PAUSE_S)

    out.parent.mkdir(parents=True, exist_ok=True)
    # Merge, never overwrite. These tables are built up across many runs -- CeX
    # and eBay both rate-limit, so a full rebuild in one pass is not possible --
    # and a query that failed this time must not delete the good price it
    # returned last time.
    merged = {}
    if out.exists():
        try:
            merged = json.loads(out.read_text(encoding="utf-8"))
        except ValueError:
            print(f"  ! {out} was unreadable, starting fresh")
    kept = len(merged)
    merged.update(table)
    out.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    print(f"table now holds {len(merged)} entries "
          f"({len(merged) - kept} new this run)")
    priced = sum(1 for v in table.values() if v["ceiling_pence"])
    print(f"\n{priced}/{len(table)} queries produced a usable ceiling")
    print(f"written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
