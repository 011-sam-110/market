"""Every tunable in one place.

The spec's rule: no hardcoded tunables at call sites. Each value below is a
calibration target rather than a finding — week one replaces them with measured
numbers (see docs/superpowers/specs, section 6).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Vinted category for "mobile phones and communication". Filtering by this
# instead of free text is what stops a search for "iphone 13" returning cases.
CATALOG_ID = 3565

# The API caps result sets here regardless of filters, so wide enumeration has
# to shard by model and price band rather than paginating one big query.
RESULT_CAP = 960


@dataclass(frozen=True)
class Config:
    # --- loop ---
    POLL_INTERVAL_S: int
    CATALOG_ID: int
    PRICE_FROM: int

    # --- pricing ---
    PRICE_WINDOW_DAYS: int
    MIN_BUCKET_SAMPLES: int

    # --- scoring bands, as signed fractions of the reference median ---
    BAND_BUY_HIGH: float   # -0.15 : shallower than this is just "fair"
    BAND_BUY_LOW: float    # -0.45 : the deep end of a genuine bargain
    BAND_BAIT: float       # -0.60 : below here, cheap means fraud

    # --- alerting ---
    ALERTS_PER_BUCKET_HOUR: int
    TELEGRAM_TOKEN: str
    TELEGRAM_CHAT_ID: str
    ALERTS_ENABLED: bool

    # --- storage ---
    DATA_DIR: Path
    MAX_STORE_BYTES: int

    def __post_init__(self) -> None:
        # A mis-ordered band silently disables the fraud floor, which is the one
        # guard standing between the alert feed and a stream of scams. Refuse to
        # start rather than run with it inverted.
        if not (self.BAND_BAIT < self.BAND_BUY_LOW < self.BAND_BUY_HIGH < 0):
            raise ValueError(
                f"scoring bands out of order: BAIT={self.BAND_BAIT} "
                f"BUY_LOW={self.BAND_BUY_LOW} BUY_HIGH={self.BAND_BUY_HIGH}; "
                "expected BAIT < BUY_LOW < BUY_HIGH < 0"
            )


def load(env: dict[str, str] | None = None) -> Config:
    """Build a Config from the environment, falling back to the spec's defaults.

    Args:
        env: Mapping to read from. Defaults to os.environ. Injected so tests
            never depend on the ambient environment.
    """
    e = os.environ if env is None else env

    def _int(key: str, default: int) -> int:
        return int(e.get(f"PHONELOOP_{key}", default))

    def _float(key: str, default: float) -> float:
        return float(e.get(f"PHONELOOP_{key}", default))

    return Config(
        POLL_INTERVAL_S=_int("POLL_INTERVAL_S", 60),
        CATALOG_ID=_int("CATALOG_ID", CATALOG_ID),
        # Below this, listings are overwhelmingly cases and accessories rather
        # than handsets. A cheap first-pass filter before the parser runs.
        PRICE_FROM=_int("PRICE_FROM", 80),
        PRICE_WINDOW_DAYS=_int("PRICE_WINDOW_DAYS", 30),
        MIN_BUCKET_SAMPLES=_int("MIN_BUCKET_SAMPLES", 8),
        BAND_BUY_HIGH=_float("BAND_BUY_HIGH", -0.15),
        BAND_BUY_LOW=_float("BAND_BUY_LOW", -0.45),
        BAND_BAIT=_float("BAND_BAIT", -0.60),
        ALERTS_PER_BUCKET_HOUR=_int("ALERTS_PER_BUCKET_HOUR", 3),
        TELEGRAM_TOKEN=e.get("PHONELOOP_TELEGRAM_TOKEN", ""),
        TELEGRAM_CHAT_ID=e.get("PHONELOOP_TELEGRAM_CHAT_ID", ""),
        ALERTS_ENABLED=e.get("PHONELOOP_ALERTS_ENABLED", "1") == "1",
        DATA_DIR=Path(e.get("PHONELOOP_DATA_DIR", "data")),
        # Hard ceiling so a runaway loop can never eat the shared Drive quota
        # that the EV client's backups depend on. 20 GB is far above this
        # milestone's expected footprint of a few GB a year.
        MAX_STORE_BYTES=_int("MAX_STORE_BYTES", 20 * 1024**3),
    )
