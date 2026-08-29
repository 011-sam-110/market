"""Ground truth for the browser's copy of the knob arithmetic.

The dashboard re-thresholds knobs client-side, which means `fees.max_cost` and
the six comparisons in `metrics.check` exist twice: here in Python, and again
in TypeScript. Two implementations of the same rule drift, and the drift is
invisible -- the page would show a confident PASS that the pipeline calls a
FAIL, and every test on both sides would stay green.

This module builds the fixture that makes that impossible: real rows out of the
real store, run through the real Python, with every output recorded. The
TypeScript side replays it and must reproduce every field including the nulls;
a Python-side change that is not mirrored fails `tests/test_knob_golden.py`
because the committed file no longer matches what the code produces.

Cases are drawn from the store rather than invented, then topped up with the
awkward ones a sample would miss: a denominator of zero, a figure sitting
exactly on its threshold, a `max_cost` that goes negative (it is deliberately
not clamped), and a leaf the fee table has never heard of.
"""

from __future__ import annotations

from dataclasses import asdict, fields, replace

from phoneloop import board, catalog_ebay, fees
from phoneloop.knobs import Knobs
from phoneloop.metrics import Metrics, check
from phoneloop.population import Build

GOLDEN_SCHEMA_VERSION = 1

# The ONLY `Metrics` fields `check` and `max_cost` consume. The fixture carries
# these and nothing else, for two reasons.
#
# The first is correctness: several other metrics are derived from the wall
# clock (`velocity_day1` divides by a listing's age *now*), so a fixture
# carrying the whole dataclass regenerates differently every second and the
# drift alarm fires on every run until somebody deletes it.
#
# The second is documentation: this tuple IS the input surface the TypeScript
# engine has to accept. `tests/test_knob_golden.py` proves the list is complete
# by perturbing every EXCLUDED field and asserting the verdicts do not move.
CHECK_INPUT_FIELDS: tuple[str, ...] = (
    "n_listings",
    "price_median_pence", "price_median_denominator",
    "price_floor_pence", "price_floor_denominator",
    "str_window_bp", "str_window_denominator", "str_window_days",
    "str_lifetime", "str_lifetime_denominator",
    "eta_median_days", "eta_median_denominator",
    "feedback_median_bp", "feedback_median_n", "feedback_median_denominator",
)


def check_inputs(m: Metrics) -> dict:
    """Just the fields a verdict depends on, in a stable order."""
    return {name: getattr(m, name) for name in CHECK_INPUT_FIELDS}


def metrics_from_inputs(inputs: dict) -> Metrics:
    """Rebuild a Metrics from a fixture case: the recorded inputs, everything
    else at a neutral unknown. Nothing outside CHECK_INPUT_FIELDS can change a
    verdict, which is exactly what the completeness test asserts."""
    return _metrics(**inputs)

# Defaults plus one perturbation per tunable knob. Each setting must actually
# move a verdict somewhere in the case set or it is testing nothing; the
# exporter checks that and says so.
KNOB_SETTINGS: tuple[tuple[str, dict], ...] = (
    ("defaults", {}),
    ("margin_doubled", {"min_margin_pct_bp": 5000}),
    ("sell_through_relaxed", {"str_threshold_bp": 100}),
    ("price_band_wide", {"price_from_pence": 100, "price_to_pence": 50000}),
    ("delivery_strict", {"max_eta_days": 2}),
    ("seller_quality_strict", {"min_seller_feedback_bp": 9990,
                               "min_seller_feedback_n": 5000}),
    ("saturation_tight", {"max_competitors": 2}),
    ("vat_registered", {"vat_on_fees_bp": 10000}),
)


def _signature(m: Metrics) -> tuple:
    """What makes a case interesting: which figures exist at all.

    Cases are bucketed on this so the sample spans the shape of the data
    instead of forty near-identical rows off the top of the board.
    """
    return (
        m.price_median_pence is not None,
        m.price_floor_pence is not None,
        m.eta_median_days is not None,
        m.feedback_median_bp is not None,
        m.str_window_bp is not None,
        m.str_lifetime is not None,
        m.n_listings > 1,
    )


