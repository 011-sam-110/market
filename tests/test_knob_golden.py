"""The fixture that stops the browser and Python disagreeing about a knob.

The dashboard re-thresholds knobs client-side, so `fees.max_cost` and the six
comparisons in `metrics.check` exist twice -- once here, once in TypeScript.
Two implementations of one rule drift, and this drift is silent: the page would
show a confident PASS on a row the pipeline calls a FAIL, with every test on
both sides green.

`dashboard/test/fixtures/knob-golden.json` is the shared ground truth. The
TypeScript suite replays it and must reproduce every field including the nulls.
These tests hold up the Python end:

- the fixture is SELF-CONTAINED, so replaying it needs no store, and a change
  to fees.py or metrics.check that nobody mirrored makes it fail here first;
- `CHECK_INPUT_FIELDS` really is every input a verdict depends on, proved by
  perturbing all the others and watching nothing move.
"""

from __future__ import annotations

import json
from dataclasses import fields, replace
from pathlib import Path

import pytest

from phoneloop import catalog_ebay, fees
from phoneloop.dashexport.golden import (
    CHECK_INPUT_FIELDS,
    KNOB_SETTINGS,
    metrics_from_inputs,
)
from phoneloop.dashexport.jsonio import dumps
from phoneloop.knobs import Knobs
from phoneloop.metrics import Metrics, check

FIXTURE = (Path(__file__).resolve().parent.parent
           / "dashboard" / "test" / "fixtures" / "knob-golden.json")


@pytest.fixture(scope="module")
def golden() -> dict:
    if not FIXTURE.exists():
        pytest.fail(
            f"{FIXTURE} is missing. Regenerate it with "
            "`python scripts/export_knob_golden.py --data-dir ~/.phoneloop/prod`. "
            "It is committed on purpose: it is the contract the TypeScript knob "
            "engine is tested against.")
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _as_shipped(value: object) -> object:
    """What the browser actually receives.

    `price_band`'s threshold is a Python tuple and arrives in JavaScript as an
    array; comparing the live object to the recorded one directly fails on that
    alone, which says nothing about the arithmetic. Both sides are put through
    the bundle's own encoder so the comparison is over the shipped shape -- and
    a type that stops surviving the trip starts failing here, which is the
    other thing worth catching.
    """
    return json.loads(dumps(value))


def _knobs_for(golden: dict, label: str) -> Knobs:
    for setting in golden["knob_settings"]:
        if setting["label"] == label:
            return Knobs(**setting["knobs"])
    raise AssertionError(f"no knob setting labelled {label!r}")


# --- the drift alarm ----------------------------------------------------------

def test_the_committed_fixture_still_matches_what_the_code_produces(golden):
    """Replay every recorded expectation through the real Python.

    This is the alarm. Change `fees.py` or `metrics.check` without
    regenerating and this goes red, which is exactly what should happen: the
    browser is holding a copy of that arithmetic and it has not been told.
    """
    top_level_of = catalog_ebay.top_level_of()
    cases = {c["id"]: c for c in golden["cases"]}
    assert golden["expected"], "an empty fixture pins nothing"

    for row in golden["expected"]:
        case = cases[row["case"]]
        knobs = _knobs_for(golden, row["setting"])
        m = metrics_from_inputs(case["metrics"])
        leaf = case["leaf_category_id"]

        max_floor = (fees.max_cost(m.price_floor_pence, leaf, knobs,
                                   top_level_of=top_level_of)
                     if m.price_floor_pence is not None else None)
        max_median = (fees.max_cost(m.price_median_pence, leaf, knobs,
                                    top_level_of=top_level_of)
                      if m.price_median_pence is not None else None)

        where = f"{row['case']} under {row['setting']}"
        assert max_floor == row["max_cost_at_floor_pence"], where
        assert max_median == row["max_cost_at_median_pence"], where

        got = check(m, knobs, competitors=case["competitors"],
                    max_cost_at_floor_pence=max_floor,
                    max_cost_at_median_pence=max_median)
        assert len(got) == len(row["knobs"]), where
        for actual, want in zip(got, row["knobs"]):
            assert actual.knob == want["knob"], where
            assert _as_shipped(actual.value) == want["value"],                 f"{where}/{want['knob']} value"
            assert _as_shipped(actual.threshold) == want["threshold"],                 f"{where}/{want['knob']} threshold"
            # `is` not `==`: None and False are both falsy and this is the one
            # comparison where confusing them changes the ranking.
            assert actual.passed is want["passed"], f"{where}/{want['knob']} passed"
            assert actual.denominator == want["denominator"], where
            assert actual.coverage == pytest.approx(want["coverage"]), where
            assert actual.note == want["note"], where


