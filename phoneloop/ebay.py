"""eBay search results: a retail ceiling and a competition signal.

What eBay is and is not good for here, established by measurement rather than
hope (2026-08-27):

    live search, scripted client      403
    live search, headed Chrome        200, but the IP gets rate-limited fast
    live search, headed + residential proxy   200, reliable
    SOLD / completed, any client      captcha -> sign-in wall

So **sold prices are out of reach**, and deliberately so rather than for want of
trying. Getting them means solving a captcha while signed in, and eBay suspends
accounts for automated access -- the account at risk is the same account you
need in order to sell. CeX and its equivalents give *committed* prices anyway,
which beat "someone once paid this" and come graded by condition.

What is left is still worth having: asking prices give a retail ceiling, and the
number of live listings is a saturation signal that says whether a product is
worth entering at all.

Two things this module refuses to be naive about:

**Landed cost, not headline price.** Same rule as the Vinted side, where
`total_item_price` is used over `price`. A £150 phone with £35 delivery is a
£185 phone.

**Overseas sellers.** A third of a UK results page ships from abroad -- 21 of 60
in the first sample, mostly Germany. They skew cheap, which makes them look like
the bargains, and they carry customs, VAT and weeks of delivery. They are
excluded from the UK reference by default rather than quietly averaged in.

Pure text parsing. Fetching lives in scripts/refresh_ebay.py because it needs a
browser and a proxy.
"""

from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import quote_plus, urlsplit, urlunsplit

from phoneloop import market_store
from phoneloop.parser import parse as _parse_model

# --- search request shape, shared with scripts/bulk_ebay.py and
# scripts/record_ebay.py ----------------------------------------------------
# The single source of truth for the URL a live eBay search request uses and
# the harness key it is recorded/replayed under. The two scripts used to
# build these independently and drifted apart -- bulk_ebay.py's leaf-sweep
# moved to `_ipg=240` plus a price band while record_ebay.py kept capturing
# `_ipg=60` with no band, under a completely different key format -- so a
# page recorded by one could never satisfy a replay lookup from the other.
# That drift is exactly what made contracts section 6.3's offline
# end-to-end run impossible (integration review, 2026-08-28): "Different
# URLs AND different keys, so no honest re-keying of the existing
# recordings exists." Fixing the naming here does not, by itself, make the
# recordings captured before this fix usable -- they were captured under the
# old, narrower shape and a fresh recording session is still needed (see
# scripts/bulk_ebay.py's module docstring and this build's unverified[]) --
# but it stops the two scripts from being able to drift apart again, since
# neither one owns its own copy of the URL or the key any more.
DEFAULT_PRICE_FROM = 15    # pounds -- the spec's default retail band (design spec §3, knob 3)
DEFAULT_PRICE_TO = 55

_SEARCH_URL_NKW = ("https://www.ebay.co.uk/sch/i.html?_nkw={term}&_ipg=240&_pgn={page}"
                    "&_udlo={lo}&_udhi={hi}")
# _fsrp=1 is load-bearing on the category-only shape. Without it eBay
# redirects the request to the /b/bn_<n> browse node of that category --
# every variant measured 2026-08-28 did (plain, rt=nc, empty _nkw, _sop=12,
# LH_BIN=1, page 2, the /sch/<id>/i.html path form) -- and the browse node
# of a hub such as 26677 lists no items at all, which the sweep would have
# recorded as "swept, genuinely nothing there". With it, 26677 and 9394
# both stayed on a 256-row results page, page 2 included, band applied.
# Keyword (_nkw=) searches never redirect. `is_browse_redirect` below is
# the guard for the day this stops being enough.
_SEARCH_URL_SACAT = ("https://www.ebay.co.uk/sch/i.html?_sacat={term}&_fsrp=1&_ipg=240"
                      "&_pgn={page}&_udlo={lo}&_udhi={hi}")


