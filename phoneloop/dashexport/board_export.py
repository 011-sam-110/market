"""The ranked board, the knobs that rank it, and the fee table that prices it.

The dashboard re-thresholds the six knobs in the browser, so this module
exports the INPUTS to a verdict, never only the verdict. A niche row ships
its `price_median_pence` and that figure's denominator, not just
`price_band: PASS` -- because a slider that only had the verdict could not
move.

What the browser is trusted to recompute is the comparison and the fee
arithmetic, and nothing else. Medians, counts and coverage are computed here
by `phoneloop.metrics` and shipped as facts.
"""

from __future__ import annotations

from dataclasses import asdict, fields

from phoneloop import board, catalog_ebay, ebay, fees
from phoneloop.knobs import Knobs
from phoneloop.metrics import (
    MIN_COMPARABLE_WINDOW_FRACTION,
    STR_WINDOW_DAYS,
    Metrics,
)
from phoneloop.population import Build

# Ranking is not capped. A limit is passed because `rank_niches`/`rank_products`
# require one, and it is set far above any population this store can hold so
# that nothing is ever dropped without the payload saying so. If a real store
# ever reaches this, `capped` in the payload goes true and the UI says so
# rather than presenting a truncated board as the whole board.
_NO_CAP = 1_000_000

# Which denominator field governs which value field. The dashboard reads this
# map rather than guessing by name, and `tests/test_dashexport_board.py`
# asserts every field of `Metrics` appears here exactly once -- so a new
# metric cannot be added upstream and reach the screen without a denominator.
METRIC_DENOMINATORS: dict[str, str | None] = {
    # counts that ARE their own denominator
    "n_listings": None,
    "n_sellers": None,
    "n_business": None,
    "n_private": None,
    "sold_known": None,
    # everything else divides by something
    "coverage": "n_listings",
    "str_lifetime": "str_lifetime_denominator",
    "velocity_day1": "velocity_day1_denominator",
    "str_window_bp": "str_window_denominator",
    "velocity_win": "velocity_win_denominator",
    "success_rate": "success_rate_denominator",
    "dispersion": "dispersion_denominator",
    "new_7d": "new_7d_denominator",
    "promoted_pct": "promoted_denominator",
    "price_median_pence": "price_median_denominator",
    "price_floor_pence": "price_floor_denominator",
    "eta_median_days": "eta_median_denominator",
    "feedback_median_bp": "feedback_median_denominator",
    "feedback_median_n": "feedback_median_denominator",
    "new_account_share": "new_account_denominator",
    "str_window_days": "str_window_denominator",
}

