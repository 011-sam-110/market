"""Record eBay UK's category tree once, then enumerate leaves offline.

What gets fetched, and why those pages specifically
-----------------------------------------------------
1. `https://www.ebay.co.uk/n/all-categories` -- once. Its "Advanced search"
   category `<select>` embeds every top-level department's real, classic
   numeric category id (the id space `_sacat=` filters on) as
   `<option value="...">`. Measured 2026-08-27, all 34 rows, including the
   nine this project cares about:

       11700  Home, Furniture & DIY        159912  Garden & Patio
        1281  Pet Supplies                    888  Sporting Goods
      131090  Vehicle Parts & Accessories   15032  Mobile Phones & Communication
       14339  Crafts                        26395  Health & Beauty
         220  Toys & Games                  11450  Clothes, Shoes & Accessories (excluded)

   Vehicle Parts & Accessories is 131090. A guess would very likely have
   reached for something in the low thousands, the way most of the other
   department ids run -- which is exactly why this project's rule is "read it
   off a real page", not "look plausible".

2. Each in-scope department's own browse page,
   `https://www.ebay.co.uk/b/x/<category_id>` -- the slug text before the id
   is cosmetic; eBay resolves purely off the numeric id and 301s to its
   canonical `/b/bn_<n>` URL regardless what the slug says (measured). Its
   "Shop by category" panel (`section.brw-category-nav`) lists the
   department's immediate subcategories as links to `/b/bn_<n>` -- eBay's
   newer *browse-node* id, used for navigation, and NOT the classic numeric
   id `leaf_category_id` is supposed to hold (contracts doc section 1.1,
   column 34).

3. Each subcategory's own `/b/bn_<n>` page, up to `--max-leaves` per
   department -- one more live fetch per leaf, because that is where the
   classic numeric id for THAT subcategory is embedded (inline in a
   server-rendered JSON blob, several shapes measured and tried in priority
   order -- see `_CATEGORY_ID_PATTERNS`). There is no bulk endpoint that
   returns many subcategories' numeric ids in one response that this project
   found without either JS interaction or reverse-engineering an internal
   GraphQL API, so this is genuinely one HTTP round trip per leaf.

That third step is the expensive one and is capped on purpose. A full
recursive crawl to eBay's true taxonomy leaves (several more levels down)
would run to hundreds of live fetches across nine departments; this instead
resolves a bounded sample per department, live, real, and reports exactly how
many it got. `leaves_for()` therefore returns "every childless category in
the tree we actually recorded", which for most departments is a
second-level browse category, not a claim that eBay has no third level under
it.

    python scripts/refresh_ebay_categories.py                 # live, direct
    python scripts/refresh_ebay_categories.py --proxy          # live, via the gateway
    python scripts/refresh_ebay_categories.py --offline        # replay only, no browser
    python scripts/refresh_ebay_categories.py --max-leaves 3   # smaller, faster run
"""

from __future__ import annotations

import argparse
import contextlib
import html
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phoneloop import harness, proxies  # noqa: E402
from phoneloop.catalog_ebay import IN_SCOPE, TREE_PATH  # noqa: E402
from phoneloop.harness import Cassette, recorded  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ALL_CATEGORIES_URL = "https://www.ebay.co.uk/n/all-categories"
DEPARTMENT_URL = "https://www.ebay.co.uk/b/x/{category_id}"
LEAF_URL = "https://www.ebay.co.uk/b/{bn_id}"

MAX_LEAVES_PER_DEPARTMENT = 5
NAV_TIMEOUT_MS = 75000
PAUSE_S = 2.0

# eBay serves a generic "SORRY / Something went wrong" error page, or a
# splashui captcha, when a session is rate-limited. Both were measured this
# way while building this script: /sch/i.html blocked a direct connection
# after roughly six requests in one session, while /b/ pages kept working.
# Checked on every fetch so a block reads as "blocked", never as "zero
# subcategories" or "no numeric id on this page".
_BLOCK_MARKERS = ("Something went wrong on our end", "splashui/captcha",
                  "Security measure")

