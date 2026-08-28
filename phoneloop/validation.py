"""The Terapeak validation table (design spec section 8; contracts section
2.12): Sam's readings from eBay Seller Hub's Terapeak Product Research
against the figures this codebase derives from the sweep, so the derived
sell-through earns trust or gets replaced (spec section 4, Tier
"VALIDATE, by hand").

Terapeak gives an official 90-day sell-through rate as a percentage, a
seller count, and an average sold price -- all by hand, one product at a
time, filtered by epid/EAN. This module never talks to Terapeak itself
(there is no API for it); it only stores what Sam typed in and compares it
to what `metrics.compute` already produced for the same product.

**Units, matched to what each side actually is.** Terapeak's sell-through is
a percentage, so it is stored in basis points like every other rate in this
codebase; its average sold price is money, so it is stored in pence.
`derived_str_lifetime` and `derived_velocity` are rates (spec section 6:
"sold_qty / sold_known", "sold / age_days"), not percentages -- a lifetime
sell-through of 3.2 means "3.2 units sold per listing over its life", a
number that can run well past 1.0, so it stays a float rather than being
coerced into a bounded percentage it is not.

**Error is expressed relative to the official figure.** `error_by_tier`
compares `derived_str_lifetime` (converted to basis points: a rate of 3.2 is
"320%", i.e. 32000bp) against `terapeak_str_90d_bp`, as a *relative* error in
basis points of the official figure -- `|derived_bp - official_bp| /
official_bp * 10000`. A derived figure that is exactly double the official
one reads as 10000bp (100%) of relative error, not as the raw basis-point
gap between two differently-scaled figures (lifetime vs. a 90-day window),
which would conflate "how wrong" with "how big the numbers happen to be".
This comparison -- and the choice of `derived_str_lifetime` over
`derived_velocity` as the figure to validate, since Terapeak has no per-day
velocity reading to check it against -- is this module's own interpretation
of spec section 4 and is recorded in `unverified[]`.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

VALIDATION_SCHEMA = pa.schema([
    ("product_key", pa.string()),
    ("identity_tier", pa.string()),
    ("derived_str_lifetime", pa.float64()),
    ("derived_velocity", pa.float64()),
    ("terapeak_str_90d_bp", pa.int32()),              # basis points
    ("terapeak_sellers", pa.int32()),
    ("terapeak_avg_sold_price_pence", pa.int64()),    # pence
    ("checked_at", pa.timestamp("us", tz="UTC")),
])


@dataclass(frozen=True)
class ValidationRecord:
    product_key: str
    identity_tier: str
    derived_str_lifetime: float | None
    derived_velocity: float | None
    terapeak_str_90d_bp: int | None
    terapeak_sellers: int | None
    terapeak_avg_sold_price_pence: int | None
    checked_at: datetime


@dataclass(frozen=True)
class TierError:
    identity_tier: str
    n: int
    median_abs_error_bp: int | None   # |derived - official| as basis points of the official figure
    median_signed_error_bp: int | None


def _round_half_up(x: float) -> int:
    """Ties away from zero. `x` is a basis-point ratio, never money -- money
    (terapeak_avg_sold_price_pence) is never computed here, only stored."""
    return math.floor(x + 0.5) if x >= 0 else -math.floor(-x + 0.5)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


class ValidationTable:
    """Sam's Terapeak readings against the derived figures, so derived STR
    earns trust or gets replaced.

    One parquet file per `add()` call, the same append-only shape
    `market_store.py` uses and for the same reason: a validation reading is
    never edited after the fact, only added to, so there is nothing to
    overwrite and nothing to lock.
    """

    def __init__(self, data_dir: Path | str):
        self.data_dir = Path(data_dir)
        self.dir = self.data_dir / "validation"
        self.dir.mkdir(parents=True, exist_ok=True)

    def add(self, record: ValidationRecord | Mapping) -> int:
        rec = record if isinstance(record, ValidationRecord) else ValidationRecord(**dict(record))
        d = asdict(rec)
        table = pa.table({name: [d[name]] for name in VALIDATION_SCHEMA.names},
                         schema=VALIDATION_SCHEMA)
        stamp = rec.checked_at.strftime("%Y%m%dT%H%M%S%f")
        path = self.dir / f"part-{stamp}.parquet"
        n = 1
        while path.exists():
            # Two readings checked in the same microsecond (a batch import,
            # or a test) must not silently overwrite one another.
            n += 1
            path = self.dir / f"part-{stamp}-{n}.parquet"
        pq.write_table(table, path)
        return 1

    def list(self) -> list[dict]:
        files = sorted(self.dir.glob("*.parquet"))
        if not files:
            return []
        table = pa.concat_tables([pq.read_table(f) for f in files])
        return table.to_pylist()

    def error_by_tier(self) -> dict[str, TierError]:
        """Derived-vs-official error per identity tier. A tier with no rows
        is absent, not zero-error -- see the module docstring for the
        arithmetic and MEDIAN, not mean, so one wildly-off reading cannot
        single-handedly certify or condemn a tier.
        """
        grouped: dict[str, list[dict]] = {}
        for row in self.list():
            grouped.setdefault(row["identity_tier"], []).append(row)

        result: dict[str, TierError] = {}
        for tier, rows in grouped.items():
            signed_errors: list[float] = []
            for row in rows:
                derived = row["derived_str_lifetime"]
                official_bp = row["terapeak_str_90d_bp"]
                if derived is None or official_bp is None or official_bp == 0:
                    continue
                derived_bp = derived * 10000.0
                signed_errors.append((derived_bp - official_bp) / official_bp * 10000.0)

            result[tier] = TierError(
                identity_tier=tier,
                n=len(rows),
                median_abs_error_bp=(
                    _round_half_up(_median([abs(e) for e in signed_errors]))
                    if signed_errors else None
                ),
                median_signed_error_bp=(
                    _round_half_up(_median(signed_errors))
                    if signed_errors else None
                ),
            )
        return result