def search_key(mode: str, term: str, lo: int, hi: int, pgn: int) -> str:
    """The `name` both scripts pass to `harness.recorded(cassette, "ebay",
    name, produce)` for the same (mode, term, price band, page) -- identical
    wording so a page scripts/record_ebay.py captures replays under
    scripts/bulk_ebay.py, and vice versa. `mode` is "nkw" (keyword) or
    "sacat" (numeric leaf category id)."""
    return f"{mode}:{term}:{lo}-{hi}:p{pgn}"


def search_url(mode: str, term: str, lo: int, hi: int, pgn: int) -> str:
    """The live URL for the same (mode, term, price band, page) `search_key`
    names. `term` is percent-encoded for `mode="nkw"` (a free-text query);
    `mode="sacat"` terms are numeric category ids and need no encoding."""
    template = _SEARCH_URL_SACAT if mode == "sacat" else _SEARCH_URL_NKW
    encoded = quote_plus(term) if mode == "nkw" else term
    return template.format(term=encoded, page=pgn, lo=lo, hi=hi)


def is_browse_redirect(url: str) -> bool:
    """True when a search landed on a /b/ browse node instead of /sch/ --
    eBay answered with a category landing page, not results. Callers treat
    it as a failed fetch, never as an empty page: the browse node of a hub
    shows nothing while the category holds a million listings (26677,
    measured 2026-08-28)."""
    return urlsplit(url).path.startswith("/b/")


_PRICE_RE = re.compile(r"£\s*([\d,]+\.\d{2})")
_DELIVERY_RE = re.compile(r"\+\s*£\s*([\d,]+\.\d{2})\s*(?:delivery|postage)", re.I)
# Anchored to the START of a line, not `.search()`ed over the whole row: a
# live listing (the_ioutlet_extra's template, measured on 11 of 528 real rows,
# 2026-08-27) carries the promotional banner line "12 Months Warranty - Fast &
# Free Delivery - Unlocked" ahead of a REAL "+£30.00 delivery" charge later in
# the same row. The old un-anchored pattern matched "free... delivery" inside
# that banner and booked the charge as 0 -- a wrong known beats nothing on
# every other line in this module, but here it was a wrong known that hid a
# real one three lines later. A genuine free-delivery statement ("Free
# international postage") always opens its own line; the banner never does.
_FREE_RE = re.compile(r"^free\b.{0,24}\b(?:delivery|postage)", re.I)
_UNSPECIFIED_RE = re.compile(r"(?:postage|delivery)\s+not\s+specified", re.I)
# "48+ watchers" (a live page, 2026-08-27) has a "+" the original pattern did
# not expect, so it silently fell back to 0 watchers on every such row.
_WATCHERS_RE = re.compile(r"(\d[\d,]*)\+?\s+watch(?:ers|ing)", re.I)
# "from Germany" is a shipping origin. "from £107.98 with coupon" is the price
# range on a multi-variant listing, and the first version of this pattern read
# that as a country -- so a UK listing with variant pricing was booked as
# overseas and dropped from the UK pool. That is the expensive direction: it
# discards good comps and shrinks exactly the sample the ceiling depends on.
# Require the origin to start with a letter.
_FROM_RE = re.compile(r"^from\s+(?![£$€\d])([A-Za-z][^\n]*?)\s*$", re.I | re.M)
_STORAGE_RE = re.compile(r"\b(\d{2,4})\s*(GB|TB)\b", re.I)

# The aspects line is eBay's own catalogue match, e.g.
#   "Pre-owned · Apple iPhone 13 · Unlocked · 128 GB"
# It is a better identity parse than any regex over the seller's title, and it
# carries storage -- the field missing from most Vinted titles.
_ASPECT_SEP = "·"

# Below this many comparable UK listings there is no market rate to report.
MIN_SAMPLES = 6

# Median absolute deviation as a fraction of the median. Above this the pool is
# not one product. Set from the live tables: coherent queries sat at 0.03-0.10,
# and the incoherent iPhone 11 128GB pool that produced a 28800 ceiling was far
# above it.
MAX_DISPERSION = 0.22