# Three shapes of the same server-rendered fact, tried in order because
# eBay's page composition varies per request (measured: the richest form,
# with categoryName and the ancestor chain, was present on some subcategory
# fetches and absent on others of the same kind). The page's OWN id is
# always the first categoryId in whichever blob is present -- ancestors and
# children come after it in every sample captured.
_CATEGORY_ID_PATTERNS = tuple(re.compile(p) for p in (
    r'"categoryId":"(\d+)","categoryName":"[^"]*"',
    r'"browseEntityContext":\{"categoryId":"(\d+)"',
    r'"category":\{"categoryId":"(\d+)"',
))
_H1_RE = re.compile(r'<h1[^>]*>([^<]+)</h1>')
_OPTION_RE = re.compile(r'<option value="(\d+)">([^<]+)</option>')

# The `brw-region--left` sidebar carries SEVERAL sections sharing the exact
# class `brw-category-nav` -- measured on Vehicle Parts & Accessories: "My
# Garage", "You're Protected", "Tyre Fitting Service", "Certified Recycled"
# and "Sell on eBay" widgets sit alongside the real "Shop by category" panel,
# and "Tyre Fitting Service" links to a real bn_ id that is a booking flow,
# not a product category ("Tyres, fitting, easy!" -> bn_7121209158). A first
# version of this parser matched every such section unscoped and picked that
# up as a fake "leaf". `_brw_sections` slices the page at each section's own
# boundary so only the one(s) actually titled "Shop by category" are scanned
# -- a department can genuinely have two (a curated flat tile row AND the
# full nested list, both real; `_dedupe` merges them).
_BRW_SECTION_MARKER = 'class="brw-category-nav"'
_BRW_TITLE_RE = re.compile(
    r'<h2\s+class="textual-display brw-category-nav__title">(?:<span[^>]*>)?([^<]*)'
)
_BRW_MAX_SCAN = 20000  # the last section on a page has no next-section bound

# Within a genuine "Shop by category" section, one link is the ANCESTOR one
# level up (e.g. "Sports, Hobbies & Leisure" above Crafts) rather than a
# subcategory of the current department -- recognisable because its own `<a>`
# is immediately followed by a nested `<section class="brw-category-nav__
# nested-items">`, a shape no genuine leaf link has (the current department
# itself appears in that nested section as a bold `<span>`, never an `<a>`,
# so it is excluded from the start).
_SUBCAT_RE = re.compile(
    r'<a\s+class="[^"]*\bbrw-category-nav__link\b[^"]*"\s+'
    r'href="(https://www\.ebay\.co\.uk/b/(bn_\d+))"[^>]*>([^<]*)<'
    r'(?!/a></span><section class="brw-category-nav__nested-items")'
)

# eBay serves a SECOND department-page template, live, no A/B cookie or query
# param controlling which one shows up -- measured 2026-08-27: Home Furniture &
# DIY, Garden & Patio, Vehicle Parts & Accessories and Crafts all rendered
# `section.brw-category-nav` (above); Pet Supplies, Sporting Goods, Mobile
# Phones & Communication, Health & Beauty and Toys & Games rendered THIS
# instead, and `_SUBCAT_RE` found nothing on any of the five -- not a timing
# problem, a different template. Its "Shop by category" panel is one or more
# `su-category-tree` blocks: sometimes a single tree with the department's own
# subcategories nested under an ANCESTOR link (that ancestor -- e.g. "Home &
# Garden" above "Pet Supplies" -- is not a subcategory and is recognisable
# because its own `<a>` is immediately followed by a nested
# `<ul class="su-subcategory-list">`, a shape no genuine leaf link has),
# sometimes many single-item trees side by side (Health & Beauty, one `<a>`
# per `su-category-tree` div). The heading text "Shop by category" appears
# TWICE on every one of these pages -- a second, unrelated
# "dp-browse-destinations-module" further down the page reuses it -- so only
# the first occurrence is used as the scan's start, and the scan stops at
# whichever comes first: the nested list's own close, or the next
# `dp-category-nav-module__title` heading (the following "Shop by brand" /
# "Shop by condition" / etc. panel, whose links are not subcategories).
_SU_CATEGORY_HEADING = ">Shop by category</h2>"
_SU_NESTED_CLOSE = "</ul></li></ul></div></div>"
_SU_NEXT_TITLE_MARKER = "dp-category-nav-module__title"
_SU_MAX_SCAN = 20000  # neither boundary found (never measured) -- a bound, not a guess
_SU_LEAF_LINK_RE = re.compile(
    r'<a\s+href="https://www\.ebay\.co\.uk/(b/bn_\d+)"[^>]*><span>([^<]*)</span></a>'
    r'(?!<ul class="su-subcategory-list">)'
)


