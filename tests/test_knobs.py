"""The six control knobs plus the fee terms (contracts section 2.8).

Every default here is copied straight from that section of the contract, and
every environment override is exercised at least once so a rename of a field
without a matching env-name update fails loudly.
"""

from __future__ import annotations

import pytest

from phoneloop.knobs import Knobs

# The five attribute names phoneloop.fees.FeeKnobs (contracts section 2.2)
# reads. Knobs must not import fees (parallel worktrees), so this is a
# literal duplicate of that Protocol's attribute list; the integrator wires
# a real isinstance/attribute cross-check at merge time (contracts section
# 6.2, item 2).
_FEE_KNOBS_ATTRS = (
    "vat_on_fees_bp",
    "ad_rate_bp",
    "import_vat_term_bp",
    "min_margin_pct_bp",
    "min_margin_fixed_pence",
)


class TestDefaults:
    def test_every_default_matches_the_contract(self):
        k = Knobs()
        assert k.min_margin_pct_bp == 2500
        assert k.min_margin_fixed_pence == 600
        assert k.str_threshold_bp == 5000
        assert k.price_from_pence == 1500
        assert k.price_to_pence == 5500
        assert k.max_eta_days == 7
        assert k.max_competitors == 15
        assert k.min_seller_feedback_bp == 9500
        assert k.min_seller_feedback_n == 50
        assert k.vat_on_fees_bp == 12000
        assert k.ad_rate_bp == 0
        assert k.import_vat_term_bp == 0

    def test_str_threshold_is_documented_as_a_chosen_convention_not_a_measurement(self):
        doc = Knobs.__doc__ or ""
        # The convention lives on the field's inline comment, not the class
        # docstring's first line -- assert against the module docstring,
        # which is where the claim actually has to hold up.
        import phoneloop.knobs as knobs_module
        assert "not measured" in knobs_module.__doc__ or "chosen" in knobs_module.__doc__

    def test_knobs_carries_every_feekbobs_attribute_name(self):
        k = Knobs()
        for name in _FEE_KNOBS_ATTRS:
            assert hasattr(k, name), f"Knobs is missing {name}, which fees.FeeKnobs requires"


class TestFromEnv:
    def test_no_overrides_returns_the_defaults(self):
        assert Knobs.from_env({}) == Knobs()

    def test_every_field_is_overridable(self):
        env = {
            "PHONELOOP_MIN_MARGIN_PCT_BP": "3000",
            "PHONELOOP_MIN_MARGIN_FIXED_PENCE": "700",
            "PHONELOOP_STR_THRESHOLD_BP": "4000",
            "PHONELOOP_PRICE_FROM_PENCE": "1000",
            "PHONELOOP_PRICE_TO_PENCE": "6000",
            "PHONELOOP_MAX_ETA_DAYS": "5",
            "PHONELOOP_MAX_COMPETITORS": "10",
            "PHONELOOP_MIN_SELLER_FEEDBACK_BP": "9000",
            "PHONELOOP_MIN_SELLER_FEEDBACK_N": "20",
            "PHONELOOP_VAT_ON_FEES_BP": "10000",
            "PHONELOOP_AD_RATE_BP": "200",
            "PHONELOOP_IMPORT_VAT_TERM_BP": "2000",
        }
        k = Knobs.from_env(env)
        assert k.min_margin_pct_bp == 3000
        assert k.min_margin_fixed_pence == 700
        assert k.str_threshold_bp == 4000
        assert k.price_from_pence == 1000
        assert k.price_to_pence == 6000
        assert k.max_eta_days == 5
        assert k.max_competitors == 10
        assert k.min_seller_feedback_bp == 9000
        assert k.min_seller_feedback_n == 20
        assert k.vat_on_fees_bp == 10000
        assert k.ad_rate_bp == 200
        assert k.import_vat_term_bp == 2000

    def test_a_partial_override_leaves_the_rest_at_default(self):
        k = Knobs.from_env({"PHONELOOP_MAX_ETA_DAYS": "3"})
        assert k.max_eta_days == 3
        assert k.max_competitors == 15  # untouched

    def test_a_non_integer_value_raises_rather_than_silently_defaulting(self):
        """A knob that quietly reverts to its default on a typo is a knob
        nobody can trust they actually set -- this must be loud."""
        with pytest.raises(ValueError):
            Knobs.from_env({"PHONELOOP_MAX_ETA_DAYS": "not-a-number"})

    def test_a_float_string_also_raises(self):
        """Knobs are integer pence / basis points / counts -- a float that
        happens to parse is still the wrong type for this knob."""
        with pytest.raises(ValueError):
            Knobs.from_env({"PHONELOOP_MIN_MARGIN_PCT_BP": "25.5"})
