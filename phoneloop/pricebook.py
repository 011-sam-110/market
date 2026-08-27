"""Rolling reference prices, bootstrapped from Vinted's own distribution.

No external price source is needed to start. The market's own median asking
price for an exact (make, model, storage, condition) bucket is the reference,
and it self-corrects for Vinted's price level in a way an eBay or CEX figure
would not.

**Median and MAD, never mean and standard deviation.** The distribution is
polluted at both ends — spares-or-repair handsets and outright scams below,
wishful pricing above — and a mean would chase every one of them.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from .config import Config
from .parser import PhoneSpec, parse
from .store import Store, from_pence, to_pence


@dataclass(frozen=True)
class Reference:
    median: Decimal
    mad: Decimal
    samples: int
    # True when this came from the lowest-capacity tier because the listing did
    # not state its storage. Roughly 55% of live listings, so worth surfacing.
    inferred_from_lowest_tier: bool = False


def bucket_key(spec: PhoneSpec, condition: str) -> tuple:
    """Condition comes from Vinted's own `status` field rather than the title,
    so it is passed in rather than read off the spec."""
    return (spec.make, spec.model, spec.storage_gb, (condition or "").lower())


class Pricebook:
    def __init__(self, store: Store, cfg: Config):
        self.store = store
        self.cfg = cfg
        self._buckets: dict[tuple, Reference] = {}
        # (make, model, condition) -> lowest storage tier seen, for the
        # null-storage fallback.
        self._lowest_tier: dict[tuple, int] = {}

    def refresh(self) -> int:
        """Rebuild every bucket from the trailing window.

        Returns:
            Number of buckets that cleared MIN_BUCKET_SAMPLES.
        """
        observations = self.store.observations_since(self.cfg.PRICE_WINDOW_DAYS)

        prices: dict[tuple, list[int]] = defaultdict(list)
        for row in observations:
            spec = parse(row.get("title", ""))
            if not spec.is_phone:
                continue
            # A spares-or-repair handset is a different product at a different
            # price. Letting them into the bucket would drag every median down
            # and make working phones look overpriced.
            if spec.disqualifiers:
                continue
            key = bucket_key(spec, row.get("condition", ""))
            prices[key].append(to_pence(row["total_item_price"]))

        self._buckets = {}
        self._lowest_tier = {}
        for key, pence_values in prices.items():
            if len(pence_values) < self.cfg.MIN_BUCKET_SAMPLES:
                continue
            median_p = statistics.median(pence_values)
            mad_p = statistics.median([abs(p - median_p) for p in pence_values])
            self._buckets[key] = Reference(
                median=from_pence(int(median_p)),
                mad=from_pence(int(mad_p)),
                samples=len(pence_values),
            )
            make, model, storage, condition = key
            if storage is not None:
                tier_key = (make, model, condition)
                current = self._lowest_tier.get(tier_key)
                if current is None or storage < current:
                    self._lowest_tier[tier_key] = storage

        return len(self._buckets)

    def reference(self, spec: PhoneSpec, condition: str) -> Reference | None:
        """Reference price for a listing, or None when nothing can be trusted.

        Storage is never guessed. When a listing does not state its capacity,
        it is priced against the **lowest** tier for that model — which makes
        its discount look smaller than it might truly be. That asymmetry is
        deliberate: a missed deal costs nothing, a wrong one costs money.
        """
        if not spec.is_phone:
            return None

        key = bucket_key(spec, condition)
        if key in self._buckets:
            return self._buckets[key]

        if spec.storage_gb is None:
            tier_key = (spec.make, spec.model, (condition or "").lower())
            lowest = self._lowest_tier.get(tier_key)
            if lowest is not None:
                fallback = self._buckets.get(
                    (spec.make, spec.model, lowest, (condition or "").lower())
                )
                if fallback is not None:
                    return Reference(
                        median=fallback.median,
                        mad=fallback.mad,
                        samples=fallback.samples,
                        inferred_from_lowest_tier=True,
                    )
        return None

    def stats(self) -> list[dict]:
        """Bucket coverage, densest first — what `phoneloop stats` prints."""
        return sorted(
            (
                {
                    "make": k[0], "model": k[1], "storage_gb": k[2], "condition": k[3],
                    "median": r.median, "mad": r.mad, "samples": r.samples,
                }
                for k, r in self._buckets.items()
            ),
            key=lambda d: -d["samples"],
        )
