"""Record a fixture set once, so everything else can run offline.

This is the only script that is *meant* to hit the network hard. Run it when
the fixtures are stale or a new marketplace shape needs covering; run anything
else against what it captured.

    python scripts/capture.py                  # everything
    python scripts/capture.py --vinted         # feed + listing pages only
    python scripts/capture.py --cex --ebay     # the browser-driven halves

What it records
---------------
    http/   Vinted homepage (the handshake), the catalogue JSON, and the
            listing pages for the most interesting candidates -- including the
            known traps, because the verifier's job is to catch those and it
            cannot be developed against listings that are all clean.
    cex/    the sell-page text per query
    ebay/   the extracted result rows per query

Nothing here trims a response. A fixture that has been tidied stops being
evidence, which is exactly how this project once shipped a photo counter that
returned zero for every real listing and passed its own test.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import quote_plus, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from phoneloop import config, harness, proxies  # noqa: E402
from phoneloop.harness import Cassette, HarnessSession, Recording, recorded  # noqa: E402
from phoneloop.parser import parse  # noqa: E402
from phoneloop.vinted_client import BASE_URL, session_headers  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Vinted 429s at roughly five listing pages in twenty seconds. Measured.
LISTING_SPACING_S = 22
BROWSER_SPACING_S = 7

# Enough traps in the set that the verifier has something to catch. These are
# real ids seen in the feed on 2026-08-27.
KNOWN_TRAPS = ["9790839626", "9791279227"]


def live_session():
    """A proxied session presenting the same headers the client does.

    Sending only a User-Agent gets a 403 that survives every rotation and reads
    exactly like a blocked address, which cost a full capture run to work out.
    """
    session = requests.Session()
    session.headers.update(session_headers())
    proxies.apply(session)
    return session


def capture_vinted(cassette: Cassette, cfg, listings: int) -> int:
    """Homepage, catalogue, and a spread of listing pages.

    Drives the requests directly rather than through VintedClient. The client
    refuses to rebuild a session it did not create -- rightly, that is the
    caller's session -- so handing it one would leave capture unable to rotate
    past a blocked exit IP, which is the single most likely way a long capture
    run dies half finished.
    """
    from urllib.parse import urlencode

    state = {"inner": live_session()}

    def recorder():
        return HarnessSession(cassette, inner=state["inner"],
                              harness_mode=harness.RECORD)

    def rotate():
        """New connection, therefore a new exit IP from the gateway."""
        try:
            state["inner"].close()
        except Exception:                                  # noqa: BLE001
            pass
        state["inner"] = live_session()

    def fetch(url: str, label: str, attempts: int = 3):
        for attempt in range(attempts):
            response = recorder().get(url, timeout=30)
            if response.status_code not in (401, 403):
                return response
            if attempt + 1 < attempts:
                print(f"  {response.status_code} on {label}, rotating exit IP")
                rotate()
                # A fresh address needs a fresh handshake to go with it.
                recorder().get(BASE_URL, timeout=30)
        return response

    print("vinted: homepage (the handshake)")
    fetch(BASE_URL, "homepage")

    print("vinted: catalogue")
    params = {"catalog_ids": cfg.CATALOG_ID, "per_page": 96,
              "order": "newest_first", "price_from": cfg.PRICE_FROM}
    catalogue = fetch(f"{BASE_URL}/api/v2/catalog/items?{urlencode(params)}",
                      "catalogue")
    if catalogue.status_code != 200:
        print(f"  catalogue returned {catalogue.status_code}; nothing captured")
        return 0
    items = catalogue.json().get("items", [])
    print(f"  {len(items)} items")

    chosen = choose_listings(items, cfg, listings)

    saved = 0
    for n, item in enumerate(chosen):
        url = item.get("url")
        if not url:
            continue
        if n:
            time.sleep(LISTING_SPACING_S)
        try:
            response = fetch(url, url[-30:])
        except Exception as exc:                           # noqa: BLE001
            print(f"  ! {url[-38:]}: {str(exc)[:50]}")
            continue
        spec = parse(item.get("title", ""))
        flag = "" if response.status_code == 200 else "  <- NOT USABLE"
        print(f"  {response.status_code}  {spec.model or '?':<18} "
              f"{len(response.text):>9,}b  {url[-38:]}{flag}")
        saved += response.status_code == 200
    return saved


def choose_listings(items, cfg, limit: int) -> list[dict]:
    """The listings whose descriptions are actually worth having.

    Ranking by price picks up the cheap tail, which is mostly Android budget
    handsets nobody is arbitraging. Ranking the way the BOARD ranks picks up
    the rows a buyer would actually look at -- and those are precisely the ones
    that need a description read, because five of five apparent bargains so far
    were declared broken or locked in text the title never mentioned.

    Falls back to cheapest-first when no price tables exist yet, so a first
    capture on a clean machine still produces something usable.
    """
    from phoneloop import pricing, spread

    cex_table = pricing.load_table(cfg.DATA_DIR / "cex_prices.json")
    ebay_table = pricing.load_table(cfg.DATA_DIR / "ebay_ceilings.json")

    by_id = {str(it.get("id")): it for it in items}
    chosen = [by_id[i] for i in KNOWN_TRAPS if i in by_id]
    seen = {id(it) for it in chosen}

    scored = []
    for item in items:
        if id(item) in seen:
            continue
        spec = parse(item.get("title", ""))
        price = (item.get("total_item_price") or {}).get("amount")
        if spec.disqualifiers or price is None:
            continue
        buy_pence = round(float(price) * 100)
        if not spec.model or not (cex_table or ebay_table):
            scored.append((0, buy_pence, item))       # no tables: cheapest wins
            continue
        cash, _ = pricing.cex_cash(cex_table, spec.model, spec.storage_gb)
        ceiling, _ = pricing.ebay_ceiling(ebay_table, spec.model, spec.storage_gb)
        result = spread.evaluate(buy_pence, cash, ceiling)
        if result is None:
            continue
        scored.append((-result.margin_pence, buy_pence, item))

    scored.sort(key=lambda row: (row[0], row[1]))
    chosen += [item for _, _, item in scored[: max(0, limit - len(chosen))]]
    return chosen


def _ua() -> str:
    from phoneloop.vinted_client import USER_AGENT
    return USER_AGENT


def playwright_proxy():
    url = proxies.proxy_url()
    if not url:
        return None
    parts = urlsplit(url)
    return {"server": f"http://{parts.hostname}:{parts.port}",
            "username": parts.username, "password": parts.password}


def capture_browser(cassette: Cassette, kind: str, queries: list[str]) -> int:
    """CeX sell pages or eBay result rows, through a headed Chrome session."""
    from playwright.sync_api import sync_playwright

    saved = 0
    with sync_playwright() as pw:
        # CeX refuses anything but a real installed Chrome; eBay wants the
        # residential exit. Giving both to both costs nothing.
        browser = pw.chromium.launch(headless=False, channel="chrome",
                                     proxy=playwright_proxy())
        page = browser.new_page(locale="en-GB")
        for n, query in enumerate(queries):
            if n:
                time.sleep(BROWSER_SPACING_S)
            try:
                data = recorded(cassette, kind, query,
                                lambda q=query: _extract(page, kind, q),
                                harness_mode=harness.RECORD)
            except Exception as exc:                   # noqa: BLE001
                print(f"  ! {query:<28} {str(exc)[:52]}")
                continue
            size = len(data) if isinstance(data, list) else len(str(data))
            print(f"  {kind}: {query:<28} {size:>6} captured")
            saved += 1
        browser.close()
    return saved


def _extract(page, kind: str, query: str):
    if kind == "cex":
        from scripts.refresh_cex import SEARCH_URL as CEX_URL  # type: ignore
        page.goto(CEX_URL.format(quote_plus(query)), timeout=75000,
                  wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
        return page.inner_text("body")

    from scripts.refresh_ebay import SEARCH_URL as EBAY_URL, _ROW_JS  # type: ignore
    page.goto(EBAY_URL.format(quote_plus(query)), timeout=75000, wait_until="load")
    page.wait_for_timeout(6000)
    body = page.inner_text("body")
    if "Security measure" in body or "splashui/captcha" in page.url:
        raise RuntimeError("captcha - not a usable capture")
    return page.evaluate(_ROW_JS)


def queries_from_tables(cfg, name: str) -> list[str]:
    """Reuse whatever the live tables already cover, so a capture matches the
    board rather than an invented list."""
    path = cfg.DATA_DIR / name
    if not path.exists():
        return []
    return sorted(json.loads(path.read_text(encoding="utf-8")))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--vinted", action="store_true")
    ap.add_argument("--cex", action="store_true")
    ap.add_argument("--ebay", action="store_true")
    ap.add_argument("--listings", type=int, default=10,
                    help="how many Vinted listing pages to record")
    ap.add_argument("--root", type=Path, default=harness.DEFAULT_ROOT)
    args = ap.parse_args()

    everything = not (args.vinted or args.cex or args.ebay)
    cfg = config.load()
    cassette = Cassette(args.root)

    print(f"recording into {cassette.root}")
    print(f"egress: {proxies.redact(proxies.proxy_url())}")
    print(f"holds {len(cassette)} recordings before this run\n")

    total = 0
    if everything or args.vinted:
        total += capture_vinted(cassette, cfg, args.listings)
    if everything or args.cex:
        queries = queries_from_tables(cfg, "cex_prices.json")
        print(f"\ncex: {len(queries)} queries (headed Chrome, be patient)")
        total += capture_browser(cassette, "cex", queries)
    if everything or args.ebay:
        queries = queries_from_tables(cfg, "ebay_ceilings.json")
        print(f"\nebay: {len(queries)} queries")
        total += capture_browser(cassette, "ebay", queries)

    print(f"\n{total} captured; cassette now holds {len(cassette)}")
    print("everything else can now run with no network:")
    print("  python scripts/review.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
