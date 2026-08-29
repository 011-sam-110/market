"""The dashboard bundle, and the four ways it could quietly lie.

Every test here exists because a specific failure would reach the screen
looking like an answer:

- a knob rendered without its denominator reads as a measurement;
- `passed=None` serialised as `false` turns "we cannot tell" into "it failed",
  and on a `score = passed - failed` board that changes the ranking;
- `rank_products` fed niche keys instead of leaf ids returns an EMPTY board
  over a full store, and "(no rows)" reads as "no opportunities today";
- a delta bucket that overlaps another means the four counts do not sum to the
  population, and nothing on the page would say so.

Fixtures follow `tests/test_population.py`: `empty_row()` so no ROW_FIELDS
column can be silently missing, and REAL in-scope leaf ids, because
`population.build` filters niches through `catalog_ebay.top_level_of()` and a
made-up leaf would be dropped before any assertion saw it.
"""

from __future__ import annotations

import json
import math
from dataclasses import fields
from datetime import UTC, date, datetime

import pytest

from phoneloop import board, ebay
from phoneloop.dashexport import (
    build_manifest,
    dumps,
    encode_listings,
    export_coverage,
    export_deltas,
    export_fees,
    export_knobs,
    export_niches,
    product_payloads,
)
from phoneloop.dashexport.board_export import METRIC_DENOMINATORS
from phoneloop.dashexport.columnar import NULL_INDEX
from phoneloop.knobs import Knobs
from phoneloop.market_store import MarketStore, empty_row
from phoneloop.metrics import Metrics
from phoneloop.population import build

LEAF_A = "9355"    # Mobile Phones & Smartphones
LEAF_B = "25863"   # Patio & Garden Furniture
BAND = "15-55"

SEEN_A = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
SEEN_B = datetime(2026, 8, 21, 9, 0, tzinfo=UTC)
GENERATED = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def _row(source_id: str, leaf: str, seller_id: str | None,
         seen_at: datetime, **over) -> dict:
    row = empty_row("ebay", source_id)
    row.update({
        "seen_at": seen_at,
        "category": "garden_patio",
        # A shard WITH a band, so niche_key is "leaf@band" and is genuinely
        # different from the bare leaf -- which is what makes the
        # leaf-versus-key test below discriminate.
        "shard": f"{leaf}/{BAND}/p1",
        "title": f"Widget {source_id}",
        "price_pence": 3600,
        "total_pence": 4000,
        "currency": "GBP",
        "available": True,
        "region": "GB",
        "seller_id": seller_id,
        "seller_type": "business",
        "seller_feedback_bp": 9900,
        "seller_feedback_n": 500,
        "eta_days": 3,
        "leaf_category_id": leaf,
        "sold_qty": 12,
        "promoted": False,
        "origin_country": "GB",
        "url": f"https://www.ebay.co.uk/itm/{source_id}",
        "extra": '{"aspects":[],"watchers":4,"ships_from":"","stock_hint":"","sold_bound":false}',
    })
    row.update(over)
    return row


@pytest.fixture
def knobs():
    return Knobs()


@pytest.fixture
def store(tmp_path):
    s = MarketStore(tmp_path)
    s.record([
        _row("a1", LEAF_A, "sellerA", SEEN_A, sold_qty=5),
        _row("a2", LEAF_A, "sellerB", SEEN_A, sold_qty=7),
        _row("b1", LEAF_B, "sellerC", SEEN_A, sold_qty=1, total_pence=5000),
    ])
    s.record([
        _row("a1", LEAF_A, "sellerA", SEEN_B, sold_qty=9),
        _row("a2", LEAF_A, "sellerB", SEEN_B, sold_qty=4),   # counter FELL
        _row("b1", LEAF_B, "sellerC", SEEN_B, sold_qty=3, total_pence=5000),
    ])
    return s


@pytest.fixture
def b(tmp_path, store, knobs):
    return build(tmp_path, knobs)


# --- the denominator contract -------------------------------------------------

