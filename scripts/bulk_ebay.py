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
initial homepage load. Decided once at the start of the run -- the same
pattern scripts/record_ebay.py uses -- and escalated again, once, if a page
mid-run comes back blocked while still on direct. What a homepage block
turned out to mean on 2026-08-28 was the BROWSER BUILD, not the address:
from the home IP the bundled Playwright Chromium 148 got the 403 error page
and Chrome for Testing 152 got a 200, while the proxy exit (a US
residential address) was refused as well. So the proxy fallback is a second
chance, not a reliable one, and the binary is chosen by phoneloop.browser
(PHONELOOP_CHROME_EXE in the secrets file). A machine-wide VPN puts direct
egress on a datacenter exit eBay refuses outright; the fix in use is a
per-executable Mullvad split tunnel for that one binary, never the machine.

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

The URL and the harness key both come from `phoneloop.ebay.search_url` /
`.search_key` now, not a copy kept in this file -- scripts/record_ebay.py
imports the same two functions, so a recording it captures satisfies this
script's replay lookup for the identical (mode, term, price band, page)
without either file having to remember to agree (integration review,
2026-08-28: the two used to build these independently and had drifted --
different URL shape, different key format -- which is why a fresh offline
sweep against fixtures/ebay/ still raises `MissingRecording` on the first
shard: those recordings were captured before this fix, under the old shape.
A live recording session with the aligned scripts is what closes that gap;
see this build's unverified[]).

DEEPEN (Tier 2, design spec section 4) is a separate mode, `--deepen`: it
does not sweep anything. It reads the most recent snapshot's eBay rows
already in the store, picks the ones with a real listing `url` and no
`seller_type` yet (never deepened), and merges `phoneloop.ebay_item`'s
item-page parse onto each via `MarketStore.record` -- see `run_deepen`'s
docstring below. This is what search-row fields alone can never supply:
`seller_type`, `listed_at`, brand/MPN/EAN, and the real lifetime sold count.

    python scripts/bulk_ebay.py --category phones
    python scripts/bulk_ebay.py --leaf-sweep --max-pages 400
    python scripts/bulk_ebay.py --category phones --direct   # no proxy fallback
    python scripts/bulk_ebay.py --deepen --deepen-limit 200
    PHONELOOP_HARNESS=replay python scripts/bulk_ebay.py --leaf-sweep --max-pages 3 --fresh
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phoneloop import catalog, config, harness, proxies  # noqa: E402
from phoneloop.browser import launch as launch_browser  # noqa: E402
from phoneloop import board, ebay_item  # noqa: E402
from phoneloop.ebay import (  # noqa: E402
    DEFAULT_PRICE_FROM, DEFAULT_PRICE_TO, band_of, is_browse_redirect,
    niche_key, parse_rows, search_key, search_url, shard_for,
    to_row, uk_share,
)
from phoneloop.ebay import VIEW as ebay_view  # noqa: E402
from phoneloop.harness import Cassette, recorded  # noqa: E402
from phoneloop.market_store import MarketStore  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# The URL template and the harness key are phoneloop.ebay.search_url /
# .search_key now -- shared with scripts/record_ebay.py so the two scripts
# cannot silently drift apart on request shape again (see phoneloop/ebay.py's
# module docstring for the drift this integration pass found and fixed).
# _ipg=240 is the largest page eBay serves. _pgn is the page. _udlo/_udhi are
# the price band, in pounds -- LH_LocatedIn is gone (see module docstring
# above). DEFAULT_PRICE_FROM/TO (the spec's default retail band, §3 knob 3)
# are used only for --leaf-sweep, which carries no per-category band the way
# phoneloop.catalog.Category does.

PAUSE_S = 4.0
NAV_TIMEOUT_MS = 75000
# Consecutive item-page failures that end a deepen run. See run_deepen.
MAX_CONSECUTIVE_FAILURES = 25
# ceil(catalog.result_cap("ebay") / 240) -- the page budget a single shard
# could ever need before it is either saturated or exhausted.
MAX_PAGE = 42


def pages_for_shard(limit: int) -> range:
    """Page numbers one shard may load. `limit` is --pages-per-shard; 0 means
    the cap-derived MAX_PAGE. Distinct from --max-pages, which budgets the
    WHOLE run and would let a single 10,000-result hub spend it all before
    the second shard started -- a recording session that wants three pages
    of every leaf needs this one, not that one."""
    return range(1, (limit or MAX_PAGE) + 1)

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
    browser = launch_browser(pw, headless=headless, proxy=None)
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
    browser = launch_browser(pw, headless=headless, proxy=proxy)
    page = browser.new_page(locale="en-GB")
    body = warm_up(page)
    if is_blocked(body, page.url):
        browser.close()
        raise RuntimeError(
            "blocked on both direct and the residential proxy -- eBay "
            "refused the homepage load either way")
    return browser, page, True


class RedirectedOffResults(RuntimeError):
    """eBay answered a search URL with its /b/ browse node instead of the
    results page. Not a block (HTTP 200, no captcha) and not an empty shard
    (the browse node of a hub lists nothing while the category holds a
    million listings) -- a third outcome that must stay visible. Measured
    2026-08-28 on every category-only URL shape without _fsrp=1; see
    phoneloop.ebay.search_url."""


def fetch_page(page, url: str) -> list[dict]:
    """One live page load. Raises on a captcha or an error page -- the
    caller is what decides whether that is a shard-ending block or a cue to
    escalate to the proxy; this function only reports what it saw."""
    page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="load")
    page.wait_for_timeout(2500)
    body = page.inner_text("body")
    if is_blocked(body, page.url):
        raise RuntimeError("blocked (captcha or error page)")
    if is_browse_redirect(page.url):
        raise RedirectedOffResults(f"redirected off the results page to {page.url}")
    return page.evaluate(_ROW_JS)