def test_the_recorded_fee_rate_matches_what_the_fee_table_resolves(golden):
    """The browser prices margin from `fvf_bp`, not from a leaf. If the
    recorded rate and the live table disagree, every margin on the page is
    wrong by a fee."""
    top_level_of = catalog_ebay.top_level_of()
    for case in golden["cases"]:
        assert case["fvf_bp"] == fees.fvf_bp(case["leaf_category_id"], top_level_of), \
            case["id"]


# --- the input surface is complete --------------------------------------------

# The fee table has to recognise this leaf or `max_cost` falls back to the
# everything-else rate and the margin knob stops responding to a fee change.
_LEAF = "9355"


def _base_metrics(**over) -> Metrics:
    inputs = {
        "n_listings": 10,
        "price_median_pence": 3000, "price_median_denominator": 6,
        "price_floor_pence": 2400, "price_floor_denominator": 5,
        "str_window_bp": 6000, "str_window_denominator": 8, "str_window_days": 30.0,
        "str_lifetime": 1.2, "str_lifetime_denominator": 8,
        "eta_median_days": 4, "eta_median_denominator": 7,
        "feedback_median_bp": 9800, "feedback_median_n": 900,
        "feedback_median_denominator": 7,
    }
    inputs.update(over)
    return metrics_from_inputs(inputs)


# One base point cannot exercise every input, because several are read only
# when another is absent:
#
#   `str_lifetime` only when `str_window_bp` is None (metrics.py:440) -- on a
#   store with two snapshots it is invisible, and on a store with one it is
#   the only sell-through figure there is;
#   `price_floor_*` only when there is no median price (metrics.py:418), the
#   conservative basis the margin knob falls back to.
#
# A single fully-populated base leaves all of those unpinned, so each absence
# gets its own world. The fixture carries the matching constructed cases
# (`sell_through_lifetime_only`, `floor_but_no_median`).
BASES = {
    "window_measured": _base_metrics(),
    "window_missing": _base_metrics(str_window_bp=None, str_window_days=None),
    "median_missing": _base_metrics(price_median_pence=None,
                                    price_median_denominator=0),
}


def _verdicts(m: Metrics, knobs: Knobs):
    """Every verdict, priced the way the pipeline prices it.

    `max_cost` is computed from the metrics rather than passed in as a
    constant. Hardcoding it here would make `price_floor_pence` look like an
    input that changes nothing -- it feeds the margin knob through
    `fees.max_cost`, not through `check` directly.
    """
    top_level_of = catalog_ebay.top_level_of()
    floor = (fees.max_cost(m.price_floor_pence, _LEAF, knobs,
                           top_level_of=top_level_of)
             if m.price_floor_pence is not None else None)
    median = (fees.max_cost(m.price_median_pence, _LEAF, knobs,
                            top_level_of=top_level_of)
              if m.price_median_pence is not None else None)
    return (floor, median,
            [(r.knob, r.value, r.threshold, r.passed, r.denominator, r.coverage,
              r.note)
             for r in check(m, knobs, competitors=4,
                            max_cost_at_floor_pence=floor,
                            max_cost_at_median_pence=median)])


def _pokes(current: object) -> list[object]:
    """Perturbations worth trying on one field.

    Both directions and the absent case, because a field can matter only when
    it goes missing (a denominator dropping to zero is the difference between
    a figure and an unknown) and a field can matter only downward (a
    sell-through window shortening is what triggers the withhold; lengthening
    it changes nothing).
    """
    if isinstance(current, bool):
        return [not current]
    if isinstance(current, (int, float)):
        return [None, 0, current + 991, type(current)(max(current - 991, -991))]
    return [991, 0, 1.0]


