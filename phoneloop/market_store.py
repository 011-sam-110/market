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

**eBay is a snapshot log, not a change-log.** The decisions above are the
right shape for a listing whose *price* is the thing worth differencing. eBay
adds `sold_qty`, and Tier 3 of the niche-finder pipeline (design spec section
4) needs `sold_qty(today) - sold_qty(previous snapshot)` -- "the value on
day D", not "the value when it last changed". A listing whose sold count did
not move is exactly the evidence that it did not move, and a change-log
cannot express that: it would just stay silent, indistinguishable from "not
observed today at all". So `ALWAYS_SNAPSHOT_SOURCES` sources write every
observation, hash or not; Vinted and CeX keep the change-log behaviour
unchanged. At eBay's realistic sweep volume (tens of thousands of listings a
day) that is a few tens of MB of zstd parquet, not a storage problem.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

# What "the listing changed" means. Deliberately excludes view and favourite
# counts: they tick on every poll, and hashing them would rewrite the whole
# catalogue twice a day and defeat the point of a change-log. sold_qty and
# qty_available DO join this set (unlike view/favourite counts) because Tier 3
# differences exactly those two fields -- they are content, not noise.
_HASHED = ("source", "source_id", "title", "price_pence", "total_pence",
           "condition", "available", "sold_qty", "qty_available")

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
    ("region", pa.string()),          # GB when known to ship from the UK; superseded for eBay by origin_country
    ("seller_id", pa.string()),
    ("url", pa.string()),
    ("extra", pa.string()),           # JSON: source-specific leftovers
    ("content_hash", pa.string()),
    # --- eBay niche-finder columns (design spec section 8) -----------------
    # Nullable on every existing source; Vinted and CeX rows are unaffected.
    ("sold_qty", pa.int64()),             # unknown, NEVER zero. Joins _HASHED
    ("qty_available", pa.int64()),        # unknown. "More than 10" -> 10, floor flag in extra. Joins _HASHED
    ("seller_type", pa.string()),         # business | private; never inferred from feedback
    ("seller_feedback_bp", pa.int32()),   # basis points; 99.8% -> 9980. 0% positive is a real 0, not NULL
    ("seller_feedback_n", pa.int64()),    # (74.6K) -> 74600; accurate to +-50 above 1,000
    ("listed_at", pa.timestamp("us", tz="UTC")),  # API itemCreationDate / item-page startDate
    ("epid", pa.string()),                # true epid OR eBay's inferredEpid; which one is in identity_tier
    ("gtin", pa.string()),                # EAN/UPC/ISBN digits only
    ("mpn", pa.string()),                 # recorded, NEVER trusted for identity -- seller-typed MPNs are junk
    ("identity_tier", pa.string()),       # catalogued | inferred | cluster | none
    ("origin_country", pa.string()),      # ISO 3166-1 alpha-2; recorded, never filtered at sweep time
    ("promoted", pa.bool_()),             # unknown for Vinted / unreadable eBay rows; True/False both real on eBay
    ("leaf_category_id", pa.string()),    # eBay's numeric leaf id as a string; the niche key and fee lookup key
    ("eta_days", pa.int32()),             # MAXIMUM promised delivery days
    ("store_joined", pa.date32()),        # month precision only; day is always 01
    ("store_items_sold", pa.int64()),     # lifetime store total; same +-50 abbreviation caveat as feedback_n
])

# Every column of the shared row, in schema order. Both transports (the HTML
# crawler and the Browse API) produce all of them; this tuple is the single
# source of truth for "what a row is" downstream of this module.
ROW_FIELDS: tuple[str, ...] = tuple(SCHEMA.names)

# Sources whose every observation is written, because differencing needs the
# value on day D, not the value when it last changed. See the module
# docstring ("eBay is a snapshot log, not a change-log").
ALWAYS_SNAPSHOT_SOURCES: frozenset[str] = frozenset({"ebay"})

_STR_COLS = ("source", "source_id", "category", "shard", "title", "currency",
             "condition", "brand", "model", "grade", "kind", "region",
             "seller_id", "url", "extra", "content_hash",
             "seller_type", "epid", "gtin", "mpn", "identity_tier",
             "origin_country", "leaf_category_id")


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