_CONDITIONS = (
    "Pre-owned", "New (other)", "Brand New", "New with box", "New without box",
    "Open box", "Very Good - Refurbished", "Excellent - Refurbished",
    "Good - Refurbished", "Certified - Refurbished", "Refurbished",
    "For parts or not working", "New",
)


def to_pence(text: str) -> int | None:
    m = _PRICE_RE.search(text or "")
    return round(float(m.group(1).replace(",", "")) * 100) if m else None


# --- decoy row -------------------------------------------------------------

# eBay's search results page renders a hidden template `<li>` ahead of the
# real rows, pointing at /itm/123456. It parses cleanly -- it has a price and
# a title -- so nothing else in this module would catch it; only the item id
# gives it away.
DECOY_ITEM_IDS: frozenset[str] = frozenset({"123456"})

_ITEM_ID_RE = re.compile(r"/itm/(\d+)")
_EPID_RE = re.compile(r"[?&]epid=(\d+)")


def _item_id_from_url(url: str) -> str:
    m = _ITEM_ID_RE.search(url or "")
    return m.group(1) if m else ""


def _epid_from_url(url: str) -> str:
    m = _EPID_RE.search(url or "")
    return m.group(1) if m else ""


def is_decoy(url: str) -> bool:
    """Is this href the hidden template row rather than a real listing?"""
    return _item_id_from_url(url) in DECOY_ITEM_IDS


# --- sold count, stock hints -------------------------------------------------

# "552+ sold", "1,424+ sold", "76 sold" -- all measured live, 2026-08-27. The
# "+" means "at least this many" and is dropped; the digits are kept exactly.
# No match is unknown, never zero: a row that genuinely says "0 sold" is a
# real zero and must come back as 0, not None.
_SOLD_RE = re.compile(r"([\d,]+)\+?\s*sold\b", re.I)

# eBay's own stock-pressure copy. Recorded verbatim rather than folded into a
# quantity -- "Almost gone" is not a number, and turning it into one would be
# inventing data that was never on the page.
_STOCK_HINTS = ("Almost gone", "Last one")


def _sold_qty(text: str) -> int | None:
    m = _SOLD_RE.search(text)
    return int(m.group(1).replace(",", "")) if m else None


def _stock_hint(text: str) -> str:
    for hint in _STOCK_HINTS:
        if hint in text:
            return hint
    return ""


# --- seller line -------------------------------------------------------------

# Two orderings were measured live on the same day, 2026-08-27, across 528
# rows from seven search pages: "jaessentials 99.8% positive (74.6K)" on 409
# of them (the contract's documented shape) and "one.stop.shoppee (3,503)
# 98.6%" -- percentage and count swapped -- on 65. The remaining 54 rows carry
# no seller line at all (a "people also viewed" style recommendation card with
# no feedback shown); that is a real absence, not a parse failure.
_SELLER_PRIMARY_RE = re.compile(
    r"^([A-Za-z0-9][\w.\-]*)\s+([\d.]+)%\s*positive\s*\(([\d,.]+[KM]?)\)\s*$",
    re.I,
)
_SELLER_ALT_RE = re.compile(
    r"^([A-Za-z0-9][\w.\-]*)\s*\(([\d,.]+[KM]?)\)\s*([\d.]+)%\s*$",
    re.I,
)


def expand_abbreviated_count(raw: str) -> int:
    """"74.6K" -> 74600, "1.7K" -> 1700, "3,503" -> 3503, "273K" -> 273000.

    eBay abbreviates any count above 1,000 to one decimal place, so a K/M
    figure is accurate to roughly +/-50 above that threshold, never exact --
    shared by every count this module reads off a K/M-abbreviated line
    (feedback counts here, store lifetime sales in ebay_item.py).
    """
    s = raw.replace(",", "").upper()
    if s.endswith("K"):
        return round(float(s[:-1]) * 1_000)
    if s.endswith("M"):
        return round(float(s[:-1]) * 1_000_000)
    return int(float(s))


def pct_to_bp(raw: str) -> int:
    """"99.8" -> 9980, "100" -> 10000, "0" -> 0. Shared with ebay_item.py."""
    return round(float(raw) * 100)