def rows_for(listings, category: str, shard: str,
            leaf_category_id: str | None) -> list[dict]:
    return [to_row(listing, category=category, shard=shard,
                   leaf_category_id=leaf_category_id)
           for listing in listings]


# The band-aware shard and niche key live in phoneloop.ebay so that
# phoneloop.board can group on exactly the key this sweep writes; they are
# imported at the top of this module and re-exported here only so the sweep
# reads in one place.


def fetch_item(page, url: str) -> dict:
    """One live item-page load -- identical shape to
    scripts/record_ebay.py's `fetch_item` (same fields, same timeouts) so a
    recording captured by either satisfies the other's replay."""
    page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="load")
    page.wait_for_timeout(3000)
    body = page.inner_text("body")
    if is_blocked(body, page.url):
        raise RuntimeError(f"blocked fetching item page {url}")
    return {"text": body, "html": page.content(), "url": page.url}


def observed_on(row: dict) -> date | None:
    """The date a stored row was observed, for resolving a yearless date.

    Prefers the store's own `date` partition, falls back to `seen_at`, and
    returns None rather than today's date when neither is present -- a
    missing reference must make the eta unknown, not silently measured from
    the wrong day.
    """
    d = row.get("date")
    if isinstance(d, date) and not isinstance(d, datetime):
        return d
    seen = row.get("seen_at")
    if isinstance(seen, datetime):
        return seen.date()
    if isinstance(d, datetime):
        return d.date()
    return None


