"""Enumerate eBay UK at scale -- direct-first, harness-honouring, cap-aware.

Location used to be pinned server-side with `LH_LocatedIn=1` ("UK only").
That is now dropped. It excludes exactly the competitors a China-sourced
dropshipper prices against, and the niche finder needs the whole competitor
set, not a UK-only slice of it -- `origin_country` on every row (from
`phoneloop.ebay.to_row`) records where a listing ships from instead, so the
information is kept without narrowing the population that produced it. What
that parameter's removal reopens is the egress-geography trap it used to
paper over: a request answered from Hong Kong sees a different, thinner, UK
pool than one answered from London, and eBay reorders by shipping proximity
to the requester. `uk_share()` still runs on every page as a result printed
alongside the sweep, not a gate that skips one -- the point of a competitor
count is to see the whole market, overseas rows included, not to throw the
page away because too much of it is overseas.

Egress: direct first, the residential proxy only after eBay blocks the
initial homepage load (measured, 2026-08-27: a headed session gets one page
directly before a captcha or an error page; the proxy is reliable). Decided
once at the start of the run -- the same pattern scripts/record_ebay.py
uses -- and escalated again, once, if a page mid-run comes back blocked while
still on direct.

Two sweep modes, same storage path:

    --category phones           existing keyword sweep (_nkw=), unchanged shape
    --leaf-sweep                new _sacat= sweep over catalog_ebay.IN_SCOPE

`--leaf-sweep` imports `phoneloop.catalog_ebay` lazily, inside the code path
that needs it, because that module belongs to a sibling stream
(build/s6_category_scope) not merged into this worktree -- see the build
report's unverified[]. The keyword sweep has no such dependency and is
fully exercisable today.

Cap: `catalog.result_cap("ebay")` (10,000, measured -- page 45 at 240/page
returned zero on a 21,000+-result query). The saturation test is "the page
came back with zero rows", never eBay's own rounded result-count header
(spec §11); `catalog.is_saturated(unique_seen, "ebay")` only ever fires as a
warning that a shard likely hides more listings than the cap let through --
this script does not price-band-split an eBay shard the way bulk_vinted.py
splits a Vinted one; a shard that saturates is reported, not sub-sharded.

A blocked fetch (captcha, error page, proxy 403) is never recorded as an
empty shard: `done[key] = 0` means "swept, genuinely nothing there", and a
blocked shard never gets that value. It is left OUT of `done` entirely (so
the next run retries it) and logged under `checkpoint["blocked"]` with a
reason, so a market that is actually empty and a market this run could not
reach do not look the same on resume.

Replay honours the harness like scripts/refresh_ebay.py: in
PHONELOOP_HARNESS=replay (the default), no browser is ever launched and
every page comes from `harness.recorded(cassette, "ebay", name, produce)` --
a shard with no matching recording raises `MissingRecording` rather than
returning an empty page, and that is left to propagate rather than caught
as "blocked": a gap in the fixtures is a different problem from eBay
blocking a live request, and conflating them would hide the gap.

    python scripts/bulk_ebay.py --category phones
    python scripts/bulk_ebay.py --leaf-sweep --max-pages 400
    python scripts/bulk_ebay.py --category phones --direct   # no proxy fallback
    PHONELOOP_HARNESS=replay python scripts/bulk_ebay.py --leaf-sweep --max-pages 3 --fresh
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
from pathlib import Path
from urllib.parse import quote_plus, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phoneloop import catalog, config, harness, proxies  # noqa: E402
from phoneloop.ebay import parse_rows, to_row, uk_share  # noqa: E402
from phoneloop.harness import Cassette, recorded  # noqa: E402
from phoneloop.market_store import MarketStore  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# _ipg=240 is the largest page eBay serves. _pgn is the page. _udlo/_udhi are
# the price band, in pounds -- LH_LocatedIn is gone (see module docstring).
SEARCH_NKW = ("https://www.ebay.co.uk/sch/i.html?_nkw={term}&_ipg=240&_pgn={page}"
              "&_udlo={lo}&_udhi={hi}")
SEARCH_SACAT = ("https://www.ebay.co.uk/sch/i.html?_sacat={term}&_ipg=240&_pgn={page}"
                "&_udlo={lo}&_udhi={hi}")

# The spec's default retail band (§3, knob 3) -- used only for --leaf-sweep,
# which carries no per-category band the way phoneloop.catalog.Category does.
DEFAULT_PRICE_FROM = 15
DEFAULT_PRICE_TO = 55

PAUSE_S = 4.0
NAV_TIMEOUT_MS = 75000
# catalog.result_cap("ebay") // 240, rounded up, plus one -- the page budget
# a single shard could ever need before it is either saturated or exhausted.
MAX_PAGE = 42

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
    page.wait_for_timeout(2000)
    return page.inner_text("body")


def open_session(pw, *, headless: bool, force_direct: bool):
    """Direct first; the residential proxy only if the homepage load itself
    comes back blocked. Returns (browser, page, used_proxy).

    `force_direct` (the --direct flag) skips the fallback entirely -- for
    deliberately testing direct-only behaviour, not for production sweeps.
    """
    browser = pw.chromium.launch(headless=headless, channel="chrome", proxy=None)
    page = browser.new_page(locale="en-GB")
    body = warm_up(page)
    if not is_blocked(body, page.url):
        return browser, page, False
    if force_direct:
        raise RuntimeError("direct egress blocked on the homepage load and "
                           "--direct forbids the proxy fallback")

    print("direct egress blocked on the homepage load -> falling back to "
          "the residential proxy")
    browser.close()
    proxy = playwright_proxy()
    if proxy is None:
        raise RuntimeError(
            "direct egress blocked and no PHONELOOP_PROXY_URL is configured")
    browser = pw.chromium.launch(headless=headless, channel="chrome", proxy=proxy)
    page = browser.new_page(locale="en-GB")
    body = warm_up(page)
    if is_blocked(body, page.url):
        browser.close()
        raise RuntimeError(
            "blocked on both direct and the residential proxy -- eBay "
            "refused the homepage load either way")
    return browser, page, True


def fetch_page(page, url: str) -> list[dict]:
    """One live page load. Raises on a captcha or an error page -- the
    caller is what decides whether that is a shard-ending block or a cue to
    escalate to the proxy; this function only reports what it saw."""
    page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="load")
    page.wait_for_timeout(2500)
    body = page.inner_text("body")
    if is_blocked(body, page.url):
        raise RuntimeError("blocked (captcha or error page)")
    return page.evaluate(_ROW_JS)


def rows_for(listings, category: str, shard: str,
            leaf_category_id: str | None) -> list[dict]:
    return [to_row(listing, category=category, shard=shard,
                   leaf_category_id=leaf_category_id)
           for listing in listings]


def leaf_targets() -> list[tuple[str, str, str]]:
    """(slug, title, leaf_category_id) for every in-scope leaf.

    Deferred import: phoneloop.catalog_ebay belongs to build/s6_category_scope,
    a sibling stream not merged into this worktree. This function is only
    ever called from the --leaf-sweep code path, so the rest of this script
    -- including the whole keyword sweep -- works today regardless of
    whether that module exists yet.
    """
    from phoneloop import catalog_ebay
    tree = catalog_ebay.load_tree()
    out = []
    for dept in catalog_ebay.IN_SCOPE:
        for leaf in catalog_ebay.leaves_for(dept.slug, tree=tree):
            out.append((dept.slug, leaf.title, leaf.category_id))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--category", action="append",
                    help="keyword sweep: a phoneloop.catalog slug")
    ap.add_argument("--leaf-sweep", action="store_true",
                    help="_sacat= sweep over catalog_ebay.IN_SCOPE leaves")
    ap.add_argument("--price-from", type=int, default=DEFAULT_PRICE_FROM,
                    help="pounds, --leaf-sweep only")
    ap.add_argument("--price-to", type=int, default=DEFAULT_PRICE_TO,
                    help="pounds, --leaf-sweep only")
    ap.add_argument("--max-pages", type=int, default=0,
                    help="stop after this many page loads (0 = unbounded)")
    ap.add_argument("--target", type=int, default=0)
    ap.add_argument("--direct", action="store_true",
                    help="direct only; refuse the proxy fallback on a block")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--checkpoint", type=Path, default=None)
    ap.add_argument("--fresh", action="store_true")
    args = ap.parse_args()

    if not args.category and not args.leaf_sweep:
        args.category = [c.slug for c in catalog.CATEGORIES]

    cfg = config.load()
    store = MarketStore(cfg.DATA_DIR, max_bytes=cfg.MAX_STORE_BYTES)

    cp_path = args.checkpoint or (cfg.DATA_DIR / "bulk_ebay_checkpoint.json")
    checkpoint: dict = {}
    if cp_path.exists() and not args.fresh:
        checkpoint = json.loads(cp_path.read_text(encoding="utf-8"))
        print(f"resuming: {len(checkpoint.get('done', {}))} shards done, "
              f"{len(checkpoint.get('blocked', {}))} previously blocked")
    done = checkpoint.setdefault("done", {})
    blocked_log = checkpoint.setdefault("blocked", {})

    # (mode, term, category_slug, leaf_category_id, price_lo, price_hi)
    targets: list[tuple[str, str, str, str | None, int, int]] = []
    if args.leaf_sweep:
        for slug, title, cat_id in leaf_targets():
            if not cat_id:
                print(f"   skipping {title!r} - no category_id read yet "
                      "(scripts/refresh_ebay_categories.py has not filled it in)")
                continue
            targets.append(("sacat", cat_id, slug, cat_id,
                            args.price_from, args.price_to))
    for slug in args.category or []:
        cat = catalog.BY_SLUG.get(slug)
        if cat is None:
            print(f"unknown category {slug!r}")
            continue
        for query in cat.ebay_queries:
            targets.append(("nkw", query, slug, None, cat.price_from, cat.price_to))

    cassette = Cassette()   # PHONELOOP_FIXTURES when set, else the repo's fixtures/
    offline = harness.mode() == harness.REPLAY
    print(f"harness: {harness.mode()}"
          + (f", {len(cassette.keys('ebay'))} ebay recordings under {cassette.root}"
             if offline else ""))
    print(f"{len(targets)} shard{'s' if len(targets) != 1 else ''} to sweep "
          f"({'leaf' if args.leaf_sweep else 'keyword'} mode)")

    cap = catalog.result_cap("ebay")
    pages_loaded = 0
    total_unique: set[str] = set()
    started = time.time()

    with contextlib.ExitStack() as stack:
        page = None
        used_proxy = False
        if not offline:
            from playwright.sync_api import sync_playwright
            pw = stack.enter_context(sync_playwright())
            browser, page, used_proxy = open_session(
                pw, headless=args.headless, force_direct=args.direct)
            # A lambda, not `browser.close` bound now: escalation below
            # reassigns `browser` to a fresh proxied one mid-run, and this
            # must close whichever browser is current when the stack unwinds,
            # not the direct one captured at entry.
            stack.callback(lambda: browser.close())
            print(f"egress: {'proxy' if used_proxy else 'DIRECT'}")

        for mode, term, slug, leaf_cat_id, lo, hi in targets:
            cat_unique: set[str] = set()
            for pgn in range(1, MAX_PAGE + 1):
                key = f"{mode}:{term}:{lo}-{hi}:p{pgn}"
                if key in done:
                    continue
                if len(cat_unique) >= cap:
                    print(f"   {term[:24]:<24} p{pgn:<3} cap reached "
                          f"({cap:,}) - stopping this shard")
                    break
                if args.max_pages and pages_loaded >= args.max_pages:
                    print("   page budget spent")
                    return _finish(store, cp_path, checkpoint, total_unique, started)

                template = SEARCH_SACAT if mode == "sacat" else SEARCH_NKW
                url = template.format(term=quote_plus(term) if mode == "nkw" else term,
                                      page=pgn, lo=lo, hi=hi)

                try:
                    raw = recorded(cassette, "ebay", key,
                                   lambda: fetch_page(page, url))
                    pages_loaded += 1
                except harness.MissingRecording:
                    # A gap in the fixtures, not eBay blocking a live
                    # request -- must propagate, not be swallowed as
                    # "blocked" (spec §10, build contract §6.3).
                    raise
                except Exception as exc:                     # noqa: BLE001
                    reason = str(exc)[:80]
                    print(f"   {term[:24]:<24} p{pgn:<3} BLOCKED {reason}")
                    blocked_log[key] = reason
                    if not offline and not used_proxy and not args.direct:
                        proxy = playwright_proxy()
                        if proxy is not None:
                            print("   still on direct egress -> escalating "
                                  "the rest of this run to the residential "
                                  "proxy")
                            # Swap in a fresh proxied browser/page; the
                            # ExitStack callback above closes whichever one
                            # is current when the run ends, not this one.
                            browser.close()
                            browser = pw.chromium.launch(
                                headless=args.headless, channel="chrome",
                                proxy=proxy)
                            page = browser.new_page(locale="en-GB")
                            used_proxy = True
                        else:
                            print("   blocked and no PHONELOOP_PROXY_URL "
                                  "configured -- staying on direct egress")
                    time.sleep(PAUSE_S * 4)
                    break

                listings = parse_rows(raw)
                if not listings:
                    # The saturation test (build contract §2.4/§4): the page
                    # came back with zero rows. This is what "done" as an
                    # empty shard actually means -- never a block.
                    print(f"   {term[:24]:<24} p{pgn:<3} empty - end of results")
                    done[key] = 0
                    break

                share = uk_share(listings)
                rows = rows_for(listings, slug, f"{term}/p{pgn}", leaf_cat_id)
                written = store.record(rows)
                ids = {r["source_id"] for r in rows}
                new = len(ids - cat_unique)
                cat_unique |= ids
                total_unique |= ids
                done[key] = len(ids)
                print(f"   {term[:24]:<24} p{pgn:<3} {len(rows):>3} rows "
                      f"{new:>3} new  {share:.0%} UK (observation only)  "
                      f"(+{written} stored)")

                if catalog.is_saturated(len(cat_unique), "ebay"):
                    print(f"   {term[:24]:<24} shard at/near the {cap:,} cap -- "
                          "likely hiding more listings than this sweep can see")

                # eBay serves the same page forever past the real end; zero
                # new ids twice running means we are there.
                if new == 0:
                    break
                if not offline:
                    time.sleep(PAUSE_S)

            cp_path.parent.mkdir(parents=True, exist_ok=True)
            cp_path.write_text(json.dumps(checkpoint), encoding="utf-8")
            if args.target and len(cat_unique) >= args.target:
                print(f"   target {args.target} reached for {term!r}")

    return _finish(store, cp_path, checkpoint, total_unique, started)


def _finish(store, cp_path, checkpoint, total_unique, started) -> int:
    cp_path.parent.mkdir(parents=True, exist_ok=True)
    cp_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    print(f"\nswept {len(total_unique):,} unique eBay listings in "
          f"{(time.time() - started) / 60:.1f} min")
    print(f"store now holds {store.total_listings('ebay'):,} distinct ebay listings")
    if checkpoint.get("blocked"):
        print(f"{len(checkpoint['blocked'])} shard(s) blocked, not swept -- "
              "left out of 'done' so the next run retries them")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