def test_every_metric_field_is_either_a_value_with_a_denominator_or_a_denominator():
    """No field of `Metrics` can reach the screen unaccounted for.

    A new metric added upstream lands in one of two buckets: it is a value and
    METRIC_DENOMINATORS names the count it was computed over, or it IS that
    count. A field in neither bucket would render as a bare number.
    """
    names = {f.name for f in fields(Metrics)}
    values = set(METRIC_DENOMINATORS)
    denominators = {v for v in METRIC_DENOMINATORS.values() if v is not None}

    unaccounted = names - values - denominators
    assert not unaccounted, (
        f"{sorted(unaccounted)} are Metrics fields with no denominator and are "
        "not denominators themselves -- add them to METRIC_DENOMINATORS"
    )
    assert values.isdisjoint(denominators - {"n_listings"}), (
        "a field cannot be both a value and another field's denominator"
    )
    for value_field, denom_field in METRIC_DENOMINATORS.items():
        assert value_field in names
        assert denom_field is None or denom_field in names


def test_every_exported_knob_carries_a_denominator(b, knobs):
    for row in export_niches(b, knobs)["rows"]:
        assert row["knobs"], "a board row with no knobs is not a board row"
        for k in row["knobs"]:
            assert "denominator" in k
            assert "coverage" in k
            assert isinstance(k["denominator"], int)


# --- unknown is null, never false ---------------------------------------------

def test_unknown_knob_serialises_as_null_not_false(b, knobs):
    """`passed=None` must survive the round trip to JSON as `null`.

    If it became `false`, every knob nobody has the data to judge would read as
    a failure, and `score = passed - failed` would rank differently.
    """
    payload = json.loads(dumps(export_niches(b, knobs)))
    seen_unknown = False
    for row in payload["rows"]:
        for k in row["knobs"]:
            assert k["passed"] in (True, False, None)
            if k["passed"] is None:
                seen_unknown = True
                assert k["unknown"] is True
                # The literal must be `null`, and `null is not false` is the
                # whole point -- an assertion on falsiness would pass either way.
                assert k["passed"] is not False
    assert seen_unknown, (
        "this fixture must contain at least one unjudgeable knob or the test "
        "asserts nothing -- sell_through has no comparable window here"
    )


def test_dumps_refuses_an_unknown_type_rather_than_stringifying():
    class Weird:
        pass

    with pytest.raises(TypeError, match="no agreed JSON form"):
        dumps({"x": Weird()})


def test_dumps_emits_no_nan_literal():
    """`json.dumps` will happily write bare `NaN`, which every browser refuses.

    That failure would appear only on the page, never in a Python test that
    round-trips with `json.loads`.
    """
    text = dumps({"coverage": float("nan"), "ratio": math.inf})
    assert "NaN" not in text and "Infinity" not in text
    assert json.loads(text) == {"coverage": None, "ratio": None}


# --- the leaf-versus-key landmine ---------------------------------------------

def test_products_are_ranked_by_leaf_id_not_niche_key(b, store, knobs):
    """The bug that printed "(no rows)" over 29,602 real listings.

    `rank_products` compares `leaf in set(niches)`. A niche key is "leaf@band"
    and matches no leaf, so passing keys returns an empty board over a full
    store -- silently, because an empty board is a legal result.

    This test discriminates: it asserts the wrong input really does produce
    nothing, so it cannot pass if the exporter is changed to use keys.
    """
    niche_rows = board.rank_niches(store, knobs, limit=100)
    keys = [r.key for r in niche_rows]
    leaves = [r.leaf_category_id for r in niche_rows]

    assert any("@" in k for k in keys), (
        "fixture must produce banded keys or this test is vacuous"
    )
    assert set(keys) != set(leaves)

    assert board.rank_products(store, knobs, keys, limit=100) == [], (
        "niche keys must NOT match leaves -- if they do, this test has stopped "
        "discriminating and the landmine is undetectable"
    )
    assert board.rank_products(store, knobs, leaves, limit=100) != []

    assert product_payloads(b, knobs)["board"]["n"] > 0


def test_board_rows_carry_the_leaf_as_a_field(b, knobs):
    """The key is deliberately unparseable; the leaf must be readable without it."""
    for row in export_niches(b, knobs)["rows"]:
        assert row["leaf_category_id"]
        assert row["leaf_category_id"] in (LEAF_A, LEAF_B)
        if row["band"]:
            assert row["key"] == f"{row['leaf_category_id']}@{row['band']}"


# --- columnar encoding --------------------------------------------------------

def test_listings_round_trip_including_nulls(b):
    payload = json.loads(dumps(encode_listings(b.rows, b.extra)))
    n = payload["n"]
    assert n == len(b.rows)

    for i, original in enumerate(b.rows):
        for col, table in payload["dict"].items():
            if col.startswith("extra_") or col == "store_joined":
                continue
            idx = payload["columns"][col][i]
            got = None if idx == NULL_INDEX else table[idx]
            want = original.get(col)
            assert got == (None if want is None else str(want)), col
        for col in ("price_pence", "sold_qty", "seller_feedback_n"):
            assert payload["columns"][col][i] == original.get(col)