def empty_row(source: str, source_id: str) -> dict:
    """A row dict with every ROW_FIELDS key present and NULL, so a forgotten
    column cannot be silent.

    A transport that builds `row = {"source": ..., "title": ...}` as a
    literal and forgets a field gets away with it silently -- the missing
    key and an explicit `None` look identical to `dict.get`. Building from
    this instead makes every column exist from the start, so "I forgot to
    fill listed_at" is a visible `row["listed_at"] is None` rather than a key
    that was simply never there. `source` and `source_id` are the two the
    caller must supply here -- the other never-NULL columns (`seen_at`,
    `available`, `content_hash`) are defaulted later, by the store rather
    than the transport (contracts section 1.1): the row this function
    returns still has them set to None, and `MarketStore.record` is what
    turns that None into `seen_at=now()`, `available=True` and a computed
    `content_hash`, treating "present and None" the same as "the transport
    never touched this" for every one of the three.
    """
    row: dict = {name: None for name in ROW_FIELDS}
    row["source"] = source
    row["source_id"] = source_id
    return row


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
        """Write rows; every eBay observation, only changed rows elsewhere.
        Returns rows written.

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
            # `.setdefault` alone only fires when the key is MISSING; a
            # transport built from empty_row() (the documented way to build
            # one, section 1 of the build contracts) has every ROW_FIELDS
            # key present and set to None, "seen_at" included. Treating an
            # explicit None the same as an omitted key is what "filled by
            # the store if the transport omits it" (the column's documented
            # NULL meaning) actually requires once that is how rows arrive.
            if row.get("seen_at") is None:
                row["seen_at"] = seen_at
            row["source_id"] = str(row["source_id"])
            h = content_hash(row)
            key = (row["source"], row["source_id"])
            # Snapshot sources (eBay) write every observation regardless of
            # whether the hash moved -- differencing needs "the value on day
            # D", and a listing whose sold_qty did not move is exactly the
            # evidence a change-log would swallow. Other sources keep the
            # original hash-skip behaviour unchanged.
            always = row["source"] in ALWAYS_SNAPSHOT_SOURCES
            if not always and self._hashes.get(key) == h:
                continue
            row["content_hash"] = h
            changed.append(row)

        if not changed:
            return 0

        # Group by (date, source): record() used to derive the partition
        # from changed[0] alone, which put a mixed-source or a
        # midnight-straddling batch entirely under one wrong partition.
        # eBay's always-write semantics make mixed batches (an eBay sweep and
        # a Vinted poll landing in the same call, or a run that crosses UTC
        # midnight) routine rather than a corner case, so this now has to be
        # right rather than merely usually right.
        groups: dict[tuple[str, str], list[dict]] = {}
        for r in changed:
            day = r["seen_at"].strftime("%Y-%m-%d")
            groups.setdefault((day, r["source"]), []).append(r)

        for i, ((day, source), group_rows) in enumerate(groups.items()):
            cols: dict[str, list] = {name: [] for name in SCHEMA.names}
            for r in group_rows:
                extra = r.get("extra")
                for name in SCHEMA.names:
                    if name == "extra":
                        cols[name].append(
                            extra if isinstance(extra, str) or extra is None
                            else json.dumps(extra, separators=(",", ":")))
                    elif name == "available":
                        # Never NULL: False once seen sold/gone, True
                        # otherwise. `r.get("available", True)` alone is
                        # wrong here for the same reason `.setdefault` alone
                        # was wrong for `seen_at` above: a row built from
                        # empty_row() (the contract-mandated way, section 1)
                        # has "available" PRESENT and set to None, so the
                        # dict.get default never fires and `bool(None)` silently
                        # writes every such listing as sold/gone. Treat
                        # present-and-None the same as omitted -- both mean
                        # "the transport did not observe this" -- and default
                        # that case to True, exactly like `seen_at` defaults
                        # an unset value to now() rather than leaving it NULL.
                        v = r.get("available")
                        cols[name].append(True if v is None else bool(v))
                    elif name == "promoted":
                        # Nullable, unlike `available`: unknown is a real
                        # third state on Vinted rows and on any eBay row
                        # whose promoted state could not be read, so it must
                        # NOT default to False the way `available` defaults
                        # to True.
                        v = r.get("promoted")
                        cols[name].append(None if v is None else bool(v))
                    elif name == "seen_at":
                        cols[name].append(r.get("seen_at") or seen_at)
                    elif name in ("listed_at", "store_joined"):
                        # timestamp / date columns: pass through as-is (or
                        # NULL). Unlike seen_at these have no "unknown means
                        # now" fallback -- unknown means unknown.
                        cols[name].append(r.get(name))
                    elif name in _STR_COLS:
                        v = r.get(name)
                        cols[name].append(None if v is None else str(v))
                    else:
                        v = r.get(name)
                        cols[name].append(None if v is None else int(v))

            # One file per (date, source) group. Few large immutable files
            # keeps any later sync cheap and keeps DuckDB's partition pruning
            # useful; the loop index in the filename keeps two groups
            # written in the same call from colliding if they land in the
            # same microsecond.
            part = self.dir / f"date={day}" / f"source={source}"
            part.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%H%M%S%f")
            pq.write_table(pa.table(cols, schema=SCHEMA),
                           part / f"part-{stamp}-{i}.parquet", compression="zstd")

        # Updated for every row in `changed`, including eBay's -- the index
        # is "latest hash seen", which is honest to keep current even when
        # the hash match was not the reason this row got written.
        for r in changed:
            self._hashes[(r["source"], r["source_id"])] = r["content_hash"]
        return len(changed)

    # --- reading -----------------------------------------------------------

    def sql(self, query: str):
        """Run SQL against the whole table, exposed as `market`.

        A `TIMESTAMPTZ` column (`seen_at`, `listed_at`) comes back rendered
        in this connection's session timezone, not forced to UTC -- e.g. a
        value stored as `09:30Z` fetches as `10:30 BST` on a box in that
        timezone. Same instant, so equality and duration arithmetic on the
        Python datetime are unaffected, but `.date()` on the fetched value
        can differ from the UTC date for a late-evening timestamp. The
        `seen_at`-bucketing queries in this class (`snapshot_dates`,
        `rows_on`) route around this with an explicit `AT TIME ZONE 'UTC'`
        before truncating to `::DATE`; a caller reading `listed_at` (e.g.
        Tier 3's `velocity_day1`, design spec section 4) and deriving a date
        from it needs the same explicit UTC cast, not `.date()` on the
        fetched value as-is.
        """
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

    def snapshot_dates(self, source: str = "ebay") -> list[date]:
        """Dates that have at least one row for this source, ascending -- the
        days Tier 3 (design spec section 4) can difference.

        Meaningful for any source, but named for eBay's default because a
        change-log source (Vinted, CeX) has at most one row per date a price
        actually moved, so "dates with a row" is not "dates observed" there
        the way it is for a snapshot source.
        """
        if not self._has_data():
            return []
        rows = self.sql(f"""
            SELECT DISTINCT (seen_at AT TIME ZONE 'UTC')::DATE AS d FROM market
            WHERE source = '{source}' ORDER BY d
        """).fetchall()
        return [r[0] for r in rows]

    def rows_on(self, day: date | str, *, source: str = "ebay") -> list[dict]:
        """Every row observed on one UTC date, newest observation per
        source_id wins.

        A snapshot source can carry more than one observation for the same
        listing on the same UTC date (a sweep and a re-check both landing
        before midnight); `rows_on` collapses that to one row per listing so
        a caller differencing two dates is never comparing the wrong pass
        within the same day.

        Every returned dict has all of `ROW_FIELDS` present. `read_parquet`'s
        `union_by_name=true` (see `sql()`) can only union columns that exist
        in at least one file on disk -- a store holding only legacy files
        written before this stream's 16 new columns existed returns rows
        with those keys simply absent, not None, and a caller doing
        `row["sold_qty"]` would KeyError instead of reading an honest
        "unknown". Reindexing here means a caller can always use plain
        indexing and get the documented NULL meaning instead.
        """
        if not self._has_data():
            return []
        day_str = day.isoformat() if isinstance(day, date) else str(day)
        rel = self.sql(f"""
            SELECT * EXCLUDE (rn) FROM (
                SELECT *, row_number() OVER (
                    PARTITION BY source_id ORDER BY seen_at DESC
                ) AS rn
                FROM market
                WHERE source = '{source}'
                  AND (seen_at AT TIME ZONE 'UTC')::DATE = DATE '{day_str}'
            ) WHERE rn = 1
        """)
        return [{name: d.get(name) for name in ROW_FIELDS}
                for d in (dict(zip(rel.columns, row)) for row in rel.fetchall())]