def _seller_from_lines(lines: list[str]) -> tuple[str, int | None, int | None]:
    """(seller_id, feedback_bp, feedback_n) from whichever ordering is on the
    row, or ("", None, None) when no seller line is present at all."""
    for ln in lines:
        m = _SELLER_PRIMARY_RE.match(ln)
        if m:
            return m.group(1), pct_to_bp(m.group(2)), expand_abbreviated_count(m.group(3))
        m = _SELLER_ALT_RE.match(ln)
        if m:
            return m.group(1), pct_to_bp(m.group(3)), expand_abbreviated_count(m.group(2))
    return "", None, None


# --- promoted flag -----------------------------------------------------------

# eBay renders "Sponsored" reversed ("derosnopS") so it cannot be selected as
# text, but the DOM's innerText still carries it -- measured on literally all
# 528 rows across seven live search pages captured 2026-08-27 (every query,
# every page). Whether that reflects this crawl's specific conditions or a
# genuinely all-sponsored results set is unverified; either way the marker is
# real and is parsed, never stripped -- the pre-existing fixtures in
# tests/test_ebay.py (captured the same day, without the marker) are the
# promoted=False case.
_PROMOTED_SUFFIX = "derosnopS"


def _is_promoted(text: str) -> bool:
    return text.rstrip().endswith(_PROMOTED_SUFFIX)


# --- title ---------------------------------------------------------------

# The row's first line is NOT reliably the title, and the exceptions cannot be
# listed. Measured over all 31,649 rows of the complete 43-hub sweep
# (2026-08-28): 837 put something ahead of it -- promo badges (NEW LOW PRICE
# 566, GREAT PRICE 93, "20% OFF", "56% OFF") and, just as often, a bare brand
# label: Google, Huawei, Motorola, Xiaomi, Redmi, Samsung, Apple, DOOGEE,
# PDKUAI, FDGAO. A badge blocklist would have to enumerate every brand eBay
# sells, so it is the wrong shape.
#
# So anchor on structure instead. "Opens in a new window or tab" is the
# accessibility suffix of the anchor element whose own text is the title, and
# it is present on 100% of those 31,649 rows -- across all 43 categories
# swept, not just the handful the rule was first drawn from. The title is the
# line immediately before it.
_TITLE_ANCHOR = "Opens in a new window or tab"

# One badge is glued on with no separator, so the anchor above cannot see it
# (981 rows). The prefix set was read off eBay's OWN second rendering of the
# same string -- it repeats the title later as "- <title>" -- rather than
# guessed: across the 2,367 rows carrying both renderings, "NEW LISTING" is
# the only glued prefix (115).
#
# It is matched EXACTLY, never as a looser leading "NEW ". Rows like "NEW
# Genuine Toomax Storaway Spare Replacement Parts" are real seller titles,
# and on iPhone rows the leading NEW is eBay's English rendering of a German
# listing's "NEU". Stripping those would corrupt a real title.
_GLUED_BADGES = ("NEW LISTING",)


def _title_from_lines(lines: list[str]) -> str:
    """The listing title, with any badge or brand label in front removed.

    Falls back to the first line when the anchor is absent, so a future layout
    that drops it degrades to the old behaviour rather than to an empty title.
    """
    if not lines:
        return ""
    try:
        anchor = lines.index(_TITLE_ANCHOR)
    except ValueError:
        anchor = -1
    # anchor at 0 would make lines[anchor - 1] the LAST line, not the title
    title = lines[anchor - 1] if anchor > 0 else lines[0]
    for badge in _GLUED_BADGES:
        if title.startswith(badge):
            return title[len(badge):].strip()
    return title


# --- delivery ETA --------------------------------------------------------