def test_a_null_is_not_the_string_none(tmp_path, knobs):
    """`None` and the literal text "None" must not collide in a dictionary.

    `str(None)` is "None", so an encoder that stringifies before checking for
    null makes a brand genuinely called "None" indistinguishable from a missing
    one.
    """
    s = MarketStore(tmp_path)
    s.record([
        _row("n1", LEAF_A, "sellerA", SEEN_A, brand=None),
        _row("n2", LEAF_A, "sellerB", SEEN_A, brand="None"),
    ])
    built = build(tmp_path, knobs)
    payload = encode_listings(built.rows, built.extra)

    idx = payload["columns"]["brand"]
    table = payload["dict"]["brand"]
    assert NULL_INDEX in idx, "the null brand must encode as the null index"
    assert "None" in table, "the literal brand 'None' must survive as a value"
    for i in idx:
        if i != NULL_INDEX:
            assert table[i] == "None"


def test_url_is_only_dropped_where_the_template_reproduces_it(b):
    payload = encode_listings(b.rows, b.extra)
    dropped = {d["column"] for d in payload["dropped_columns"]}
    assert "url" in dropped
    for i, row in enumerate(b.rows):
        rebuilt = payload["url_exceptions"].get(
            str(i), payload["url_template"].format(source_id=row["source_id"]))
        assert rebuilt == row["url"]


def test_every_row_field_is_classified(b):
    """A column added to the store schema must not vanish from the bundle."""
    payload = encode_listings(b.rows, b.extra)
    unclassified = [d["column"] for d in payload["dropped_columns"]
                    if "not yet classified" in d["reason"]]
    assert not unclassified, (
        f"{unclassified} are stored but have no home in columnar.py -- give "
        "them a column group or an explicit reason for being dropped"
    )


# --- deltas -------------------------------------------------------------------

def test_delta_buckets_are_disjoint_and_total(b):
    payload = export_deltas(b)
    assert payload["state"] == "live"
    counts = payload["counts"]
    assert set(counts) == set(payload["buckets"])
    assert sum(counts.values()) == payload["n_listings"], (
        "the four buckets must partition the population, or the page can show "
        "four numbers that do not add up and say nothing about it"
    )


def test_a_falling_sold_counter_is_reported_not_hidden(b):
    """eBay's counter resets on a relist. `board` excludes negatives from the
    metrics, correctly. The EXPORT keeps them, because a quarter of the
    movement being a counter reset is a fact about the data."""
    payload = export_deltas(b)
    assert payload["counts"]["known_negative"] >= 1, (
        "fixture a2 falls from 7 to 4 -- if this is zero the negative is being "
        "swallowed somewhere between the store and the payload"
    )


def test_omitted_delta_aggregates_are_counted_not_silent(b):
    payload = export_deltas(b)
    omitted = payload["aggregates_omitted"]
    assert set(omitted) >= {"niche", "product"}
    for scope in ("niche", "product"):
        assert omitted[scope]["omitted"] >= 0
        assert omitted[scope]["of"] >= len(payload[f"by_{scope}"])


# --- fees ---------------------------------------------------------------------

def test_every_leaf_in_the_store_has_a_resolved_fee_rate(b):
    payload = export_fees(b)
    present = {r["leaf_category_id"] for r in b.rows if r.get("leaf_category_id")}
    assert present, "fixture must have leaves"
    for leaf in present:
        assert leaf in payload["fvf_bp_by_leaf"], (
            f"leaf {leaf} has no fee rate -- the browser prices margin with "
            "this map and a missing leaf silently gets no margin at all"
        )
        assert payload["fvf_bp_by_leaf"][leaf] > 0


# --- knobs --------------------------------------------------------------------

def test_knob_metadata_covers_every_field_and_flags_the_unmeasured():
    payload = export_knobs(Knobs())
    exported = {f["field"] for f in payload["fields"]}
    assert exported == {f.name for f in fields(Knobs)}

    by_field = {f["field"]: f for f in payload["fields"]}
    assert by_field["str_threshold_bp"]["provenance"] == "chosen"
    assert by_field["import_vat_term_bp"]["provenance"] == "unverified"
    for f in payload["fields"]:
        assert f["provenance"] in {"measured", "chosen", "unverified", "decision"}
        assert f["min"] <= f["value"] <= f["max"]
        assert f["env_var"] == f"PHONELOOP_{f['field'].upper()}"