def rows_to_deepen(store: MarketStore, *, limit: int) -> list[dict]:
    """The most recent snapshot's eBay rows that carry a real item `url` and
    have never been deepened (`seller_type` still unset) -- capped at
    `limit`, because a live run spends one page load per row and Tier 2 is a
    bounded daily budget (design spec section 4), not a full re-crawl.

    Reads via `MarketStore.snapshot_dates` / `.rows_on` -- the same public
    pair `phoneloop.board._latest_rows` reads the board's current population
    from -- rather than a new store method, so this stays a caller of
    MarketStore's existing surface instead of growing a sibling stream's
    file for one script's convenience.

    Ordered so the budget lands where the board reads it. Tier 2 buys a few
    hundred page loads against a population of ~30,000, and the store's own
    row order scatters them: on 2026-08-28, 99 item pages were fetched and
    min_margin / price_band / delivery still read `?` at denom=0 cov=0%,
    because almost none of the 99 belonged to a row the board reports.

    The rows a board product row is made of are the ones that GROUP -- several
    sellers listing the same item. Those are deepened first, largest group
    first, via the same board.group_products_in_leaf the board itself uses, so
    the two cannot drift into enriching one set and reporting another.

    Ordering only. The seller_type and url filters are unchanged, the limit
    still caps the batch, and ungrouped rows keep their place behind the
    grouped ones rather than being dropped -- a store where nothing groups
    must still deepen a full batch, or a bad grouping would silently starve
    Tier 2 instead of failing.
    """
    dates = store.snapshot_dates(source="ebay")
    if not dates:
        return []
    rows = store.rows_on(dates[-1], source="ebay")
    candidates = [r for r in rows if r.get("url") and not r.get("seller_type")]
    if not candidates:
        return []

    by_leaf: dict[str, list[dict]] = {}
    for r in candidates:
        leaf = r.get("leaf_category_id")
        if leaf:
            by_leaf.setdefault(leaf, []).append(r)

    grouped: list[list[dict]] = []      # one list per product group
    seen: set[str] = set()
    for leaf, leaf_rows in by_leaf.items():
        groups, _ = board.group_products_in_leaf(leaf_rows)
        for prod_rows in groups.values():
            if len(prod_rows) < 2:
                continue            # a group of one is not a product
            grouped.append(prod_rows)
            seen.update(r["source_id"] for r in prod_rows)

    # Biggest groups first, but ROUND-ROBIN across them rather than draining
    # each in turn. Sorting flat by group size empties the whole budget into
    # the single largest group: measured live 2026-08-28, a 40-page budget
    # went entirely into one 48-listing group of vintage phone spares, every
    # row came back seller_type='private', and not one business knob moved.
    # A product row needs only ONE business listing before min_margin,
    # price_band and delivery can read, so one observation each of forty
    # products is worth far more than forty of one.
    grouped.sort(key=len, reverse=True)
    ordered: list[dict] = []
    for i in range(max((len(g) for g in grouped), default=0)):
        for prod_rows in grouped:
            if i < len(prod_rows):
                ordered.append(prod_rows[i])

    # Ungrouped rows keep their place behind the grouped ones -- never
    # dropped, so a store where nothing groups still deepens a full batch
    # instead of silently starving Tier 2.
    ordered += [r for r in candidates if r["source_id"] not in seen]
    return ordered[:limit]