# Slider bounds, units and provenance for every knob. `provenance` is the
# field the UI badges: three of these twelve are not measurements and saying
# so on screen is the difference between a control and a claim.
_KNOB_META: dict[str, dict] = {
    "min_margin_pct_bp": {
        "knob": 1, "label": "Min net margin", "unit": "basis_points",
        "min": 0, "max": 10000, "step": 100, "provenance": "chosen",
        "note": "Priced through fees.max_cost rather than filtered -- a margin "
                "needs a supplier cost eBay cannot give.",
    },
    "min_margin_fixed_pence": {
        "knob": 1, "label": "Min net margin (floor)", "unit": "pence",
        "min": 0, "max": 5000, "step": 50, "provenance": "chosen",
        "note": "Whichever of the two margin terms is larger wins.",
    },
    "str_threshold_bp": {
        "knob": 2, "label": "Sell-through threshold", "unit": "basis_points",
        "min": 0, "max": 20000, "step": 250, "provenance": "chosen",
        "note": "CHOSEN, never measured: the spec names sell-through as knob 2 "
                "and gives no number. Units sold per listing per window, so it "
                "is unbounded -- a listing selling twice pushes past 10000bp.",
    },
    "price_from_pence": {
        "knob": 3, "label": "Price band, from", "unit": "pence",
        "min": 0, "max": 20000, "step": 100, "provenance": "chosen",
        "note": "Landed price (item + postage), business sellers only.",
    },
    "price_to_pence": {
        "knob": 3, "label": "Price band, to", "unit": "pence",
        "min": 0, "max": 20000, "step": 100, "provenance": "chosen",
        "note": "Landed price (item + postage), business sellers only.",
    },
    "max_eta_days": {
        "knob": 4, "label": "Max delivery", "unit": "days",
        "min": 1, "max": 30, "step": 1, "provenance": "measured",
        "note": "Read off the item page, LATE end of a range, to the sweeping "
                "machine's own postcode -- a UK-buyer ETA, not a property of "
                "the listing.",
    },
    "min_seller_feedback_bp": {
        "knob": 5, "label": "Min competitor feedback", "unit": "basis_points",
        "min": 0, "max": 10000, "step": 10, "provenance": "chosen",
        "note": "Describes the competitive field; eBay shows rivals, not suppliers.",
    },
    "min_seller_feedback_n": {
        "knob": 5, "label": "Min competitor ratings", "unit": "count",
        "min": 0, "max": 1000, "step": 10, "provenance": "chosen",
        "note": "Counts above 1,000 are eBay abbreviations, accurate to about +/-50.",
    },
    "max_competitors": {
        "knob": 6, "label": "Max competitors", "unit": "count",
        "min": 1, "max": 200, "step": 1, "provenance": "chosen",
        "note": "Distinct sellers per product at the best identity tier available.",
    },
    "vat_on_fees_bp": {
        "knob": None, "label": "VAT on fees", "unit": "basis_points",
        "min": 10000, "max": 12000, "step": 2000, "provenance": "decision",
        "note": "12000 = 1.2x, not VAT-registered. 10000 = registered and "
                "reclaiming. A live business decision, not a measurement.",
    },
    "ad_rate_bp": {
        "knob": None, "label": "Promoted Listings ad rate", "unit": "basis_points",
        "min": 0, "max": 1500, "step": 10, "provenance": "chosen",
        "note": "Placeholder at 0 -- no ad spend is priced in today.",
    },
    "import_vat_term_bp": {
        "knob": None, "label": "Import VAT term", "unit": "basis_points",
        "min": 0, "max": 2000, "step": 10, "provenance": "unverified",
        "note": "UNVERIFIED (spec 13.6): whether eBay's buyer-side import VAT "
                "is additive or already inclusive on the listed price has never "
                "been established. The knob exists so answering it is config.",
    },
}


def export_knobs(knobs: Knobs) -> dict:
    """Every knob field with its value, slider bounds, unit and provenance."""
    out = []
    for f in fields(Knobs):
        meta = _KNOB_META[f.name]
        out.append({
            "field": f.name,
            "value": getattr(knobs, f.name),
            "default": f.default,
            "env_var": f"PHONELOOP_{f.name.upper()}",
            **meta,
        })
    return {
        "fields": out,
        "knob_names": ["min_margin", "sell_through", "price_band",
                       "delivery", "seller_quality", "saturation"],
        "sell_through_window": {
            "target_days": STR_WINDOW_DAYS,
            "min_comparable_fraction": MIN_COMPARABLE_WINDOW_FRACTION,
            "min_comparable_days": STR_WINDOW_DAYS * MIN_COMPARABLE_WINDOW_FRACTION,
            "note": "A window shorter than min_comparable_days is not compared "
                    "to the threshold at all. A short window under-reads sales "
                    "by construction, so the wrong answer it would give is a "
                    "CONFIDENT FAIL -- withholding is the honest output.",
        },
    }


def export_fees(b: Build) -> dict:
    """The fee table, plus a resolved FVF rate for every leaf in the store.

    Resolved here rather than in the browser because `fees.fvf_bp` walks a
    leaf to its top-level ancestor through a mapping the browser does not
    have. Every leaf present in the snapshot gets an entry, including the
    ones the catalogue does not know about -- those resolve to
    EVERYTHING_ELSE_BP, which is the WORST rate, and that is the safe
    direction: it understates max_cost rather than overstating it.
    """
    top_level_of = catalog_ebay.top_level_of()
    leaves = sorted({r["leaf_category_id"] for r in b.rows
                     if r.get("leaf_category_id")})

    resolved = {leaf: fees.fvf_bp(leaf, top_level_of) for leaf in leaves}
    unknown = [leaf for leaf in leaves if leaf not in top_level_of]

    return {
        "fvf_bp_by_top_level_name": dict(fees.FVF_BP_BY_TOP_LEVEL_NAME),
        "top_level_id_to_name": dict(fees.TOP_LEVEL_ID_TO_NAME),
        "everything_else_bp": fees.EVERYTHING_ELSE_BP,
        "regulatory_bp": fees.REGULATORY_BP,
        "per_order_pence_low": fees.PER_ORDER_PENCE_LOW,
        "per_order_pence_high": fees.PER_ORDER_PENCE_HIGH,
        "per_order_threshold_pence": fees.PER_ORDER_THRESHOLD_PENCE,
        "fvf_bp_by_leaf": resolved,
        "leaves": leaves,
        "leaves_unresolved": unknown,
        "n_leaves": len(leaves),
        "n_leaves_unresolved": len(unknown),
        "note": "The rate table is PARTIAL -- only the five rate groups the spec "
                "quotes were ever sourced, and two live fetches of eBay's fee "
                "page timed out. Anything unmatched falls to everything_else_bp "
                "on purpose.",
    }


