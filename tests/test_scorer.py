"""Scorer tests.

The single most important property in this codebase is that a listing cheap
enough to be fraud is never alertable. Several tests below exist only to pin
that down.
"""

from decimal import Decimal

import pytest

from phoneloop import config
from phoneloop.parser import PhoneSpec, parse
from phoneloop.scorer import Band, score

CFG = config.load(env={})
CLEAN = parse("iPhone 13 128GB")  # a clean, well-parsed spec


def D(x):
    return Decimal(str(x))


# reference is 200 throughout; bands are -0.15 / -0.45 / -0.60
@pytest.mark.parametrize("price,band,alertable", [
    (250, Band.OVERPRICED, False),   # +25%
    (200, Band.FAIR,       False),   # exactly the median
    (190, Band.FAIR,       False),   # -5%, too shallow to act on
    (170, Band.BUY,        True),    # -15%, exactly the edge: inclusive in BUY
    (150, Band.BUY,        True),    # -25%, the target case
    (115, Band.BUY,        True),    # -42.5%
    (108, Band.STRONG_BUY, True),    # -46%, deep but above the fraud floor
    (85,  Band.STRONG_BUY, True),    # -57.5%, still above the floor
    (80,  Band.BAIT,       False),   # -60%, at the floor — treated as bait
    (70,  Band.BAIT,       False),   # -65%
    (20,  Band.BAIT,       False),   # -90%, obviously not a bargain
])
def test_bands(price, band, alertable):
    v = score(CLEAN, D(price), D(200), CFG)
    assert v.band is band, f"{price} -> {v.band}, expected {band}"
    assert v.alertable is alertable


def test_bait_is_never_alertable():
    """The most important assertion here. A rank-by-discount feed would put
    scams at the top every single time; the floor is what stops that."""
    for price in (79, 50, 25, 1):
        v = score(CLEAN, D(price), D(200), CFG)
        assert v.band is Band.BAIT
        assert v.alertable is False


def test_disqualified_spec_never_alertable():
    """A disqualifier vetoes regardless of how good the price looks."""
    broken = parse("iPhone 13 128GB spares or repair")
    assert broken.disqualifiers
    v = score(broken, D(150), D(200), CFG)
    assert v.alertable is False
    assert "spares_repair" in v.reason


def test_none_reference_is_unpriceable_not_a_free_phone():
    """A thin bucket must never be read as a 100% discount."""
    v = score(CLEAN, D(150), None, CFG)
    assert v.band is Band.UNPRICEABLE
    assert v.alertable is False
    assert v.delta is None


def test_non_phone_is_unpriceable():
    v = score(parse("iphone 13 case"), D(5), D(200), CFG)
    assert v.band is Band.UNPRICEABLE
    assert v.alertable is False


def test_zero_reference_does_not_divide_by_zero():
    v = score(CLEAN, D(150), D(0), CFG)
    assert v.band is Band.UNPRICEABLE
    assert v.alertable is False


def test_delta_sign_convention():
    """delta is negative when the listing is BELOW market. A sign flip here
    would invert the entire system, so it is pinned explicitly."""
    assert score(CLEAN, D(150), D(200), CFG).delta == pytest.approx(-0.25)
    assert score(CLEAN, D(250), D(200), CFG).delta == pytest.approx(0.25)


def test_discount_pct_is_human_readable():
    v = score(CLEAN, D(150), D(200), CFG)
    assert v.discount_pct == pytest.approx(25.0)


def test_unparsed_storage_still_scores():
    """55% of live listings omit storage. They must remain scoreable, priced
    against the lowest tier by the pricebook rather than dropped here."""
    spec = parse("iPhone 13 White")
    assert spec.storage_gb is None
    v = score(spec, D(150), D(200), CFG)
    assert v.band is Band.BUY
    assert v.alertable is True


def test_verdict_reason_is_always_populated():
    """Every verdict explains itself — an alert with no rationale is unactionable
    and an unexplained rejection is undebuggable."""
    for price, ref in [(150, 200), (250, 200), (50, 200), (150, None)]:
        v = score(CLEAN, D(price), D(ref) if ref else None, CFG)
        assert v.reason, f"empty reason for price={price} ref={ref}"


def test_band_edges_are_inclusive_in_the_safe_direction():
    """Both edges pinned deliberately, because a boundary that drifts is a
    boundary nobody decided.

    BAND_BUY_HIGH (-15%) is the top of BUY, so exactly -15% IS alertable.
    BAND_BAIT (-60%) is the fraud floor, so exactly -60% is NOT — the tie
    breaks toward silence, which is the direction that cannot cost money.
    """
    assert score(CLEAN, D(170), D(200), CFG).band is Band.BUY        # -15.0%
    assert score(CLEAN, D(171), D(200), CFG).band is Band.FAIR       # -14.5%
    assert score(CLEAN, D(80), D(200), CFG).band is Band.BAIT        # -60.0%
    assert score(CLEAN, D(81), D(200), CFG).band is Band.STRONG_BUY  # -59.5%