# None of these three phrasings appeared on any of the 528 real rows captured
# for this build (2026-08-27) -- eBay did not show a delivery estimate on this
# particular sweep. They are the design spec's own documented copy (§2.4 of
# the build contract) and are exercised directly against that literal text
# rather than through a page recording; see tests/test_ebay.py and
# unverified[] in the build report.
_GET_IT_BY_RE = re.compile(r"Get it by\s+[A-Za-z]+,?\s+(\d{1,2})\s+([A-Za-z]+)", re.I)
_ETA_RANGE_RE = re.compile(r"(\d+)\s*[-–]\s*(\d+)\s*days?\b", re.I)
_ETA_SINGLE_RE = re.compile(r"\bin\s+(\d+)\s+days?\b", re.I)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _eta_days(text: str, today: date | None) -> int | None:
    """Maximum promised delivery days. Pure: never reads the clock itself --
    "Get it by <date>" needs `today` to turn a date into a day count, and
    without one it honestly returns None rather than guessing a year."""
    m = _GET_IT_BY_RE.search(text)
    if m:
        if today is None:
            return None
        month = _MONTHS.get(m.group(2)[:3].lower())
        if month is None:
            return None
        try:
            target = date(today.year, month, int(m.group(1)))
        except ValueError:
            return None
        if target < today:
            target = date(today.year + 1, month, int(m.group(1)))
        return (target - today).days

    m = _ETA_RANGE_RE.search(text)
    if m:
        return max(int(m.group(1)), int(m.group(2)))

    m = _ETA_SINGLE_RE.search(text)
    return int(m.group(1)) if m else None


@dataclass(frozen=True)
class EbayListing:
    title: str
    price_pence: int
    delivery_pence: int | None      # None means eBay did not state it
    condition: str = ""
    aspects: tuple[str, ...] = field(default_factory=tuple)
    storage_gb: int | None = None
    watchers: int = 0
    ships_from: str = ""            # empty means UK
    url: str = ""
    # added by s2 -- every one defaults, so existing constructions keep working
    item_id: str = ""                       # from the /itm/ URL
    sold_qty: int | None = None             # "1,283 sold" -> 1283; absent -> None, NEVER 0
    seller_id: str = ""                     # "jaessentials"
    seller_feedback_bp: int | None = None   # "99.8% positive" -> 9980
    seller_feedback_n: int | None = None    # "(74.6K)" -> 74600
    promoted: bool = False                  # row text ends with the reversed word "derosnopS"
    eta_days: int | None = None             # max promised delivery days
    epid: str = ""                          # from an epid= href on the row
    stock_hint: str = ""                    # "Almost gone" | "Last one" | ""

    @property
    def landed_pence(self) -> int | None:
        """Price plus delivery. None when delivery was never stated.

        None rather than a guess of zero: "Postage not specified" on a heavy or
        overseas item is routinely the difference between a bargain and a loss,
        and 4 of the first 60 rows said exactly that.
        """
        if self.delivery_pence is None:
            return None
        return self.price_pence + self.delivery_pence

    @property
    def is_uk(self) -> bool:
        return not self.ships_from

    @property
    def catalogue_model(self) -> str | None:
        """The model slug from eBay's OWN catalogue match, not the title.

        A search for "iphone 13" returns 13 mini, 13 Pro and 13 Pro Max as
        well, and a Pro Max is worth two to three times a mini. Pooling them
        produces a median that describes no actual handset. This is the field
        that lets the pool be narrowed to one model.

        None when eBay showed no catalogue line, which is most of the page --
        only about a third of rows carry one.
        """
        for part in self.aspects[1:]:
            slug = _parse_model(part).model
            if slug:
                return slug
        return None

    @property
    def is_used(self) -> bool:
        """Genuinely second-hand, comparable to a handset bought off Vinted.

        "New (other)" is deliberately NOT used. eBay applies it to sealed and
        shop-soiled retail stock, which prices like new -- pooling it with
        pre-owned inflates the ceiling, and inflating the ceiling is the
        direction that loses money. It showed up as an iPhone 11 128GB ceiling
        of 28800 when the model-wide used figure was 12316.
        """
        c = self.condition.lower()
        return "pre-owned" in c or "refurb" in c or "open box" in c