class Blocked(RuntimeError):
    """eBay answered with a challenge or an error page instead of content."""


# --- pure parsing: no network, exercised directly by tests -----------------

def parse_top_level_ids(html_text: str) -> dict[str, str]:
    """Department title -> classic numeric category id, from the
    "Advanced search" `<select>` on /n/all-categories."""
    return {html.unescape(title): cat_id
            for cat_id, title in _OPTION_RE.findall(html_text)}


def _dedupe(pairs) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for bn_id, title in pairs:
        if bn_id in seen:
            continue
        seen.add(bn_id)
        out.append({"title": html.unescape(title).strip(), "bn_id": bn_id})
    return out


def _brw_sections(html_text: str) -> list[str]:
    """Every `<section class="brw-category-nav">` block's own content,
    sliced at the next such section's start (or a bound for the last one),
    so a genuine subcategory panel is never blurred with the widget after
    it -- see the comment above `_BRW_SECTION_MARKER`."""
    starts = [m.start() for m in re.finditer(re.escape(_BRW_SECTION_MARKER), html_text)]
    return [html_text[start:(starts[i + 1] if i + 1 < len(starts) else start + _BRW_MAX_SCAN)]
            for i, start in enumerate(starts)]


def _parse_brw_subcats(html_text: str) -> list[dict]:
    """`section.brw-category-nav` template -- see the comment above
    `_BRW_SECTION_MARKER` for why this is scoped per-section rather than run
    over the whole page. De-duplicated: the panel has repeated a bn_id under
    more than one heading on at least one department page measured."""
    pairs = []
    for section in _brw_sections(html_text):
        m = _BRW_TITLE_RE.search(section)
        if not m or m.group(1).strip() != "Shop by category":
            continue
        pairs.extend((bn_id, title) for _href, bn_id, title in _SUBCAT_RE.findall(section))
    return _dedupe(pairs)


def _parse_su_subcats(html_text: str) -> list[dict]:
    """`su-category-tree` template -- see the module-level comment above
    `_SU_LEAF_LINK_RE` for what each boundary is doing and why."""
    start = html_text.find(_SU_CATEGORY_HEADING)
    if start == -1:
        return []
    start += len(_SU_CATEGORY_HEADING)
    ends = [i for i in (
        html_text.find(_SU_NESTED_CLOSE, start),
        html_text.find(_SU_NEXT_TITLE_MARKER, start),
    ) if i != -1]
    end = min(ends) if ends else start + _SU_MAX_SCAN
    scope = html_text[start:end]
    return _dedupe((bn_path.split("/", 1)[1], title)
                    for bn_path, title in _SU_LEAF_LINK_RE.findall(scope))


def parse_department_subcats(html_text: str) -> list[dict]:
    """[{"title", "bn_id"}] from one department's "Shop by category" panel.

    eBay serves at least two different page templates for this panel
    (measured 2026-08-27 -- see the comment above `_SU_LEAF_LINK_RE`). The
    `brw-category-nav` parser runs first because it is the simpler, more
    literal shape; if it finds nothing this falls back to the `su-category-tree`
    shape rather than reporting zero subcategories on a page that has some.
    """
    subcats = _parse_brw_subcats(html_text)
    if subcats:
        return subcats
    return _parse_su_subcats(html_text)


