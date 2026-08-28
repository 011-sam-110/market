"""Per-source result caps (design spec section 11).

Vinted's cap (960, ten pages of 96) and eBay's cap (10,000, measured: page 45
at 240/page returned zero on a "21,000+ results" query) are different numbers
on different platforms. A cap that silently defaulted to Vinted's for eBay
would stop an eBay sweep at roughly 1/10th of its real cap (960 vs 10,000)
-- missing about 90% of the results -- and nobody would notice, because the
sweep loop would just look "done" -- this is the guard against that.
"""

import pytest

from phoneloop import catalog


def test_result_cap_vinted():
    assert catalog.result_cap("vinted") == 960


def test_result_cap_ebay():
    assert catalog.result_cap("ebay") == 10_000


def test_result_cap_unknown_source_raises():
    with pytest.raises(KeyError):
        catalog.result_cap("nope")


def test_is_saturated_per_source_guard():
    """The same raw count of 910 unique items is saturated for Vinted (the
    threshold is 94% of 960 == 902.4, i.e. 903 and up) but not for eBay
    (94% of 10,000 == 9,400, and 910 is nowhere close). This is the "cap is
    per source" guard from spec section 11: a naive is_saturated that
    ignored its source argument would call 910 saturated either way, or
    unsaturated either way.
    """
    assert catalog.is_saturated(910, "vinted") is True
    assert catalog.is_saturated(910, "ebay") is False


def test_is_saturated_ebay_needs_the_full_cap():
    """eBay's saturation fraction is 1.0 -- unlike Vinted, there is no "pages
    return 94 of 96" slack, an offset search either fills the page or empties
    it. 9,999 of 10,000 is not yet saturated; 10,000 is."""
    assert catalog.is_saturated(9_999, "ebay") is False
    assert catalog.is_saturated(10_000, "ebay") is True


def test_is_saturated_default_source_is_vinted():
    """scripts/bulk_vinted.py:194 calls `catalog.is_saturated(n)` with a
    single positional argument. The default must keep that call site's
    existing behaviour identical to before this change."""
    assert catalog.is_saturated(903) is True   # >= 960 * 0.94 == 902.4
    assert catalog.is_saturated(902) is False


def test_result_caps_dict_matches_result_cap_constant():
    """RESULT_CAP is kept as a bare module constant so existing callers and
    config.RESULT_CAP still agree -- it must not drift from RESULT_CAPS["vinted"]."""
    assert catalog.RESULT_CAP == catalog.RESULT_CAPS["vinted"] == 960
