"""The six control knobs plus the fee terms (contracts section 2.8).

Every default here is copied straight from that section of the contract, and
every environment override is exercised at least once so a rename of a field
without a matching env-name update fails loudly.
"""

from __future__ import annotations

import pytest

from phoneloop.fees import FeeKnobs
from phoneloop.knobs import Knobs

# Wired at integration (contracts section 6.2, item 2): this used to be a
# literal duplicate of phoneloop.fees.FeeKnobs's attribute list, because
# knobs.py must not import fees.py (s1 and s4 build in parallel) and so
# could not reference the real Protocol from its own tests. Now that both
# streams are merged, the attribute list is read directly off
# FeeKnobs.__annotations__ instead of being copied by hand -- if the
# Protocol ever gains, loses, or renames an attribute, this test starts
# checking the new list automatically rather than silently checking a
# stale one.
_FEE_KNOBS_ATTRS = tuple(FeeKnobs.__annotations__.keys())


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
        # The convention lives on the module docstring, which is where the
        # claim actually has to hold up -- and it must name str_threshold_bp
        # specifically, not just use the word "chosen" somewhere else in the
        # module (e.g. describing import_vat_term_bp), or this test would
        # keep passing after the str_threshold_bp caveat was deleted.
        import phoneloop.knobs as knobs_module
        doc = knobs_module.__doc__ or ""
        assert "str_threshold_bp" in doc
        idx = doc.index("str_threshold_bp")
        nearby = doc[idx:idx + 200]
        assert "not a measurement" in nearby or "CHOSEN" in nearby or "chosen" in nearby

    def test_knobs_carries_every_feeknobs_attribute_name(self):
        # Guards the guard: if FeeKnobs.__annotations__ ever came back empty
        # (e.g. a bad refactor dropping the Protocol's body), the loop below
        # would pass vacuously. Five is the count contracts section 2.2 pins.
        assert len(_FEE_KNOBS_ATTRS) == 5
        k = Knobs()
        for name in _FEE_KNOBS_ATTRS:
            assert hasattr(k, name), f"Knobs is missing {name}, which fees.FeeKnobs requires"
        # isinstance half of "isinstance/attribute cross-check" (contracts
        # section 6.2, item 2): FeeKnobs is a structural Protocol, so runtime
        # isinstance needs @runtime_checkable, which the Protocol does not
        # declare (adding it would be an s1 contract change made from here).
        # The attribute walk above is the check that Protocol supports
        # without that decorator, and is what the contract's own wording
        # ("isinstance/attribute cross-check", either kind) calls for.


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
