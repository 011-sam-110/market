"""Price against reference -> a banded verdict.

Pure. No I/O, no clock, no config globals.

The governing idea: **cheapness is not monotonic.** Past a point a low price stops
meaning bargain and starts meaning fraud, brick, or stolen. So this returns a
band rather than a rank — and the very cheapest listings are routed away from
alerts rather than to the top of them. A naive rank-by-discount feed would
surface scams first, every single time.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from .config import Config
from .parser import PhoneSpec


class Band(str, Enum):
    """Where a listing sits relative to its bucket's median.

    Ordered here from cheapest to dearest, which is *not* worst-to-best —
    that is the whole point.
    """

    BAIT = "bait"                  # so cheap it is probably not real
    STRONG_BUY = "strong_buy"      # deep discount, above the fraud floor
    BUY = "buy"                    # the target band
    FAIR = "fair"                  # around the median
    OVERPRICED = "overpriced"      # above the median
    UNPRICEABLE = "unpriceable"    # no reference, or not a phone


# The only two bands that ever reach the user's phone.
_ALERTABLE_BANDS = frozenset({Band.BUY, Band.STRONG_BUY})


@dataclass(frozen=True)
class Verdict:
    band: Band
    delta: float | None
    alertable: bool
    reason: str

    @property
    def discount_pct(self) -> float | None:
        """Positive percentage below market, for humans. `delta` is the signed
        internal form; this is what goes in an alert message."""
        return None if self.delta is None else -self.delta * 100.0


def _unpriceable(reason: str) -> Verdict:
    return Verdict(band=Band.UNPRICEABLE, delta=None, alertable=False, reason=reason)


def score(
    spec: PhoneSpec,
    total_price: Decimal,
    reference: Decimal | None,
    cfg: Config,
) -> Verdict:
    """Band a listing against its bucket's reference median.

    Args:
        spec: Parsed identity. Disqualifiers here veto everything downstream.
        total_price: **Landed cost** — Vinted's `total_item_price`, which already
            includes the buyer protection fee. Never the bare asking price.
        reference: Bucket median, or None when the bucket is too thin to trust.
        cfg: Band edges.

    Returns:
        A Verdict that always explains itself. Every failure path here lands on
        `alertable=False`: a missed deal costs nothing, a bad alert costs money.
    """
    if not spec.is_phone:
        return _unpriceable("not a phone we can price")

    if reference is None:
        return _unpriceable(
            f"no reference for {spec.model} — bucket below "
            f"{cfg.MIN_BUCKET_SAMPLES} samples"
        )

    if reference <= 0:
        return _unpriceable(f"invalid reference {reference}")

    # Negative delta means the listing is BELOW market. A sign flip here would
    # invert the entire system, so the convention is pinned by test.
    delta = float((total_price - reference) / reference)

    if delta <= cfg.BAND_BAIT:
        band = Band.BAIT
        reason = (
            f"{-delta:.0%} below median {reference} — past the "
            f"{-cfg.BAND_BAIT:.0%} floor, treated as bait not bargain"
        )
    elif delta <= cfg.BAND_BUY_LOW:
        band = Band.STRONG_BUY
        reason = f"{-delta:.0%} below median {reference}"
    elif delta <= cfg.BAND_BUY_HIGH:
        band = Band.BUY
        reason = f"{-delta:.0%} below median {reference}"
    elif delta <= 0:
        band = Band.FAIR
        reason = f"{-delta:.0%} below median {reference} — too shallow to act on"
    else:
        band = Band.OVERPRICED
        reason = f"{delta:.0%} above median {reference}"

    alertable = band in _ALERTABLE_BANDS

    # A disqualifier vetoes however good the price looks. Checked last so the
    # band and reason still explain what the price *would* have been — useful
    # when reviewing what the filter threw away.
    if spec.disqualifiers:
        alertable = False
        reason = f"disqualified ({', '.join(spec.disqualifiers)}); {reason}"

    return Verdict(band=band, delta=delta, alertable=alertable, reason=reason)
