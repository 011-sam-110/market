"""Append-only observation store: Parquet on disk, DuckDB for queries.

Two design choices carry most of the weight here.

**Money is integer pence, never float.** Every price crosses the boundary as a
Decimal and is held as an int. A float would drift and quietly corrupt every
median the pricebook computes, which is the kind of bug that costs money without
ever raising.

**Only changes are written.** A content hash over the fields that actually matter
turns re-observing an unchanged listing into a no-op. The result is a change-log
rather than a snapshot history — roughly two orders of magnitude smaller, and it
answers "when did this go out of stock" directly.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

# Fields that define a listing's identity for change detection. Deliberately
# excludes favourite_count and view_count: those tick constantly, and including
# them would rewrite the whole catalogue on every poll.
_HASHED_FIELDS = ("item_id", "title", "price", "total_item_price", "condition")

_SCHEMA = pa.schema([
    ("item_id", pa.int64()),
    ("seen_at", pa.timestamp("us", tz="UTC")),
    ("title", pa.string()),
    ("price_pence", pa.int64()),
    ("total_price_pence", pa.int64()),
    ("service_fee_pence", pa.int64()),
    ("currency", pa.string()),
    ("condition", pa.string()),
    ("brand_title", pa.string()),
    ("favourite_count", pa.int32()),
    ("view_count", pa.int32()),
    ("photo_count", pa.int32()),
    ("seller_id", pa.int64()),
    ("url", pa.string()),
    ("content_hash", pa.string()),
])


def to_pence(value: Decimal | str | int | float | None) -> int:
    """Decimal -> integer pence, rounded half-up at the boundary."""
    if value is None:
        return 0
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return int((d * 100).quantize(Decimal("1")))


def from_pence(pence: int | None) -> Decimal:
    """Integer pence -> Decimal, exact."""
    return Decimal(pence or 0) / 100


def content_hash(observation: dict) -> str:
    """Stable digest over the fields that decide whether a listing changed.

    blake2b rather than a cryptographic hash — this is a change detector, not a
    security boundary, and stdlib means one less dependency.
    """
    parts = []
    for field in _HASHED_FIELDS:
        value = observation.get(field)
        if isinstance(value, Decimal):
            value = to_pence(value)
        parts.append(f"{field}={value}")
    return hashlib.blake2b("|".join(parts).encode("utf-8"), digest_size=8).hexdigest()


class Store:
    """Date-partitioned Parquet with an in-memory hash index for the hot path."""

    def __init__(self, data_dir: Path | str, max_bytes: int | None = None):
        self.data_dir = Path(data_dir)
        self.obs_dir = self.data_dir / "observations"
        self.obs_dir.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self._vanished: dict[int, datetime] = {}
        # Loading the index up front means a restarted loop does not rewrite
        # every listing it has already seen.
        self._hashes: dict[int, str] = self._load_hash_index()

    # --- internals ---------------------------------------------------------

    def _parquet_glob(self) -> str:
        return str(self.obs_dir / "**" / "*.parquet")

    def _has_data(self) -> bool:
        return any(self.obs_dir.rglob("*.parquet"))

    def _load_hash_index(self) -> dict[int, str]:
        if not self._has_data():
            return {}
        rows = duckdb.sql(f"""
            SELECT item_id, content_hash FROM (
                SELECT item_id, content_hash,
                       row_number() OVER (PARTITION BY item_id ORDER BY seen_at DESC) AS rn
                FROM read_parquet('{self._parquet_glob()}')
            ) WHERE rn = 1
        """).fetchall()
        return {int(item_id): h for item_id, h in rows}

    def _current_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.obs_dir.rglob("*.parquet"))

    # --- public API --------------------------------------------------------

    def record(self, observations: list[dict]) -> int:
        """Write only the observations whose content has changed.

        Returns:
            The number of rows actually written.

        Raises:
            RuntimeError: if the store is already over its byte budget. Refusing
                to grow is deliberate — this store shares a Drive quota with a
                client's backups, and silently expanding into it is not an option.
        """
        if self.max_bytes is not None and self._current_bytes() >= self.max_bytes:
            raise RuntimeError(
                f"store byte budget exhausted: {self._current_bytes()} >= {self.max_bytes}"
            )

        changed = []
        for o in observations:
            h = content_hash(o)
            if self._hashes.get(o["item_id"]) == h:
                continue
            changed.append((o, h))

        if not changed:
            return 0

        rows = {name: [] for name in _SCHEMA.names}
        for o, h in changed:
            seen_at = o.get("seen_at") or datetime.now(timezone.utc)
            rows["item_id"].append(int(o["item_id"]))
            rows["seen_at"].append(seen_at)
            rows["title"].append(o.get("title", ""))
            rows["price_pence"].append(to_pence(o.get("price")))
            rows["total_price_pence"].append(to_pence(o.get("total_item_price")))
            rows["service_fee_pence"].append(to_pence(o.get("service_fee")))
            rows["currency"].append(o.get("currency", "GBP"))
            rows["condition"].append(o.get("condition") or "")
            rows["brand_title"].append(o.get("brand_title") or "")
            rows["favourite_count"].append(int(o.get("favourite_count") or 0))
            rows["view_count"].append(int(o.get("view_count") or 0))
            rows["photo_count"].append(int(o.get("photo_count") or 0))
            rows["seller_id"].append(int(o.get("seller_id") or 0))
            rows["url"].append(o.get("url", ""))
            rows["content_hash"].append(h)

        # One file per write, partitioned by date. Few large immutable files is
        # the rule that keeps the eventual Drive sync cheap — Drive rate limits
        # count requests, not bytes.
        day = rows["seen_at"][0].strftime("%Y-%m-%d")
        part_dir = self.obs_dir / f"date={day}"
        part_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%H%M%S%f")
        pq.write_table(
            pa.table(rows, schema=_SCHEMA),
            part_dir / f"part-{stamp}.parquet",
            compression="zstd",
        )

        for o, h in changed:
            self._hashes[o["item_id"]] = h
        return len(changed)

    def observations_since(self, days: int) -> list[dict]:
        """Observations from the trailing window, newest first, money as Decimal."""
        if not self._has_data():
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        rel = duckdb.sql(f"""
            SELECT * FROM read_parquet('{self._parquet_glob()}')
            WHERE seen_at >= TIMESTAMPTZ '{cutoff.isoformat()}'
            ORDER BY seen_at DESC
        """)
        cols = rel.columns
        out = []
        for row in rel.fetchall():
            d = dict(zip(cols, row))
            d["price"] = from_pence(d.pop("price_pence"))
            d["total_item_price"] = from_pence(d.pop("total_price_pence"))
            d["service_fee"] = from_pence(d.pop("service_fee_pence"))
            out.append(d)
        return out

    def known_ids(self) -> set[int]:
        return set(self._hashes)

    def mark_vanished(self, item_ids: list[int], at: datetime) -> None:
        """Record that a listing left the feed.

        A disappearance is ambiguous — sold, deleted, expired, or edited out of
        our filter — so it is stored as an observation of absence rather than
        interpreted as a sale here.
        """
        for item_id in item_ids:
            self._vanished.setdefault(int(item_id), at)

    def vanished_ids(self) -> dict[int, datetime]:
        return dict(self._vanished)