def test_a_constant_threshold_collapses_but_a_varying_one_does_not(b, knobs):
    payload = product_payloads(b, knobs)["board"]
    for name, slot in payload["knobs"]["by_knob"].items():
        has_scalar = slot.get("threshold_constant") is not None
        has_array = "threshold" in slot
        assert has_scalar != has_array or not payload["n"], (
            f"{name}: exactly one of threshold_constant and the threshold array "
            "must be present, or the reader cannot tell which to trust"
        )


# --- degenerate stores --------------------------------------------------------

def test_an_empty_store_is_a_named_state_not_a_page_of_zeros(tmp_path, knobs):
    MarketStore(tmp_path)
    built = build(tmp_path, knobs)

    manifest = build_manifest(built, git_sha="deadbeef", generated_at=GENERATED,
                              schema_version=1, store_path=str(tmp_path))
    assert manifest["empty"] is True
    assert manifest["latest_date"] is None
    assert manifest["counts_by_date"] == []

    assert export_niches(built, knobs)["empty_reason"]
    assert product_payloads(built, knobs)["board"]["empty_reason"]
    # It must serialise. A crash here is the exporter deciding an empty store
    # is a bug rather than a Tuesday.
    json.loads(dumps(export_coverage(built)))


def test_a_one_date_store_locks_the_delta_rather_than_computing_one(tmp_path, knobs):
    s = MarketStore(tmp_path)
    s.record([_row("a1", LEAF_A, "sellerA", SEEN_A)])
    built = build(tmp_path, knobs)

    payload = export_deltas(built)
    assert payload["state"] == "locked"
    assert payload["unlocks_at_dates"] == 2
    assert "counts" not in payload, (
        "a locked panel must not carry a figure -- a zeroed count reads as "
        "'nothing moved', which is an answer, and there is no answer here"
    )


def test_manifest_reports_which_trend_reads_are_still_locked(b):
    manifest = build_manifest(b, git_sha="abc", generated_at=GENERATED,
                              schema_version=1, store_path="x")
    states = {u["at_dates"]: u for u in manifest["unlocks"]}
    assert states[2]["state"] == "live"
    assert states[7]["state"] == "locked"
    assert states[7]["dates_to_go"] == 5
    assert states[14]["dates_to_go"] == 12


# --- coverage -----------------------------------------------------------------

def test_coverage_reports_departments_as_an_array_never_a_mean(b):
    payload = export_coverage(b)
    per_dept = payload["call_outs"]["per_department_seller_coverage"]
    assert isinstance(per_dept, list)
    assert per_dept, "a mean cannot show one department carrying the deficit"
    for row in per_dept:
        assert "category" in row and "seller_cov" in row and "n" in row


def test_coverage_panels_never_render_unavailable_as_live(b):
    payload = export_coverage(b)
    for panel in payload["panels"]:
        assert panel["state"] in {"live", "unavailable", "error"}
        if panel["state"] == "unavailable":
            assert panel["data"] is None
            assert panel["note"], "an unavailable panel must say why"


def test_a_constant_column_is_reported_as_constant_not_dropped(b):
    """`promoted` is one value on every row in the real store. A dashboard tile
    reading "100% promoted" would be measuring nothing."""
    payload = export_coverage(b)
    by_col = {c["col"]: c for c in payload["columns"]}
    assert "promoted" in by_col
    assert by_col["promoted"]["band"] in {"CONSTANT", "DEAD"}


def test_the_whole_bundle_serialises(b, knobs):
    """Every exporter, through the real encoder, in one go.

    A type the encoder refuses is a crash at export time rather than a silently
    stringified value on the page -- but only if something actually calls it.
    """
    products = product_payloads(b, knobs)
    for payload in (
        build_manifest(b, git_sha="abc", generated_at=GENERATED,
                       schema_version=1, store_path="x"),
        export_knobs(knobs),
        export_fees(b),
        export_niches(b, knobs),
        products["board"],
        products["metrics"],
        export_coverage(b),
        export_deltas(b),
        encode_listings(b.rows, b.extra),
    ):
        assert json.loads(dumps(payload)) is not None
