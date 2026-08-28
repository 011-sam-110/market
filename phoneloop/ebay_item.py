"""eBay item pages: the fields a search row never shows.

A search row gets one shot at a listing; the item page it links to carries
several things a niche board needs and no search page does -- how many have
actually sold (a real lifetime count, not "N+"), whether the seller is a
business, when the listing itself went live, and the manufacturer's own
Brand/MPN/EAN rather than whatever the seller typed into a title.

Two input strings, on purpose. `text` is the rendered page's visible body --
`page.inner_text("body")` in scripts/record_ebay.py -- and is where every
seller-facing figure lives (sold count, feedback, "Joined <month> <year>").
`html` is the raw page source, needed for exactly two things this project
could not find anywhere in the rendered text: the listing's own creation
timestamp (embedded as JSON in a `SEMANTIC_DATA` module, to the second) and
its numeric leaf category id (embedded in an unrelated `itmdesc.ebaydesc.com`
description-iframe URL, of all places -- there is no cleaner source that
was found on a real page). Everything else is read from `text` alone.

Every regex below was built against six real item pages this build recorded
live through scripts/record_ebay.py on 2026-08-27 (tests/test_ebay_item.py
replays them via PHONELOOP_FIXTURES), not from documentation -- eBay's
rendered markup for these figures is not published anywhere the way the
Buy Browse API's JSON shape is.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime

from phoneloop.ebay import expand_abbreviated_count, pct_to_bp

# --- item id -----------------------------------------------------------

# Present on all six real item pages, always this exact wording, no space
# after the colon: "eBay item number:225236871939".
_ITEM_NUMBER_RE = re.compile(r"eBay item number:\s*(\d+)")
_URL_ITEM_ID_RE = re.compile(r"/itm/(\d+)")


def _item_id(text: str, url: str) -> str:
    m = _ITEM_NUMBER_RE.search(text)
    if m:
        return m.group(1)
    m = _URL_ITEM_ID_RE.search(url or "")
    return m.group(1) if m else ""


# --- quantity: available / sold -----------------------------------------

# "3 available2,858 sold" -- eBay renders these with no separator between
# them, measured on 3 of 6 real item pages (the other 3 show neither: no
# "Quantity:" block is rendered for those listings at all). "available" and
# "sold" are matched independently rather than as one combined pattern so
# either can be missing without losing the other.
_MORE_THAN_AVAILABLE_RE = re.compile(r"More than\s+([\d,]+)\s*available", re.I)
_AVAILABLE_RE = re.compile(r"(\d[\d,]*)\s*available", re.I)

# "This one's trending. 2858 have already sold." is the unambiguous source:
# it names its own subject. The alternative -- a bare "(\d+)\s*sold" search
# over the whole page -- would also catch "6 SOLD TODAY" (a same-page but
# DIFFERENT figure, today's velocity, not the lifetime total) on whichever
# page has both, and "6 SOLD TODAY" sorts earlier in the page. Never fall
# back to that bare pattern for this field.
_TRENDING_SOLD_RE = re.compile(r"trending\.\s*([\d,]+)\s*have already sold", re.I)
_AVAILABLE_SOLD_COMBO_RE = re.compile(r"\d[\d,]*\s*available\s*([\d,]+)\s*sold\b", re.I)


def _qty_available(text: str) -> tuple[int | None, bool]:
    m = _MORE_THAN_AVAILABLE_RE.search(text)
    if m:
        return int(m.group(1).replace(",", "")), True
    m = _AVAILABLE_RE.search(text)
    if m:
        return int(m.group(1).replace(",", "")), False
    return None, False


def _sold_qty(text: str) -> int | None:
    m = _TRENDING_SOLD_RE.search(text)
    if m:
        return int(m.group(1).replace(",", ""))
    m = _AVAILABLE_SOLD_COMBO_RE.search(text)
    return int(m.group(1).replace(",", "")) if m else None


# --- seller ---------------------------------------------------------------

# The seller's top-of-page block, e.g. "The_iOutlet\n(76581)\nBusiness\n99.9%
# positive" or "bl_850779\n(1)\nPrivate\n100% positive" -- present, in this
# exact shape, on all six real item pages, for both business and private
# sellers. This is where feedback COUNT lives; the later "About this seller"
# section repeats the percentage but not the count.
_TOP_SELLER_BLOCK_RE = re.compile(
    r"\((\d[\d,]*)\)\n(?:Business|Private)\n([\d.]+)%\s*positive", re.I
)

# "Registered as a business seller" / "Registered as a private seller" is the
# ONE explicit marker this module trusts for seller_type (build contract
# §2.5) -- both wordings were measured on real pages. Its absence is not
# evidence of "private"; three of the six real pages show neither line at
# all, and those stay "" rather than being guessed.
_BUSINESS_MARKER = "Registered as a business seller"
_PRIVATE_MARKER = "Registered as a private seller"

_JOINED_RE = re.compile(r"Joined\s+([A-Za-z]{3})[a-z]*\s+(\d{4})")
_ITEMS_SOLD_RE = re.compile(r"([\d,.]+[KM]?)\s*items sold", re.I)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _seller_feedback(text: str) -> tuple[int | None, int | None]:
    """(feedback_n, feedback_bp) from the top-of-page seller block."""
    m = _TOP_SELLER_BLOCK_RE.search(text)
    if not m:
        return None, None
    return expand_abbreviated_count(m.group(1)), pct_to_bp(m.group(2))


def _seller_type(text: str) -> str:
    if _BUSINESS_MARKER in text:
        return "business"
    if _PRIVATE_MARKER in text:
        return "private"
    return ""


def _store_joined(text: str) -> date | None:
    m = _JOINED_RE.search(text)
    if not m:
        return None
    month = _MONTHS.get(m.group(1)[:3].lower())
    if month is None:
        return None
    # eBay shows month precision only ("Joined Mar 2019") -- the day is
    # always 01, and that is a display limit, not a real join-date of the
    # 1st for every seller.
    return date(int(m.group(2)), month, 1)


def _store_items_sold(text: str) -> int | None:
    m = _ITEMS_SOLD_RE.search(text)
    return expand_abbreviated_count(m.group(1)) if m else None


# --- item specifics: Brand / MPN / EAN ------------------------------------

_BRAND_RE = re.compile(r"Brand\n([^\n]+)")
_MPN_RE = re.compile(r"MPN\n([^\n]+)")
_EAN_RE = re.compile(r"EAN\n([^\n]+)")


def _specific(pattern: re.Pattern, text: str) -> str:
    m = pattern.search(text)
    return m.group(1).strip() if m else ""


# --- html-only: listing start date, leaf category id ----------------------

# The item's own SEMANTIC_DATA module, embedded once per page as JSON inside
# a much larger script blob. A full JSON parse of that blob was not
# attempted -- there is no clean boundary to slice it at without a brace-
# matching walk, and the timestamp itself sits at a fixed, simple key that a
# direct regex reaches without one. Present, exactly once, on all six real
# item pages.
_START_DATE_RE = re.compile(r'"startDate":\{"value":"([^"]+)"')

# eBay's numeric leaf category id appears nowhere in the rendered page text
# (the visible breadcrumb carries only titles) and nowhere reliable in the
# JSON either -- the one JSON field that does carry it, `itemCategoryId=`
# inside an SME_BTF recommendation-module URL, was present on only one of
# six real pages. What IS on all six is the item description iframe's own
# URL, which eBay stamps with the category as a query parameter:
# "https://itm.ebaydesc.com/itmdesc/<id>?t=<ts>&category=9355".
_LEAF_CATEGORY_RE = re.compile(r"itmdesc/\d+\?t=\d+&category=(\d+)")


def _listed_at(html: str) -> datetime | None:
    m = _START_DATE_RE.search(html)
    if not m:
        return None
    try:
        return datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
    except ValueError:
        return None


def _leaf_category_id(html: str) -> str:
    m = _LEAF_CATEGORY_RE.search(html)
    return m.group(1) if m else ""


@dataclass(frozen=True)
class EbayItem:
    item_id: str
    sold_qty: int | None = None
    qty_available: int | None = None
    qty_available_is_floor: bool = False   # True for "More than 10 available"
    seller_id: str = ""
    seller_feedback_bp: int | None = None
    seller_feedback_n: int | None = None
    seller_type: str = ""                  # "business" | "private" | ""
    store_joined: date | None = None       # "Joined Mar 2019" -> 2019-03-01, month precision only
    store_items_sold: int | None = None
    listed_at: datetime | None = None      # SEMANTIC_DATA "startDate", UTC
    brand: str = ""
    mpn: str = ""                          # recorded, never used for identity
    gtin: str = ""
    epid: str = ""
    leaf_category_id: str = ""


def parse_item(text: str, *, html: str = "", url: str = "") -> EbayItem | None:
    """Parse one item page. `text` is the rendered body text; `html` carries
    the embedded SEMANTIC_DATA and the description-iframe URL.

    Returns None when there is no page to read at all, or no item id can be
    found either in the text or the URL -- an item page with no identity is
    not usable for anything downstream.
    """
    if not text:
        return None
    item_id = _item_id(text, url)
    if not item_id:
        return None

    qty_available, qty_floor = _qty_available(text)
    ean = _specific(_EAN_RE, text)
    gtin = re.sub(r"\D", "", ean)  # "printed, digits only" (build contract §2.5)

    # None of the six real item pages this build recorded carried an epid --
    # measured, not assumed (see unverified[] in the build report). There is
    # no confirmed pattern to extract one from, so this always comes back "".
    epid = ""

    seller_n, seller_bp = _seller_feedback(text)

    # The seller's own username (e.g. "the_ioutlet_extra") comes from the
    # embedded SEMANTIC_DATA rather than the page's display name ("The
    # iOutlet") -- the two differ in case and punctuation on every one of the
    # six real pages, and the display name is not what a URL or an API call
    # would use.
    seller_id_m = re.search(r'"sellerUserName":"([^"]+)"', html)
    seller_id = seller_id_m.group(1) if seller_id_m else ""

    return EbayItem(
        item_id=item_id,
        sold_qty=_sold_qty(text),
        qty_available=qty_available,
        qty_available_is_floor=qty_floor,
        seller_id=seller_id,
        seller_feedback_bp=seller_bp,
        seller_feedback_n=seller_n,
        seller_type=_seller_type(text),
        store_joined=_store_joined(text),
        store_items_sold=_store_items_sold(text),
        listed_at=_listed_at(html),
        brand=_specific(_BRAND_RE, text),
        mpn=_specific(_MPN_RE, text),
        gtin=gtin,
        epid=epid,
        leaf_category_id=_leaf_category_id(html),
    )


def to_row_updates(item: EbayItem) -> dict:
    """The subset of ROW_FIELDS an item page can fill, for merging onto a
    sweep row (build contract §2.5) -- only keys this parser has an actual
    opinion on, so merging never clobbers a search row's own good data with
    an item-page absence.

    `qty_available_is_floor` is not a ROW_FIELDS column; it rides in `extra`
    as its own JSON object. A caller merging this onto a sweep row's `extra`
    must merge the JSON, not overwrite the sweep row's `extra` wholesale --
    `ebay.to_row` already put aspects/watchers/stock_hint there.
    """
    updates: dict = {}
    if item.sold_qty is not None:
        updates["sold_qty"] = item.sold_qty
    if item.qty_available is not None:
        updates["qty_available"] = item.qty_available
    if item.seller_type:
        updates["seller_type"] = item.seller_type
    if item.seller_id:
        updates["seller_id"] = item.seller_id
    if item.seller_feedback_bp is not None:
        updates["seller_feedback_bp"] = item.seller_feedback_bp
    if item.seller_feedback_n is not None:
        updates["seller_feedback_n"] = item.seller_feedback_n
    if item.listed_at is not None:
        updates["listed_at"] = item.listed_at
    if item.brand:
        updates["brand"] = item.brand
    if item.mpn:
        updates["mpn"] = item.mpn
    if item.gtin:
        updates["gtin"] = item.gtin
    if item.epid:
        updates["epid"] = item.epid
    if item.leaf_category_id:
        updates["leaf_category_id"] = item.leaf_category_id
    if item.store_joined is not None:
        updates["store_joined"] = item.store_joined
    if item.store_items_sold is not None:
        updates["store_items_sold"] = item.store_items_sold
    if item.qty_available_is_floor:
        updates["extra"] = json.dumps({"qty_available_is_floor": True},
                                      separators=(",", ":"))
    return updates
