"""Every marketplace, every category, one append-only table.

The original store (`phoneloop.store`) is Vinted-shaped: `item_id` is an int64
because Vinted's are, and there are columns for `favourite_count` and
`service_fee` that only Vinted has. That was right while there was one source.
It stops being right the moment the question becomes "what is this handset worth
on eBay versus what it costs on Vinted", because answering it means joining rows
that do not share a schema.

So this is the wide table: a row is *one listing seen on one marketplace at one
moment*, whatever the marketplace and whatever the category. Source-specific
fields live in `extra` as JSON rather than as columns, because a column that is
null for two sources out of three is a column pretending to be shared.

Three decisions carry the weight.

**Money is integer pence, and "unknown" is NULL, not zero.** A missing delivery
charge is not free delivery. Storing it as 0 turns "we don't know" into "it's
free", which is the direction that loses money, and it is unrecoverable once
written.

**Only changes are written.** A content hash over the fields that matter makes
re-seeing an unchanged listing a no-op. Twice-daily collection over months would
otherwise be mostly duplicate rows; instead the table is a change-log, which is
both smaller and a better answer to "when did this price move".

**Identity is (source, source_id).** eBay item numbers and Vinted item ids
collide numerically, so the key is a pair and `source_id` is a string. CeX has
no per-listing id at all, so its rows key on the box SKU.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

# What "the listing changed" means. Deliberately excludes view and favourite
# counts: they tick on every poll, and hashing them would rewrite the whole
# catalogue twice a day and defeat the point of a change-log.
_HASHED = ("source", "source_id", "title", "price_pence", "total_pence",
           "condition", "available")

SCHEMA = pa.schema([
    ("source", pa.string()),          # vinted | ebay | cex
    ("source_id", pa.string()),       # item id / SKU; string, ids collide across sources
    ("seen_at", pa.timestamp("us", tz="UTC")),
    ("category", pa.string()),        # our slug: phones, consoles, cameras...
    ("shard", pa.string()),           # the query that found it, for coverage audits
    ("title", pa.string()),
    ("price_pence", pa.int64()),      # headline, before delivery
    ("total_pence", pa.int64()),      # landed; NULL when delivery was not stated
    ("currency", pa.string()),
    ("condition", pa.string()),
    ("brand", pa.string()),
    ("model", pa.string()),           # parser slug, NULL when unparsed
    ("storage_gb", pa.int32()),
    ("grade", pa.string()),           # CeX A/B/C; empty elsewhere
    ("kind", pa.string()),            # ask | committed
    ("available", pa.bool_()),        # False once seen sold/gone
    ("region", pa.string()),          # GB when known to ship from the UK
    ("seller_id", pa.string()),
    ("url", pa.string()),
    ("extra", pa.string()),           # JSON: source-specific leftovers
    ("content_hash", pa.string()),
])

_STR_COLS = ("source", "source_id", "category", "shard", "title", "currency",
             "condition", "brand", "model", "grade", "kind", "region",
             "seller_id", "url", "extra", "content_hash")


def to_pence(value) -> int | None:
    """Money -> integer pence. None stays None; it means 'not stated'."""
    if value is None or value == "":
        return None
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return int((d * 100).quantize(Decimal("1")))


def content_hash(row: dict) -> str:
    parts = [f"{f}={row.get(f)}" for f in _HASHED]
    return hashlib.blake2b("|".join(parts).encode("utf-8"),
                           digest_size=8).hexdigest()


class MarketStore:
    """Date/source-partitioned Parquet with an in-memory hash index."""

    def __init__(self, data_dir: Path | str, max_bytes: int | None = None):
        self.data_dir = Path(data_dir)
        self.dir = self.data_dir / "market"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self._hashes: dict[tuple[str, str], str] = self._load_index()

    # --- internals ---------------------------------------------------------

    def _glob(self) -> str:
        return str(self.dir / "**" / "*.parquet").replace("\\", "/")

    def _has_data(self) -> bool:
        return any(self.dir.rglob("*.parquet"))

    def _load_index(self) -> dict[tuple[str, str], str]:
        """Latest hash per (source, source_id), so a restart is not a rewrite."""
        if not self._has_data():
            return {}
        rows = duckdb.sql(f"""
            SELECT source, source_id, content_hash FROM (
                SELECT source, source_id, content_hash,
                       row_number() OVER (PARTITION BY source, source_id
                                          ORDER BY seen_at DESC) AS rn
                FROM read_parquet('{self._glob()}', union_by_name=true)
            ) WHERE rn = 1
        """).fetchall()
        return {(s, i): h for s, i, h in rows}

    def _current_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.dir.rglob("*.parquet"))

    # --- writing -----------------------------------------------------------

    def record(self, rows: list[dict]) -> int:
        """Write only rows whose content changed. Returns rows written.

        Raises:
            RuntimeError: if the byte budget is already spent. Refusing to grow
                beats silently eating a disk that other projects share.
        """
        if not rows:
            return 0
        if self.max_bytes is not None and self._current_bytes() >= self.max_bytes:
            raise RuntimeError(
                f"market store byte budget spent: "
                f"{self._current_bytes()} >= {self.max_bytes}")

        seen_at = datetime.now(UTC)
        changed: list[dict] = []
        for r in rows:
            row = dict(r)
            row.setdefault("seen_at", seen_at)
            row["source_id"] = str(row["source_id"])
            h = content_hash(row)
            key = (row["source"], row["source_id"])
            if self._hashes.get(key) == h:
                continue
            row["content_hash"] = h
            changed.append(row)

        if not changed:
            return 0

        cols: dict[str, list] = {name: [] for name in SCHEMA.names}
        for r in changed:
            extra = r.get("extra")
            for name in SCHEMA.names:
                if name == "extra":
                    cols[name].append(
                        extra if isinstance(extra, str) or extra is None
                        else json.dumps(extra, separators=(",", ":")))
                elif name == "available":
                    cols[name].append(bool(r.get("available", True)))
                elif name == "seen_at":
                    cols[name].append(r.get("seen_at") or seen_at)
                elif name in _STR_COLS:
                    v = r.get(name)
                    cols[name].append(None if v is None else str(v))
                else:
                    v = r.get(name)
                    cols[name].append(None if v is None else int(v))

        # One file per write, partitioned by date then source. Few large
        # immutable files keeps any later sync cheap and keeps DuckDB's
        # partition pruning useful.
        day = changed[0]["seen_at"].strftime("%Y-%m-%d")
        part = self.dir / f"date={day}" / f"source={changed[0]['source']}"
        part.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%H%M%S%f")
        pq.write_table(pa.table(cols, schema=SCHEMA),
                       part / f"part-{stamp}.parquet", compression="zstd")

        for r in changed:
            self._hashes[(r["source"], r["source_id"])] = r["content_hash"]
        return len(changed)

    # --- reading -----------------------------------------------------------

    def sql(self, query: str):
        """Run SQL against the whole table, exposed as `market`."""
        if not self._has_data():
            return duckdb.sql("SELECT 1 WHERE false")
        con = duckdb.connect()
        con.execute(
            f"CREATE VIEW market AS SELECT * FROM "
            f"read_parquet('{self._glob()}', union_by_name=true)")
        return con.sql(query)

    def counts(self) -> list[tuple]:
        """Distinct listings per source and category — the coverage scoreboard."""
        if not self._has_data():
            return []
        return self.sql("""
            SELECT source, category,
                   count(DISTINCT source_id) AS listings,
                   count(*)                  AS rows,
                   min(seen_at)::DATE        AS first_seen,
                   max(seen_at)::DATE        AS last_seen
            FROM market GROUP BY 1, 2 ORDER BY listings DESC
        """).fetchall()

    def total_listings(self, source: str | None = None) -> int:
        if not self._has_data():
            return 0
        where = f"WHERE source = '{source}'" if source else ""
        return self.sql(
            f"SELECT count(DISTINCT source_id) FROM market {where}"
        ).fetchone()[0]

    def known(self, source: str) -> set[str]:
        return {sid for (src, sid) in self._hashes if src == source}

    def since(self, days: int, source: str | None = None) -> list[dict]:
        if not self._has_data():
            return []
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        where = f"AND source = '{source}'" if source else ""
        rel = self.sql(f"""
            SELECT * FROM market
            WHERE seen_at >= TIMESTAMPTZ '{cutoff}' {where}
            ORDER BY seen_at DESC
        """)
        return [dict(zip(rel.columns, row)) for row in rel.fetchall()]
