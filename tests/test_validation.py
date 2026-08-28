"""The Terapeak validation table: Sam's readings against the derived
figures, so derived STR earns trust or gets replaced (design spec section 8;
contracts section 2.12).
"""

from __future__ import annotations

from datetime import UTC, datetime

from phoneloop.validation import TierError, ValidationRecord, ValidationTable


def _rec(product_key, tier, derived_str, official_bp, checked_at=None, **over):
    base = dict(
        product_key=product_key, identity_tier=tier,
        derived_str_lifetime=derived_str, derived_velocity=0.1,
        terapeak_str_90d_bp=official_bp, terapeak_sellers=5,
        terapeak_avg_sold_price_pence=1999,
        checked_at=checked_at or datetime(2026, 8, 27, tzinfo=UTC),
    )
    base.update(over)
    return ValidationRecord(**base)


class TestRoundTrip:
    def test_add_and_list_round_trip(self, tmp_path):
        table = ValidationTable(tmp_path)
        table.add(_rec("epid:123", "catalogued", 3.2, 3500))
        table.add(_rec("cluster:139971:0", "cluster", 1.1, 4000,
                       checked_at=datetime(2026, 8, 27, 1, tzinfo=UTC)))
        rows = table.list()
        assert len(rows) == 2
        keys = {r["product_key"] for r in rows}
        assert keys == {"epid:123", "cluster:139971:0"}
        row = next(r for r in rows if r["product_key"] == "epid:123")
        assert row["derived_str_lifetime"] == 3.2
        assert row["terapeak_str_90d_bp"] == 3500

    def test_add_accepts_a_plain_mapping(self, tmp_path):
        table = ValidationTable(tmp_path)
        table.add(dict(
            product_key="epid:9", identity_tier="catalogued",
            derived_str_lifetime=2.0, derived_velocity=0.2,
            terapeak_str_90d_bp=2000, terapeak_sellers=3,
            terapeak_avg_sold_price_pence=999,
            checked_at=datetime(2026, 8, 27, tzinfo=UTC),
        ))
        assert len(table.list()) == 1

    def test_add_returns_rows_written(self, tmp_path):
        table = ValidationTable(tmp_path)
        assert table.add(_rec("epid:1", "catalogued", 1.0, 1000)) == 1

    def test_two_records_same_instant_do_not_clobber_each_other(self, tmp_path):
        """Both records share checked_at to the microsecond (a batch import,
        or -- as here -- a fixed test clock); the filename collision guard
        in add() must not let the second write erase the first."""
        table = ValidationTable(tmp_path)
        stamp = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)
        table.add(_rec("epid:1", "catalogued", 1.0, 1000, checked_at=stamp))
        table.add(_rec("epid:2", "catalogued", 2.0, 2000, checked_at=stamp))
        assert len(table.list()) == 2


class TestErrorByTier:
    def test_tier_with_no_rows_is_absent_not_zero(self, tmp_path):
        table = ValidationTable(tmp_path)
        table.add(_rec("epid:1", "catalogued", 3.2, 3500))
        errors = table.error_by_tier()
        assert "cluster" not in errors
        assert "inferred" not in errors
        assert "catalogued" in errors

    def test_error_direction_and_magnitude(self, tmp_path):
        """derived_str_lifetime=2.0 -> 20000bp. official=10000bp (100%).
        signed error = (20000 - 10000) / 10000 * 10000 = 10000bp: the
        derived figure overstates the official one by exactly 100% of the
        official figure's own size.
        """
        table = ValidationTable(tmp_path)
        table.add(_rec("epid:1", "catalogued", 2.0, 10000))
        errors = table.error_by_tier()
        te = errors["catalogued"]
        assert isinstance(te, TierError)
        assert te.n == 1
        assert te.median_signed_error_bp == 10000
        assert te.median_abs_error_bp == 10000

    def test_understating_gives_a_negative_signed_error(self, tmp_path):
        """derived=0.5 -> 5000bp, official=10000bp: signed error =
        (5000-10000)/10000*10000 = -5000bp -- derived UNDERstates by half."""
        table = ValidationTable(tmp_path)
        table.add(_rec("epid:1", "catalogued", 0.5, 10000))
        te = table.error_by_tier()["catalogued"]
        assert te.median_signed_error_bp == -5000
        assert te.median_abs_error_bp == 5000

    def test_median_not_mean_across_two_readings(self, tmp_path):
        """Reading 1: derived=2.0 vs official=10000bp -> +10000bp signed.
        Reading 2: derived=0.5 vs official=10000bp -> -5000bp signed.
        Median of [10000, -5000] (sorted [-5000, 10000]) is their average,
        -5000+10000 = 5000, / 2 = 2500.
        """
        table = ValidationTable(tmp_path)
        table.add(_rec("epid:1", "catalogued", 2.0, 10000,
                       checked_at=datetime(2026, 8, 27, 1, tzinfo=UTC)))
        table.add(_rec("epid:2", "catalogued", 0.5, 10000,
                       checked_at=datetime(2026, 8, 27, 2, tzinfo=UTC)))
        te = table.error_by_tier()["catalogued"]
        assert te.n == 2
        assert te.median_signed_error_bp == 2500

    def test_missing_derived_excluded_from_the_comparison_but_counted_in_n(self, tmp_path):
        table = ValidationTable(tmp_path)
        table.add(_rec("epid:1", "catalogued", None, 10000))
        te = table.error_by_tier()["catalogued"]
        assert te.n == 1
        assert te.median_signed_error_bp is None
        assert te.median_abs_error_bp is None