def run_deepen(store: MarketStore, cassette: Cassette, *, offline: bool,
               limit: int, headless: bool, direct: bool,
               max_seconds: float = 0,
               clock: Callable[[], float] = time.monotonic) -> int:
    """Tier 2 (design spec section 4): enrich a bounded batch of already-swept
    eBay listings with what only the item page carries -- `seller_type`,
    `listed_at`, brand/MPN/EAN, the real lifetime sold count -- none of
    which a search row ever shows (phoneloop/ebay_item.py's module
    docstring). Without this, those columns can never populate from any
    shipped script (integration review, 2026-08-28, blocking finding #2) and
    every knob that depends on them reads `?` forever.

    For each candidate from `rows_to_deepen`: fetch (or replay) the item page
    under the SAME harness name scripts/record_ebay.py records it under --
    `f"item:{url}"` -- parse it with `phoneloop.ebay_item.parse_item`, and
    merge `to_row_updates` onto the sweep row's own `extra` (passed as
    `sweep_extra` so the merge -- not a wholesale overwrite -- happens
    inside `to_row_updates` itself; see that function's docstring for the
    clobber this closes). The merged row is written back via
    `MarketStore.record` with `seen_at` cleared to `None` so the store
    stamps the actual deepen time -- eBay is an always-snapshot source
    (market_store.py), so this lands as a new, later observation of the same
    listing rather than mutating the sweep row in place, and reliably
    outranks it in `rows_on`'s `ORDER BY seen_at DESC`.

    CAVEAT (unverified, flagged rather than silently assumed): `board.py`'s
    `_latest_rows` reads a single calendar date -- `rows_on(dates[-1])`.
    This function is meant to run in the same daily invocation as the
    sweep, immediately after it (scripts/run_daily.cmd's [2/3]), so the
    deepened rows land on the SAME UTC date as the sweep they enrich. Run
    it a calendar day or more after its sweep instead, and the deepened
    rows alone become "the latest snapshot" -- the hundreds of un-deepened
    listings that sweep also wrote would silently drop out of the board
    until the next sweep. Not exercised here; a same-day production run
    never hits it.

    A missing item-page recording for a candidate this function actually
    asked for propagates as `MissingRecording`, same as the sweep's own
    handling -- these are targeted lookups (rows this run chose to deepen),
    not a scan of "whatever happens to be recorded", so a gap here is a real
    gap, not a normal empty result (contracts section 6.3).
    """
    candidates = rows_to_deepen(store, limit=limit)
    print(f"{len(candidates)} listing{'s' if len(candidates) != 1 else ''} "
          f"to deepen (latest snapshot, capped at {limit})")
    if not candidates:
        return 0

    deepened = 0
    # A sustained block must stop the run, not be absorbed one item at a
    # time. Each failure below is caught and skipped, which is right for an
    # item eBay happens to refuse -- and wrong for an eBay that has started
    # refusing everything. Without this, a budget of several thousand burns
    # itself down fetching nothing, prints a BLOCKED line per item, and
    # finishes reporting "deepened 0/5000" after hours of it.
    #
    # 25 is deliberately well above the worst clean run measured: the
    # 2026-08-30 nightly deepened 200 of 200 with no failure at all. A real
    # streak of 25 is not bad luck.
    consecutive_failures = 0
    stopped_early = ""
    # A wall-clock budget, for the same reason as the breaker: deepen runs
    # BEFORE the board, the reports and the publish step. On Windows the
    # task's 3h ExecutionTimeLimit killed the whole chain in the middle of a
    # 5,000-item deepen on most nights after 2026-09-08, so no report was
    # written or published although every sweep had landed. A budget ends
    # deepen cleanly and leaves the rest of the night to run. 0 = no budget.
    deadline = clock() + max_seconds if max_seconds > 0 else None
    with contextlib.ExitStack() as stack:
        page = None
        if not offline:
            from playwright.sync_api import sync_playwright
            pw = stack.enter_context(sync_playwright())
            browser, page, used_proxy = open_session(
                pw, headless=headless, force_direct=direct)
            stack.callback(lambda: browser.close())
            print(f"egress: {'proxy' if used_proxy else 'DIRECT'}")

        attempted = 0
        for row in candidates:
            if deadline is not None and clock() >= deadline:
                stopped_early = (
                    f"the {max_seconds / 60:.0f}-minute deepen budget ran out. "
                    "Stopped so that the board, the reports and the publish "
                    "step still run tonight.")
                break
            attempted += 1
            url = row["url"]
            try:
                data = recorded(cassette, "ebay_item", f"item:{url}",
                                lambda u=url: fetch_item(page, u))
            except harness.MissingRecording:
                # A gap in the fixtures for a URL this run specifically
                # targeted -- must propagate, not be swallowed (module
                # docstring above; contracts section 6.3).
                raise
            except Exception as exc:                     # noqa: BLE001
                print(f"   {url} -> BLOCKED {str(exc)[:80]}")
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    stopped_early = (
                        f"{consecutive_failures} item pages failed in a row. "
                        "Stopped rather than spend the rest of the budget on "
                        "a source that is refusing this session.")
                    break
                continue
            consecutive_failures = 0

            item = ebay_item.parse_item(data.get("text", ""),
                                        html=data.get("html", ""),
                                        url=data.get("url") or url)
            if item is None:
                print(f"   {url} -> no parseable item id, skipped")
                continue
            # The item page states delivery DATES with no year, so turning
            # them into a day count needs a reference date. That reference is
            # the row's own observation date, not the clock: an eta belongs
            # to the observation that carries it, and using "now" would make
            # a replayed recording answer differently every day it is
            # replayed -- the exact drift the harness exists to remove. In
            # the daily pipeline (sweep -> deepen -> board, same invocation)
            # the two are the same day anyway.
            updates = ebay_item.to_row_updates(
                item, sweep_extra=row.get("extra"), today=observed_on(row))
            if not updates:
                print(f"   {url} -> item page carried nothing new")
                continue

            merged = dict(row)
            merged.update(updates)
            merged["seen_at"] = None
            written = store.record([merged])
            deepened += 1
            print(f"   {url} -> {sorted(updates)} (+{written} stored)")

    print(f"\ndeepened {deepened}/{len(candidates)} listings")
    if stopped_early:
        # Exit 0 on purpose. Every row deepened before the stop is real and
        # already in the store, the board is still worth building on it, and
        # a non-zero here would abort the rest of the nightly pipeline. The
        # shortfall is not hidden: it prints here, and the board's own deepen
        # coverage figure carries its denominator, so a short night reads as
        # a short night rather than as a market with no margins.
        print(f"STOPPED EARLY: {stopped_early}")
        # Three different numbers, and merging any two of them overstates
        # something. `attempted - deepened` were tried and refused;
        # `len(candidates) - attempted` were never reached at all.
        print(f"{attempted - deepened} attempted and refused, "
              f"{len(candidates) - attempted} never attempted. "
              "Both keep their place in tomorrow's batch.")
    return 0


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


