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

from phoneloop import config, proxies  # noqa: E402
from phoneloop.ebay import parse_rows, retail_ceiling, saturation  # noqa: E402
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


def fetch(page, text: str) -> list:
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
    return parse_rows(page.evaluate(_ROW_JS))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--query", action="append")
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--out", type=Path, default=None)
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

    from playwright.sync_api import sync_playwright

    table: dict[str, dict] = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless, channel="chrome",
                                     proxy=proxy)
        page = browser.new_page(locale="en-GB")
        for (model, storage), text in zip(pairs, texts):
            try:
                listings = fetch(page, text)
            except Exception as exc:                     # noqa: BLE001
                print(f"  {text:<30} FAILED {str(exc)[:56]}")
                time.sleep(PAUSE_S * 3)
                continue

            ceiling = retail_ceiling(listings, storage_gb=storage)
            sat = saturation(listings)
            table[text] = {
                "model": model if not args.query else None,
                "storage_gb": storage if not args.query else None,
                "ceiling_pence": ceiling.median_pence if ceiling else None,
                "mad_pence": ceiling.mad_pence if ceiling else None,
                "samples": ceiling.samples if ceiling else 0,
                "saturation": sat,
                "refreshed": datetime.now(UTC).isoformat(),
            }
            shown = (f"median £{ceiling.median_pence/100:.0f} "
                     f"(n={ceiling.samples})" if ceiling else "too few UK comps")
            print(f"  {text:<30} {sat['live_listings']:>3} live "
                  f"({sat['overseas_listings']:>2} overseas)  {shown}")
            time.sleep(PAUSE_S)
        browser.close()

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(table, indent=2), encoding="utf-8")
    priced = sum(1 for v in table.values() if v["ceiling_pence"])
    print(f"\n{priced}/{len(table)} queries produced a usable ceiling")
    print(f"written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