def cases_from_store(b: Build, knobs: Knobs, *, per_bucket: int = 4,
                     limit: int = 60) -> list[dict]:
    """Real rows, spanning the shapes the store actually contains.

    Deterministic: rows are bucketed by which figures exist and taken in key
    order, never sampled randomly, because the committed fixture is compared
    byte for byte and a shuffled sample would fail that every run.
    """
    if b.day is None:
        return []

    leaves = sorted({r["leaf_category_id"] for r in b.rows
                     if r.get("leaf_category_id")})
    rows = (board.rank_niches(b.store, knobs, difference=b.diff, limit=100000)
            + board.rank_products(b.store, knobs, leaves,
                                  difference=b.diff, limit=100000))

    top_level_of = catalog_ebay.top_level_of()
    buckets: dict[tuple, list] = {}
    for r in sorted(rows, key=lambda r: (r.level, r.key)):
        buckets.setdefault(_signature(r.metrics), []).append(r)

    out: list[dict] = []
    for sig in sorted(buckets, key=repr):
        for r in buckets[sig][:per_bucket]:
            out.append({
                "id": f"{r.level}:{r.key}",
                "source": "store",
                "leaf_category_id": r.leaf_category_id,
                "fvf_bp": fees.fvf_bp(r.leaf_category_id, top_level_of),
                "competitors": r.metrics.n_sellers,
                "metrics": check_inputs(r.metrics),
            })
    return out[:limit]


def _metrics(**over) -> Metrics:
    """A Metrics with every field at a neutral unknown, then the overrides.

    Built field by field from the dataclass rather than a literal, so a new
    metric upstream lands here as an explicit None instead of a TypeError that
    somebody fixes by deleting the case.
    """
    base: dict = {}
    for f in fields(Metrics):
        if f.name.endswith("_denominator") or f.name.startswith("n_") \
                or f.name in ("sold_known", "new_7d"):
            base[f.name] = 0
        elif f.name == "coverage":
            base[f.name] = 0.0
        else:
            base[f.name] = None
    base.update(over)
    return Metrics(**base)


def constructed_cases(knobs: Knobs) -> list[dict]:
    """The edges a sample of real rows will not reliably contain."""
    k = knobs
    cases = [
        ("all_unknown", _metrics(), "9355", 0,
         "every denominator zero -- every knob must read null, not false"),

        ("price_exactly_on_lower_bound",
         _metrics(price_median_pence=k.price_from_pence, price_median_denominator=3,
                  n_listings=3, coverage=1.0),
         "9355", 1, "a figure sitting exactly on its threshold"),

        ("price_exactly_on_upper_bound",
         _metrics(price_median_pence=k.price_to_pence, price_median_denominator=3,
                  n_listings=3, coverage=1.0),
         "9355", 1, "the other end of the same band"),

        ("eta_exactly_on_threshold",
         _metrics(eta_median_days=k.max_eta_days, eta_median_denominator=5,
                  n_listings=5),
         "9355", 1, "delivery exactly at the maximum"),

        ("feedback_exactly_on_threshold",
         _metrics(feedback_median_bp=k.min_seller_feedback_bp,
                  feedback_median_n=k.min_seller_feedback_n,
                  feedback_median_denominator=9, n_listings=9),
         "9355", 1, "both quality floors met exactly"),

        ("competitors_exactly_at_cap",
         _metrics(n_listings=40), "9355", k.max_competitors,
         "saturation exactly at the cap"),

        ("sell_through_exactly_on_threshold",
         _metrics(str_window_bp=k.str_threshold_bp, str_window_denominator=12,
                  str_window_days=30.0, n_listings=12, sold_known=12, coverage=1.0),
         "9355", 3, "a comparable window landing exactly on the threshold"),

        ("sell_through_window_too_short",
         _metrics(str_window_bp=99999, str_window_denominator=12,
                  str_window_days=1.18, n_listings=12, sold_known=12, coverage=1.0),
         "9355", 3,
         "a spectacular figure over a 1.18-day window -- must be WITHHELD, "
         "not passed, because a short window under-reads by construction"),

        ("sell_through_lifetime_only",
         _metrics(str_lifetime=2.5, str_lifetime_denominator=8,
                  n_listings=8, sold_known=8, coverage=1.0),
         "9355", 3, "lifetime is never comparable to the threshold"),

        ("max_cost_goes_negative",
         _metrics(price_median_pence=200, price_median_denominator=2,
                  price_floor_pence=150, price_floor_denominator=2, n_listings=2),
         "9355", 1,
         "a 2 pound item cannot carry the fees plus a 6 pound margin floor -- "
         "max_cost is deliberately NOT clamped at zero"),

        ("leaf_unknown_to_the_fee_table",
         _metrics(price_median_pence=3000, price_median_denominator=4,
                  price_floor_pence=2500, price_floor_denominator=4, n_listings=4),
         "999999999", 2,
         "an unmapped leaf must fall to EVERYTHING_ELSE_BP, the worst rate, "
         "which understates max_cost -- the safe direction"),

        ("leaf_is_none",
         _metrics(price_median_pence=3000, price_median_denominator=4, n_listings=4),
         None, 2, "no leaf at all resolves the same safe way"),

        ("per_order_fee_boundary_low",
         _metrics(price_median_pence=fees.PER_ORDER_THRESHOLD_PENCE,
                  price_median_denominator=2, n_listings=2),
         "9355", 1, "exactly at the 10 pound per-order fee step"),

        ("per_order_fee_boundary_high",
         _metrics(price_median_pence=fees.PER_ORDER_THRESHOLD_PENCE + 1,
                  price_median_denominator=2, n_listings=2),
         "9355", 1, "one penny over the step"),

        ("floor_but_no_median",
         _metrics(price_floor_pence=2000, price_floor_denominator=1, n_listings=6),
         "9355", 2, "margin falls back to the floor basis"),

        ("competitors_not_supplied",
         _metrics(n_listings=6), "9355", None,
         "saturation with no competitor count is unknown, not zero"),
    ]

    top_level_of = catalog_ebay.top_level_of()
    return [
        {
            "id": f"constructed:{name}",
            "source": "constructed",
            "why": why,
            "leaf_category_id": leaf,
            "fvf_bp": fees.fvf_bp(leaf, top_level_of),
            "competitors": competitors,
            "metrics": check_inputs(m),
        }
        for name, m, leaf, competitors, why in cases
    ]


