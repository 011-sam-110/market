"""Market selection: every rule, and every drop counted by its reason."""

from __future__ import annotations

from datetime import timedelta

import pytest

from forecast import gamma
from tests.forecast_fixtures import NOW, raw_market


def _select(rows, limit=20, known_markets=(), known_events=()):
    return gamma.select(rows, NOW, limit=limit, known_markets=set(known_markets),
                        known_events=set(known_events))


def test_parse_reads_a_two_outcome_market():
    market, why = gamma.parse_market(raw_market("1", outcomes=("Jets", "Bears"),
                                                prices=("0.365", "0.635")))
    assert why == ""
    assert market.outcomes == ("Jets", "Bears")
    assert market.price == pytest.approx(0.365)
    assert market.event_id == "ev-1"
    assert market.final_prices is None  # only a closed market carries final prices


def test_parse_refuses_three_outcomes():
    market, why = gamma.parse_market(raw_market("1", outcomes=("A", "B", "C"),
                                                prices=("0.2", "0.3", "0.5")))
    assert market is None
    assert why == "not two outcomes"


@pytest.mark.parametrize("kwargs, reason", [
    ({"neg_risk": True}, "negRisk"),
    ({"liquidity": 9_999.0}, "liquidity below floor"),
    ({"prices": ("0.97", "0.03")}, "price outside 0.05-0.95"),
    ({"prices": ("0.02", "0.98")}, "price outside 0.05-0.95"),
    ({"end_in": timedelta(hours=23)}, "ends within 24h"),
    ({"end_in": timedelta(days=31)}, "ends after 30 days"),
    ({"recurrence": "hourly"}, "sub-daily series"),
    ({"recurrence": "5m"}, "sub-daily series"),
    ({"closed": True}, "closed"),
])
def test_each_rule_drops_and_names_its_reason(kwargs, reason):
    selection = _select([raw_market("1", **kwargs)])
    assert selection.picked == []
    assert selection.excluded == {reason: 1}
    assert selection.eligible == 0


def test_known_market_and_known_event_are_never_forecast_twice():
    rows = [raw_market("1"), raw_market("2", event_id="E9"), raw_market("3")]
    selection = _select(rows, known_markets={"1"}, known_events={"E9"})
    assert [m.id for m in selection.picked] == ["3"]
    assert selection.excluded["already forecast"] == 1
    assert selection.excluded["event already forecast"] == 1


def test_one_market_per_event_keeps_the_most_liquid():
    rows = [raw_market("1", event_id="E", liquidity=20_000),
            raw_market("2", event_id="E", liquidity=90_000),
            raw_market("3", event_id="E", liquidity=40_000)]
    selection = _select(rows)
    assert [m.id for m in selection.picked] == ["2"]
    assert selection.excluded["another market of the same event"] == 2


def test_sports_and_other_alternate_by_liquidity():
    rows = [raw_market(f"s{i}", sports="moneyline", liquidity=100_000 - i) for i in range(5)]
    rows += [raw_market(f"o{i}", liquidity=20_000 - i) for i in range(2)]
    selection = _select(rows, limit=5)
    assert [m.id for m in selection.picked] == ["o0", "s0", "o1", "s1", "s2"]


def test_limit_caps_the_pick_and_counts_every_row():
    rows = [raw_market(str(i)) for i in range(30)] + [raw_market("x", neg_risk=True)]
    selection = _select(rows, limit=20)
    assert len(selection.picked) == 20
    assert selection.read == 31
    assert selection.eligible == 30
    text = "\n".join(selection.lines())
    assert "read 31 open markets" in text
    assert "excluded (negRisk): 1" in text
    assert "picked: 20" in text


def test_fetch_open_pages_until_a_short_page():
    calls = []

    def get_json(url):
        calls.append(url)
        offset = int(url.split("offset=")[1].split("&")[0])
        size = gamma.PAGE_SIZE if offset < 200 else 7
        return [raw_market(f"{offset}-{i}") for i in range(size)]

    rows = gamma.fetch_open(get_json, NOW, sleep=lambda s: None)
    assert len(rows) == 207
    assert len(calls) == 3
    assert all(u.startswith(gamma.GAMMA_ROOT + "/markets?") for u in calls)
    assert "closed=false" in calls[0] and "liquidity_num_min=10000" in calls[0]


def test_fetch_open_stops_at_the_offset_limit():
    calls = []

    def get_json(url):
        calls.append(url)
        return [raw_market(str(i)) for i in range(gamma.PAGE_SIZE)]

    gamma.fetch_open(get_json, NOW, sleep=lambda s: None)
    assert len(calls) == gamma.MAX_OFFSET // gamma.PAGE_SIZE + 1


def test_no_order_code_and_no_key_in_the_package():
    """Paper only: the package sends GETs to Gamma and holds no order path."""
    import pathlib

    import forecast
    root = pathlib.Path(forecast.__file__).parent
    text = "\n".join(p.read_text(encoding="utf-8").lower() for p in root.glob("*.py"))
    for banned in ("clob.polymarket.com", "/order", "private_key", "py_clob_client",
                   "eth_account", "poly_api_key"):
        assert banned not in text, banned