def _metrics_dict(m: Metrics) -> dict:
    return asdict(m)


def _knob_dict(k) -> dict:
    """One KnobResult. `passed` stays tri-state: true / false / null."""
    return {
        "knob": k.knob,
        "value": k.value,
        "threshold": k.threshold,
        "passed": k.passed,          # None -> JSON null, never false
        "denominator": k.denominator,
        "coverage": k.coverage,
        "note": k.note,
        "unknown": k.passed is None,
    }


def _row_dict(r: board.BoardRow, *, band: str | None,
              members: list[str] | None) -> dict:
    return {
        "key": r.key,
        "label": r.label,
        "level": r.level,
        "identity_tier": r.identity_tier,
        # Read the leaf from this FIELD. The key is deliberately not parseable
        # back into a leaf -- a membership test against a key is what printed
        # "(no rows)" over 29,602 real listings while every test stayed green.
        "leaf_category_id": r.leaf_category_id,
        "band": band,
        "score": r.score,
        "str_source": r.str_source,
        "max_cost_at_floor_pence": r.max_cost_at_floor_pence,
        "max_cost_at_median_pence": r.max_cost_at_median_pence,
        "metrics": _metrics_dict(r.metrics),
        "knobs": [_knob_dict(k) for k in r.knobs],
        "n_passed": sum(1 for k in r.knobs if k.passed is True),
        "n_failed": sum(1 for k in r.knobs if k.passed is False),
        "n_unknown": sum(1 for k in r.knobs if k.passed is None),
        "members": members,
    }


def _bands_by_key(b: Build) -> dict[str, str | None]:
    """niche key -> the price band it was swept under, in one pass.

    A band is a property of the shard string, and `niche_key` already folds
    page number out of it, so every row of one niche agrees. Built as a map
    rather than searched per niche because the per-niche version is a scan of
    30,000 rows for each of ~130 niches.
    """
    out: dict[str, str | None] = {}
    for r in b.rows:
        leaf = r.get("leaf_category_id")
        if not leaf:
            continue
        shard = r.get("shard") or ""
        key = ebay.niche_key(leaf, shard)
        if key not in out:
            out[key] = ebay.band_of(shard)
    return out


def export_niches(b: Build, knobs: Knobs) -> dict:
    """Every niche in the store, ranked. Niche = leaf x price band."""
    if b.day is None:
        return {"rows": [], "n": 0, "capped": False, "limit": _NO_CAP,
                "empty_reason": "no eBay snapshots on file"}

    rows = board.rank_niches(b.store, knobs, difference=b.diff, limit=_NO_CAP)
    bands = _bands_by_key(b)
    out = [
        _row_dict(r, band=bands.get(r.key), members=None)
        for r in rows
    ]
    return {
        "rows": out,
        "n": len(out),
        "capped": len(out) >= _NO_CAP,
        "limit": _NO_CAP,
        "in_scope_leaves": sorted(b.in_scope),
        "note": "Ranked by knobs passed, then windowed demand, then size. A "
                "niche whose score rests on absent data scores the same as one "
                "that earned it -- see the coverage payload's "
                "green_because_broken list before reading this as a ranking.",
    }