def parse_row(text: str, url: str = "", *, today: date | None = None) -> EbayListing | None:
    """Parse one search-result row's visible text.

    Returns None for the hidden /itm/123456 decoy row, and for any row with
    no usable price, rather than inventing one. Stays pure -- `today` is a
    parameter, not `date.today()`, so a "Get it by <date>" ETA is reproducible
    from a fixture instead of changing answer with the calendar.
    """
    if is_decoy(url):
        return None
    if not text:
        return None
    price = to_pence(text)
    if price is None:
        return None

    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    delivery: int | None = None
    if any(_FREE_RE.match(ln) for ln in lines):
        delivery = 0
    else:
        m = _DELIVERY_RE.search(text)
        if m:
            delivery = round(float(m.group(1).replace(",", "")) * 100)
        elif _UNSPECIFIED_RE.search(text):
            delivery = None

    aspects: tuple[str, ...] = ()
    condition = ""
    for line in lines:
        if _ASPECT_SEP in line:
            parts = tuple(p.strip() for p in line.split(_ASPECT_SEP) if p.strip())
            head = parts[0] if parts else ""
            if any(head.lower().startswith(c.lower()) for c in _CONDITIONS):
                aspects, condition = parts, head
                break

    storage = None
    for part in aspects:
        m = _STORAGE_RE.search(part)
        if m:
            storage = int(m.group(1)) * (1024 if m.group(2).upper() == "TB" else 1)
            break

    watchers_m = _WATCHERS_RE.search(text)
    from_m = _FROM_RE.search(text)
    seller_id, feedback_bp, feedback_n = _seller_from_lines(lines)

    return EbayListing(
        title=_title_from_lines(lines),
        price_pence=price,
        delivery_pence=delivery,
        condition=condition,
        aspects=aspects,
        storage_gb=storage,
        watchers=int(watchers_m.group(1).replace(",", "")) if watchers_m else 0,
        ships_from="" if not from_m else from_m.group(1).strip(),
        url=url,
        item_id=_item_id_from_url(url),
        sold_qty=_sold_qty(text),
        seller_id=seller_id,
        seller_feedback_bp=feedback_bp,
        seller_feedback_n=feedback_n,
        promoted=_is_promoted(text),
        eta_days=_eta_days(text, today),
        epid=_epid_from_url(url),
        stock_hint=_stock_hint(text),
    )


def parse_rows(rows, *, today: date | None = None) -> list[EbayListing]:
    """Parse many rows, dropping decoys and anything that yields nothing usable."""
    out = []
    for row in rows:
        text = row.get("text", "") if isinstance(row, dict) else str(row)
        url = row.get("href", "") if isinstance(row, dict) else ""
        listing = parse_row(text, url, today=today)
        if listing is not None:
            out.append(listing)
    return out


# --- ROW_FIELDS ----------------------------------------------------------

# eBay's HTML shows a shipping country as a name ("from Germany"), not the
# alpha-2 code origin_country wants (build contract §1.1, col 32). Only names
# actually seen on a live UK results page, 2026-08-27, are mapped; an
# unmapped name comes back None rather than a guessed code, because a wrong
# code is worse downstream than an absent one.
#
# Measured across the stream's 528 recorded search rows (all seven captured
# search pages): United States 49, China 27, Germany 15, Switzerland 12,
# Italy 8, Canada 5, Spain 3, Australia 3, Denmark 3, Ireland 2, Lithuania 2,
# Austria 1, India 1, Ukraine 1. "Hong Kong" is NOT one of them -- it was
# wrongly mapped in an earlier pass (the only place "Hong Kong" appears in
# any recording is an unrelated shipping-destination country-code dropdown
# on the item pages, not a "ships from" line) and is dropped here rather
# than kept as a guess.
_ORIGIN_COUNTRY_CODES = {
    "germany": "DE", "united states": "US", "switzerland": "CH",
    "italy": "IT", "spain": "ES", "india": "IN", "lithuania": "LT",
    "australia": "AU", "china": "CN", "canada": "CA", "denmark": "DK",
    "ireland": "IE", "austria": "AT", "ukraine": "UA",
}


def _origin_country(listing: EbayListing) -> str | None:
    if listing.is_uk:
        return "GB"
    return _ORIGIN_COUNTRY_CODES.get(listing.ships_from.strip().lower())


