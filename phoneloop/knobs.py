"""The six control knobs from the source design, plus the fee terms, as one
frozen bundle (spec section 1, contracts section 2.8).

Every value here is an integer -- basis points for a rate, pence for money,
days or a plain count otherwise -- because the rest of the pipeline holds to
"money is integer pence, percentages are integer basis points" and a knob is
exactly the kind of value that leaks a float into a comparison if it is typed
loosely. `from_env` keeps that guarantee at the boundary: a non-integer
override raises rather than being silently coerced or ignored.

Two of the twelve fields are not measurements. `str_threshold_bp` is a chosen
convention -- the spec names sell-through as knob 2 and gives no number, so
50% is picked to have a value at all, not because 50% was ever observed as
the market's real cut-off. `import_vat_term_bp` defaults to 0 because
whether eBay's buyer-side import VAT is additive or already inclusive on the
listed price is spec section 13.6, explicitly unverified; the knob exists so
answering that question later is a config change, not a rewrite.

`Knobs` is deliberately shaped to satisfy `phoneloop.fees.FeeKnobs`
structurally (same five attribute names, same types) without importing
`fees` -- s1 and s4 build in parallel worktrees and neither can import the
other's new module. The cross-check that actually proves the two agree is
wired at integration (contracts section 6.2); this module only promises the
attribute names and keeps them in `tests/test_knobs.py` as a literal tuple.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from pathlib import Path

# Prefix every knob's environment override shares. The suffix is the field
# name, uppercased -- see contracts section 2.8 for the full list of twelve.
_ENV_PREFIX = "PHONELOOP_"


@dataclass(frozen=True)
class Knobs:
    """The six control knobs plus the fee terms, all integers, all overridable
    from the environment.

    Field order mirrors the spec's numbering (section 1) and contracts
    section 2.8: knobs 1, 2, 3, 4, 6 first (5 -- competitor quality -- sits
    with 6 because both describe rivals rather than a supplier filter), then
    the fee terms knob 1 is priced through.
    """

    # 1 -- min net profit margin (inverted into fees.max_cost; a margin knob
    # needs a supplier cost eBay cannot give, so this prices instead of filters).
    min_margin_pct_bp: int = 2500          # 25%
    min_margin_fixed_pence: int = 600      # £6

    # 2 -- sell-through threshold, compared against metrics.str_window_bp:
    # Sigma(delta_sold) / known_count x 10000, in basis points. This is
    # units sold per listing in the window, not a share of listings sold --
    # it is UNBOUNDED (a listing that resells more than once during the
    # window pushes the ratio past 10000bp/100%). CHOSEN, not measured --
    # see the module docstring.
    str_threshold_bp: int = 5000           # 50%, i.e. half a unit sold per listing on average

    # 3 -- retail price range, landed (item + postage).
    price_from_pence: int = 1500           # £15
    price_to_pence: int = 5500             # £55

    # 4 -- max delivery + handling, days. Own supplier window is unknowable
    # until a supplier exists (spec table, knob 4); this is the competitor
    # ceiling in the meantime.
    max_eta_days: int = 7

    # 6 -- market saturation cap, distinct sellers per product at the best
    # identity tier available (phoneloop.identity.tier_for).
    max_competitors: int = 15

    # 5 -- competitor quality floors. Reframed from "min supplier quality":
    # eBay shows rivals, not suppliers, so this describes the field rather
    # than gating a purchase decision.
    min_seller_feedback_bp: int = 9500     # 95.0% positive
    min_seller_feedback_n: int = 50

    # Fee terms (spec section 7) -- what fees.fees()/fees.max_cost() price a
    # sale with. Kept here rather than in fees.py so a VAT-registration or
    # ad-spend change is one env var, not a code change.
    vat_on_fees_bp: int = 12000            # 1.2x, non-VAT-registered. 10000 if
                                            # registered and reclaiming.
    ad_rate_bp: int = 0                    # Promoted Listings; set when used.
    import_vat_term_bp: int = 0            # UNVERIFIED -- see module docstring.

    @classmethod
    def _env_overrides(cls, e: Mapping[str, str]) -> dict[str, int]:
        overrides: dict[str, int] = {}
        for f in fields(cls):
            key = f"{_ENV_PREFIX}{f.name.upper()}"
            if key not in e:
                continue
            raw = e[key]
            try:
                overrides[f.name] = int(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{key}={raw!r} is not an integer -- every knob is pence, "
                    "basis points or a plain count, so a non-integer override "
                    "is refused rather than silently ignored"
                ) from exc
        return overrides

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Knobs":
        """Defaults overridden by PHONELOOP_<FIELD_NAME_UPPER>. Every value is
        an int; a bad one raises rather than falling back to the default,
        because a knob that silently reverts to its default on a typo is a
        knob nobody can trust they actually set.
        """
        e = os.environ if env is None else env
        overrides = cls._env_overrides(e)
        base = cls()
        return replace(base, **overrides) if overrides else base

    @classmethod
    def default_path(cls, env: Mapping[str, str] | None = None) -> Path:
        """Where a saved preset lives: `~/.phoneloop/knobs.json`.

        Beside the data directory, never inside it. The store is a pile of
        parquet partitions that gets rebuilt, replayed and thrown away; a knob
        preset must outlive that, and putting it in `prod/` would make
        "rebuild the store" quietly mean "lose your settings".
        """
        e = os.environ if env is None else env
        override = e.get(f"{_ENV_PREFIX}KNOBS_FILE")
        if override:
            return Path(override)
        return Path.home() / ".phoneloop" / "knobs.json"

    @classmethod
    def parse_preset(cls, payload: object) -> dict[str, int]:
        """Validate a preset envelope and return its overrides.

        Shared by the loader and the fetcher on purpose: the fetcher validates
        a downloaded payload with exactly the code that will later read it, so
        there is no window where a payload passes the network check and then
        fails the load.

        The envelope is `{"schema_version": 1, "knobs": {field: int}}`. A bare
        map is refused rather than guessed at -- a preset is a thing the
        nightly run obeys unattended, so an ambiguous shape is not worth the
        convenience.
        """
        if not isinstance(payload, dict):
            raise ValueError(
                f"a preset must be a JSON object, got {type(payload).__name__}")
        knobs = payload.get("knobs")
        if not isinstance(knobs, dict):
            raise ValueError(
                'a preset must be {"schema_version": 1, "knobs": {...}} -- a '
                "bare map of fields is refused rather than guessed at")

        valid = {f.name for f in fields(cls)}
        overrides: dict[str, int] = {}
        for name, raw in knobs.items():
            if name not in valid:
                raise ValueError(
                    f"{name!r} is not a knob. Refused rather than ignored: a "
                    "preset that silently drops the one field you came to set "
                    "is worse than one that fails loudly. Known knobs: "
                    f"{sorted(valid)}")
            if isinstance(raw, bool) or not isinstance(raw, int):
                # bool is an int subclass in Python, and `True` reaching a
                # pence field as 1 is exactly the silent coercion this
                # codebase refuses everywhere else.
                raise ValueError(
                    f"knobs.{name}={raw!r} is not an integer -- every knob is "
                    "pence, basis points or a plain count")
            overrides[name] = raw
        return overrides

    @classmethod
    def load(cls, *, path: Path | str | None = None,
             env: Mapping[str, str] | None = None) -> "Knobs":
        """Defaults, then a saved preset file, then the environment.

        **The environment always wins.** A preset arrives from the dashboard
        over the network and can be hours stale; a `PHONELOOP_MAX_ETA_DAYS` on
        the command line is somebody standing at the keyboard right now. If the
        remote value could override that, the only way to be sure what a run
        used would be to go and read a file on another machine.

        A missing file is not an error -- it is the normal state before anyone
        has saved a preset, and it degrades exactly to `from_env`. A file that
        EXISTS and is malformed does raise, for the same reason a bad env var
        does: the run would otherwise proceed under settings nobody chose.
        """
        e = os.environ if env is None else env
        p = Path(path) if path is not None else cls.default_path(e)

        overrides: dict[str, int] = {}
        if p.is_file():
            try:
                payload = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ValueError(
                    f"{p} exists but could not be read as JSON: {exc}. Refused "
                    "rather than ignored -- a knob file that silently falls "
                    "back to defaults is a knob file nobody can trust."
                ) from exc
            overrides.update(cls.parse_preset(payload))

        overrides.update(cls._env_overrides(e))
        base = cls()
        return replace(base, **overrides) if overrides else base
