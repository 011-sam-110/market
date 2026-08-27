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

import re
import statistics
from dataclasses import dataclass, field

_PRICE_RE = re.compile(r"£\s*([\d,]+\.\d{2})")
_DELIVERY_RE = re.compile(r"\+\s*£\s*([\d,]+\.\d{2})\s*(?:delivery|postage)", re.I)
_FREE_RE = re.compile(r"\bfree\b.{0,24}\b(?:delivery|postage)", re.I)
_UNSPECIFIED_RE = re.compile(r"(?:postage|delivery)\s+not\s+specified", re.I)
_WATCHERS_RE = re.compile(r"(\d[\d,]*)\s+watch(?:ers|ing)", re.I)
_FROM_RE = re.compile(r"^from\s+(.+?)\s*$", re.I | re.M)
_STORAGE_RE = re.compile(r"\b(\d{2,4})\s*(GB|TB)\b", re.I)

# The aspects line is eBay's own catalogue match, e.g.
#   "Pre-owned · Apple iPhone 13 · Unlocked · 128 GB"
# It is a better identity parse than any regex over the seller's title, and it
# carries storage -- the field missing from most Vinted titles.
_ASPECT_SEP = "·"

_CONDITIONS = (
    "Pre-owned", "New (other)", "Brand New", "New with box", "New without box",
    "Open box", "Very Good - Refurbished", "Excellent - Refurbished",
    "Good - Refurbished", "Certified - Refurbished", "Refurbished",
    "For parts or not working", "New",
)


def to_pence(text: str) -> int | None:
    m = _PRICE_RE.search(text or "")
    return round(float(m.group(1).replace(",", "")) * 100) if m else None


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
    def is_used(self) -> bool:
        c = self.condition.lower()
        return "pre-owned" in c or "refurb" in c or "open box" in c or "new (other)" in c


def parse_row(text: str, url: str = "") -> EbayListing | None:
    """Parse one search-result row's visible text.

    Returns None when there is no usable price, rather than inventing one.
    """
    if not text:
        return None
    price = to_pence(text)
    if price is None:
        return None

    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    delivery: int | None = None
    if _FREE_RE.search(text):
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

    return EbayListing(
        title=lines[0] if lines else "",
        price_pence=price,
        delivery_pence=delivery,
        condition=condition,
        aspects=aspects,
        storage_gb=storage,
        watchers=int(watchers_m.group(1).replace(",", "")) if watchers_m else 0,
        ships_from="" if not from_m else from_m.group(1).strip(),
        url=url,
    )


def parse_rows(rows) -> list[EbayListing]:
    """Parse many rows, skipping any that yield nothing usable."""
    out = []
    for row in rows:
        text = row.get("text", "") if isinstance(row, dict) else str(row)
        url = row.get("href", "") if isinstance(row, dict) else ""
        listing = parse_row(text, url)
        if listing is not None:
            out.append(listing)
    return out


@dataclass(frozen=True)
class Ceiling:
    """What a handset is currently being *asked* for on eBay UK."""

    median_pence: int
    mad_pence: int
    samples: int
    overseas_excluded: int
    unpriceable_excluded: int


def retail_ceiling(listings, storage_gb: int | None = None,
                   used_only: bool = True) -> Ceiling | None:
    """Median landed asking price of comparable UK listings.

    Median and MAD rather than mean and standard deviation, as everywhere else
    here: one £1,500 outlier in a page of £200 handsets would drag a mean into
    nonsense, and search results are full of them.

    Returns None below four samples. A ceiling built on two listings is not a
    market rate, it is two people's opinions.
    """
    overseas = sum(1 for x in listings if not x.is_uk)
    pool = [x for x in listings if x.is_uk]
    if used_only:
        pool = [x for x in pool if x.is_used]
    if storage_gb is not None:
        pool = [x for x in pool if x.storage_gb == storage_gb]

    landed = [x.landed_pence for x in pool if x.landed_pence is not None]
    unpriceable = len(pool) - len(landed)
    if len(landed) < 4:
        return None

    median = int(statistics.median(landed))
    mad = int(statistics.median([abs(v - median) for v in landed]))
    return Ceiling(
        median_pence=median,
        mad_pence=mad,
        samples=len(landed),
        overseas_excluded=overseas,
        unpriceable_excluded=unpriceable,
    )


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
