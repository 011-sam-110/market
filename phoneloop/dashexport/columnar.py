"""Every listing on the latest snapshot, and what moved since the last one.

Row-shaped JSON repeats all 37 key names on every one of 29,863 listings,
which is most of the payload and none of the information. This module ships
columns instead: one array per field, plus a dictionary for the string
columns that repeat (12,881 sellers across 29,863 rows, 9 categories, 130
leaves). The browser rebuilds rows on demand.

Two things this module refuses to do:

It does not sample. If the payload is bigger than the target, it ships the
whole population anyway and reports the real size. A dashboard whose row
count silently depends on a byte budget is a dashboard that answers "how many
listings are there" with "it depends".

It does not filter negative sold-deltas out of the export. `board.py` filters
them out of the *metrics*, correctly -- a relist resets eBay's lifetime
counter and a negative delta is not a sale. But 251 of 4,752 known deltas on
the real pair were negative, worst -7,468 on a listing whose title and price
were identical both days, and that is a fact about the data worth putting on
screen, not a number to quietly drop.
"""

from __future__ import annotations

from datetime import date, datetime

from phoneloop import ebay
from phoneloop.market_store import ROW_FIELDS
from phoneloop.metrics import MIN_COMPARABLE_WINDOW_FRACTION, STR_WINDOW_DAYS
from phoneloop.population import Build

# Repeated-string columns worth a dictionary. Chosen by measurement, not by
# type: `title` is a string too, but it is 38,299 distinct values over 29,863
# rows, so a dictionary would be pure overhead.
DICT_COLUMNS: tuple[str, ...] = (
    "source", "category", "shard", "currency", "condition", "brand", "model",
    "grade", "kind", "region", "seller_id", "seller_type", "identity_tier",
    "origin_country", "leaf_category_id",
)

# Kept verbatim -- high cardinality, and the content the UI actually shows.
RAW_STRING_COLUMNS: tuple[str, ...] = ("source_id", "title", "epid", "gtin", "mpn")

NUMERIC_COLUMNS: tuple[str, ...] = (
    "price_pence", "total_pence", "storage_gb", "sold_qty", "qty_available",
    "seller_feedback_bp", "seller_feedback_n", "eta_days", "store_items_sold",
)

BOOL_COLUMNS: tuple[str, ...] = ("available", "promoted")

TIME_COLUMNS: tuple[str, ...] = ("seen_at", "listed_at")

DATE_COLUMNS: tuple[str, ...] = ("store_joined",)

# Flattened out of the `extra` JSON blob into real columns. The blob itself is
# then dropped, with that stated in `dropped_columns`.
EXTRA_DICT_COLUMNS: tuple[str, ...] = ("ships_from", "stock_hint")
EXTRA_NUMERIC_COLUMNS: tuple[str, ...] = ("watchers", "aspects_n")
EXTRA_BOOL_COLUMNS: tuple[str, ...] = ("sold_bound", "qty_available_is_floor")

_URL_PREFIX = "https://www.ebay.co.uk/itm/"

# Null in a dictionary-encoded column. Distinct from every real index, which
# start at 0, so a null can never be confused with the first dictionary entry.
NULL_INDEX = -1


def _encode_dict_column(values: list) -> tuple[list[int], list[str]]:
    table: dict[str, int] = {}
    out: list[int] = []
    for v in values:
        if v is None:
            out.append(NULL_INDEX)
            continue
        s = str(v)
        idx = table.get(s)
        if idx is None:
            idx = len(table)
            table[s] = idx
        out.append(idx)
    return out, list(table.keys())


def _as_epoch(v: object) -> int | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return int(v.timestamp())
    return None


