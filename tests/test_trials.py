"""The trial log (phoneloop/trials.py): sample orders, test listings, and the
report that sets predicted figures against realised ones, each with its n."""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from phoneloop import supplier, trials
from phoneloop.supplier import CHOSEN_ALLOWANCE, Quote, QuoteBook
from phoneloop.trials import SampleOrder, TrialLog

REPO = Path(__file__).resolve().parent.parent
T0 = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


# --- sample orders ------------------------------------------------------------------

def test_sample_delivery_days_and_gate_pass(tmp_path):
    log = TrialLog(tmp_path)
    log.record_sample("s1", quote_id="q1", ordered_on=date(2026, 10, 6),
                      promised_days=7, cost_pence=850, now=T0)
    s = log.record_sample("s1", arrived_on=date(2026, 10, 11),
                          condition="as_described", name_on_slip="mine", now=T0)
    assert s.delivery_days == 5
    assert s.cost_pence == 850             # kept from the first call
    assert s.gate() == (True, [])


def test_sample_gate_is_unknown_until_it_arrives(tmp_path):
    s = TrialLog(tmp_path).record_sample("s1", ordered_on=date(2026, 10, 6),
                                         promised_days=7)
    ok, reasons = s.gate()
    assert ok is None
    assert "arrived_on" in reasons[0]


def test_sample_gate_fails_with_each_reason():
    s = SampleOrder("s2", ordered_on=date(2026, 10, 1), arrived_on=date(2026, 10, 15),
                    promised_days=7, condition="damaged", name_on_slip="supplier")
    ok, reasons = s.gate()
    assert ok is False
    assert len(reasons) == 3
    assert "14 days" in reasons[0]


@pytest.mark.parametrize("over", [
    {"condition": "fine"}, {"name_on_slip": "bob"}, {"cost_pence": 8.5},
    {"cost_pence": -1}, {"promised_days": -1},
    {"ordered_on": date(2026, 10, 9), "arrived_on": date(2026, 10, 8)},
])
def test_invalid_samples_raise(over):
    with pytest.raises(ValueError):
        SampleOrder("s3", **over)


def test_an_invalid_update_writes_nothing(tmp_path):
    log = TrialLog(tmp_path)
    log.record_sample("s1", ordered_on=date(2026, 10, 6))
    with pytest.raises(ValueError):
        log.record_sample("s1", condition="fine")
    lines = (tmp_path / trials.TRIALS_FILE).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


def test_trials_dir_refuses_a_path_inside_the_repo():
    with pytest.raises(ValueError, match="outside the repo"):
        trials.trials_dir(env={"PHONELOOP_TRIALS_DIR": str(REPO / "trials")})
    assert trials.trials_dir(env={}) == Path.home() / ".phoneloop" / "trials"


# --- test listings ----------------------------------------------------------------------

PREDICTED = {
    "source": "board-join.json generated 2026-10-14",
    "price_pence": {"value": 2499, "n": 12, "basis": "board median landed price"},
    "net_per_unit_pence": {"value": 310, "n": 12, "basis": "median - fees - landed - returns"},
    "units_per_listing": {"value": 6000, "n": 40, "basis": "board sell-through over 26 days"},
    "delivery_days": {"value": 4, "n": 1, "basis": "the quote's latest delivery day"},
    "return_rate_bp": {"value": 1000, "n": 0, "basis": "returns allowance, chosen"},
}


def test_listing_freezes_its_first_prediction(tmp_path):
    log = TrialLog(tmp_path)
    log.record_listing("l1", predicted=PREDICTED, quote_id="q1",
                       listed_on=date(2026, 10, 14), price_pence=2499)
    later = dict(PREDICTED, price_pence={"value": 9999, "n": 1, "basis": "x"})
    lst = log.record_listing("l1", predicted=later, units_sold=2,
                             as_of=date(2026, 10, 21))
    assert lst.predicted["price_pence"]["value"] == 2499
    assert log.listings()["l1"].predicted["price_pence"]["value"] == 2499
    assert lst.price_pence == 2499 and lst.units_sold == 2


def test_listing_derived_days(tmp_path):
    lst = TrialLog(tmp_path).record_listing(
        "l1", listed_on=date(2026, 10, 14), first_sale_on=date(2026, 10, 17),
        as_of=date(2026, 10, 28))
    assert lst.days_to_first_sale == 3
    assert lst.days_listed == 14


def test_returns_cannot_exceed_units_sold():
    with pytest.raises(ValueError):
        trials.TestListing("l9", units_sold=1, returns=2)


