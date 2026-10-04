"""Scrapling bake-off: does Scrapling get past the bot checks that block our scrapers?

A measurement script, not part of the pipeline. Nothing imports it, and it
imports nothing from `phoneloop`, so it runs from a bare Scrapling venv and can
be copied to another machine on its own:

    pip install "scrapling[fetchers,ai]"   # then: scrapling install
    python scripts/scrapling_bakeoff.py --targets ebay,cex,reuters \
        --variants fetcher,stealth_headless,stealth_headed --out results.json

Three targets, two pages each, the real URLs our scrapers use:

    ebay     UK search results, the nightly's sacat URL shape
             (phoneloop.ebay.search_url("sacat", leaf, 15, 55, pgn))
    cex      the sell-search page scripts/refresh_cex.py loads
    reuters  two World sections from NewsScraper (scraper/reuters.py SECTIONS)

Three variants:

    fetcher           scrapling.Fetcher, HTTP with curl_cffi Chrome TLS impersonation
    stealth_headless  scrapling.StealthyFetcher (patchright Chromium), headless
    stealth_headed    the same, with a visible window

Each request is judged by a CONTENT signal, never by the status alone:

    ebay     not blocked (bulk_ebay.is_blocked: "Something went wrong" /
             "Security measure" in the body text, "splashui/captcha" in the
             URL), not a /b/ browse-node redirect, and real result rows
             (bulk_ebay._ROW_JS: an <li> with a £ price and an /itm/ link,
             the hidden 123456 template row excluded)
    cex      a "CASH" line followed by a £ amount (phoneloop.cex.parse_sell_page)
    reuters  window.Fusion.globalContent is set (NewsScraper wait_for_reuters_page).
             DataDome answers the first document with 401 and its own JS then
             reloads the page, so the goto status says nothing on its own.
             Two pages per session on purpose: under Xvfb the FIRST Reuters
             page passes and later ones fail.

Request budget: at most 2 pages x 3 variants per target per run, at least
`--gap` seconds (default 6) between requests to the same host, and no retries.
The run stops before any request if the egress is a Mullvad exit, because eBay
refuses VPN exits and a result from one says nothing about the home line.
"""
from __future__ import annotations

import argparse
import json
import platform
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlsplit

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# --- targets -----------------------------------------------------------------

# Leaf 20742 is the control leaf in phoneloop/ebay.py (list view, a seller line
# on ~97% of rows); eBay titles the page "Dog Supplies". Same URL shape as the
# nightly's sweep.
_EBAY_URL = ("https://www.ebay.co.uk/sch/i.html?_sacat=20742&_fsrp=1&_ipg=240"
             "&_pgn={pgn}&_udlo=15&_udhi=55&_dmd=1")

TARGETS: dict[str, list[str]] = {
    "ebay": [_EBAY_URL.format(pgn=1), _EBAY_URL.format(pgn=2)],
    "cex": [
        "https://uk.webuy.com/sell/search?stext=iphone%2012%20128gb",
        "https://uk.webuy.com/sell/search?stext=iphone%2013%20128gb",
    ],
    "reuters": [
        "https://www.reuters.com/world/africa/",
        "https://www.reuters.com/world/europe/",
    ],
}

VARIANTS = ("fetcher", "stealth_headless", "stealth_headed")

STEALTH_TIMEOUT_MS = 90_000   # solve_cloudflare needs >= 60 s per Scrapling's docs
FETCHER_TIMEOUT_S = 30


# --- content signals -----------------------------------------------------------

_ITM_RE = re.compile(r"/itm/(\d+)")
_DECOY_ITEM_IDS = {"123456"}
_CEX_MONEY_RE = re.compile(r"£\s*[\d,]+(?:\.\d{2})?")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def _body_text(resp) -> str:
    """Visible body text (script and style dropped), like Playwright's inner_text."""
    try:
        bodies = resp.css("body")
        if bodies:
            return str(bodies[0].get_all_text(separator="\n", strip=True))
    except Exception:  # noqa: BLE001
        pass
    return ""


def _html(resp) -> str:
    try:
        return str(resp.html_content)
    except Exception:  # noqa: BLE001
        body = getattr(resp, "body", b"")
        return body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)


def ebay_signal(resp, html: str, text: str, extra: dict) -> tuple[bool, str]:
    url = str(getattr(resp, "url", "") or "")
    if "Something went wrong" in text or "Security measure" in text or "splashui/captcha" in url:
        return False, "blocked page (Something went wrong / Security measure / captcha)"
    if urlsplit(url).path.startswith("/b/"):
        return False, f"redirected to browse node {url}"
    ids: set[str] = set()
    for li in resp.css("li"):
        li_text = str(li.get_all_text(separator="\n", strip=True))
        if "£" not in li_text or not 20 <= len(li_text) <= 900:
            continue
        for a in li.css('a[href*="/itm/"]'):
            m = _ITM_RE.search(str(a.attrib.get("href", "")))
            if m and m.group(1) not in _DECOY_ITEM_IDS:
                ids.add(m.group(1))
                break
    if len(ids) >= 10:
        return True, f"{len(ids)} result rows"
    return False, f"{len(ids)} result rows (need >= 10)"