def _as_iso_date(v: object) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def encode_listings(rows: list[dict], extra: list[dict]) -> dict:
    """All rows, columnar and dictionary-encoded.

    `extra` is the pre-parsed `extra` JSON per row, positionally aligned with
    `rows` -- `population.build()` already parsed it, so it is passed in
    rather than parsed a second time.
    """
    n = len(rows)
    if len(extra) != n:
        raise ValueError(
            f"extra has {len(extra)} entries for {n} rows -- these are "
            "positionally aligned and a mismatch would silently attach one "
            "listing's watchers to another listing"
        )

    columns: dict[str, list] = {}
    dictionaries: dict[str, list[str]] = {}
    dropped: list[dict] = []

    for col in DICT_COLUMNS:
        idx, table = _encode_dict_column([r.get(col) for r in rows])
        columns[col] = idx
        dictionaries[col] = table

    for col in RAW_STRING_COLUMNS:
        columns[col] = [None if r.get(col) is None else str(r.get(col)) for r in rows]

    for col in NUMERIC_COLUMNS:
        columns[col] = [None if r.get(col) is None else int(r[col]) for r in rows]

    for col in BOOL_COLUMNS:
        columns[col] = [None if r.get(col) is None else bool(r[col]) for r in rows]

    # Timestamps as epoch seconds relative to a base, so a column of 29,863
    # near-identical ISO strings becomes a column of four-digit integers.
    time_base: int | None = None
    epochs: dict[str, list[int | None]] = {}
    for col in TIME_COLUMNS:
        vals = [_as_epoch(r.get(col)) for r in rows]
        epochs[col] = vals
        present = [v for v in vals if v is not None]
        if present:
            lo = min(present)
            time_base = lo if time_base is None else min(time_base, lo)
    base = time_base or 0
    for col in TIME_COLUMNS:
        columns[col] = [None if v is None else v - base for v in epochs[col]]

    for col in DATE_COLUMNS:
        idx, table = _encode_dict_column([_as_iso_date(r.get(col)) for r in rows])
        columns[col] = idx
        dictionaries[col] = table

    # `url` is derivable from source_id -- but only if it actually is, on every
    # row. Verified rather than assumed; any row that does not match ships its
    # url explicitly, so the saving is never bought with a wrong link.
    url_exceptions: dict[str, str] = {}
    for i, r in enumerate(rows):
        url = r.get("url")
        sid = r.get("source_id")
        if url and sid and url != f"{_URL_PREFIX}{sid}":
            url_exceptions[str(i)] = str(url)
    dropped.append({
        "column": "url",
        "reason": f"derivable as url_template + source_id; verified on "
                  f"{n - len(url_exceptions)} of {n} rows, the rest are in "
                  f"url_exceptions",
    })

    dropped.append({
        "column": "content_hash",
        "reason": "a change-detection digest with no meaning on screen",
    })
    dropped.append({
        "column": "extra",
        "reason": "flattened into the extra_* columns below; the raw blob adds "
                  "no field the UI reads",
    })

    for col in EXTRA_DICT_COLUMNS:
        idx, table = _encode_dict_column([e.get(col) for e in extra])
        columns[f"extra_{col}"] = idx
        dictionaries[f"extra_{col}"] = table

    for col in EXTRA_NUMERIC_COLUMNS:
        if col == "aspects_n":
            columns["extra_aspects_n"] = [len(e.get("aspects") or []) for e in extra]
            continue
        columns[f"extra_{col}"] = [
            None if e.get(col) is None else int(e[col]) for e in extra
        ]

    for col in EXTRA_BOOL_COLUMNS:
        columns[f"extra_{col}"] = [
            None if e.get(col) is None else bool(e[col]) for e in extra
        ]

    covered = (set(DICT_COLUMNS) | set(RAW_STRING_COLUMNS) | set(NUMERIC_COLUMNS)
               | set(BOOL_COLUMNS) | set(TIME_COLUMNS) | set(DATE_COLUMNS)
               | {"url", "content_hash", "extra"})
    missing = [c for c in ROW_FIELDS if c not in covered]
    for col in missing:
        # Not silently absent: a column added to the store schema upstream
        # shows up here by name so it gets a deliberate home.
        dropped.append({
            "column": col,
            "reason": "not yet classified by phoneloop.dashexport.columnar -- "
                      "add it to a column group",
        })

    return {
        "n": n,
        "columns": columns,
        "dict": dictionaries,
        "null_index": NULL_INDEX,
        "time_base_epoch": base,
        "url_template": _URL_PREFIX + "{source_id}",
        "url_exceptions": url_exceptions,
        "dropped_columns": dropped,
        "row_fields": list(ROW_FIELDS),
        "note": "Dictionary columns hold integer indices into dict[<column>]; "
                f"{NULL_INDEX} means null. Time columns are seconds after "
                "time_base_epoch. Nothing here is sampled -- n is every listing "
                "on the latest snapshot.",
    }


# Precedence is fixed and the four buckets are disjoint, in this order:
# unknown beats bounded beats negative beats positive. Stated once here
# because four counts that overlap would not sum to the population and the
# reader would have no way to tell.
_DELTA_BUCKETS = ("unknown", "bounded_refused", "known_negative", "known_positive")


def _bucket(d) -> str:
    if not d.known or d.delta_sold is None:
        return "unknown"
    if d.sold_a_bound or d.sold_b_bound:
        return "bounded_refused"
    if d.delta_sold < 0:
        return "known_negative"
    return "known_positive"


def _zero_counts() -> dict[str, int]:
    return {b: 0 for b in _DELTA_BUCKETS}