# --- the price band is part of the niche, so it has to reach the row -------

# A niche is "leaf x price band" (spec section 5), and the sweep filters on a
# band (`_udlo`/`_udhi` -- the "15-55" in a search_key). The shard was written
# as "<category>/p<n>", dropping the band entirely, while board.rank_niches
# groups on leaf_category_id alone.
#
# With one band swept that is invisible. With two it is silently wrong: both
# bands become one niche and every niche-level figure -- median price,
# sell-through, competitor count -- describes a blend of the two, with
# nothing going red. Sweeping a second band is planned, not hypothetical:
# the ASP band is knob 3.
#
# So the band rides in the shard, and the niche key is (leaf, band).
_BAND_SEP = "/"


def shard_for(term: str, lo: int, hi: int, pgn: int) -> str:
    """The shard string stored on every row of one swept page."""
    return f"{term}{_BAND_SEP}{lo}-{hi}{_BAND_SEP}p{pgn}"


def band_of(shard: str) -> str | None:
    """The price band a shard was swept under, or None if it predates this.

    Rows written before this change read "16086/p1" -- two parts, no band --
    and come back None ("band unknown") rather than raising or, worse,
    reporting a band they never carried.
    """
    parts = (shard or "").split(_BAND_SEP)
    if len(parts) < 3:
        return None
    return parts[1] or None


def niche_key(leaf_category_id: str, shard: str) -> str:
    """The niche a row belongs to: leaf x band, never leaf x page.

    Page is a fetch detail -- pages 1 and 3 of one band are one niche. A row
    with no band recorded keys on the leaf alone, which is exactly the old
    behaviour, so a store holding both formats degrades per-row instead of
    splitting one real niche into a banded and an unbanded half.
    """
    band = band_of(shard)
    return f"{leaf_category_id}@{band}" if band else str(leaf_category_id)


def to_row(listing: EbayListing, *, category: str, shard: str,
           leaf_category_id: str | None = None) -> dict:
    """One EbayListing -> the shared ROW_FIELDS dict.

    Built from market_store.empty_row() rather than a literal (build contract
    §2.4), so a column this parser has nothing to say about comes back NULL
    instead of silently missing from the dict entirely.

    `stock_hint` is not a ROW_FIELDS column -- it rides in `extra` along with
    the other row-level leftovers (aspects, watchers, the raw ships_from
    text), never promoted into a numeric field it is not.
    """
    row = market_store.empty_row("ebay", listing.item_id)
    extra = {
        "aspects": list(listing.aspects),
        "watchers": listing.watchers,
        "ships_from": listing.ships_from,
        "stock_hint": listing.stock_hint,
    }
    url = urlunsplit(urlsplit(listing.url)._replace(query="", fragment="")) if listing.url else None
    row.update({
        "category": category,
        "shard": shard,
        "title": listing.title or None,
        "price_pence": listing.price_pence,
        "total_pence": listing.landed_pence,
        "currency": "GBP",
        "condition": listing.condition or None,
        "model": listing.catalogue_model,
        "storage_gb": listing.storage_gb,
        "kind": "ask",
        "available": True,
        "region": "GB" if listing.is_uk else None,
        "seller_id": listing.seller_id or None,
        "url": url,
        "extra": json.dumps(extra, separators=(",", ":")),
        "sold_qty": listing.sold_qty,
        "seller_feedback_bp": listing.seller_feedback_bp,
        "seller_feedback_n": listing.seller_feedback_n,
        "epid": listing.epid or None,
        # eBay attaches epid= to a search-row href precisely when it has
        # matched the listing to a catalogue product -- the same signal the
        # Browse API exposes as itemSummary.epid (spec §5's "catalogued"
        # tier). The HTML transport never sees an *inferred* epid (that is
        # getItem-only), so any epid this transport finds is the exact kind.
        "identity_tier": "catalogued" if listing.epid else None,
        "origin_country": _origin_country(listing),
        "promoted": listing.promoted,
        "leaf_category_id": leaf_category_id,
        "eta_days": listing.eta_days,
    })
    return row