def test_report_sets_predicted_against_realised_with_n(tmp_path):
    log = TrialLog(tmp_path)
    log.record_sample("s1", quote_id="q1", ordered_on=date(2026, 10, 6),
                      arrived_on=date(2026, 10, 12), promised_days=7,
                      condition="as_described", name_on_slip="none", cost_pence=850)
    log.record_listing("l1", predicted=PREDICTED, quote_id="q1", product_key="gtin:111",
                       listed_on=date(2026, 10, 14), price_pence=2499)
    log.record_listing("l1", units_sold=4, first_sale_on=date(2026, 10, 16),
                       returns=1, cases=0, realised_net_pence=900,
                       as_of=date(2026, 10, 28))

    figures = {f.name: f for f in trials.figures_for(log.listings()["l1"],
                                                     list(log.samples().values()))}
    assert (figures["price"].predicted, figures["price"].predicted_n) == ("£24.99", 12)
    assert (figures["net per unit"].realised, figures["net per unit"].realised_n) == ("£2.25", 4)
    assert figures["units sold"].realised == "4 in 14 days"
    assert (figures["delivery days"].realised, figures["delivery days"].realised_n) == ("6", 1)
    assert (figures["return rate"].predicted, figures["return rate"].realised) == ("10.0%", "25.0%")
    assert figures["return rate"].realised_n == 4
    assert figures["days to first sale"].realised == "2"

    text = trials.render_report(log)
    assert "predicted against realised" in text
    assert "£24.99 (n=12)" in text
    assert "gate [PASS]" in text
    assert "total £0.50" in text          # 900 net - 850 sample


def test_report_prints_unknown_not_zero(tmp_path):
    log = TrialLog(tmp_path)
    log.record_listing("l1", listed_on=date(2026, 10, 14), price_pence=2499)
    figures = {f.name: f for f in trials.figures_for(log.listings()["l1"], [])}
    assert (figures["net per unit"].realised, figures["net per unit"].realised_n) == ("?", 0)
    assert (figures["price"].predicted, figures["price"].predicted_n) == ("?", 0)
    assert "none frozen" in trials.render_report(log)


def test_empty_log():
    assert "no sample orders" in trials.render_report(TrialLog(Path("does-not-exist-xyz")))


# --- predictions from the board join ---------------------------------------------------------

def test_predictions_come_from_the_saved_join(tmp_path):
    payload = {"generated_at": "2026-10-14T03:00:00+00:00", "date_a": "2026-09-18",
               "date_b": "2026-10-14", "quotes": {"q1": {
                   "price_median_pence": 2499, "price_median_n": 12,
                   "net_per_unit_at_median_pence": 310,
                   "str_window_bp": 6000, "str_window_n": 40, "str_window_days": 26.0,
                   "str_comparable": False, "str_note": "1-day window - not comparable",
                   "latest_delivery_days": 4}}}
    (tmp_path / supplier.JOIN_FILE).write_text(json.dumps(payload), encoding="utf-8")
    p = trials.predictions_for_quote(tmp_path, "q1", CHOSEN_ALLOWANCE)
    assert p["price_pence"] == {"value": 2499, "n": 12, "basis": "board median landed price"}
    # A sell-through the knob withheld is not frozen as a prediction.
    assert p["units_per_listing"]["value"] is None
    assert p["units_per_listing"]["n"] == 0
    assert "withheld" in p["units_per_listing"]["basis"]
    assert trials.predictions_for_quote(tmp_path, "q404") == {}


# --- the command line -------------------------------------------------------------------------

def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_sample_listing_and_report(tmp_path, monkeypatch, capsys):
    sup, tri = tmp_path / "suppliers", tmp_path / "trials"
    monkeypatch.setenv("PHONELOOP_SUPPLIERS_DIR", str(sup))
    monkeypatch.setenv("PHONELOOP_TRIALS_DIR", str(tri))
    QuoteBook(sup).add(Quote(
        product_key="gtin:111", supplier="Example Ltd", supplier_kind="wholesale",
        unit_cost_pence=640, postage_pence=210, ship_from="GB",
        latest_delivery_days=4, returns_terms="30 days", quote_date=date(2026, 10, 5),
        source_url="https://example.com/q/1"))

    trial = _load("trial")
    assert trial.main(["sample", "--id", "s1", "--quote", "q1", "--ordered", "2026-10-06",
                       "--promised-days", "7", "--cost", "8.50"]) == 0
    assert trial.main(["sample", "--id", "s1", "--arrived", "2026-10-11",
                       "--condition", "as_described", "--slip", "mine"]) == 0
    assert "gate [PASS]" in capsys.readouterr().out

    assert trial.main(["listing", "--id", "l1", "--quote", "q1", "--listed", "2026-10-14",
                       "--price", "24.99"]) == 0
    assert "no board join" in capsys.readouterr().out
    assert trial.main(["listing", "--id", "l1", "--units-sold", "2", "--returns", "0",
                       "--cases", "0", "--net", "7.40", "--as-of", "2026-10-21"]) == 0
    assert trial.main(["sample", "--id", "s2", "--quote", "q404"]) == 2

    assert _load("trial_report").main([]) == 0
    out = capsys.readouterr().out
    assert "s1" in out and "l1" in out
    assert "£3.70 (n=2)" in out                    # 740 net over 2 units
    assert "total -£1.10" in out                   # 740 - 850