def export_deltas(b: Build) -> dict:
    """Day-over-day sold movement, per listing and aggregated.

    Aggregated two ways, per niche and per product, each carrying the four
    disjoint counts rather than one blended "delta". A single summed delta
    hides that a quarter of the movement was a counter resetting.
    """
    if b.diff is None:
        return {
            "state": "locked",
            "reason": "a difference needs two snapshot dates and the store has "
                      f"{len(b.dates)}",
            "n_dates": len(b.dates),
            "unlocks_at_dates": 2,
        }

    diff = b.diff
    niche_of: dict[str, str] = {}
    for r in b.rows:
        leaf = r.get("leaf_category_id")
        sid = r.get("source_id")
        if leaf and sid:
            niche_of[str(sid)] = ebay.niche_key(leaf, r.get("shard") or "")

    product_of: dict[str, str] = {}
    for _leaf, (groups, _tier) in b.groups.items():
        for key, prod_rows in groups.items():
            for pr in prod_rows:
                sid = pr.get("source_id")
                if sid:
                    product_of[str(sid)] = key

    ids: list[str] = []
    sold_a: list[int | None] = []
    sold_b: list[int | None] = []
    delta: list[int | None] = []
    days: list[float] = []
    bucket_idx: list[int] = []

    by_niche: dict[str, dict] = {}
    by_product: dict[str, dict] = {}
    totals = _zero_counts()

    for sid, d in diff.listings.items():
        bucket = _bucket(d)
        totals[bucket] += 1

        ids.append(str(sid))
        sold_a.append(d.sold_a)
        sold_b.append(d.sold_b)
        delta.append(d.delta_sold)
        days.append(d.days)
        bucket_idx.append(_DELTA_BUCKETS.index(bucket))

        for scope, key in (("niche", niche_of.get(str(sid))),
                           ("product", product_of.get(str(sid)))):
            if key is None:
                continue
            target = by_niche if scope == "niche" else by_product
            agg = target.setdefault(key, {
                "key": key, "counts": _zero_counts(),
                "sum_delta_known_positive": 0,
                "sum_delta_known_negative": 0,
                "worst_negative": None,
            })
            agg["counts"][bucket] += 1
            if bucket == "known_positive":
                agg["sum_delta_known_positive"] += d.delta_sold
            elif bucket == "known_negative":
                agg["sum_delta_known_negative"] += d.delta_sold
                worst = agg["worst_negative"]
                if worst is None or d.delta_sold < worst:
                    agg["worst_negative"] = d.delta_sold

    window_days = (diff.date_b - diff.date_a).days
    min_comparable = STR_WINDOW_DAYS * MIN_COMPARABLE_WINDOW_FRACTION

    # An aggregate whose every listing is `unknown` carries no information -- it
    # is a product that was seen on one day only. There are tens of thousands of
    # those and shipping them is most of this payload. Omitted, and COUNTED, so
    # the reader sees that "1,043 products have a delta" is out of 27,206 rather
    # than being left to infer that the rest had a delta of zero.
    def _prune(aggs: dict[str, dict]) -> tuple[list[dict], int]:
        kept = [a for a in aggs.values()
                if a["counts"]["unknown"] != sum(a["counts"].values())]
        return sorted(kept, key=lambda a: a["key"]), len(aggs) - len(kept)

    niche_rows, niche_dropped = _prune(by_niche)
    product_rows, product_dropped = _prune(by_product)

    return {
        "state": "live",
        "date_a": diff.date_a,
        "date_b": diff.date_b,
        "window_days": window_days,
        "n_listings": diff.n_listings,
        "n_known": diff.n_known,
        "coverage": diff.coverage,
        "buckets": list(_DELTA_BUCKETS),
        "bucket_precedence": "unknown > bounded_refused > known_negative > "
                             "known_positive; disjoint, so the four counts sum "
                             "to n_listings",
        "counts": totals,
        "listings": {
            "source_id": ids,
            "sold_a": sold_a,
            "sold_b": sold_b,
            "delta_sold": delta,
            "days": days,
            "bucket": bucket_idx,
        },
        "by_niche": niche_rows,
        "by_product": product_rows,
        "aggregates_omitted": {
            "niche": {"omitted": niche_dropped, "of": len(by_niche)},
            "product": {"omitted": product_dropped, "of": len(by_product)},
            "reason": "every listing in the group was `unknown` -- seen on one "
                      "of the two dates only, so there is no delta to report. "
                      "Omitted is NOT a delta of zero.",
        },
        "sell_through_comparable": {
            "window_days": window_days,
            "min_comparable_days": min_comparable,
            "target_days": STR_WINDOW_DAYS,
            "comparable": window_days >= min_comparable,
            "note": "Below min_comparable_days the sell-through knob is "
                    "withheld entirely. A short window under-reads sales by "
                    "construction, so comparing it to a 30-day threshold "
                    "produces a confident FAIL for a healthy product.",
        },
        "note": "eBay's sold counter is NOT monotonic: it is a lifetime figure "
                "that resets on a relist, and a multi-variant listing renders "
                "whichever variant eBay picked, moving sold_qty and price "
                "together. Negative deltas are real observations of that, kept "
                "here and excluded from metrics.",
    }