@dataclass(frozen=True)
class Ceiling:
    """What a handset is currently being *asked* for on eBay UK."""

    median_pence: int
    mad_pence: int
    samples: int
    overseas_excluded: int
    unpriceable_excluded: int
    variant_excluded: int = 0


def retail_ceiling(listings, storage_gb: int | None = None,
                   used_only: bool = True,
                   model: str | None = None) -> Ceiling | None:
    """Median landed asking price of comparable UK listings.

    Median and MAD rather than mean and standard deviation, as everywhere else
    here: one £1,500 outlier in a page of £200 handsets would drag a mean into
    nonsense, and search results are full of them.

    Returns None below MIN_SAMPLES, and None again when the surviving prices
    are too scattered to describe one product. Both guards exist because the
    thin pools are exactly the wrong ones to trust: a capacity-specific query
    leaves four or five UK comps, and four listings that disagree wildly are
    not a market rate, they are four people's opinions.
    """
    overseas = sum(1 for x in listings if not x.is_uk)
    pool = [x for x in listings if x.is_uk]
    variant = 0
    if model is not None:
        # Keep only rows eBay itself catalogued as this exact model. Rows with
        # no catalogue line are dropped rather than assumed to match: an
        # uncatalogued row is unknown, and a ceiling is not the place to guess.
        before = len(pool)
        pool = [x for x in pool if x.catalogue_model == model]
        variant = before - len(pool)
    if used_only:
        pool = [x for x in pool if x.is_used]
    if storage_gb is not None:
        pool = [x for x in pool if x.storage_gb == storage_gb]

    landed = [x.landed_pence for x in pool if x.landed_pence is not None]
    unpriceable = len(pool) - len(landed)
    if len(landed) < MIN_SAMPLES:
        return None

    median = int(statistics.median(landed))
    mad = int(statistics.median([abs(v - median) for v in landed]))
    # A coherent product prices tightly. When the spread is this wide the pool
    # is still holding more than one thing -- a different capacity, a bundle,
    # a for-parts unit -- and its median describes none of them.
    if median <= 0 or mad / median > MAX_DISPERSION:
        return None
    return Ceiling(
        median_pence=median,
        mad_pence=mad,
        samples=len(landed),
        overseas_excluded=overseas,
        unpriceable_excluded=unpriceable,
        variant_excluded=variant,
    )


# A UK search served from a UK address returns mostly UK sellers. Below this
# share the request was almost certainly geolocated somewhere else, and the
# result set has been reordered by shipping proximity to that somewhere.
MIN_UK_SHARE = 0.55


def uk_share(listings) -> float:
    """Fraction of results that ship from the UK."""
    if not listings:
        return 0.0
    return sum(1 for x in listings if x.is_uk) / len(listings)


def looks_geolocated_abroad(listings) -> bool:
    """Was this result set served to a non-UK address?

    Worth checking explicitly, because it is invisible downstream. Captured
    through a rotating residential gateway that takes a fresh exit per
    navigation, the UK share swung between queries in a SINGLE run -- 79 of 88
    for one handset and 28 of 84 for the next. eBay reorders by shipping
    proximity, so a query answered from Hong Kong yields a thin, unrepresentative
    UK pool. The ceiling built from it is not wrong-looking, just wrong.

    An empty result set is not evidence of this, so it does not trip.
    """
    return bool(listings) and uk_share(listings) < MIN_UK_SHARE


def saturation(listings) -> dict:
    """How crowded is this product, and is anyone watching?

    The count is the competition signal the arbitrage side has been missing: a
    product with four hundred live listings is not one to enter, however good
    the buy price looks.
    """
    total = len(listings)
    watched = sum(x.watchers for x in listings)
    return {
        "live_listings": total,
        "uk_listings": sum(1 for x in listings if x.is_uk),
        "overseas_listings": sum(1 for x in listings if not x.is_uk),
        "total_watchers": watched,
        "watchers_per_listing": round(watched / total, 1) if total else 0.0,
    }
