"""Record real eBay UK pages through the harness, once, for offline parser work.

Every fixture `ebay.py` and `ebay_item.py` are tested against has to come from
here -- a real page fetched live -- never from a hand-typed string, per the
project's standing "never fake results" rule.

Egress, measured this session (2026-08-27): a scripted `requests` GET 403s
immediately; headed Chrome direct gets exactly one page before eBay serves an
"Error Page | eBay" / "Something went wrong on our end" response -- observed on
the second request in a row, proxy or not. What clears it is loading the
homepage first (`warm_up`) to pick up cookies before the first search or item
navigation. `open_session` tries that direct; if the homepage load itself comes
back blocked, it falls back to the residential proxy for the rest of the run,
which is the direct-first-then-proxy rule from the contract.

Captures, minimum per the build contract:
    >= 3 search-result pages   (two different queries/leaves, + page 2 of one)
    >= 6 item pages
    1   /p/<epid> product page (an epid= query param lifted off a search row)

Recordings land in the directory named by PHONELOOP_FIXTURES -- pass it
explicitly, do not rely on a default, because the repo's own fixtures/ must
never receive this stream's captures.

    PHONELOOP_HARNESS=record python scripts/record_ebay.py \\
        --root C:/Users/sampo/.phoneloop/fixtures
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phoneloop import harness, proxies  # noqa: E402
from phoneloop.browser import launch as launch_browser  # noqa: E402
from phoneloop.ebay import (  # noqa: E402
    DEFAULT_PRICE_FROM, DEFAULT_PRICE_TO, search_key, search_url,
)
from phoneloop.harness import Cassette, recorded  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# The URL and the harness key are phoneloop.ebay.search_url / .search_key --
# the same functions scripts/bulk_ebay.py fetches with -- so a page captured
# here satisfies that script's replay lookup for the identical (mode, term,
# price band, page). They used to be built independently in each script and
# had drifted (integration review, 2026-08-28); see phoneloop/ebay.py's
# module docstring for what that broke and phoneloop/../scripts/bulk_ebay.py
# for the remaining gap this alone does not close (a fresh recording session
# under the now-aligned shape).
NAV_TIMEOUT_MS = 60000
PAUSE_S = 5.0

# Same row extraction as scripts/bulk_ebay.py / scripts/refresh_ebay.py: an
# <li> carrying a price and an /itm/ link. The hidden "Shop on eBay" template
# row prices in USD ($20.00, measured), so this filter already excludes it on
# a UK-currency page -- is_decoy() in phoneloop/ebay.py is the second, load
# -bearing guard for whenever that stops being true.
_ROW_JS = """() => {
  const out = [];
  document.querySelectorAll('li').forEach(li => {
    const t = li.innerText || '';
    if (!/£/.test(t)) return;
    if (t.length < 20 || t.length > 900) return;
    const a = li.querySelector('a[href*="/itm/"]');
    if (!a) return;
    out.push({ text: t, href: a.href });
  });
  return out;
}"""

_EPID_RE = re.compile(r"[?&]epid=(\d+)")


def playwright_proxy():
    url = proxies.proxy_url()
    if not url:
        return None
    p = urlsplit(url)
    return {"server": f"http://{p.hostname}:{p.port}",
            "username": p.username, "password": p.password}


def is_blocked(body: str, url: str) -> bool:
    return ("Something went wrong" in body or "Security measure" in body
            or "splashui/captcha" in url)


def warm_up(page) -> str:
    page.goto("https://www.ebay.co.uk/", timeout=NAV_TIMEOUT_MS, wait_until="load")
    page.wait_for_timeout(2200)
    return page.inner_text("body")


def open_session(pw):
    """Direct first; fall back to the residential proxy only if the homepage
    load itself comes back blocked. Returns (browser, page, used_proxy)."""
    browser = launch_browser(pw, headless=False, proxy=None)
    page = browser.new_page(locale="en-GB")
    body = warm_up(page)
    if not is_blocked(body, page.url):
        print("egress: DIRECT")
        return browser, page, False

    print("direct egress blocked on the homepage load -> falling back to the "
          "residential proxy")
    browser.close()
    proxy = playwright_proxy()
    if proxy is None:
        raise RuntimeError(
            "direct egress blocked and no PHONELOOP_PROXY_URL is configured")
    browser = launch_browser(pw, headless=False, proxy=proxy)
    page = browser.new_page(locale="en-GB")
    body = warm_up(page)
    if is_blocked(body, page.url):
        browser.close()
        raise RuntimeError(
            "blocked on both direct and the residential proxy -- eBay refused "
            "the homepage load either way")
    print(f"egress: proxy ({proxies.redact(proxies.proxy_url())})")
    return browser, page, True


def fetch_search(page, query: str, pgn: int) -> list[dict]:
    url = search_url("nkw", query, DEFAULT_PRICE_FROM, DEFAULT_PRICE_TO, pgn)
    page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="load")
    page.wait_for_timeout(3500)
    body = page.inner_text("body")
    if is_blocked(body, page.url):
        raise RuntimeError(f"blocked fetching search page {query!r} p{pgn}")
    return page.evaluate(_ROW_JS)


def fetch_item(page, url: str) -> dict:
    page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="load")
    page.wait_for_timeout(3000)
    body = page.inner_text("body")
    if is_blocked(body, page.url):
        raise RuntimeError(f"blocked fetching item page {url}")
    return {"text": body, "html": page.content(), "url": page.url}


def item_urls_from(rows: list[dict], limit: int) -> list[str]:
    """Distinct /itm/ hrefs off a captured search page, query string stripped."""
    seen: list[str] = []
    for r in rows:
        href = (r.get("href") or "").split("?")[0]
        if href and href not in seen and "/itm/" in href:
            seen.append(href)
        if len(seen) >= limit:
            break
    return seen


def epid_from(rows: list[dict]) -> str | None:
    for r in rows:
        m = _EPID_RE.search(r.get("href") or "")
        if m:
            return m.group(1)
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=harness.DEFAULT_ROOT)
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    if harness.mode() != harness.RECORD:
        print(f"PHONELOOP_HARNESS={harness.mode()!r} -- this script only makes "
              "sense with PHONELOOP_HARNESS=record")
        return 2

    cassette = Cassette(args.root)
    print(f"recording into {cassette.root}")

    from playwright.sync_api import sync_playwright

    item_urls: list[str] = []
    epid: str | None = None

    with sync_playwright() as pw:
        browser, page, _used_proxy = open_session(pw)
        try:
            # --- search pages: two leaves/queries, + page 2 of one --------
            queries = [("iphone 13 128gb", 1), ("iphone 13 128gb", 2),
                      ("wireless charger", 1)]
            for query, pgn in queries:
                name = search_key("nkw", query, DEFAULT_PRICE_FROM, DEFAULT_PRICE_TO, pgn)
                rows = recorded(cassette, "ebay", name,
                                lambda q=query, p=pgn: fetch_search(page, q, p))
                print(f"  search {query!r} p{pgn}: {len(rows)} rows")
                item_urls.extend(item_urls_from(rows, 6))
                if epid is None:
                    epid = epid_from(rows)
                page.wait_for_timeout(int(PAUSE_S * 1000))

            # --- item pages -------------------------------------------------
            seen: set[str] = set()
            unique_urls = [u for u in item_urls if not (u in seen or seen.add(u))]
            for url in unique_urls[:6]:
                name = f"item:{url}"
                data = recorded(cassette, "ebay_item", name,
                                lambda u=url: fetch_item(page, u))
                print(f"  item {url} -> {len(data['text'])} chars text, "
                      f"{len(data['html'])} chars html")
                page.wait_for_timeout(int(PAUSE_S * 1000))

            # --- one /p/<epid> product page ---------------------------------
            if epid:
                purl = f"https://www.ebay.co.uk/p/{epid}"
                name = f"product:{purl}"
                data = recorded(cassette, "ebay_product", name,
                                lambda u=purl: fetch_item(page, u))
                print(f"  product /p/{epid} -> {len(data['text'])} chars text")
            else:
                print("  no epid= href found on any captured search page -- "
                      "no /p/<epid> page recorded")
        finally:
            browser.close()

    print(f"\ncassette now holds {len(cassette)} recordings under {cassette.root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
