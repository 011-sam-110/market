"""Tier 3 of the niche-finder pipeline: sold_qty(today) - sold_qty(previous
snapshot), per listing, summed per product and per niche (design spec
section 4; contracts section 2.11).

This is what turns eBay's lifetime sold counter into a windowed sell-through.
The store (`market_store.MarketStore`) already gives us "every observation
eBay wrote, one row per (source_id, day)" via `rows_on` -- this module's only
job is to line up two of those days and say, per listing, what moved.

**Missing is unknown, never zero.** A listing present on day A and absent on
day B may have sold out, been ended by the seller, or simply fallen out of
this pass's sweep leaves -- the store cannot tell those apart, and neither
can this module. Treating an absence as "sold to zero stock" or "no sale"
would silently manufacture a number the data does not support, which is
exactly the failure mode the whole niche-finder design (spec section 6,
"missing sold is unknown, not zero") exists to avoid at every other layer
too. So a one-sided listing gets `delta_sold=None`, `known=False`, and every
aggregate here excludes it from both numerator and denominator rather than
folding it in as zero.

**A negative delta is recorded, not clamped.** `sold_qty` is eBay's lifetime
counter and a relist resets it, so `sold(day B) < sold(day A)` is a real,
if awkward, observation -- not a bug in this module. It is marked `known`
(both sides existed and were non-NULL) and carries its true negative value;
a caller that wants to treat a negative delta as a relist artefact rather
than a correction can filter it out itself, but `difference()` does not
decide that on the caller's behalf.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from phoneloop.market_store import MarketStore


def _as_date(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


@dataclass(frozen=True)
class ListingDelta:
    """One listing's sold_qty movement between two snapshot dates."""

    source_id: str
    sold_a: int | None
    sold_b: int | None
    delta_sold: int | None   # None when either side is NULL or the listing is missing on a side
    days: float               # elapsed between the two observations
    known: bool                # delta_sold is not None


@dataclass(frozen=True)
class Difference:
    """The result of differencing two snapshot dates for one source."""

    date_a: date
    date_b: date
    listings: dict[str, ListingDelta]
    n_listings: int
    n_known: int
    coverage: float

    def by_product(self, key_of: Callable[[str], str | None]) -> dict[str, tuple[int, int]]:
        """product_key -> (delta_sold, delta_days), summing only known deltas.

        `delta_days` in the aggregate is the nominal window this Difference
        covers (`date_b - date_a`, the same for every group), not a sum of
        each listing's own elapsed time -- a per-listing `days` can vary by a
        few hours depending on when a sweep pass actually touched it (see
        `ListingDelta.days`), but "how many days this window spans" is one
        number for the whole differencing operation, and that is what a
        caller dividing a summed delta into a rate actually wants. This
        reading of "sums per product" (spec section 4) is this module's own
        interpretation -- recorded in `unverified[]`.
        """
        return self._grouped_sums(key_of)

    def by_niche(self, niche_of: Callable[[str], str | None]) -> dict[str, tuple[int, int]]:
        """niche key -> (delta_sold, delta_days), summing only known deltas.

        Same aggregation and the same `delta_days` convention as
        `by_product` -- see its docstring.
        """
        return self._grouped_sums(niche_of)

    def _grouped_sums(self, key_of: Callable[[str], str | None]) -> dict[str, tuple[int, int]]:
        window_days = (self.date_b - self.date_a).days
        sums: dict[str, int] = {}
        for source_id, delta in self.listings.items():
            if not delta.known:
                continue
            key = key_of(source_id)
            if key is None:
                continue
            sums[key] = sums.get(key, 0) + delta.delta_sold
        return {key: (total, window_days) for key, total in sums.items()}


def difference(store: MarketStore, date_a: date | str, date_b: date | str, *,
               source: str = "ebay") -> Difference:
    """Per-listing sold_qty delta between two snapshot dates. NULL on either
    side is unknown, never zero.

    Reads through `market_store.MarketStore.rows_on` -- this module does not
    read parquet itself, per the ownership split (contracts section 4: s5
    "reads the store through market_store").
    """
    da = _as_date(date_a)
    db = _as_date(date_b)
    nominal_days = float((db - da).days)

    rows_a = {r["source_id"]: r for r in store.rows_on(da, source=source)}
    rows_b = {r["source_id"]: r for r in store.rows_on(db, source=source)}

    listings: dict[str, ListingDelta] = {}
    n_known = 0
    for source_id in sorted(set(rows_a) | set(rows_b)):
        ra = rows_a.get(source_id)
        rb = rows_b.get(source_id)
        sold_a = ra["sold_qty"] if ra is not None else None
        sold_b = rb["sold_qty"] if rb is not None else None

        if sold_a is not None and sold_b is not None:
            delta_sold = sold_b - sold_a
            known = True
            n_known += 1
            seen_a = ra.get("seen_at")
            seen_b = rb.get("seen_at")
            if seen_a is not None and seen_b is not None:
                days = (seen_b - seen_a).total_seconds() / 86400.0
            else:
                days = nominal_days
        else:
            delta_sold = None
            known = False
            days = nominal_days

        listings[source_id] = ListingDelta(
            source_id=source_id, sold_a=sold_a, sold_b=sold_b,
            delta_sold=delta_sold, days=days, known=known,
        )

    n_listings = len(listings)
    coverage = (n_known / n_listings) if n_listings else 0.0
    return Difference(
        date_a=da, date_b=db, listings=listings,
        n_listings=n_listings, n_known=n_known, coverage=coverage,
    )
