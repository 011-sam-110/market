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

import os
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace

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

    # 2 -- sell-through threshold, basis points of listings sold in the window.
    # CHOSEN, not measured -- see the module docstring.
    str_threshold_bp: int = 5000           # 50%

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
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Knobs":
        """Defaults overridden by PHONELOOP_<FIELD_NAME_UPPER>. Every value is
        an int; a bad one raises rather than falling back to the default,
        because a knob that silently reverts to its default on a typo is a
        knob nobody can trust they actually set.
        """
        e = os.environ if env is None else env

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

        base = cls()
        return replace(base, **overrides) if overrides else base