def _expected(case: dict, knobs: Knobs) -> dict:
    m = metrics_from_inputs(case["metrics"])
    top_level_of = catalog_ebay.top_level_of()
    leaf = case["leaf_category_id"]

    max_floor = (fees.max_cost(m.price_floor_pence, leaf, knobs,
                               top_level_of=top_level_of)
                 if m.price_floor_pence is not None else None)
    max_median = (fees.max_cost(m.price_median_pence, leaf, knobs,
                                top_level_of=top_level_of)
                  if m.price_median_pence is not None else None)

    results = check(m, knobs, competitors=case["competitors"],
                    max_cost_at_floor_pence=max_floor,
                    max_cost_at_median_pence=max_median)
    return {
        "max_cost_at_floor_pence": max_floor,
        "max_cost_at_median_pence": max_median,
        "knobs": [
            {"knob": r.knob, "value": r.value, "threshold": r.threshold,
             "passed": r.passed, "denominator": r.denominator,
             "coverage": r.coverage, "note": r.note}
            for r in results
        ],
    }


def build_golden(cases: list[dict], settings=KNOB_SETTINGS) -> dict:
    """Every case under every knob setting, with the real Python's answers."""
    base = Knobs()
    expected = []
    for label, overrides in settings:
        knobs = replace(base, **overrides) if overrides else base
        for case in cases:
            expected.append({
                "case": case["id"],
                "setting": label,
                **_expected(case, knobs),
            })

    # A setting that changes no verdict anywhere is testing nothing, and a
    # fixture that quietly contains one lets a whole knob go unpinned.
    by_setting: dict[str, list] = {}
    for row in expected:
        by_setting.setdefault(row["setting"], []).append(row)
    baseline = {r["case"]: r for r in by_setting["defaults"]}
    inert = [
        label for label in by_setting
        if label != "defaults" and all(
            r["knobs"] == baseline[r["case"]]["knobs"]
            and r["max_cost_at_floor_pence"] == baseline[r["case"]]["max_cost_at_floor_pence"]
            and r["max_cost_at_median_pence"] == baseline[r["case"]]["max_cost_at_median_pence"]
            for r in by_setting[label]
        )
    ]

    return {
        "schema_version": GOLDEN_SCHEMA_VERSION,
        "note": "Ground truth for the TypeScript knob engine. Every field must "
                "be reproduced exactly, INCLUDING nulls: `passed: null` means "
                "the data to judge this does not exist, and rendering it as "
                "false would turn 'cannot tell' into 'failed' on a board where "
                "score = passed - failed.",
        "fee_constants": {
            "everything_else_bp": fees.EVERYTHING_ELSE_BP,
            "regulatory_bp": fees.REGULATORY_BP,
            "per_order_pence_low": fees.PER_ORDER_PENCE_LOW,
            "per_order_pence_high": fees.PER_ORDER_PENCE_HIGH,
            "per_order_threshold_pence": fees.PER_ORDER_THRESHOLD_PENCE,
        },
        "knob_settings": [
            {"label": label,
             "knobs": asdict(replace(base, **overrides) if overrides else base)}
            for label, overrides in settings
        ],
        "inert_settings": inert,
        "check_input_fields": list(CHECK_INPUT_FIELDS),
        "cases": cases,
        "expected": expected,
        "n_cases": len(cases),
        "n_expectations": len(expected),
    }
