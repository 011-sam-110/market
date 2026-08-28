"""The population review page: the whole scraped eBay population, and how
much of it can actually be read.

Fixtures are built with `market_store.empty_row()`, the same pattern
`tests/test_board.py` uses, so every ROW_FIELDS column is present and a
forgotten one cannot be silent.

Leaf ids are REAL in-scope leaves out of `ebay_categories.json`, not arbitrary
strings: `population.build` filters its niches to
`catalog_ebay.top_level_of()`, so a synthetic leaf id would be dropped before
any panel saw it and every assertion below would pass vacuously.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from phoneloop.knobs import Knobs
from phoneloop.market_store import MarketStore, empty_row

LEAF_A = "9355"      # Mobile Phones & Smartphones (dept 15032)
LEAF_B = "25863"     # Patio & Garden Furniture (dept 159912)
LEAF_SPARSE = "20444"  # Home Bedding (dept 11700)

DAY_A = date(2026, 8, 20)
DAY_B = date(2026, 8, 21)
SEEN_A = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
SEEN_B = datetime(2026, 8, 21, 9, 0, tzinfo=UTC)


def _row(source_id: str, leaf: str, seller_id: str | None, seen_at: datetime,
         **over) -> dict:
    row = empty_row("ebay", source_id)
    row.update({
        "seen_at": seen_at,
        "category": "garden_patio",
        "shard": f"{leaf}/15-55/p1",
        "title": f"Widget {source_id}",
        "price_pence": 3600,
        "total_pence": 4000,
        "currency": "GBP",
        "condition": "New",
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
        "extra": '{"aspects":[],"watchers":0,"ships_from":"","stock_hint":""}',
    })
    row.update(over)
    return row


@pytest.fixture
def knobs():
    return Knobs()


@pytest.fixture
def one_date_store(tmp_path):
    """One snapshot date -- nothing windowed can be computed from it."""
    s = MarketStore(tmp_path)
    s.record([
        _row("a1", LEAF_A, "sellerA", SEEN_A),
        _row("a2", LEAF_A, "sellerB", SEEN_A, total_pence=4200),
        _row("b1", LEAF_B, "sellerC", SEEN_A, total_pence=5000),
    ])
    return s


@pytest.fixture
def two_date_store(tmp_path):
    """The SAME listings observed on two dates, with sold_qty moving -- the
    only fixture that can make a windowed sell-through real."""
    s = MarketStore(tmp_path)
    s.record([
        _row("a1", LEAF_A, "sellerA", SEEN_A, sold_qty=5),
        _row("a2", LEAF_A, "sellerB", SEEN_A, sold_qty=7),
    ])
    s.record([
        _row("a1", LEAF_A, "sellerA", SEEN_B, sold_qty=9),
        _row("a2", LEAF_A, "sellerB", SEEN_B, sold_qty=11),
    ])
    return s


@pytest.fixture
def sparse_seller_store(tmp_path):
    """A leaf whose seller column is 5% present, beside one that is complete.

    This is the shape that makes a board row green for the wrong reason: with
    one known seller the saturation knob PASSES (1 <= max_competitors), and
    board.py's score counts an unknown knob as neither pass nor fail, so
    absence and satisfaction score identically.
    """
    s = MarketStore(tmp_path)
    rows = [_row(f"s{i}", LEAF_SPARSE, None, SEEN_A) for i in range(19)]
    rows.append(_row("s19", LEAF_SPARSE, "lonelySeller", SEEN_A))
    rows += [_row(f"h{i}", LEAF_A, f"seller{i}", SEEN_A) for i in range(20)]
    s.record(rows)
    return s


class TestLockedPanels:
    def test_locked_panel_names_its_requirement(self, one_date_store, knobs,
                                                tmp_path):
        """A panel that cannot be computed from one date must still be
        PRESENT, must name what it needs, and must show no figure at all --
        an absent panel silently rewards a thin store, and a placeheld one
        lies about it."""
        from phoneloop import population, population_html

        b = population.build(tmp_path, knobs)
        out = population_html.render(b)

        block = _panel_block(out, "sell_through")
        assert block is not None, "the sell_through panel is missing entirely"
        assert "needs a 2nd sweep" in block.lower()
        assert "1 of 2 snapshot dates" in block
        assert 'class="figure"' not in block


    def test_two_date_fixture_fills_the_same_panel(self, two_date_store, knobs,
                                                   tmp_path):
        """The same render path, one fixture heavier: the panel that was
        locked above now carries a real figure with a denominator and no
        longer asks for a second sweep.

        This is the only honest evidence that the locked panels fill in
        without a rewrite, and it costs exactly one fixture."""
        from phoneloop import population, population_html

        b = population.build(tmp_path, knobs)
        assert len(b.dates) == 2, "the fixture must produce two snapshot dates"
        out = population_html.render(b)

        block = _panel_block(out, "sell_through")
        assert block is not None
        assert "needs a 2nd sweep" not in block.lower()
        assert 'class="figure"' in block
        assert 'class="denom"' in block


class TestSelfContained:
    def test_self_contained_no_external_resources(self, one_date_store, knobs,
                                                  tmp_path):
        """Mirrors tests/test_board.py's guard: the page must carry every
        byte it needs, so it survives being emailed, archived or opened with
        no network."""
        from phoneloop import population, population_html

        out = population_html.render(population.build(tmp_path, knobs))
        assert "http://" not in out
        assert "https://" not in out
        assert "<style>" in out

    def test_no_listing_url_reaches_the_output(self, one_date_store, knobs,
                                               tmp_path):
        """Every fixture row carries a real listing url. `url` is outside the
        explorer whitelist by construction, and this is what enforces that --
        the self-contained guard alone would not, because it cannot tell a
        listing url from a stylesheet link."""
        from phoneloop import population, population_html

        out = population_html.render(population.build(tmp_path, knobs))
        assert "ebay.co.uk/itm" not in out
        assert "http://" not in out
        assert "https://" not in out

    def test_url_in_a_title_is_stripped(self, tmp_path, knobs):
        """A title may legitimately contain a url, which would carry an
        `https://` substring past the guard above. It is replaced BEFORE
        encoding, and the replacement is visible rather than silent."""
        from phoneloop import population, population_html

        s = MarketStore(tmp_path)
        s.record([_row("u1", LEAF_A, "sellerA", SEEN_A,
                       title="Widget https://evil.example/x")])
        out = population_html.render(population.build(tmp_path, knobs))
        assert "https://" not in out
        assert "evil.example" in out          # the row is still shown
        assert "[link removed]" in out

    def test_script_close_tag_in_a_title_cannot_break_out(self, tmp_path, knobs):
        """`<` is emitted as \\u003c throughout the JSON blob, so a title
        holding a closing script tag cannot end the element early and turn
        the rest of the payload into markup."""
        from phoneloop import population, population_html

        s = MarketStore(tmp_path)
        s.record([_row("x1", LEAF_A, "sellerA", SEEN_A,
                       title="Widget </script><img src=x> end")])
        out = population_html.render(population.build(tmp_path, knobs))
        assert out.count("</script>") == out.count("<script")
        assert out.count("</script>") == 2   # the JSON blob and the explorer JS


class TestEveryFigureCarriesADenominator:
    def test_every_figure_carries_a_denominator(self, one_date_store, knobs,
                                                tmp_path):
        """Rule 2 of the project's data rules, enforced structurally rather
        than by review: a number without the evidence it rests on cannot be
        rendered, because the only helper that emits one emits both."""
        from phoneloop import population, population_html

        out = population_html.render(population.build(tmp_path, knobs))
        assert out.count('class="figure"') == out.count('class="denom"')
        assert out.count('class="figure"') > 0


class TestMissingDataPasses:
    def test_missing_data_pass_is_flagged(self, sparse_seller_store, knobs,
                                          tmp_path):
        from phoneloop import population

        b = population.build(tmp_path, knobs)
        payload, reason = population.panel_green_because_broken(b)
        assert reason is None
        flagged = {f["leaf"] for f in payload["flagged"]}
        assert LEAF_SPARSE in flagged
        entry = next(f for f in payload["flagged"] if f["leaf"] == LEAF_SPARSE)
        assert entry["seller_cov"] < 0.5
        assert "saturation" in entry["passing"]

    def test_low_seller_coverage_leaf_is_excluded_from_density(
            self, sparse_seller_store, knobs, tmp_path):
        """Ungated, this leaf shows a 100% top-5 share off ONE known seller
        and ranks as the store's most concentrated market. Absence must not
        be able to masquerade as a finding."""
        from phoneloop import population, population_html

        b = population.build(tmp_path, knobs)
        payload, reason = population.panel_competition_density(b)
        assert reason is None
        assert LEAF_SPARSE not in {e["leaf"] for e in payload["measured"]}
        excluded = {e["leaf"]: e for e in payload["excluded"]}
        assert LEAF_SPARSE in excluded
        assert excluded[LEAF_SPARSE]["seller_cov"] == pytest.approx(1 / 20)

        block = _panel_block(population_html.render(b), "competition_density")
        assert "5.00%" in block
        assert "not measurable" in block


class TestEmptyStore:
    def test_empty_store_renders_a_named_state(self, tmp_path, knobs):
        """A store with no eBay rows renders a NAMED state, not a page of
        zeroes -- "0 listings" and "we never looked" are different claims."""
        from phoneloop import population, population_html

        MarketStore(tmp_path)
        b = population.build(tmp_path, knobs)
        assert b.day is None
        out = population_html.render(b)
        assert "no eBay snapshots" in out
        assert str(tmp_path) in out
        assert 'class="figure"' not in out


class TestSqlAgreesWithRowsOn:
    def test_sql_and_rows_on_agree(self, one_date_store, knobs, tmp_path):
        """The provenance count and `rows_on` must be the same population.
        Without the newest-per-source_id window the raw table overstates the
        listing count, and every figure on the page divides by it."""
        from phoneloop import population

        b = population.build(tmp_path, knobs)
        payload, reason = population.panel_provenance(b)
        assert reason is None
        assert payload["listings"] == len(one_date_store.rows_on(b.day))
        assert payload["listings"] == len(b.rows)


class TestWriteReport:
    def test_writes_under_data_dir_reports(self, one_date_store, knobs, tmp_path):
        from phoneloop import population, population_html

        b = population.build(tmp_path, knobs)
        path = population_html.write_report(b, tmp_path)
        assert path.exists()
        assert path.parent.name == "reports"
        assert path.parent.parent == tmp_path
        text = path.read_text(encoding="utf-8")
        assert "http://" not in text
        assert "https://" not in text


def _panel_block(html_out: str, panel: str) -> str | None:
    """The rendered markup of one panel, by its data-panel attribute."""
    marker = f'data-panel="{panel}"'
    start = html_out.find(marker)
    if start < 0:
        return None
    # Panels are siblings; the next data-panel (or end of document) bounds
    # this one. Crude, and deliberately so -- a real parser here would let a
    # figure in a nested panel leak into this block unnoticed.
    nxt = html_out.find("data-panel=", start + len(marker))
    return html_out[start:nxt if nxt > 0 else len(html_out)]