def default_checkpoint_path(data_dir: Path, today: date | None = None) -> Path:
    """Where the sweep checkpoints when --checkpoint is not given: one file
    PER UTC DAY. scripts/run_daily.cmd runs without --fresh so a failed run
    resumes, but a single undated file carried yesterday's done-set into
    today, and the daily sweep would have skipped every shard it finished
    the day before -- Tier 3 differences consecutive daily observations of
    the SAME population, so a skipped shard is a hole in the difference,
    not a saving. Same-day re-run: resumes. New day: starts clean. UTC
    matches the store's snapshot dates (phoneloop.snapshots)."""
    d = today or datetime.now(UTC).date()
    return data_dir / f"bulk_ebay_checkpoint_{d:%Y-%m-%d}.json"


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
                    help="stop after this many page loads IN TOTAL (0 = unbounded)")
    ap.add_argument("--pages-per-shard", type=int, default=0,
                    help="cap page loads PER SHARD (0 = MAX_PAGE, the cap-derived "
                         "maximum); use this, not --max-pages, to sample every leaf")
    ap.add_argument("--target", type=int, default=0)
    ap.add_argument("--direct", action="store_true",
                    help="direct only; refuse the proxy fallback on a block")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--checkpoint", type=Path, default=None)
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--deepen", action="store_true",
                    help="Tier 2: enrich already-swept listings from their "
                         "item pages, instead of sweeping (see run_deepen)")
    ap.add_argument("--deepen-limit", type=int, default=200,
                    help="max listings to deepen per run, --deepen only")
    ap.add_argument("--deepen-max-minutes", type=float, default=0,
                    help="wall-clock budget for --deepen; stops cleanly (exit "
                         "0) when it runs out. 0 = no budget")
    args = ap.parse_args()

    if not args.category and not args.leaf_sweep and not args.deepen:
        args.category = [c.slug for c in catalog.CATEGORIES]

    cfg = config.load()
    store = MarketStore(cfg.DATA_DIR, max_bytes=cfg.MAX_STORE_BYTES)

    if args.deepen:
        cassette = Cassette()
        offline = harness.mode() == harness.REPLAY
        print(f"harness: {harness.mode()}"
              + (f", {len(cassette.keys('ebay_item'))} ebay_item recordings "
                 f"under {cassette.root}" if offline else ""))
        return run_deepen(store, cassette, offline=offline,
                          limit=args.deepen_limit, headless=args.headless,
                          direct=args.direct,
                          max_seconds=args.deepen_max_minutes * 60)

    cp_path = args.checkpoint or default_checkpoint_path(cfg.DATA_DIR)
    checkpoint: dict = {}
    if cp_path.exists() and not args.fresh:
        checkpoint = json.loads(cp_path.read_text(encoding="utf-8"))
        print(f"resuming: {len(checkpoint.get('done', {}))} shards done, "
              f"{len(checkpoint.get('blocked', {}))} previously blocked")
    done = checkpoint.setdefault("done", {})
    blocked_log = checkpoint.setdefault("blocked", {})

    # The shape of tonight's sweep, recorded beside what it swept.
    #
    # `done`'s keys carry the leaf, the band and the page, so a change to any
    # of those three is already visible to phoneloop.footprint. The results
    # VIEW is not in them, and it changes what a page returns: the same leaf,
    # band and page came back with a seller line on 97.3% of rows in list
    # view and 3.1% in gallery (phoneloop/ebay.py's _dmd note). A night swept
    # under a different view is a different population under identical shard
    # keys, and without this block the guard would certify that pair as
    # comparable.
    #
    # `pages_per_shard` is what was ASKED for, and is recorded next to the
    # per-leaf depths that say what happened. A leaf that ran out of results
    # at page 2 under a setting of 10 is a very different night from the same
    # depth under a setting of 3, and only this field tells them apart.
    checkpoint["sweep"] = {
        "view": ebay_view,
        "pages_per_shard": args.pages_per_shard,
        "price_band": f"{args.price_from}-{args.price_to}",
    }

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
            for pgn in pages_for_shard(args.pages_per_shard):
                key = search_key(mode, term, lo, hi, pgn)
                if key in done:
                    continue
                if len(cat_unique) >= cap:
                    print(f"   {term[:24]:<24} p{pgn:<3} cap reached "
                          f"({cap:,}) - stopping this shard")
                    break
                if args.max_pages and pages_loaded >= args.max_pages:
                    print("   page budget spent")
                    return _finish(store, cp_path, checkpoint, total_unique, started)

                url = search_url(mode, term, lo, hi, pgn)

                try:
                    raw = recorded(cassette, "ebay", key,
                                   lambda: fetch_page(page, url))
                    pages_loaded += 1
                except harness.MissingRecording:
                    # A gap in the fixtures, not eBay blocking a live
                    # request -- must propagate, not be swallowed as
                    # "blocked" (spec §10, build contract §6.3).
                    raise
                except RedirectedOffResults as exc:
                    # Not an egress problem, so no proxy escalation: the
                    # URL shape is wrong for this category. Logged as
                    # blocked (never done=0) so the next run retries it.
                    reason = str(exc)[:80]
                    print(f"   {term[:24]:<24} p{pgn:<3} REDIRECTED {reason}")
                    blocked_log[key] = reason
                    break
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
                            browser = launch_browser(
                                pw, headless=args.headless, proxy=proxy)
                            page = browser.new_page(locale="en-GB")
                            used_proxy = True
                        else:
                            print("   blocked and no PHONELOOP_PROXY_URL "
                                  "configured -- staying on direct egress")
                    time.sleep(PAUSE_S * 4)
                    break

                if not raw:
                    # The saturation test (build contract §2.4/§4): the page
                    # itself came back with zero row elements. This is what
                    # "done" as an empty shard actually means -- never a
                    # block. Checked against `raw` (the DOM rows the page JS
                    # matched), not the post-parse `listings` -- a page that
                    # has real rows but whose rows all fail to yield a price
                    # is a parse problem, not "end of results", and must not
                    # be recorded as one.
                    print(f"   {term[:24]:<24} p{pgn:<3} empty - end of results")
                    done[key] = 0
                    break

                listings = parse_rows(raw)

                share = uk_share(listings)
                rows = rows_for(listings, slug, shard_for(term, lo, hi, pgn),
                                leaf_cat_id)
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

                # eBay serves the same page forever past the real end; a
                # page with zero new ids means we are there. (Inherited from
                # master's bulk_ebay.py, which this stream rewrote: the old
                # comment there claimed "twice running" but the code has
                # always broken on the first zero-new page -- fixing the
                # comment to match, not changing the behaviour.)
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