def cex_signal(resp, html: str, text: str, extra: dict) -> tuple[bool, str]:
    lines = [ln.strip() for ln in text.splitlines()]
    boxes = 0
    first_price = ""
    for i, ln in enumerate(lines):
        if ln.upper() != "CASH":
            continue
        for fwd in lines[i + 1:i + 6]:
            m = _CEX_MONEY_RE.search(fwd)
            if m:
                boxes += 1
                first_price = first_price or m.group(0)
                break
    if boxes:
        return True, f"{boxes} CASH prices, first {first_price}"
    low = text.lower() + html[:20000].lower()
    if "security verification" in low or "ray id" in low or "just a moment" in low:
        return False, "Cloudflare challenge page"
    return False, "no CASH price on the page"


def reuters_signal(resp, html: str, text: str, extra: dict) -> tuple[bool, str]:
    live = extra.get("fusion_live")
    in_html = "Fusion.globalContent" in html
    if live:
        return True, f"window.Fusion.globalContent set after {extra.get('fusion_wait_s')}s"
    if live is None and in_html:
        return True, "Fusion.globalContent in the HTML"
    low = html[:20000].lower()
    why = "DataDome captcha page" if "captcha-delivery.com" in low else "no Fusion.globalContent"
    return False, why


SIGNALS = {"ebay": ebay_signal, "cex": cex_signal, "reuters": reuters_signal}


# --- page actions for the browser variants -------------------------------------

def _make_page_action(target: str, extra: dict):
    """Wait for the app signal inside the browser before Scrapling snapshots the page."""

    def cex_action(page):
        try:
            page.wait_for_selector("text=CASH", timeout=25_000)
            extra["cash_visible"] = True
        except Exception:  # noqa: BLE001
            extra["cash_visible"] = False
        return page

    def reuters_action(page):
        start = time.monotonic()
        extra["fusion_live"] = False
        while time.monotonic() - start < 30:
            try:
                if page.evaluate("() => Boolean(window.Fusion && window.Fusion.globalContent)"):
                    extra["fusion_live"] = True
                    break
            except Exception:  # noqa: BLE001
                pass  # the DataDome check reloads the page and destroys the context
            page.wait_for_timeout(1000)
        extra["fusion_wait_s"] = round(time.monotonic() - start, 1)
        return page

    def ebay_action(page):
        page.wait_for_timeout(2500)   # same settle time as bulk_ebay.fetch_page
        return page

    return {"cex": cex_action, "reuters": reuters_action, "ebay": ebay_action}[target]


# --- runner ----------------------------------------------------------------------

_last_hit: dict[str, float] = {}


def _pace(url: str, gap: float) -> None:
    host = urlsplit(url).hostname or ""
    last = _last_hit.get(host)
    if last is not None:
        wait = gap - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
    _last_hit[host] = time.monotonic()


def egress() -> dict:
    """Public IP and whether it is a Mullvad exit, from Mullvad's own checker."""
    try:
        with urllib.request.urlopen("https://am.i.mullvad.net/json", timeout=15) as r:
            data = json.loads(r.read().decode())
        return {k: data.get(k) for k in ("ip", "mullvad_exit_ip", "organization", "country", "city")}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def _record(target: str, page_no: int, variant: str, url: str, resp, extra: dict,
            seconds: float, error: str = "") -> dict:
    row = {"target": target, "page": page_no, "variant": variant, "url": url,
           "status": None, "signal": False, "signal_detail": "", "seconds": round(seconds, 1),
           "final_url": "", "title": "", "error": error}
    if resp is None:
        row["signal_detail"] = "no response"
        return row
    html = _html(resp)
    text = _body_text(resp)
    row["status"] = getattr(resp, "status", None)
    row["final_url"] = str(getattr(resp, "url", "") or "")
    m = _TITLE_RE.search(html)
    row["title"] = re.sub(r"\s+", " ", m.group(1)).strip()[:90] if m else ""
    try:
        headers = {str(k).lower(): str(v) for k, v in (getattr(resp, "headers", {}) or {}).items()}
        if "x-datadome" in headers:
            row["x_datadome"] = headers["x-datadome"]
    except Exception:  # noqa: BLE001
        pass
    try:
        ok, detail = SIGNALS[target](resp, html, text, extra)
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, f"signal check failed: {type(exc).__name__}: {exc}"
    row["signal"], row["signal_detail"] = ok, detail
    if extra:
        row["browser_checks"] = dict(extra)
    return row