def parse_leaf_category_id(html_text: str) -> str | None:
    """The classic numeric id a subcategory's own page embeds for itself, or
    None if none of the known shapes matched -- never a guess."""
    for pattern in _CATEGORY_ID_PATTERNS:
        m = pattern.search(html_text)
        if m:
            return m.group(1)
    return None


def parse_leaf_title(html_text: str) -> str | None:
    m = _H1_RE.search(html_text)
    return html.unescape(m.group(1)).strip() if m else None


def check_blocked(html_text: str, page_url: str) -> None:
    if "captcha" in page_url:
        raise Blocked(f"captcha redirect: {page_url}")
    for marker in _BLOCK_MARKERS:
        if marker in html_text:
            raise Blocked(f"blocked - saw {marker!r}")


# --- live fetch: only ever called from record/live mode, via recorded() ----

def scrape(page, url: str, *, wait_for: str | None = None) -> str:
    """Navigate and return the rendered HTML.

    `wait_for` names one or more comma-separated CSS selectors to wait on
    before capturing, used for the department pages' "Shop by category"
    panel -- a client-hydrated module, so a fixed sleep alone is not a
    reliable signal that it has rendered. Two different selectors are passed
    for that panel (see `parse_department_subcats`) because eBay serves two
    different page templates for it and which one a given department gets is
    not something this script controls. Absence of both selectors after the
    timeout is not treated as a failure here -- it is passed through to the
    parser, which reports "0 subcats seen" honestly rather than this function
    guessing why.
    """
    page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="load")
    if wait_for:
        with contextlib.suppress(Exception):
            page.wait_for_selector(wait_for, timeout=10000)
    page.wait_for_timeout(1500)
    html_text = page.content()
    check_blocked(html_text, page.url)
    return html_text


def playwright_proxy() -> dict | None:
    url = proxies.proxy_url()
    if not url:
        return None
    parts = urlsplit(url)
    return {"server": f"http://{parts.hostname}:{parts.port}",
            "username": parts.username, "password": parts.password}


# --- orchestration -----------------------------------------------------

