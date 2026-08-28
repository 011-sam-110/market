"""Store tests.

Two properties matter here: money never drifts, and an unchanged listing costs
nothing to re-observe. The second is what keeps a change-log ~100x smaller than
a snapshot history.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from phoneloop.store import Store, content_hash


def obs(item_id=1, price="150.00", total="157.50", condition="Very good",
        title="iPhone 13 128GB", seen_at=None):
    return {
        "item_id": item_id,
        "seen_at": seen_at or datetime.now(timezone.utc),
        "title": title,
        "price": Decimal(price),
        "total_item_price": Decimal(total),
        "service_fee": Decimal("7.50"),
        "currency": "GBP",
        "condition": condition,
        "brand_title": "Apple",
        "favourite_count": 3,
        "view_count": 40,
        "photo_count": 4,
        "seller_id": 999,
        "url": "https://www.vinted.co.uk/items/1-iphone-13",
    }


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)


def test_first_observation_is_written(store):
    assert store.record([obs()]) == 1


def test_unchanged_listing_writes_no_row(store):
    store.record([obs()])
    assert store.record([obs()]) == 0, "re-observing an unchanged listing must be free"


def test_price_change_writes_a_row(store):
    store.record([obs(price="150.00")])
    assert store.record([obs(price="140.00")]) == 1


def test_condition_change_writes_a_row(store):
    store.record([obs(condition="Very good")])
    assert store.record([obs(condition="Good")]) == 1


def test_money_survives_a_roundtrip_exactly(store):
    """Prices are held as integer pence. A float would drift and quietly
    corrupt every median in the pricebook."""
    store.record([obs(price="199.99", total="209.98")])
    rows = store.observations_since(days=1)
    assert rows[0]["price"] == Decimal("199.99")
    assert rows[0]["total_item_price"] == Decimal("209.98")


def test_awkward_pence_values_roundtrip(store):
    for amount in ["0.01", "3.43", "1234.56", "0.10"]:
        s = Store(store.data_dir / amount.replace(".", "_"))
        s.record([obs(price=amount, total=amount)])
        assert s.observations_since(days=1)[0]["price"] == Decimal(amount)


def test_hashes_survive_restart(store):
    """A restarted loop must not rewrite every listing it has already seen."""
    store.record([obs()])
    reopened = Store(store.data_dir)
    assert reopened.record([obs()]) == 0


def test_content_hash_ignores_volatile_fields(store):
    """Favourites and views tick constantly. If they fed the hash, every poll
    would rewrite the entire catalogue."""
    a = obs(); a["favourite_count"] = 3; a["view_count"] = 10
    b = obs(); b["favourite_count"] = 99; b["view_count"] = 900
    assert content_hash(a) == content_hash(b)


def test_vanished_listings_recorded(store):
    store.record([obs(item_id=1), obs(item_id=2)])
    at = datetime.now(timezone.utc)
    store.mark_vanished([1], at)
    vanished = store.vanished_ids()
    assert 1 in vanished and 2 not in vanished


def test_observations_since_respects_window(store):
    old = datetime.now(timezone.utc) - timedelta(days=10)
    store.record([obs(item_id=1, seen_at=old)])
    store.record([obs(item_id=2)])
    recent = store.observations_since(days=2)
    assert {r["item_id"] for r in recent} == {2}


def test_byte_budget_refuses_to_grow(store):
    """Hard ceiling so a runaway loop can never consume the shared Drive quota
    the EV client's backups depend on.

    Semantics are check-before-write: the write that crosses the ceiling is
    allowed, and every write after it is refused. Checking after the fact would
    mean discovering the overrun only once the disk was already full.
    """
    assert store.record([obs()]) == 1     # under budget, writes fine
    store.max_bytes = 1                   # now over
    with pytest.raises(RuntimeError, match="byte budget"):
        store.record([obs(price="99.00")])


def test_byte_budget_of_none_is_unlimited(store):
    store.max_bytes = None
    assert store.record([obs()]) == 1


# ---------------------------------------------------------------------------
# MarketStore -- the shared eBay/Vinted/CeX table (phoneloop/market_store.py)
#
# `tests/test_store.py` on master tests the OLD phoneloop/store.py, above.
# market_store.py is the wide table the niche-finder design (design spec
# section 8, contracts section 1) adds columns and snapshot semantics to.
# Added here per the build contract rather than a separate file, so both
# stores' tests live where the next reader expects "store tests" to be.
# ---------------------------------------------------------------------------

from datetime import UTC as _UTC
from datetime import date as _date
from datetime import datetime as _datetime

import pyarrow as pa
import pyarrow.parquet as pq

from phoneloop.market_store import (
    ALWAYS_SNAPSHOT_SOURCES,
    ROW_FIELDS,
    SCHEMA,
    MarketStore,
    content_hash as market_content_hash,
    empty_row,
)


def ebay_row(source_id="111", sold_qty=None, price_pence=1999, title="Wireless Charger 15W"):
    row = empty_row("ebay", source_id)
    row["title"] = title
    row["price_pence"] = price_pence
    row["total_pence"] = price_pence + 200
    row["currency"] = "GBP"
    row["condition"] = "New"
    row["available"] = True
    row["sold_qty"] = sold_qty
    return row


def vinted_row(source_id="222", price_pence=1999, title="iPhone 13 128GB"):
    row = empty_row("vinted", source_id)
    row["title"] = title
    row["price_pence"] = price_pence
    row["total_pence"] = price_pence
    row["currency"] = "GBP"
    row["condition"] = "Very good"
    row["available"] = True
    return row


@pytest.fixture
def market_store(tmp_path):
    return MarketStore(tmp_path)


def test_row_fields_has_37_entries_matching_schema_names_in_order():
    # `ROW_FIELDS == tuple(SCHEMA.names)` alone is tautological -- ROW_FIELDS
    # is *defined* as tuple(SCHEMA.names) in market_store.py, so that
    # assertion can never fail on its own. Pin the actual expected order
    # from contracts section 1.1 (21 existing columns, unchanged, then the
    # 16 new ones in the order the contract lists them) so a column that
    # gets silently reordered or renamed is caught here.
    assert len(ROW_FIELDS) == 37
    assert ROW_FIELDS == tuple(SCHEMA.names)
    assert ROW_FIELDS == (
        "source", "source_id", "seen_at", "category", "shard", "title",
        "price_pence", "total_pence", "currency", "condition", "brand",
        "model", "storage_gb", "grade", "kind", "available", "region",
        "seller_id", "url", "extra", "content_hash",
        "sold_qty", "qty_available", "seller_type", "seller_feedback_bp",
        "seller_feedback_n", "listed_at", "epid", "gtin", "mpn",
        "identity_tier", "origin_country", "promoted", "leaf_category_id",
        "eta_days", "store_joined", "store_items_sold",
    )


def test_empty_row_produces_every_row_fields_key():
    row = empty_row("ebay", "12345")
    assert set(row.keys()) == set(ROW_FIELDS)
    assert row["source"] == "ebay"
    assert row["source_id"] == "12345"
    # every other column is NULL, so a forgotten column cannot be silent
    for name in ROW_FIELDS:
        if name in ("source", "source_id"):
            continue
        assert row[name] is None, f"{name} should be NULL in an empty row"


def test_empty_row_built_row_records_as_available(market_store):
    """A row built exactly the contract-mandated way -- straight from
    empty_row(), never touching "available" by hand -- must record as
    available=True, never False.

    This is deliberately NOT using the ebay_row()/vinted_row() helpers
    above: both of them set row["available"] = True manually, which is
    precisely why they could not catch the bug this guards. `empty_row()`
    sets "available" PRESENT and None (not absent), so `r.get("available",
    True)` alone returns None, not the default -- `bool(None)` is False,
    silently recording every real transport's listing as sold/gone unless
    the transport remembers to set "available" itself. Contract row 16:
    "never NULL; False once seen sold/gone" -- a row nobody has observed
    sold/gone must default True, the same way an omitted seen_at defaults
    to now() rather than staying NULL.
    """
    row = empty_row("ebay", "avail-1")
    row["title"] = "Wireless Charger 15W"
    row["price_pence"] = 1999
    row["sold_qty"] = 4
    assert row["available"] is None, "empty_row must leave available untouched (None)"

    market_store.record([row])
    stored = market_store.since(days=1, source="ebay")
    assert len(stored) == 1
    assert stored[0]["available"] is True, (
        "a row nobody marked sold/gone must not be stored as unavailable"
    )


def test_ebay_same_sold_qty_writes_two_rows_snapshot_semantics_guard(market_store):
    """The snapshot-semantics guard (build contracts, acceptance section):
    an eBay listing observed twice with the SAME sold_qty must still write
    TWO rows, because Tier 3 differencing needs "the value on day D" even
    when the value did not move -- that not-moving IS the observation.
    """
    market_store.record([ebay_row(sold_qty=5)])
    written = market_store.record([ebay_row(sold_qty=5)])
    assert written == 1, "the second identical eBay observation must still be written"
    assert len(market_store.since(days=1, source="ebay")) == 2, (
        "both observations must actually be queryable, not just counted"
    )


def test_vinted_same_content_writes_one_row(market_store):
    """The control for the guard above: Vinted keeps the original
    change-log behaviour -- an unchanged listing costs nothing to re-observe."""
    market_store.record([vinted_row()])
    written = market_store.record([vinted_row()])
    assert written == 0, "an unchanged Vinted listing must NOT be rewritten"


def test_ebay_changed_sold_qty_writes_two_rows_with_different_hashes(market_store):
    """The _HASHED guard: sold_qty joins the hashed field set, so a listing
    whose sold_qty actually moved gets a different content_hash on its
    second row (on top of always being written for eBay regardless)."""
    market_store.record([ebay_row(sold_qty=5)])
    rows = market_store.since(days=1, source="ebay")
    first_hash = rows[0]["content_hash"]

    market_store.record([ebay_row(sold_qty=8)])
    rows = market_store.since(days=1, source="ebay")
    assert len(rows) == 2
    hashes = {r["content_hash"] for r in rows}
    assert len(hashes) == 2, "a different sold_qty must produce a different content_hash"
    assert first_hash in hashes


def test_batch_with_ebay_and_vinted_rows_partitions_by_source(market_store):
    """The partitioning guard: record() must group by (date, source) rather
    than deriving the whole batch's partition from changed[0] -- a mixed
    batch is now routine once eBay always writes, not a corner case."""
    market_store.record([ebay_row(source_id="e1"), vinted_row(source_id="v1")])

    ebay_dirs = list(market_store.dir.glob("date=*/source=ebay/*.parquet"))
    vinted_dirs = list(market_store.dir.glob("date=*/source=vinted/*.parquet"))
    assert len(ebay_dirs) == 1
    assert len(vinted_dirs) == 1

    ebay_rows = market_store.since(days=1, source="ebay")
    vinted_rows = market_store.since(days=1, source="vinted")
    assert {r["source_id"] for r in ebay_rows} == {"e1"}
    assert {r["source_id"] for r in vinted_rows} == {"v1"}


def test_market_store_money_roundtrips_and_none_stays_none(market_store):
    row = ebay_row(source_id="m1", price_pence=1234)
    row["total_pence"] = None  # delivery not stated
    market_store.record([row])
    rows = market_store.since(days=1, source="ebay")
    assert rows[0]["price_pence"] == 1234
    assert rows[0]["total_pence"] is None, "unstated delivery must stay NULL, never become 0"


def test_content_hash_is_stable_over_the_hashed_fields():
    a = ebay_row(sold_qty=3)
    b = ebay_row(sold_qty=3)
    assert market_content_hash(a) == market_content_hash(b)

    # An UN-hashed field (view/favourite counts, stored in `extra`) must not
    # move the hash -- that is the entire point of _HASHED excluding it.
    b["extra"] = '{"view_count": 999}'
    assert market_content_hash(a) == market_content_hash(b), (
        "a field outside _HASHED changed the hash"
    )


def test_snapshot_dates_lists_days_with_at_least_one_ebay_row(market_store):
    day1 = _datetime(2026, 8, 20, 10, 0, tzinfo=_UTC)
    day2 = _datetime(2026, 8, 21, 10, 0, tzinfo=_UTC)
    row1 = ebay_row(source_id="s1")
    row1["seen_at"] = day1
    row2 = ebay_row(source_id="s2")
    row2["seen_at"] = day2
    market_store.record([row1, row2])
    assert market_store.snapshot_dates("ebay") == [_date(2026, 8, 20), _date(2026, 8, 21)]


def test_rows_on_returns_newest_observation_per_listing(market_store):
    """Two observations of the same listing land on the same UTC date
    (a sweep and a re-check); rows_on must collapse them to the newest."""
    day = _datetime(2026, 8, 20, 8, 0, tzinfo=_UTC)
    later = _datetime(2026, 8, 20, 20, 0, tzinfo=_UTC)
    early = ebay_row(source_id="dup", sold_qty=2)
    early["seen_at"] = day
    market_store.record([early])
    late = ebay_row(source_id="dup", sold_qty=4)
    late["seen_at"] = later
    market_store.record([late])

    rows = market_store.rows_on(_date(2026, 8, 20), source="ebay")
    assert len(rows) == 1
    assert rows[0]["sold_qty"] == 4


def test_snapshot_dates_and_rows_on_split_a_midnight_straddle_in_utc(market_store):
    """Timezone guard: both queries must bucket by UTC date, never the
    DuckDB session timezone. A naive `seen_at::DATE` evaluates in
    whatever local timezone the box is set to (Europe/London on this
    machine, which is UTC+1 under BST) and would merge these two rows
    into the wrong single day -- the write path already partitions on
    disk by `seen_at.strftime("%Y-%m-%d")` in UTC, so the read path
    disagreeing with it silently hides or double-counts an observation
    across every midnight while BST is in effect."""
    late = ebay_row(source_id="late", sold_qty=1)
    late["seen_at"] = _datetime(2026, 8, 20, 23, 59, tzinfo=_UTC)
    early = ebay_row(source_id="early", sold_qty=1)
    early["seen_at"] = _datetime(2026, 8, 21, 0, 1, tzinfo=_UTC)
    market_store.record([late, early])

    assert market_store.snapshot_dates("ebay") == [_date(2026, 8, 20), _date(2026, 8, 21)]

    day1 = market_store.rows_on(_date(2026, 8, 20), source="ebay")
    day2 = market_store.rows_on(_date(2026, 8, 21), source="ebay")
    assert [r["source_id"] for r in day1] == ["late"]
    assert [r["source_id"] for r in day2] == ["early"]


def test_always_snapshot_sources_contains_only_ebay():
    assert ALWAYS_SNAPSHOT_SOURCES == frozenset({"ebay"})


_LEGACY_SCHEMA = pa.schema([
    ("source", pa.string()), ("source_id", pa.string()),
    ("seen_at", pa.timestamp("us", tz="UTC")), ("category", pa.string()),
    ("shard", pa.string()), ("title", pa.string()),
    ("price_pence", pa.int64()), ("total_pence", pa.int64()),
    ("currency", pa.string()), ("condition", pa.string()),
    ("brand", pa.string()), ("model", pa.string()),
    ("storage_gb", pa.int32()), ("grade", pa.string()),
    ("kind", pa.string()), ("available", pa.bool_()),
    ("region", pa.string()), ("seller_id", pa.string()),
    ("url", pa.string()), ("extra", pa.string()),
    ("content_hash", pa.string()),
])


def test_rows_on_reindexes_a_legacy_21_column_only_store_to_all_37_keys(market_store):
    """A store holding ONLY pre-this-stream (21-column) parquet files --
    the real live store's actual shape as of this stream, before any eBay
    row has ever been written -- must still return dicts with every
    ROW_FIELDS key. `read_parquet(..., union_by_name=true)` can only union
    columns that exist in at least one file on disk, so without the
    reindex in rows_on(), row["sold_qty"] would KeyError here instead of
    reading None, and s5 has no reason to expect that on a source this
    stream is meant to make safe to read.
    """
    day_dir = market_store.dir / "date=2026-08-20" / "source=ebay"
    day_dir.mkdir(parents=True, exist_ok=True)
    legacy_row = {
        "source": ["ebay"], "source_id": ["legacy-1"],
        "seen_at": [_datetime(2026, 8, 20, 10, 0, tzinfo=_UTC)],
        "category": [None], "shard": [None], "title": ["Old-style row"],
        "price_pence": [999], "total_pence": [999], "currency": ["GBP"],
        "condition": ["New"], "brand": [None], "model": [None],
        "storage_gb": [None], "grade": [None], "kind": ["ask"],
        "available": [True], "region": [None], "seller_id": [None],
        "url": [None], "extra": [None], "content_hash": ["deadbeef"],
    }
    pq.write_table(pa.table(legacy_row, schema=_LEGACY_SCHEMA),
                   day_dir / "part-legacy.parquet", compression="zstd")

    rows = market_store.rows_on(_date(2026, 8, 20), source="ebay")
    assert len(rows) == 1
    assert set(rows[0].keys()) == set(ROW_FIELDS)
    assert rows[0]["title"] == "Old-style row"
    assert rows[0]["sold_qty"] is None
    assert rows[0]["promoted"] is None
    assert rows[0]["store_joined"] is None
