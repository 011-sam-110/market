"""Exit arithmetic.

The numbers in these tests are the real ones measured on 2026-08-27: CeX pays
12300p cash for a grade-B iPhone 13, and the eBay UK used median landed price
for a 128GB iPhone 13 was 21642p across 14 comps.
"""

from __future__ import annotations

from phoneloop.spread import (
    BUSINESS,
    PRIVATE,
    Exit,
    FeeModel,
    cex_exit,
    ebay_exit,
    evaluate,
)

CEX_IPHONE13_B = 12300
EBAY_IPHONE13_128 = 21642


class TestFees:
    def test_a_rate_and_a_fixed_charge(self):
        assert BUSINESS.on(10000) == 1280 + 30

    def test_a_fee_free_account_takes_nothing(self):
        assert PRIVATE.on(10000) == 0

    def test_fees_round_to_whole_pence(self):
        assert isinstance(FeeModel("x", 0.128).on(19999), int)


class TestExits:
    def test_cex_is_committed_and_uncharged(self):
        e = cex_exit(CEX_IPHONE13_B)
        assert e.net_pence == CEX_IPHONE13_B
        assert e.committed is True

    def test_ebay_is_discounted_twice_and_never_committed(self):
        """Once to undercut the median, once for fees, once for postage."""
        e = ebay_exit(EBAY_IPHONE13_128, fees=BUSINESS, undercut=0.05,
                      ship_pence=850)
        assert e.gross_pence == 20560          # 5% under the median ask
        assert e.fees_pence == 2662            # 12.8% + 30p
        assert e.net_pence == 20560 - 2662 - 850
        assert e.committed is False

    def test_the_fee_model_materially_changes_the_answer(self):
        """Which account you sell from is worth ~26 pounds on one handset,
        which is why the default is the conservative one."""
        biz = ebay_exit(EBAY_IPHONE13_128, fees=BUSINESS)
        pvt = ebay_exit(EBAY_IPHONE13_128, fees=PRIVATE)
        assert pvt.net_pence - biz.net_pence == 2662


class TestSpread:
    def test_it_picks_the_better_exit(self):
        s = evaluate(11000, CEX_IPHONE13_B, EBAY_IPHONE13_128)
        assert s.best.venue == "ebay"

    def test_the_floor_is_the_committed_exit_not_the_best_one(self):
        s = evaluate(11000, CEX_IPHONE13_B, EBAY_IPHONE13_128)
        assert s.floor.venue == "cex"
        assert s.floor_margin_pence == CEX_IPHONE13_B - 11000

    def test_a_buy_under_the_cex_floor_cannot_lose(self):
        s = evaluate(11000, CEX_IPHONE13_B, EBAY_IPHONE13_128)
        assert s.downside_covered is True

    def test_a_buy_over_the_cex_floor_is_a_bet_on_ebay(self):
        """The eBay margin still looks healthy here, and the trade is still
        exposed: if it does not sell, the guaranteed exit is a loss."""
        s = evaluate(15000, CEX_IPHONE13_B, EBAY_IPHONE13_128)
        assert s.margin_pence > 0
        assert s.downside_covered is False
        assert s.floor_margin_pence == -2700

    def test_cex_only_still_evaluates(self):
        s = evaluate(9000, cex_cash_pence=CEX_IPHONE13_B)
        assert s.best.venue == "cex"
        assert s.downside_covered is True

    def test_ebay_only_has_no_floor_at_all(self):
        s = evaluate(9000, ebay_ceiling_pence=EBAY_IPHONE13_128)
        assert s.floor is None
        assert s.floor_margin_pence is None
        assert s.downside_covered is False

    def test_no_priceable_exit_is_none_not_zero(self):
        """A handset with no CeX box and too few comps has no answer. Saying
        so is the whole point; a zero would read as a bad deal rather than an
        unknown one."""
        assert evaluate(9000) is None

    def test_roi_survives_a_zero_buy_price(self):
        assert evaluate(0, CEX_IPHONE13_B).roi == 0.0