def build_tree(cassette: Cassette, page, *, max_leaves: int,
               harness_mode: str) -> tuple[dict, list[tuple[str, int, int]]]:
    """The whole recorded tree, plus a (department, subcats_seen,
    leaves_resolved) row per department for the report.

    `page` is None in replay -- every network-shaped call below goes through
    `recorded()`, which never calls `scrape` in that mode, so this function
    runs with no browser at all when replaying.
    """
    def rec(kind: str, name: str, produce):
        return recorded(cassette, kind, name, produce, harness_mode=harness_mode)

    all_cats_html = rec("ebay_categories", "all-categories",
                        lambda: scrape(page, ALL_CATEGORIES_URL,
                                       wait_for="select option[value='0']"))
    top_ids = parse_top_level_ids(all_cats_html)

    departments: dict[str, dict] = {}
    report: list[tuple[str, int, int]] = []

    for dept in IN_SCOPE:
        measured = top_ids.get(dept.title)
        if measured and measured != dept.category_id:
            print(f"  ! {dept.title}: /n/all-categories now says {measured}, "
                  f"catalog_ebay.IN_SCOPE says {dept.category_id} - "
                  "IN_SCOPE needs updating")

        try:
            dept_html = rec("ebay_categories", f"dept-{dept.slug}",
                            lambda d=dept: scrape(
                                page, DEPARTMENT_URL.format(category_id=d.category_id),
                                wait_for="section.brw-category-nav, "
                                         "div.dp-category-nav-module__group"))
        except Blocked as exc:
            print(f"  ! {dept.title:<32} BLOCKED fetching the department page: {exc}")
            departments[dept.slug] = {
                "title": dept.title, "category_id": dept.category_id,
                "leaves": [], "blocked": True,
            }
            report.append((dept.title, 0, 0))
            continue

        subcats = parse_department_subcats(dept_html)
        leaves: list[dict] = []
        for sub in subcats[:max_leaves]:
            try:
                leaf_html = rec("ebay_categories", f"leaf-{sub['bn_id']}",
                                lambda s=sub: scrape(page, LEAF_URL.format(bn_id=s["bn_id"])))
            except Blocked as exc:
                print(f"    ! {sub['title']:<30} BLOCKED: {exc}")
                continue
            leaf_id = parse_leaf_category_id(leaf_html)
            if not leaf_id:
                print(f"    ! {sub['title']:<30} no numeric category id found "
                      "on its own page (none of the known shapes matched)")
                continue
            leaves.append({
                "category_id": leaf_id,
                "title": parse_leaf_title(leaf_html) or sub["title"],
            })
            if page is not None:
                time.sleep(PAUSE_S)

        departments[dept.slug] = {
            "title": dept.title, "category_id": dept.category_id,
            "leaves": leaves,
        }
        report.append((dept.title, len(subcats), len(leaves)))
        print(f"  {dept.title:<32} {len(subcats):>3} subcats seen, "
              f"{len(leaves):>2} leaves resolved to a real numeric id")

    tree = {
        "_generated_at": datetime.now(UTC).isoformat(),
        "_source": {
            "all_categories": ALL_CATEGORIES_URL,
            "department_page": DEPARTMENT_URL,
            "leaf_page": LEAF_URL,
        },
        "departments": departments,
    }
    return tree, report


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path,
                    default=Path(os.environ.get("PHONELOOP_FIXTURES",
                                                str(harness.DEFAULT_ROOT))))
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--max-leaves", type=int, default=MAX_LEAVES_PER_DEPARTMENT)
    ap.add_argument("--offline", action="store_true",
                    help="force replay regardless of PHONELOOP_HARNESS; never "
                         "opens a browser")
    ap.add_argument("--headless", action="store_true",
                    help="eBay's browse pages tolerate this better than search "
                         "does, but headed is still the default for consistency")
    ap.add_argument("--proxy", action="store_true",
                    help="route through the residential gateway from the "
                         "start, rather than direct-first")
    args = ap.parse_args()

    out = args.out or TREE_PATH
    cassette = Cassette(args.root)
    harness_mode = harness.REPLAY if args.offline else harness.mode()
    offline = harness_mode == harness.REPLAY

    print(f"fixtures: {cassette.root}")
    print(f"harness: {harness_mode}" + (
        f", {len(cassette.keys('ebay_categories'))} ebay_categories recordings"
        if offline else ""))
    if not offline:
        print("egress: " + (f"proxy {proxies.redact(proxies.proxy_url())}"
                            if args.proxy else "DIRECT (switch with --proxy if blocked)"))
    print(f"in-scope departments: {len(IN_SCOPE)}, up to {args.max_leaves} leaves each\n")

    with contextlib.ExitStack() as stack:
        page = None
        if not offline:
            from playwright.sync_api import sync_playwright
            pw = stack.enter_context(sync_playwright())
            browser = pw.chromium.launch(
                headless=args.headless, channel="chrome",
                proxy=playwright_proxy() if args.proxy else None)
            stack.callback(browser.close)
            page = browser.new_page(locale="en-GB")

        tree, report = build_tree(cassette, page, max_leaves=args.max_leaves,
                                  harness_mode=harness_mode)

    out.write_text(json.dumps(tree, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")

    print(f"\nwritten to {out}")
    print("\nleaves per in-scope department:")
    total = 0
    for title, subcats_seen, leaves_resolved in report:
        print(f"  {title:<32} {leaves_resolved:>2} / {subcats_seen:>3} subcats seen")
        total += leaves_resolved
    print(f"\n{total} leaves total across {len(report)} departments")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
