"""The scorer: settlement, Brier against the market, paper P&L, and WITHHELD."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from forecast import WITHHOLD_BELOW, gamma, score
from forecast.export import build_payload
from forecast.ledger import Ledger
from phoneloop.dashexport.jsonio import dumps
from tests.forecast_fixtures import NOW, raw_market


def _row(mid, *, p=0.6, price=0.5, side="a", stake=10.0, fill=0.5):
    return {"market_id": mid, "question": f"q{mid}", "outcome_a": "Yes",
            "outcome_b": "No", "market_price": price, "price_read_at": "t",
            "probability": p, "reddit_status": "no reddit evidence: x",
            "news_status": "news: 2 headlines from 9 stories in 14 days",
            "side": side, "fill_price": fill, "kelly_full": 0.2,
            "stake_fraction": 0.05, "stake": stake, "free_capital": 1000.0,
            "model": "deepseek-flash", "prompt_version": "v", "created_at": "t"}


def _closed(mid, prices, uma="resolved"):
    market, _ = gamma.parse_market(raw_market(mid, prices=prices, closed=True, uma=uma,
                                              end_in=timedelta(days=-1)))
    return market


def test_settlement_rules():
    assert score.settlement(_closed("1", ("1", "0"))) == ("resolved", 1.0)
    assert score.settlement(_closed("1", ("0", "1"))) == ("resolved", 0.0)
    assert score.settlement(_closed("1", ("0.5", "0.5"))) == ("void", 0.5)
    assert score.settlement(_closed("1", ("0.9995", "0.0005"))) is None
    assert score.settlement(_closed("1", ("1", "0"), uma="proposed")) is None
    open_market, _ = gamma.parse_market(raw_market("1", prices=("1", "0")))
    assert score.settlement(open_market) is None


def test_paper_pnl():
    assert score.paper_pnl("a", 10.0, 0.5, 1.0) == pytest.approx(10.0)
    assert score.paper_pnl("a", 10.0, 0.5, 0.0) == pytest.approx(-10.0)
    assert score.paper_pnl("b", 10.0, 0.25, 0.0) == pytest.approx(30.0)
    assert score.paper_pnl("a", 10.0, 0.5, 0.5) == pytest.approx(0.0)
    assert score.paper_pnl("none", 0.0, None, 1.0) == 0.0


def test_check_resolutions_settles_only_what_resolved(tmp_path):
    answers = {"1": raw_market("1", prices=("1", "0"), closed=True, uma="resolved"),
               "2": raw_market("2", prices=("0.5", "0.5"), closed=True, uma="resolved"),
               "3": raw_market("3", prices=("0.7", "0.3"))}

    def get_json(url):
        mid = url.rsplit("/", 1)[1]
        if mid == "4":
            raise gamma.GammaError("HTTP 500")
        return answers[mid]

    with Ledger(tmp_path / "l.sqlite3") as ledger:
        for mid in "1234":
            ledger.record_forecast(_row(mid))
        counts = score.check_resolutions(ledger, get_json, NOW)
        assert counts == {"checked": 4, "resolved": 1, "void": 1, "still open": 1,
                          "fetch failed": 1}
        rows = {r["market_id"]: r for r in ledger.all_forecasts()}
        assert rows["1"]["status"] == "resolved" and rows["1"]["pnl"] == pytest.approx(10.0)
        assert rows["2"]["status"] == "void" and rows["2"]["pnl"] == pytest.approx(0.0)
        assert rows["3"]["status"] == "open"


def _settled_ledger(tmp_path, n, *, void=0):
    ledger = Ledger(tmp_path / "l.sqlite3")
    for i in range(n):
        fid = ledger.record_forecast(_row(str(i), p=0.7, price=0.5))
        outcome = 1.0 if i % 2 == 0 else 0.0
        ledger.settle(fid, status="resolved", final_price_a=outcome,
                      pnl=score.paper_pnl("a", 10.0, 0.5, outcome), settled_at=NOW)
    for j in range(void):
        fid = ledger.record_forecast(_row(f"v{j}", p=0.99, price=0.5))
        ledger.settle(fid, status="void", final_price_a=0.5, pnl=0.0, settled_at=NOW)
    ledger.record_forecast(_row("open-1"))
    return ledger


def test_below_one_hundred_every_comparison_is_withheld_with_its_n(tmp_path):
    with _settled_ledger(tmp_path, 3) as ledger:
        report = score.build_report(ledger, NOW)
        lines = "\n".join(score.report_lines(report))
    assert report["brier"]["agent"]["withheld"] is True
    assert report["brier"]["agent"]["n"] == 3
    assert "agent Brier    WITHHELD (n=3; needs 100 resolved)" in lines
    assert "market Brier   WITHHELD (n=3; needs 100 resolved)" in lines
    assert "paper P&L      WITHHELD (n=3; needs 100 resolved)" in lines
    assert "measured, not compared" in lines
    assert "forecasts: 4 (open 1, resolved 3, void 0)" in lines


def test_with_nothing_resolved_the_stamp_has_no_reading(tmp_path):
    with Ledger(tmp_path / "l.sqlite3") as ledger:
        ledger.record_forecast(_row("1"))
        lines = "\n".join(score.report_lines(score.build_report(ledger, NOW)))
    assert "agent Brier    WITHHELD (n=0; needs 100 resolved)" in lines
    assert "measured" not in lines.split("agent Brier")[1].splitlines()[0]


def test_at_one_hundred_the_comparison_prints(tmp_path):
    with _settled_ledger(tmp_path, WITHHOLD_BELOW, void=2) as ledger:
        report = score.build_report(ledger, NOW)
        lines = "\n".join(score.report_lines(report))
    # p=0.7 against outcomes alternating 1,0: agent Brier = (0.09 + 0.49) / 2 = 0.29.
    assert report["brier"]["agent"]["value"] == pytest.approx(0.29)
    assert report["brier"]["market"]["value"] == pytest.approx(0.25)
    assert report["brier"]["gap"]["value"] == pytest.approx(-0.04)
    assert report["brier"]["agent"]["withheld"] is False
    # The two void forecasts stay out of the Brier n.
    assert report["brier"]["agent"]["n"] == 100
    assert report["counts"]["void"] == 2
    assert "agent Brier    0.2900 (n=100)" in lines
    assert "WITHHELD" not in lines


def test_export_payload_is_valid_json_with_its_limits(tmp_path):
    with _settled_ledger(tmp_path, 3) as ledger:
        payload = build_payload(ledger, NOW, git_sha="abc")
    text = dumps(payload)
    back = json.loads(text)
    assert back["report"]["counts"]["forecasts"] == 4
    assert back["recent_limit"] == 60 and back["recent_dropped"] == 0
    assert back["report"]["brier"]["agent"]["withheld"] is True
    assert back["recent"][0]["market_id"] == "open-1"
