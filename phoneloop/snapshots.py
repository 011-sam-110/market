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

import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date

from phoneloop.market_store import MarketStore
from phoneloop.metrics import MIN_COMPARABLE_WINDOW_FRACTION, STR_WINDOW_DAYS

#: The shortest window `metrics.check` judges sell-through on:
#: STR_WINDOW_DAYS x MIN_COMPARABLE_WINDOW_FRACTION = 24.0 days. Read from
#: metrics.py so the two cannot drift. This module does not change it.
MIN_WINDOW_DAYS: float = STR_WINDOW_DAYS * MIN_COMPARABLE_WINDOW_FRACTION

#: The smallest gap, in calendar dates, between the Tier 3 baseline and the
#: newest snapshot date. It is 25, not 24.
#:
#: A snapshot date is a UTC day, not an instant. Two observations 24 dates
#: apart can be anything from 23 to 25 days apart in elapsed time, and
#: `metrics.check` judges the elapsed time. On the prod store, the pair
#: 2026-09-10 -> 2026-10-04 (24 dates) measured a median of 23.998 days, and
#: 4,409 of the 4,726 listings seen on both dates were under 24.0 days. So a
#: 24-date baseline withholds the verdict on almost every row, and the board
#: prints "24-day window ... not comparable". With a gap of
#: ceil(MIN_WINDOW_DAYS) + 1 dates, every listing's elapsed time is more than
#: MIN_WINDOW_DAYS, whatever time of day each sweep ran.
BASELINE_MIN_GAP_DAYS: int = math.ceil(MIN_WINDOW_DAYS) + 1


def _as_date(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _sold_bound_of(row: dict | None) -> bool | None:
    """Was this row's sold_qty a lower bound ("10,000+ sold")?

    None when the row is absent, carries no `extra`, or predates ebay.py
    writing the key -- all of which mean "not known", which is deliberately
    not the same answer as False. Unparseable `extra` is treated as unknown
    rather than raising: a malformed blob on one row must not take down a
    differencing pass over 40,000.
    """
    if row is None:
        return None
    raw = row.get("extra")
    if not raw:
        return None
    try:
        extra = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return None
    if not isinstance(extra, dict) or "sold_bound" not in extra:
        return None
    return bool(extra["sold_bound"])


@dataclass(frozen=True)
class ListingDelta:
    """One listing's sold_qty movement between two snapshot dates."""

    source_id: str
    sold_a: int | None
    sold_b: int | None
    delta_sold: int | None   # None when either side is NULL or the listing is missing on a side
    days: float               # elapsed between the two observations
    known: bool                # delta_sold is not None
    # Was that side's count a LOWER BOUND ("10,000+ sold") rather than an
    # exact figure? None means the row predates ebay.py recording it, and is
    # NOT the same as False -- "we did not look" against "we looked and it
    # was exact". Reported here and acted on by the consumer, the same split
    # this module already applies to a negative delta.
    sold_a_bound: bool | None = None
    sold_b_bound: bool | None = None


@dataclass(frozen=True)
class Difference:
    """The result of differencing two snapshot dates for one source."""

    date_a: date
    date_b: date
    listings: dict[str, ListingDelta]
    n_listings: int
    n_known: int
    coverage: float
    # Listings with a row on BOTH dates, whatever their sold_qty. Not the
    # same as n_known: a listing seen on both dates with no sold count on one
    # side is "seen on both" but has no delta. Over a 25-day baseline this
    # count falls a lot, because only 65.6% of listings returned after ONE
    # night (2026-08-28 -> 08-29), so the board prints it.
    n_on_both: int = 0

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
            sold_a_bound=_sold_bound_of(ra), sold_b_bound=_sold_bound_of(rb),
        )

    n_listings = len(listings)
    coverage = (n_known / n_listings) if n_listings else 0.0
    return Difference(
        date_a=da, date_b=db, listings=listings,
        n_listings=n_listings, n_known=n_known, coverage=coverage,
        n_on_both=len(set(rows_a) & set(rows_b)),
    )


@dataclass(frozen=True)
class Baseline:
    """The two snapshot dates Tier 3 differences.

    `long_enough` is True when `date_a` is BASELINE_MIN_GAP_DAYS or more
    dates before `date_b`. When it is False, the pair is the fallback (the
    two newest dates), and the sell-through window it gives is not
    comparable to the 30-day threshold. `metrics.check` withholds that
    verdict by itself. This flag only lets the board say why.
    """

    date_a: date
    date_b: date
    long_enough: bool

    @property
    def gap_days(self) -> int:
        return (self.date_b - self.date_a).days


def pick_baseline(dates: Sequence[date | str], *,
                  min_gap_days: int = BASELINE_MIN_GAP_DAYS) -> Baseline | None:
    """Which snapshot date to difference the newest date against.

    The newest date that is `min_gap_days` or more dates before the newest
    date. When no such date exists, the two newest dates, marked
    `long_enough=False`. None when there are fewer than two dates.

    The newest date was ALWAYS differenced against the date before it,
    which on a nightly schedule is a window of about 1 day. That window can
    never pass MIN_COMPARABLE_WINDOW_FRACTION, so sell-through stayed
    unknown however many nights the store collected.
    """
    ordered = sorted({_as_date(d) for d in dates})
    if len(ordered) < 2:
        return None
    newest = ordered[-1]
    old_enough = [d for d in ordered[:-1] if (newest - d).days >= min_gap_days]
    if old_enough:
        return Baseline(date_a=old_enough[-1], date_b=newest, long_enough=True)
    return Baseline(date_a=ordered[-2], date_b=newest, long_enough=False)


def describe_baseline(baseline: Baseline, diff: Difference) -> str:
    """The Tier 3 lines the board prints above its tables: both dates, the
    gap, whether the pair is the long baseline or the fallback, and how many
    listings were seen on both dates."""
    gap = baseline.gap_days
    head = (f"Tier 3 difference: {diff.date_a} -> {diff.date_b} "
            f"({gap}-day gap)")
    if baseline.long_enough:
        why = (f"baseline = the newest snapshot {BASELINE_MIN_GAP_DAYS} or "
               f"more days before {diff.date_b}")
    else:
        why = (f"no snapshot {BASELINE_MIN_GAP_DAYS} or more days before "
               f"{diff.date_b}, so this is the fallback pair (the two newest "
               f"dates). Sell-through over it is not comparable to the "
               f"{STR_WINDOW_DAYS}-day threshold")
    return "\n".join([
        f"{head}; {why}",
        f"  {diff.n_on_both} listings seen on both dates; "
        f"{diff.n_known}/{diff.n_listings} with a known delta "
        f"({diff.coverage:.0%} coverage)",
    ])