def _dict_encode(values: list) -> tuple[list[int], list[str]]:
    """Repeated strings to integer indices; -1 is null."""
    table: dict[str, int] = {}
    out: list[int] = []
    for v in values:
        if v is None:
            out.append(-1)
            continue
        s = str(v)
        idx = table.get(s)
        if idx is None:
            idx = len(table)
            table[s] = idx
        out.append(idx)
    return out, list(table.keys())


# Fields the board itself reads. Everything else about a product lives in the
# companion metrics payload, loaded only when a row is opened -- because 27,206
# products x 56 metric fields is a payload nobody looks at on the way to
# reading the top twenty.
_PRODUCT_BOARD_SCALARS: tuple[str, ...] = (
    "score", "max_cost_at_floor_pence", "max_cost_at_median_pence",
    "n_passed", "n_failed", "n_unknown", "n_members",
)
_PRODUCT_BOARD_METRICS: tuple[str, ...] = (
    "n_listings", "n_sellers", "n_business", "n_private", "sold_known",
)


def _knobs_columnar(rows: list[board.BoardRow], knob_names: list[str]) -> dict:
    """The six knobs across every row, one array per (knob, field).

    `note` is dictionary-encoded because there are a few dozen distinct notes
    across tens of thousands of rows, and the note is the part that explains a
    `null` verdict -- dropping it to save bytes would leave every unknown on
    the board unexplained.
    """
    by_knob: dict[str, dict[str, list]] = {
        name: {"value": [], "threshold": [], "passed": [], "denominator": [],
               "coverage": [], "note_idx": []}
        for name in knob_names
    }
    notes: dict[str, int] = {}

    for r in rows:
        found = {k.knob: k for k in r.knobs}
        for name in knob_names:
            k = found.get(name)
            slot = by_knob[name]
            if k is None:
                slot["value"].append(None)
                slot["threshold"].append(None)
                slot["passed"].append(None)
                slot["denominator"].append(0)
                slot["coverage"].append(0.0)
                slot["note_idx"].append(-1)
                continue
            slot["value"].append(k.value)
            slot["threshold"].append(k.threshold)
            slot["passed"].append(k.passed)      # tri-state; None -> null
            slot["denominator"].append(k.denominator)
            slot["coverage"].append(k.coverage)
            idx = notes.get(k.note)
            if idx is None:
                idx = len(notes)
                notes[k.note] = idx
            slot["note_idx"].append(idx)

    # A threshold comes from the knob bundle, so it is the same on every row --
    # 27,206 copies of the number 5000. Collapsed to a scalar, but VERIFIED
    # rather than assumed: if a threshold ever does vary per row the array
    # stays, because silently publishing one row's threshold as everyone's
    # would make a pass look explicable when it was not.
    for name in knob_names:
        slot = by_knob[name]
        distinct = {repr(t) for t in slot["threshold"]}
        if len(distinct) == 1 and slot["threshold"]:
            slot["threshold_constant"] = slot["threshold"][0]
            del slot["threshold"]
        else:
            slot["threshold_constant"] = None

    return {
        "by_knob": by_knob,
        "notes": list(notes.keys()),
        "null_index": -1,
        "note": "Where `threshold_constant` is present the per-row `threshold` "
                "array is omitted, because every row shared that value. Where "
                "it is null the array is present because the value varied.",
    }


def _cluster_histogram(members_of: dict, rows: list[board.BoardRow]) -> dict:
    """How many listings each product is built from.

    The single most load-bearing distribution on the board: a product with one
    listing is one seller selling one thing, which carries no repeated-supply
    signal at all. If almost every product is a singleton, the product tier is
    far narrower than its row count suggests, and the reader has to be told.
    """
    hist: dict[int, int] = {}
    for r in rows:
        n = len(members_of.get((r.leaf_category_id, r.key), []))
        hist[n] = hist.get(n, 0) + 1
    total = sum(hist.values())
    singles = hist.get(1, 0)
    return {
        "histogram": [{"members": k, "products": v} for k, v in sorted(hist.items())],
        "n_products": total,
        "n_singletons": singles,
        "singleton_share": (singles / total) if total else 0.0,
        "n_two_plus": total - singles,
        "n_five_plus": sum(v for k, v in hist.items() if k >= 5),
        "note": "A singleton product is one listing by one seller. It can still "
                "score well -- score = passed - failed, and a knob with no data "
                "scores as neither -- but it is not evidence of repeatable "
                "supply. The two-plus count is the width of the real funnel.",
    }


