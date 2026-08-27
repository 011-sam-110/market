"""Every metric spec section 6 defines, each carrying its own denominator
(contracts section 2.10).

Rows are literal dicts built from the ROW_FIELDS column list (contracts
section 1.1) via `_row()` below -- every field not overridden is None,
exactly what `market_store.empty_row()` would produce for an unset column.
Every non-trivial expectation is hand-computed in its test's docstring.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from phoneloop.knobs import Knobs
from phoneloop.metrics import Metrics, check, compute

NOW = datetime(2026, 8, 27, tzinfo=UTC)
LEAF = "139971"


def _row(**over):
    base = {
        "source": "ebay", "source_id": "1", "seen_at": None, "category": "garden",
        "shard": "shard1", "title": "Item", "price_pence": 1000, "total_pence": 1000,
        "currency": "GBP", "condition": "New", "brand": None, "model": None,
        "storage_gb": None, "grade": None, "kind": "ask", "available": True,
        "region": None, "seller_id": "seller1", "url": None, "extra": None,
        "sold_qty": None, "qty_available": None, "seller_type": None,
        "seller_feedback_bp": None, "seller_feedback_n": None, "listed_at": None,
        "epid": None, "gtin": None, "mpn": None, "identity_tier": None,
        "origin_country": None, "promoted": None, "leaf_category_id": LEAF,
        "eta_days": None, "store_joined": None, "store_items_sold": None,
    }
    base.update(over)
    return base


class TestPopulation:
    def test_n_listings_n_sellers_n_business_n_private(self):
        rows = [
            _row(source_id="1", seller_id="a", seller_type="business"),
            _row(source_id="2", seller_id="a", seller_type="business"),  # same seller
            _row(source_id="3", seller_id="b", seller_type="private"),
            _row(source_id="4", seller_id="c", seller_type=None),
        ]
        m = compute(rows)
        assert m.n_listings == 4
        assert m.n_sellers == 3          # a, b, c -- distinct, a counted once
        assert m.n_business == 2
        assert m.n_private == 1


class TestCoverageGuard:
    def test_halving_known_denominator_changes_str_lifetime_and_coverage(self):
        """Two 4-row populations with the SAME numerator (Sigma sold_qty = 20)
        but a different count of known rows:

        X: sold_qty = [10, 10, None, None] -> sold_known=2, Sigma=20
           str_lifetime = 20 / 2 = 10.0, coverage = 2 / 4 = 0.5
        Y: sold_qty = [20, None, None, None] -> sold_known=1, Sigma=20
           str_lifetime = 20 / 1 = 20.0, coverage = 1 / 4 = 0.25

        Halving sold_known (2 -> 1) halves coverage (0.5 -> 0.25) and
        DOUBLES str_lifetime (10.0 -> 20.0), even though the total demand
        observed (Sigma sold_qty) never changed. The denominator is not
        decoration -- it changes the figure it sits beside.
        """
        x = compute([
            _row(source_id="1", sold_qty=10), _row(source_id="2", sold_qty=10),
            _row(source_id="3", sold_qty=None), _row(source_id="4", sold_qty=None),
        ])
        y = compute([
            _row(source_id="1", sold_qty=20), _row(source_id="2", sold_qty=None),
            _row(source_id="3", sold_qty=None), _row(source_id="4", sold_qty=None),
        ])
        assert x.sold_known == 2 and y.sold_known == 1
        assert x.coverage == pytest.approx(0.5)
        assert y.coverage == pytest.approx(0.25)
        assert x.str_lifetime == pytest.approx(10.0)
        assert y.str_lifetime == pytest.approx(20.0)


class TestMissingSoldExcluded:
    def test_a_row_with_sold_qty_none_is_excluded_not_counted_as_zero(self):
        """rows sold_qty = [5, None, 7]. sold_known must be 2 (the None row
        is excluded, not treated as a 0-sale row), so:

        str_lifetime = (5 + 7) / 2 = 6.0

        If the None were wrongly counted as a zero, sold_known would read 3
        and str_lifetime would be (5 + 0 + 7) / 3 = 4.0 -- a different,
        wrong, answer. success_rate over the two KNOWN rows is 2/2 = 1.0
        (both sold > 0); if the None counted as a non-selling row it would
        read 2/3.
        """
        m = compute([
            _row(source_id="1", sold_qty=5), _row(source_id="2", sold_qty=None),
            _row(source_id="3", sold_qty=7),
        ])
        assert m.sold_known == 2
        assert m.str_lifetime == pytest.approx(6.0)
        assert m.success_rate == pytest.approx(1.0)
        assert m.success_rate_denominator == 2


class TestPrivateSeller:
    def test_a_private_seller_row_increments_n_private_but_leaves_price_median_unchanged(self):
        """Three business rows landed at 2000p, 3000p, 4000p -- an odd
        count, so the median is the exact middle value with no rounding:
        price_median_pence = 3000.

        Adding one private-seller row landed at 100p must NOT move that
        median (private prices include the buyer-protection fee and are
        excluded from price aggregates by contract): if it were wrongly
        included, the four landed prices sorted would be
        [100, 2000, 3000, 4000], an even count, median = (2000+3000+1)//2
        = 2500 -- a different, wrong, answer. n_private must still read 1.
        """
        rows = [
            _row(source_id="1", seller_type="business", total_pence=2000),
            _row(source_id="2", seller_type="business", total_pence=3000),
            _row(source_id="3", seller_type="business", total_pence=4000),
            _row(source_id="4", seller_type="private", total_pence=100),
        ]
        m = compute(rows)
        assert m.n_private == 1
        assert m.price_median_pence == 3000
        assert m.price_median_denominator == 3

    def test_a_row_with_unset_seller_type_is_also_excluded_from_price_not_assumed_business(self):
        rows = [
            _row(source_id="1", seller_type="business", total_pence=2000),
            _row(source_id="2", seller_type=None, total_pence=1),
        ]
        m = compute(rows)
        assert m.price_median_pence == 2000
        assert m.price_median_denominator == 1


class TestPriceFloor:
    def test_price_floor_is_min_of_five_cheapest_business_rows_with_more_than_five(self):
        """Six business landed prices: 1000, 1100, 1200, 1300, 1400, 5000.
        The five cheapest are the first five; their min is 1000 -- the same
        as the overall min, which is the point of calling it a floor. The
        denominator caps at 5 even though six rows exist.
        """
        rows = [
            _row(source_id=str(i), seller_type="business", total_pence=p)
            for i, p in enumerate([1000, 1100, 1200, 1300, 1400, 5000])
        ]
        m = compute(rows)
        assert m.price_floor_pence == 1000
        assert m.price_floor_denominator == 5

    def test_price_floor_denominator_reports_fewer_than_five_when_fewer_exist(self):
        rows = [
            _row(source_id=str(i), seller_type="business", total_pence=p)
            for i, p in enumerate([2000, 2100, 2200])
        ]
        m = compute(rows)
        assert m.price_floor_pence == 2000
        assert m.price_floor_denominator == 3

    def test_no_business_rows_gives_none_not_zero(self):
        rows = [_row(source_id="1", seller_type="private", total_pence=100)]
        m = compute(rows)
        assert m.price_floor_pence is None
        assert m.price_floor_denominator == 0


class TestMedianRounding:
    def test_even_count_median_of_integer_pence_rounds_half_up(self):
        """Two business rows at 1001p and 1002p: (1001+1002)/2 = 1001.5,
        which rounds half up to 1002 -- (1001+1002+1)//2 = 1002.
        """
        rows = [
            _row(source_id="1", seller_type="business", total_pence=1001),
            _row(source_id="2", seller_type="business", total_pence=1002),
        ]
        m = compute(rows)
        assert m.price_median_pence == 1002

    def test_odd_count_median_needs_no_rounding(self):
        rows = [
            _row(source_id="1", seller_type="business", total_pence=1000),
            _row(source_id="2", seller_type="business", total_pence=2000),
            _row(source_id="3", seller_type="business", total_pence=9000),
        ]
        m = compute(rows)
        assert m.price_median_pence == 2000


class TestVelocity:
    def test_velocity_day1_sums_each_known_listings_own_rate(self):
        """r1: sold_qty=10, listed 10 days ago -> 10/10 = 1.0 units/day.
        r2: sold_qty=30, listed 5 days ago -> 30/5 = 6.0 units/day.
        r3: sold_qty=None -> excluded.
        velocity_day1 = 1.0 + 6.0 = 7.0, denominator = 2.
        """
        rows = [
            _row(source_id="1", sold_qty=10, listed_at=datetime(2026, 8, 17, tzinfo=UTC)),
            _row(source_id="2", sold_qty=30, listed_at=datetime(2026, 8, 22, tzinfo=UTC)),
            _row(source_id="3", sold_qty=None, listed_at=datetime(2026, 8, 1, tzinfo=UTC)),
        ]
        m = compute(rows, now=NOW)
        assert m.velocity_day1 == pytest.approx(7.0)
        assert m.velocity_day1_denominator == 2

    def test_a_listing_with_no_elapsed_time_is_excluded_not_a_division_by_zero(self):
        rows = [_row(source_id="1", sold_qty=5, listed_at=NOW)]
        m = compute(rows, now=NOW)
        assert m.velocity_day1 is None
        assert m.velocity_day1_denominator == 0


class TestWindowedDemand:
    def test_str_window_and_velocity_win_from_the_deltas_mapping(self):
        """deltas gives source "1" -> (delta_sold=2, delta_days=7) and
        "2" -> (4, 7); "3" is absent (unknown this window).

        str_window_bp: Sigma delta_sold / known_count x 100 (percent) x 100
        (bp) = (2+4)/2 x 10000 = 3.0 x 10000 = 30000.
        velocity_win: Sigma (delta_sold / delta_days) = 2/7 + 4/7 = 6/7.
        """
        rows = [_row(source_id="1"), _row(source_id="2"), _row(source_id="3")]
        deltas = {"1": (2, 7), "2": (4, 7)}
        m = compute(rows, deltas=deltas)
        assert m.str_window_denominator == 2
        assert m.str_window_bp == 30000
        assert m.velocity_win_denominator == 2
        assert m.velocity_win == pytest.approx(6 / 7)

    def test_no_deltas_leaves_the_window_figures_none_not_zero(self):
        m = compute([_row(source_id="1")], deltas=None)
        assert m.str_window_bp is None
        assert m.str_window_denominator == 0
        assert m.velocity_win is None

    def test_a_source_id_absent_from_deltas_is_unknown_this_window(self):
        m = compute([_row(source_id="9")], deltas={"1": (5, 7)})
        assert m.str_window_bp is None
        assert m.str_window_denominator == 0


class TestDispersion:
    def test_dispersion_is_sellers_with_sales_over_sellers_with_known_sold_qty(self):
        """Seller A: one row sold_qty=5 (known, >0), one row sold_qty=None.
        Seller B: one row sold_qty=0 (known, not >0).
        Seller C: one row sold_qty=None (never contributes a known figure).

        sellers_known = {A, B} (C has no row with a known sold_qty) = 2.
        sellers_with_sales = {A} = 1.
        dispersion = 1 / 2 = 0.5.
        """
        rows = [
            _row(source_id="1", seller_id="A", sold_qty=5),
            _row(source_id="2", seller_id="A", sold_qty=None),
            _row(source_id="3", seller_id="B", sold_qty=0),
            _row(source_id="4", seller_id="C", sold_qty=None),
        ]
        m = compute(rows)
        assert m.dispersion_denominator == 2
        assert m.dispersion == pytest.approx(0.5)


class TestSaturationSignals:
    def test_new_7d_counts_listings_from_the_last_seven_days_only(self):
        """r1 listed 2 days before `now` -- within 7 days, counts.
        r2 listed 17 days before `now` -- outside the window, does not.
        r3 has no listed_at at all -- excluded from the denominator too.
        new_7d = 1, denominator = 2 (r1 and r2 both have a listed_at).
        """
        rows = [
            _row(source_id="1", listed_at=datetime(2026, 8, 25, tzinfo=UTC)),
            _row(source_id="2", listed_at=datetime(2026, 8, 10, tzinfo=UTC)),
            _row(source_id="3", listed_at=None),
        ]
        m = compute(rows, now=NOW)
        assert m.new_7d == 1
        assert m.new_7d_denominator == 2

    def test_promoted_pct_excludes_unknown_promoted_state(self):
        rows = [
            _row(source_id="1", promoted=True),
            _row(source_id="2", promoted=False),
            _row(source_id="3", promoted=None),
        ]
        m = compute(rows)
        assert m.promoted_denominator == 2
        assert m.promoted_pct == pytest.approx(0.5)


class TestDeliveryAndFeedback:
    def test_eta_median_is_business_sellers_only(self):
        rows = [
            _row(source_id="1", seller_type="business", eta_days=3),
            _row(source_id="2", seller_type="business", eta_days=5),
            _row(source_id="3", seller_type="private", eta_days=1),
        ]
        m = compute(rows)
        assert m.eta_median_denominator == 2
        assert m.eta_median_days == 4  # (3+5+1)//2 = 4

    def test_feedback_median_needs_both_bp_and_n_known(self):
        rows = [
            _row(source_id="1", seller_feedback_bp=9800, seller_feedback_n=500),
            _row(source_id="2", seller_feedback_bp=9900, seller_feedback_n=1000),
            _row(source_id="3", seller_feedback_bp=None, seller_feedback_n=200),
        ]
        m = compute(rows)
        assert m.feedback_median_denominator == 2
        assert m.feedback_median_bp == 9850
        assert m.feedback_median_n == 750

    def test_new_account_share_uses_store_joined_against_now(self):
        """r1 joined 26 days before `now` -- under a year, counts.
        r2 joined in 2020 -- well over a year, does not.
        r3 has no store_joined -- excluded from the denominator.
        new_account_share = 1 / 2.
        """
        rows = [
            _row(source_id="1", store_joined=date(2026, 8, 1)),
            _row(source_id="2", store_joined=date(2020, 1, 1)),
            _row(source_id="3", store_joined=None),
        ]
        m = compute(rows, now=NOW)
        assert m.new_account_denominator == 2
        assert m.new_account_share == pytest.approx(0.5)


class TestEmptyInput:
    def test_no_rows_gives_zero_coverage_and_no_crash(self):
        m = compute([])
        assert m.n_listings == 0
        assert m.coverage == 0.0
        assert m.str_lifetime is None
        assert m.price_median_pence is None


class TestCheck:
    def test_knob_order_is_fixed(self):
        m = compute([])
        results = check(m, Knobs())
        assert [r.knob for r in results] == [
            "min_margin", "sell_through", "price_band",
            "delivery", "seller_quality", "saturation",
        ]

    def test_sell_through_never_falls_back_to_lifetime_when_window_is_unknown(self):
        """sold_qty = [9000, 9000] gives a huge str_lifetime (9000.0) --
        larger, as a raw number, than knobs.str_threshold_bp (5000). A
        buggy check() that compared str_lifetime directly to the bp
        threshold would wrongly report passed=True. No deltas are supplied,
        so str_window_bp is None, and the correct answer is passed=None
        with a note that only a lifetime figure exists.
        """
        rows = [_row(source_id="1", sold_qty=9000), _row(source_id="2", sold_qty=9000)]
        m = compute(rows, deltas=None)
        assert m.str_window_bp is None
        assert m.str_lifetime == pytest.approx(9000.0)

        results = check(m, Knobs())
        sell_through = next(r for r in results if r.knob == "sell_through")
        assert sell_through.passed is None
        assert "lifetime" in sell_through.note
        assert "not comparable" in sell_through.note or "window" in sell_through.note

    def test_sell_through_passes_when_the_window_figure_clears_the_threshold(self):
        rows = [_row(source_id="1"), _row(source_id="2")]
        deltas = {"1": (1, 30), "2": (0, 30)}  # 1 sale out of 2 known -> 50%
        m = compute(rows, deltas=deltas)
        assert m.str_window_bp == 5000  # exactly the default threshold
        results = check(m, Knobs())
        sell_through = next(r for r in results if r.knob == "sell_through")
        assert sell_through.passed is True

    def test_price_band_pass_and_fail(self):
        in_band = compute([_row(source_id="1", seller_type="business", total_pence=3000)])
        out_of_band = compute([_row(source_id="1", seller_type="business", total_pence=100)])
        knobs = Knobs()
        in_result = next(r for r in check(in_band, knobs) if r.knob == "price_band")
        out_result = next(r for r in check(out_of_band, knobs) if r.knob == "price_band")
        assert in_result.passed is True
        assert out_result.passed is False

    def test_price_band_unknown_when_no_business_price_observed(self):
        m = compute([_row(source_id="1", seller_type="private", total_pence=3000)])
        result = next(r for r in check(m, Knobs()) if r.knob == "price_band")
        assert result.passed is None
        assert result.denominator == 0

    def test_delivery_pass_and_fail(self):
        fast = compute([_row(source_id="1", seller_type="business", eta_days=3)])
        slow = compute([_row(source_id="1", seller_type="business", eta_days=10)])
        knobs = Knobs()
        assert next(r for r in check(fast, knobs) if r.knob == "delivery").passed is True
        assert next(r for r in check(slow, knobs) if r.knob == "delivery").passed is False

    def test_seller_quality_pass_and_fail(self):
        good = compute([_row(source_id="1", seller_feedback_bp=9900, seller_feedback_n=200)])
        thin = compute([_row(source_id="1", seller_feedback_bp=9900, seller_feedback_n=5)])
        knobs = Knobs()
        assert next(r for r in check(good, knobs) if r.knob == "seller_quality").passed is True
        assert next(r for r in check(thin, knobs) if r.knob == "seller_quality").passed is False

    def test_saturation_pass_and_fail_and_unknown(self):
        m = compute([])
        knobs = Knobs()
        low = next(r for r in check(m, knobs, competitors=10) if r.knob == "saturation")
        high = next(r for r in check(m, knobs, competitors=20) if r.knob == "saturation")
        unknown = next(r for r in check(m, knobs, competitors=None) if r.knob == "saturation")
        assert low.passed is True
        assert high.passed is False
        assert unknown.passed is None

    def test_saturation_zero_competitors_is_known_not_a_zero_denominator(self):
        """competitors=0 is a real, known result (no rivals found at the
        best identity tier) and passes trivially -- but the old code set
        `denominator=competitors or 0`, so a known-zero collided with the
        check() docstring's own rule that a zero denominator means
        "unknown". denominator must track whether the count was supplied,
        not the count's value.
        """
        m = compute([])
        zero = next(r for r in check(m, Knobs(), competitors=0) if r.knob == "saturation")
        assert zero.passed is True
        assert zero.denominator == 1
        assert zero.coverage == pytest.approx(1.0)

        none_supplied = next(r for r in check(m, Knobs(), competitors=None)
                             if r.knob == "saturation")
        assert none_supplied.passed is None
        assert none_supplied.denominator == 0
        assert none_supplied.coverage == pytest.approx(0.0)

    def test_min_margin_pass_fail_and_unknown(self):
        m = compute([])
        knobs = Knobs()
        clears = next(r for r in check(m, knobs, max_cost_at_median_pence=1832)
                     if r.knob == "min_margin")
        fails = next(r for r in check(m, knobs, max_cost_at_median_pence=-50)
                    if r.knob == "min_margin")
        unknown = next(r for r in check(m, knobs) if r.knob == "min_margin")
        assert clears.passed is True
        assert fails.passed is False
        assert unknown.passed is None

    def test_min_margin_floor_basis_names_itself_and_carries_the_floor_denominator(self):
        """One business row -> price_floor_denominator = min(5, 1) = 1 and
        price_median_denominator = 1 too, but they must not be conflated:
        calling check() with ONLY max_cost_at_floor_pence (no
        max_cost_at_median_pence) must report the floor basis in `note` and
        the floor's own denominator/coverage, never the median's -- even
        though both happen to be 1 here, this pins the branch, not the
        coincidence. See test_min_margin_floor_basis_denominator_differs_
        from_median below for a case where they actually differ.
        """
        m = compute([_row(source_id="1", seller_type="business", total_pence=2000)])
        assert m.price_floor_denominator == 1
        result = next(r for r in check(m, Knobs(), max_cost_at_floor_pence=1500)
                     if r.knob == "min_margin")
        assert result.value == 1500
        assert result.passed is True
        assert "price_floor_pence" in result.note
        assert "price_median_pence" not in result.note
        assert result.denominator == m.price_floor_denominator == 1
        assert result.coverage == pytest.approx(1.0)

    def test_min_margin_floor_basis_denominator_differs_from_median(self):
        """A private-seller-only niche: business_landed is empty, so BOTH
        price_median_denominator and price_floor_denominator are 0 -- but
        before the fix, check() hard-wired price_median_denominator into
        every min_margin result regardless of which basis was actually
        used, and mislabelled the note as median even on the floor branch.
        This reproduces the reviewer's exact repro: a real, known
        max_cost_at_floor_pence must not come back mislabelled.
        """
        m = compute([_row(source_id="1", seller_type="private", total_pence=2000)])
        result = next(r for r in check(m, Knobs(), max_cost_at_floor_pence=1500)
                     if r.knob == "min_margin")
        assert result.value == 1500
        assert result.passed is True
        assert "price_floor_pence" in result.note
        assert result.denominator == m.price_floor_denominator == 0
        assert result.coverage == pytest.approx(0.0)

    def test_min_margin_median_basis_still_used_when_supplied(self):
        """When the caller supplies max_cost_at_median_pence, the median
        basis and its own denominator/coverage are used, unchanged from
        before the fix -- pins that the branch selection itself (median
        wins when both could apply) was not disturbed.
        """
        m = compute([_row(source_id="1", seller_type="business", total_pence=2000)])
        result = next(r for r in check(m, Knobs(), max_cost_at_median_pence=1832,
                                        max_cost_at_floor_pence=1500)
                     if r.knob == "min_margin")
        assert result.value == 1832
        assert "price_median_pence" in result.note
        assert result.denominator == m.price_median_denominator

    def test_a_zero_denominator_is_none_never_false(self):
        """Every rows-derived knob on an empty population must read
        passed=None, not passed=False -- "we don't know" and "it failed"
        are different claims."""
        m = compute([])
        results = check(m, Knobs())
        by_knob = {r.knob: r for r in results}
        assert by_knob["sell_through"].passed is None
        assert by_knob["price_band"].passed is None
        assert by_knob["delivery"].passed is None
        assert by_knob["seller_quality"].passed is None