def run_fetcher(target: str, urls: list[str], gap: float) -> list[dict]:
    from scrapling.fetchers import FetcherSession

    rows = []
    with FetcherSession(impersonate="chrome", stealthy_headers=True, retries=1,
                        timeout=FETCHER_TIMEOUT_S, follow_redirects=True) as session:
        for i, url in enumerate(urls, 1):
            _pace(url, gap)
            t0 = time.monotonic()
            try:
                resp = session.get(url)
                rows.append(_record(target, i, "fetcher", url, resp, {}, time.monotonic() - t0))
            except Exception as exc:  # noqa: BLE001
                rows.append(_record(target, i, "fetcher", url, None, {}, time.monotonic() - t0,
                                    error=f"{type(exc).__name__}: {str(exc)[:200]}"))
            print(_line(rows[-1]), flush=True)
    return rows


def run_stealth(target: str, urls: list[str], gap: float, headless: bool) -> list[dict]:
    from scrapling.fetchers import StealthySession

    variant = "stealth_headless" if headless else "stealth_headed"
    rows = []
    # One session for both pages, as a real scraper would run it: this is what
    # catches a check that passes the first page and refuses the second.
    with StealthySession(headless=headless, solve_cloudflare=True, timeout=STEALTH_TIMEOUT_MS,
                         retries=1, locale="en-GB") as session:
        for i, url in enumerate(urls, 1):
            _pace(url, gap)
            extra: dict = {}
            t0 = time.monotonic()
            try:
                resp = session.fetch(url, page_action=_make_page_action(target, extra))
                rows.append(_record(target, i, variant, url, resp, extra, time.monotonic() - t0))
            except Exception as exc:  # noqa: BLE001
                rows.append(_record(target, i, variant, url, None, extra, time.monotonic() - t0,
                                    error=f"{type(exc).__name__}: {str(exc)[:200]}"))
            print(_line(rows[-1]), flush=True)
    return rows


def _line(r: dict) -> str:
    sig = "YES" if r["signal"] else "no"
    detail = r["signal_detail"] if not r["error"] else f"ERROR {r['error'][:60]}"
    return (f"{r['target']:<8} p{r['page']}  {r['variant']:<17} {str(r['status']):<5} "
            f"{sig:<4} {r['seconds']:>6.1f}s  {detail}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--targets", default="ebay,cex,reuters")
    ap.add_argument("--variants", default=",".join(VARIANTS))
    ap.add_argument("--gap", type=float, default=6.0,
                    help="minimum seconds between requests to one host (>= 5)")
    ap.add_argument("--machine-label", default="", help="free text stored with the results")
    ap.add_argument("--out", required=True, help="results JSON path")
    args = ap.parse_args(argv)

    targets = [t.strip() for t in args.targets.split(",") if t.strip()]
    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    bad = [t for t in targets if t not in TARGETS] + [v for v in variants if v not in VARIANTS]
    if bad:
        ap.error(f"unknown target or variant: {bad}")
    if args.gap < 5:
        ap.error("--gap must be at least 5 seconds")

    try:
        from importlib.metadata import version
        scrapling_version = version("scrapling")
        patchright_version = version("patchright")
        curl_cffi_version = version("curl_cffi")
    except Exception:  # noqa: BLE001
        scrapling_version = patchright_version = curl_cffi_version = "unknown"

    meta = {
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "machine": platform.node(),
        "machine_label": args.machine_label,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "scrapling": scrapling_version,
        "patchright": patchright_version,
        "curl_cffi": curl_cffi_version,
        "egress": egress(),
        "targets": targets,
        "variants": variants,
        "gap_s": args.gap,
    }
    print(json.dumps(meta, indent=2))
    eg = meta["egress"]
    if eg.get("error") or eg.get("mullvad_exit_ip") is not False:
        print("STOP: egress is a Mullvad exit or could not be checked; no request was sent.")
        _write(args.out, meta, [])
        return 2

    rows: list[dict] = []
    for target in targets:
        for variant in variants:
            urls = TARGETS[target]
            if variant == "fetcher":
                rows += run_fetcher(target, urls, args.gap)
            else:
                rows += run_stealth(target, urls, args.gap, headless=(variant == "stealth_headless"))
    meta["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _write(args.out, meta, rows)

    print()
    print(f"{'target':<8} {'pg':<3} {'variant':<17} {'HTTP':<5} {'real page':<9} {'secs':>6}  detail")
    for r in rows:
        print(f"{r['target']:<8} p{r['page']:<2} {r['variant']:<17} {str(r['status']):<5} "
              f"{('yes' if r['signal'] else 'no'):<9} {r['seconds']:>6.1f}  "
              f"{r['signal_detail'] if not r['error'] else 'ERROR ' + r['error'][:70]}")
    return 0


def _write(path: str, meta: dict, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"meta": meta, "results": rows}, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