def product_payloads(b: Build, knobs: Knobs) -> dict:
    """Both product files in one ranking pass.

    Returns {"board": ..., "metrics": ...}. Split because the board payload is
    read on every page load and the metrics payload only when a row is opened,
    and merged they are one file too big to be either.
    """
    if b.day is None:
        empty = {"n": 0, "encoding": "columnar", "columns": {}, "dict": {},
                 "empty_reason": "no eBay snapshots on file"}
        return {"board": empty, "metrics": dict(empty)}

    # Leaf IDS, never niche keys. `rank_products` compares `leaf in set(niches)`
    # and a banded key like "9355@15-55" matches no leaf, which returns an
    # empty board over a full store.
    leaves = sorted({r["leaf_category_id"] for r in b.rows
                     if r.get("leaf_category_id")})

    rows = board.rank_products(b.store, knobs, leaves,
                               difference=b.diff, limit=_NO_CAP)

    # Member ids come from the ONE grouping pass `population.build` already
    # did, so the drill-through cannot disagree with the board about which
    # listings make up a product.
    members_of: dict[tuple[str, str], list[str]] = {}
    for leaf, (groups, _tier) in b.groups.items():
        for key, prod_rows in groups.items():
            members_of[(leaf, key)] = sorted(
                str(pr["source_id"]) for pr in prod_rows if pr.get("source_id")
            )

    members = [members_of.get((r.leaf_category_id, r.key), []) for r in rows]
    knob_names = ["min_margin", "sell_through", "price_band",
                  "delivery", "seller_quality", "saturation"]

    columns: dict[str, list] = {
        "key": [r.key for r in rows],
        "label": [r.label for r in rows],
        "n_members": [len(m) for m in members],
        "score": [r.score for r in rows],
        "max_cost_at_floor_pence": [r.max_cost_at_floor_pence for r in rows],
        "max_cost_at_median_pence": [r.max_cost_at_median_pence for r in rows],
        "n_passed": [sum(1 for k in r.knobs if k.passed is True) for r in rows],
        "n_failed": [sum(1 for k in r.knobs if k.passed is False) for r in rows],
        "n_unknown": [sum(1 for k in r.knobs if k.passed is None) for r in rows],
    }
    for field in _PRODUCT_BOARD_METRICS:
        columns[field] = [getattr(r.metrics, field) for r in rows]

    dictionaries: dict[str, list[str]] = {}
    for col, values in (("leaf_category_id", [r.leaf_category_id for r in rows]),
                        ("identity_tier", [r.identity_tier for r in rows]),
                        ("str_source", [r.str_source for r in rows])):
        idx, table = _dict_encode(values)
        columns[col] = idx
        dictionaries[col] = table

    board_payload = {
        "n": len(rows),
        "encoding": "columnar",
        "float_dp": 6,
        "null_index": -1,
        "capped": len(rows) >= _NO_CAP,
        "limit": _NO_CAP,
        "n_leaves": len(leaves),
        "columns": columns,
        "dict": dictionaries,
        "knobs": _knobs_columnar(rows, knob_names),
        "knob_names": knob_names,
        "clusters": _cluster_histogram(members_of, rows),
        "scalar_fields": list(_PRODUCT_BOARD_SCALARS),
        "note": "A product is a catalogued id (gtin/epid), an inferred epid, or "
                "a title cluster within ONE leaf. Read the leaf from the "
                "leaf_category_id column, never by parsing the key. Full "
                "metrics for a row live in the companion product-metrics file.",
    }

    metric_fields = [f.name for f in fields(Metrics)]
    metrics_payload = {
        "n": len(rows),
        "encoding": "columnar",
        "float_dp": 6,
        "key": [r.key for r in rows],
        "fields": metric_fields,
        "denominators": dict(METRIC_DENOMINATORS),
        "columns": {
            name: [getattr(r.metrics, name) for r in rows]
            for name in metric_fields
        },
        "members": members,
        "note": "Positionally aligned with the product board payload by index, "
                "and by `key` for a direct lookup. Every value field has an "
                "entry in `denominators` naming the count it was computed "
                "over; a null there means the field IS a count.",
    }

    return {"board": board_payload, "metrics": metrics_payload}
