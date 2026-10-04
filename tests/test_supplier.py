"""Supplier quotes and the three-state verdict (phoneloop/supplier.py).

The rules these tests hold:

- money is integer pence, parsed without a float;
- a retail-marketplace quote is refused for eBay before it is stored;
- a quote clears, fails or is unknown against max_cost, after the returns
  allowance, at the floor price and at the median price;
- an unknown max_cost gives unknown, never a zero ceiling;
- the returns allowance is not a knob, so the knob golden cannot move.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from dataclasses import fields, replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from phoneloop import catalog_ebay, fees, supplier
from phoneloop.knobs import Knobs
from phoneloop.market_store import MarketStore, empty_row
from phoneloop.supplier import (CHOSEN_ALLOWANCE, CLEARS, FAILS, UNKNOWN, Quote,
                                QuoteBook, QuoteRefused, ReturnsAllowance)

LEAF = "9355"           # a real in-scope leaf, so fees price it at its own rate
SEEN = datetime(2026, 10, 4, 9, 0, tzinfo=UTC)
REPO = Path(__file__).resolve().parent.parent
BOARD_SCRIPT = REPO / "scripts" / "niche_board.py"


def _quote(**over) -> Quote:
    base = dict(
        product_key="gtin:111", supplier="Example Wholesale Ltd",
        supplier_kind="wholesale", unit_cost_pence=640, postage_pence=210,
        ship_from="GB", latest_delivery_days=4,
        returns_terms="30 days, buyer pays return postage",
        quote_date=date(2026, 10, 5), source_url="https://example.com/q/1",
        leaf_category_id=LEAF)
    base.update(over)
    return Quote(**base)


# --- money --------------------------------------------------------------------------

@pytest.mark.parametrize("text,pence", [
    ("12.34", 1234), ("£12.34", 1234), ("12.3", 1230), ("12", 1200),
    ("0.07", 7), ("1,234.50", 123450), (" 6.40 ", 640), ("-5.50", -550),
    (640, 640),
])
def test_parse_pence(text, pence):
    assert supplier.parse_pence(text) == pence


@pytest.mark.parametrize("text", ["12.345", "abc", "", "12.", "£", True])
def test_parse_pence_refuses_what_it_cannot_read_exactly(text):
    with pytest.raises(ValueError):
        supplier.parse_pence(text)


def test_unknown_money_prints_a_question_mark_not_zero():
    assert supplier.fmt_pence(None) == "?"
    assert supplier.fmt_pence(0) == "£0.00"


# --- the quote record -----------------------------------------------------------------

def test_a_valid_quote_and_its_landed_cost():
    q = _quote()
    assert q.landed_pence == 850


def test_marketplace_retail_is_refused_for_ebay():
    with pytest.raises(QuoteRefused) as exc:
        _quote(supplier_kind="marketplace_retail")
    assert supplier.EBAY_DROPSHIP_POLICY_URL in str(exc.value)


@pytest.mark.parametrize("kind", ["manufacturer", "wholesale", "dropship_agent"])
def test_other_kinds_are_accepted(kind):
    assert _quote(supplier_kind=kind).supplier_kind == kind


@pytest.mark.parametrize("over", [
    {"supplier_kind": "aliexpress"},
    {"unit_cost_pence": 6.4},
    {"unit_cost_pence": -1},
    {"postage_pence": True},
    {"ship_from": "gb"},
    {"ship_from": "GBR"},
    {"latest_delivery_days": -2},
    {"source_url": ""},
    {"source_url": "an email from Bob"},
    {"supplier": " "},
    {"returns_terms": ""},
    {"quote_date": "2026-10-05"},
])
def test_invalid_quotes_raise(over):
    with pytest.raises(ValueError):
        _quote(**over)


def test_quote_book_appends_and_numbers(tmp_path):
    book = QuoteBook(tmp_path)
    a = book.add(_quote())
    b = book.add(_quote(product_key="gtin:222", unit_cost_pence=999))
    assert (a.quote_id, b.quote_id) == ("q1", "q2")
    assert [q.quote_id for q in book.list()] == ["q1", "q2"]
    assert book.get("q2").unit_cost_pence == 999
    lines = (tmp_path / supplier.QUOTES_FILE).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["unit_cost_pence"] == 640     # pence, an int


def test_a_refused_quote_is_never_written(tmp_path):
    book = QuoteBook(tmp_path)
    with pytest.raises(QuoteRefused):
        book.add(_quote(supplier_kind="marketplace_retail"))
    assert book.list() == []


def test_suppliers_dir_default_is_outside_the_repo():
    path = supplier.suppliers_dir(env={})
    assert path == Path.home() / ".phoneloop" / "suppliers"
    assert REPO.resolve() not in path.resolve().parents


def test_suppliers_dir_refuses_a_path_inside_the_repo():
    with pytest.raises(ValueError, match="outside the repo"):
        supplier.suppliers_dir(env={"PHONELOOP_SUPPLIERS_DIR": str(REPO / "suppliers")})


# --- the returns allowance --------------------------------------------------------------

def test_allowance_arithmetic_is_integer_pence():
    """per return = 350 + 210 + 50% of 640 = 880; 10% of 880 = 88."""
    assert supplier.allowance_pence(_quote(), CHOSEN_ALLOWANCE) == 88


def test_allowance_is_not_a_knob():
    """The knob golden pins every Knobs field. The allowance must not be one."""
    names = {f.name for f in fields(Knobs)}
    assert not names & {f.name for f in fields(ReturnsAllowance)}
    assert not any("return" in n for n in names)


def test_load_allowance_default_and_file(tmp_path):
    allowance, source = supplier.load_allowance(tmp_path)
    assert allowance == CHOSEN_ALLOWANCE
    assert "chosen" in source
    (tmp_path / supplier.RETURNS_FILE).write_text(json.dumps(
        {"return_rate_bp": 500, "return_postage_pence": 299, "unsellable_bp": 2000}),
        encoding="utf-8")
    allowance, source = supplier.load_allowance(tmp_path)
    assert allowance == ReturnsAllowance(500, 299, 2000)
    assert source.endswith(supplier.RETURNS_FILE)


def test_allowance_refuses_a_rate_over_100_percent():
    with pytest.raises(ValueError):
        ReturnsAllowance(return_rate_bp=10001, return_postage_pence=0, unsellable_bp=0)


# --- the verdict ---------------------------------------------------------------------------

def test_unknown_max_cost_is_unknown_never_zero():
    v = supplier.verdict(_quote(), None, 0, basis="floor", allowance=CHOSEN_ALLOWANCE)
    assert v.state == UNKNOWN
    assert v.max_cost_pence is None
    assert v.headroom_pence is None
    text = supplier._fmt_verdict(v)
    assert "max_cost=?" in text and "£0.00" not in text


def test_a_known_zero_ceiling_fails_and_is_not_unknown():
    """The other half of the rule: zero is a real ceiling. Only None is unknown."""
    v = supplier.verdict(_quote(), 0, 3, basis="floor", allowance=CHOSEN_ALLOWANCE)
    assert v.state == FAILS
    assert v.headroom_pence == -(850 + 88)


def test_equal_clears_and_one_penny_less_fails():
    needed = 850 + 88
    q = _quote()
    assert supplier.verdict(q, needed, 5, basis="median",
                            allowance=CHOSEN_ALLOWANCE).state == CLEARS
    assert supplier.verdict(q, needed - 1, 5, basis="median",
                            allowance=CHOSEN_ALLOWANCE).state == FAILS


def test_the_allowance_can_turn_a_clear_into_a_fail():
    q = _quote()
    max_cost = 900          # clears on landed cost alone (850), not after returns (938)
    assert q.landed_pence <= max_cost
    assert supplier.verdict(q, max_cost, 5, basis="floor",
                            allowance=CHOSEN_ALLOWANCE).state == FAILS
    none = ReturnsAllowance(0, 0, 0)
    assert supplier.verdict(q, max_cost, 5, basis="floor", allowance=none).state == CLEARS


# --- the board join ---------------------------------------------------------------------------

def _row(sid: str, gtin: str, total: int, seller_type: str = "business") -> dict:
    row = empty_row("ebay", sid)
    row.update({
        "seen_at": SEEN, "category": "phones", "shard": f"leaf{LEAF}",
        "title": f"Widget {gtin}", "price_pence": total - 400, "total_pence": total,
        "currency": "GBP", "condition": "New", "available": True,
        "seller_id": f"seller_{sid}", "seller_type": seller_type,
        "seller_feedback_bp": 9900, "seller_feedback_n": 500,
        "eta_days": 3, "leaf_category_id": LEAF, "sold_qty": 10, "gtin": gtin,
    })
    return row


@pytest.fixture
def quote_store(tmp_path) -> Path:
    """Three products by gtin, so their keys are stable:
    111 sells at GBP 40 (max_cost well above the quote), 222 at GBP 12
    (max_cost below it), 333 only from private sellers (no business price,
    so no max_cost)."""
    data = tmp_path / "data"
    s = MarketStore(data)
    s.record([_row("a1", "111", 4000), _row("a2", "111", 4000), _row("a3", "111", 4200),
              _row("b1", "222", 1200), _row("b2", "222", 1250),
              _row("c1", "333", 4000, "private"), _row("c2", "333", 4100, "private")])
    return data


@pytest.fixture
def quotes_dir(tmp_path) -> Path:
    d = tmp_path / "suppliers"
    book = QuoteBook(d)
    book.add(_quote(product_key="gtin:111", label="Widget 111"))
    book.add(_quote(product_key="gtin:222"))
    book.add(_quote(product_key="gtin:333"))
    book.add(_quote(product_key="gtin:999", ship_from="CN", latest_delivery_days=14))
    return d


def test_join_gives_clears_fails_and_unknown(quote_store, quotes_dir):
    store = MarketStore(quote_store)
    knobs = Knobs()
    lines = supplier.join_quotes(store, knobs, None, QuoteBook(quotes_dir).list(),
                                 CHOSEN_ALLOWANCE)
    by_key = {ln.quote.product_key: ln for ln in lines}

    top = catalog_ebay.top_level_of()
    assert by_key["gtin:111"].median.max_cost_pence == fees.max_cost(
        4000, LEAF, knobs, top_level_of=top)
    assert by_key["gtin:111"].floor.state == CLEARS
    assert by_key["gtin:111"].median.state == CLEARS
    assert by_key["gtin:111"].floor.denominator == 3
    assert by_key["gtin:222"].floor.state == FAILS
    assert by_key["gtin:333"].floor.state == UNKNOWN          # private sellers only
    assert by_key["gtin:333"].floor.max_cost_pence is None
    assert by_key["gtin:333"].found is True
    assert by_key["gtin:999"].found is False                  # not on the store
    assert by_key["gtin:999"].median.state == UNKNOWN
    notes = " ".join(by_key["gtin:999"].notes)
    assert "import_vat_term_bp is 0" in notes
    assert "max_eta_days" in notes


def test_join_flags_a_changed_label(quote_store, tmp_path):
    store = MarketStore(quote_store)
    q = QuoteBook(tmp_path / "s").add(_quote(product_key="gtin:111", label="Old title"))
    (ln,) = supplier.join_quotes(store, Knobs(), None, [q], CHOSEN_ALLOWANCE)
    assert any("label changed" in n for n in ln.notes)


def test_render_counts_the_three_states_apart(quote_store, quotes_dir):
    store = MarketStore(quote_store)
    lines = supplier.join_quotes(store, Knobs(), None, QuoteBook(quotes_dir).list(),
                                 CHOSEN_ALLOWANCE)
    text = supplier.render_quote_table(lines, CHOSEN_ALLOWANCE, "chosen default")
    assert "floor CLEARS" in text
    assert "floor FAILS" in text
    assert "floor UNKNOWN max_cost=?" in text
    assert "at the floor: 1 clear, 1 fail, 2 unknown (of 4 quotes)" in text


def _run_board(monkeypatch, capsys, data_dir: Path, suppliers: Path, *args) -> str:
    for key in list(os.environ):
        if key.startswith("PHONELOOP_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PHONELOOP_DATA_DIR", str(data_dir))
    monkeypatch.setenv("PHONELOOP_SUPPLIERS_DIR", str(suppliers))
    monkeypatch.setattr(sys, "argv", ["niche_board.py", *args])
    spec = importlib.util.spec_from_file_location("niche_board_quotes", BOARD_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main() == 0
    return capsys.readouterr().out


def test_board_with_quotes_prints_each_verdict_and_saves_the_join(
        quote_store, quotes_dir, monkeypatch, capsys):
    out = _run_board(monkeypatch, capsys, quote_store, quotes_dir, "--with-quotes")
    assert "=== SUPPLIER QUOTES" in out
    for state in ("CLEARS", "FAILS", "UNKNOWN"):
        assert state in out
    assert "(n=3)" in out                       # the denominator under a max_cost
    join = json.loads((quotes_dir / supplier.JOIN_FILE).read_text(encoding="utf-8"))
    assert join["quotes"]["q1"]["verdict_floor"]["state"] == CLEARS
    assert join["quotes"]["q3"]["verdict_floor"]["max_cost_pence"] is None


def test_board_without_the_flag_prints_no_quotes(quote_store, quotes_dir,
                                                 monkeypatch, capsys):
    out = _run_board(monkeypatch, capsys, quote_store, quotes_dir)
    assert "SUPPLIER QUOTES" not in out
    assert not (quotes_dir / supplier.JOIN_FILE).exists()


# --- the command line -----------------------------------------------------------------------

def _cli():
    spec = importlib.util.spec_from_file_location(
        "supplier_quote_cli", REPO / "scripts" / "supplier_quote.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_ADD = ["add", "--product-key", "gtin:111", "--leaf", LEAF, "--supplier", "Example Ltd",
        "--unit-cost", "6.40", "--postage", "2.10", "--ship-from", "gb",
        "--latest-delivery-days", "4", "--returns-terms", "30 days",
        "--quote-date", "2026-10-05", "--source-url", "https://example.com/q/1"]


def test_cli_add_and_list(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PHONELOOP_SUPPLIERS_DIR", str(tmp_path))
    cli = _cli()
    assert cli.main(_ADD + ["--kind", "wholesale"]) == 0
    assert "stored q1" in capsys.readouterr().out
    q = QuoteBook(tmp_path).get("q1")
    assert (q.unit_cost_pence, q.postage_pence, q.ship_from) == (640, 210, "GB")
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "landed £8.50" in out and "returns allowance £0.88" in out


def test_cli_refuses_marketplace_retail(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PHONELOOP_SUPPLIERS_DIR", str(tmp_path))
    assert _cli().main(_ADD + ["--kind", "marketplace_retail"]) == 2
    assert "REFUSED" in capsys.readouterr().err
    assert QuoteBook(tmp_path).list() == []