def test_no_field_outside_check_input_fields_can_move_a_verdict():
    """Proves the fixture is not missing an input.

    The golden records only `CHECK_INPUT_FIELDS`. If `check` ever started
    reading another metric, the fixture would silently stop pinning it and the
    TypeScript engine could diverge on that field forever. So: perturb every
    excluded field, in every direction, on both bases, and assert nothing moves.
    """
    knobs = Knobs()
    excluded = [f.name for f in fields(Metrics) if f.name not in CHECK_INPUT_FIELDS]
    assert excluded, "this test asserts nothing if every field is an input"

    for base_name, base in BASES.items():
        baseline = _verdicts(base, knobs)
        for name in excluded:
            for poked in _pokes(getattr(base, name)):
                try:
                    moved = _verdicts(replace(base, **{name: poked}), knobs)
                except (TypeError, ValueError):
                    # A field `check` never touches cannot make it raise either.
                    raise AssertionError(
                        f"Metrics.{name}={poked!r} made check() raise on "
                        f"{base_name}, so it IS an input. Add it to "
                        "CHECK_INPUT_FIELDS and regenerate the fixture.")
                assert moved == baseline, (
                    f"setting Metrics.{name} to {poked!r} on {base_name} moved a "
                    "verdict, but the golden fixture does not record that field. "
                    "Add it to CHECK_INPUT_FIELDS and regenerate, or the "
                    "TypeScript engine is free to disagree about it.")


def test_every_check_input_field_earns_its_place():
    """The other direction: a field in the list that changes nothing anywhere is
    dead weight in the contract, and dead weight is how a contract stops being
    read."""
    knobs = Knobs()
    baselines = {n: _verdicts(b, knobs) for n, b in BASES.items()}

    def moves(base_name, base, name, poked) -> bool:
        try:
            return _verdicts(replace(base, **{name: poked}), knobs) != baselines[base_name]
        except (TypeError, ValueError):
            # `check` raising on a value proves it reads it. A denominator set
            # to None reaches `_coverage`, which is annotated `int` and divides
            # -- real metrics always carry an integer count, so this only
            # happens under a synthetic poke, and it still counts as evidence.
            return True

    inert = [name for name in CHECK_INPUT_FIELDS
             if not any(moves(bn, b, name, poked)
                        for bn, b in BASES.items()
                        for poked in _pokes(getattr(b, name)))]
    assert not inert, (
        f"{inert} are recorded as verdict inputs but move nothing on any base. "
        "Either drop them from CHECK_INPUT_FIELDS or add a base that "
        "exercises them.")


# --- the fixture covers the awkward cases -------------------------------------

def test_no_knob_setting_is_inert(golden):
    """A setting that moves no verdict on any case pins nothing, and a whole
    knob would go untested behind it."""
    assert golden["inert_settings"] == [], (
        f"{golden['inert_settings']} changed no verdict on any case")
    assert len(golden["knob_settings"]) == len(KNOB_SETTINGS)


def test_the_fixture_contains_the_cases_a_sample_would_miss(golden):
    ids = {c["id"] for c in golden["cases"]}
    for needed in ("constructed:all_unknown",
                   "constructed:sell_through_window_too_short",
                   "constructed:max_cost_goes_negative",
                   "constructed:leaf_unknown_to_the_fee_table",
                   "constructed:competitors_not_supplied"):
        assert needed in ids, f"{needed} is missing from the fixture"

    rows = golden["expected"]
    assert any(k["passed"] is None for r in rows for k in r["knobs"]), \
        "no unknown verdict anywhere -- the null path is unpinned"
    assert any(k["passed"] is True for r in rows for k in r["knobs"])
    assert any(k["passed"] is False for r in rows for k in r["knobs"])
    assert any((r["max_cost_at_median_pence"] or 0) < 0 for r in rows), \
        "max_cost is deliberately not clamped at zero; pin that it can go negative"


def test_a_spectacular_figure_over_a_short_window_is_withheld_not_passed(golden):
    """The single most important verdict in the fixture.

    A sell-through of 999.99% over a 1.18-day window is what the real store
    produces today. Compared naively it sails past a 50% threshold. It must
    come back unknown, because a short window under-reads by construction and
    the confident answer it invites is the wrong one.
    """
    rows = [r for r in golden["expected"]
            if r["case"] == "constructed:sell_through_window_too_short"]
    assert rows, "the case is missing"
    for row in rows:
        knob = next(k for k in row["knobs"] if k["knob"] == "sell_through")
        assert knob["passed"] is None, (
            f"under {row['setting']} a 1.18-day window produced "
            f"passed={knob['passed']!r} -- it must be withheld")
