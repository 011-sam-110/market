"""What eBay charges a business seller on a sale, and the highest landed
supplier cost that still clears the margin knob (design spec section 7).

The margin knob is inverted here. eBay can tell us everything about demand
and competition; it cannot tell us what a supplier will charge, so knob 1
("min net profit margin") is answered as `max_cost` per product instead of
as a filter -- the supplier stage decides whether a real quote clears it.

**Money is integer pence throughout.** `bp()` is the only arithmetic this
module does on money, and it rounds half up: `(x * n + 5000) // 10000`.

**Rounding is sequential, not combined, and that is a deliberate choice, not
a shortcut.** The final value fee and the 0.35% regulatory fee are summed as
basis points and rounded to pence once; VAT is then applied to that already-
rounded pence figure as a second, separate rounding step. A single combined
percentage (e.g. "12.3%" applied directly to the price in one division) can
differ from this by a penny -- at GBP 30.00 sequential rounding gives 370p
(307.5 -> 308, then x1.2 -> 369.6 -> 370) while the combined 12.3% gives
369p. Both are defensible answers to "what does 12.3% of GBP 30 cost", but
only one of them is reproducible by hand from the two published rates (FVF
and VAT-on-fees) without knowing eBay's internal combination order. Sequential
is the rule this project uses -- 370 is correct here, and a "fix" back to 369
is regression, not a correction.

**The fee table is partial.** eBay help id=4809 (updated 4 Aug 2026) lists
28 category rows; two live fetch attempts against it here both timed out
(WebFetch, 60s each, 2026-08-27), so `FVF_BP_BY_TOP_LEVEL_NAME` below ships
only the five rate groups spec section 7 quotes directly, plus
`EVERYTHING_ELSE_BP` as the fallback for every category not named. An
unrecognised category is charged the *worst* rate on purpose: overstating
eBay's cut understates `max_cost`, which is the safe direction (a supplier
quote that clears an understated max_cost also clears the real one).

**`TOP_LEVEL_ID_TO_NAME` is filled at integration, from `catalog_ebay.IN_SCOPE`.**
s1 shipped it empty on purpose (see above) because no live category id was
sourced by this stream in isolation. At integration, `phoneloop.catalog_ebay`
(s6) is merged and every id in its `IN_SCOPE` tuple was read off a real eBay
category page (`scripts/refresh_ebay_categories.py`, run live 2026-08-27) --
so this module imports that tuple and keys it by `category_id`. A department
whose `category_id` is still `""` (none, currently) is skipped rather than
entered, so it keeps falling to `EVERYTHING_ELSE_BP`. This import is one-way
(`fees` -> `catalog_ebay`); `catalog_ebay` never imports `fees`, so there is
no cycle. Note this still only fills the *id -> title* half of the lookup --
`fvf_bp`'s caller must also pass the *leaf -> id* half via its `top_level_of`
argument (`catalog_ebay.top_level_of()`); `board.py` does this at integration
too, described in its own module docstring.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

# Integration-only import: catalog_ebay (s6) did not exist in this stream's
# worktree, so s1 could not fill TOP_LEVEL_ID_TO_NAME itself (see module
# docstring). One-directional -- catalog_ebay.py has no import of fees.py.
from phoneloop.catalog_ebay import IN_SCOPE as _IN_SCOPE_DEPARTMENTS

# eBay's regulatory operating fee: 0.35%, quoted ex-VAT, charged on the same
# base (price + postage) as the final value fee. Folded into the "variable"
# fee step alongside FVF so VAT is applied to their sum once, not twice.
REGULATORY_BP: int = 35

PER_ORDER_PENCE_LOW: int = 30     # order total <= GBP 10
PER_ORDER_PENCE_HIGH: int = 40    # order total  > GBP 10
PER_ORDER_THRESHOLD_PENCE: int = 1000

# The fallback final value fee: the worst (highest) rate in the table, not
# the cheapest. An unknown category should never look cheaper to sell in
# than a known one -- that would make max_cost overstate what a supplier can
# be paid, which is the direction that loses money.
EVERYTHING_ELSE_BP: int = 1290

# Business-seller final value fee in basis points, by eBay UK top-level
# department title (design spec section 7, sourced from eBay help id=4809 as
# quoted there, updated 4 Aug 2026 -- not independently re-fetched here, see
# the module docstring). Departments the spec groups under one rate get one
# entry each so a lookup by title needs no further parsing.
#
# Two entries are duplicated under eBay's real department title as well as
# spec section 7's shorthand: section 7's own table says "Toys" and "Garden",
# but section 9 (listing the actual departments stage one sweeps) names them
# "Toys & Games" and "Garden & Patio" -- those are the strings
# catalog_ebay.top_level_of() will actually read off a live category page at
# integration. Without the alias, either leaf would silently fall through to
# EVERYTHING_ELSE_BP forever, one basis-point table permanently unreachable
# for two of the nine stage-one departments. The other seven titles are not
# independently confirmed against a live page by this stream (see the module
# docstring); if one of those turns out to be shortened the same way, it
# fails the same safe way (EVERYTHING_ELSE_BP), just not silently anymore
# once this pattern is known.
FVF_BP_BY_TOP_LEVEL_NAME: dict[str, int] = {
    "Mobile Phones & Communication": 990,
    "Vehicle Parts & Accessories": 950,
    "Health & Beauty": 1090,
    "Toys": 1090,
    "Toys & Games": 1090,
    "Sporting Goods": 1090,
    "Garden": 1090,
    "Garden & Patio": 1090,
    "Home, Furniture & DIY": 1190,
    "Pet Supplies": 1290,
    "Crafts": 1290,
}

# top-level category id -> department title, for the fee-rate lookup above.
# Filled at integration (contracts section 6.2, item 1) from every IN_SCOPE
# department whose category_id was actually read off a live page -- see the
# module docstring. A department that ships with category_id == "" (none,
# currently) is left out on purpose, same reasoning as an unsourced fee-table
# row: falling to EVERYTHING_ELSE_BP is the safe direction, inventing an id
# is not.
TOP_LEVEL_ID_TO_NAME: dict[str, str] = {
    dept.category_id: dept.title
    for dept in _IN_SCOPE_DEPARTMENTS
    if dept.category_id
}


class FeeKnobs(Protocol):
    """The knob attributes this module reads. `phoneloop.knobs.Knobs`
    satisfies this structurally -- fees.py must not import phoneloop.knobs
    (s1 and s4 build in parallel; this Protocol is how the two streams meet
    without a build-order dependency between them)."""

    vat_on_fees_bp: int
    ad_rate_bp: int
    import_vat_term_bp: int
    min_margin_pct_bp: int
    min_margin_fixed_pence: int


def bp(amount_pence: int, basis_points: int) -> int:
    """amount x bp/10000, rounded half up, in integer pence.

    The only arithmetic money is allowed in this module. `(x*n + 5000) //
    10000` is the standard round-half-up-via-floor-division trick: adding
    half the divisor before flooring turns "truncate" into "round to
    nearest, ties up". Defined for non-negative `amount_pence`; every caller
    in this module only ever passes a price or a fee, never a difference.
    """
    return (amount_pence * basis_points + 5000) // 10000


def fvf_bp(leaf_category_id: str | None,
           top_level_of: Mapping[str, str] | None = None) -> int:
    """Final value fee for a leaf, via its top-level ancestor.

    Every step that cannot be resolved -- no leaf, no top_level_of mapping,
    the leaf missing from it, the resulting top-level id missing from
    TOP_LEVEL_ID_TO_NAME, or that name missing from the rate table -- falls
    to EVERYTHING_ELSE_BP rather than raising. A niche board has to price
    thousands of leaves; a category the tables don't yet know about must
    degrade to the safe (highest) rate, not stop the run.
    """
    if not leaf_category_id or not top_level_of:
        return EVERYTHING_ELSE_BP
    top_level_id = top_level_of.get(leaf_category_id)
    if top_level_id is None:
        return EVERYTHING_ELSE_BP
    name = TOP_LEVEL_ID_TO_NAME.get(top_level_id)
    if name is None:
        return EVERYTHING_ELSE_BP
    return FVF_BP_BY_TOP_LEVEL_NAME.get(name, EVERYTHING_ELSE_BP)


def per_order_pence(price_pence: int) -> int:
    """30p at or below GBP 10, 40p above. Quoted ex-VAT, like every other
    eBay fee -- the caller applies vat_on_fees_bp to this, same as the FVF."""
    if price_pence <= PER_ORDER_THRESHOLD_PENCE:
        return PER_ORDER_PENCE_LOW
    return PER_ORDER_PENCE_HIGH


def fees(price_pence: int, leaf_category_id: str | None, knobs: FeeKnobs, *,
         top_level_of: Mapping[str, str] | None = None) -> int:
    """Total eBay fees on a sale at this landed price, integer pence.

    variable = bp(bp(price, fvf_bp + REGULATORY_BP), knobs.vat_on_fees_bp)
    order    = bp(per_order_pence(price),            knobs.vat_on_fees_bp)
    fees     = variable + order + price*ad_rate_bp + price*import_vat_term_bp

    See the module docstring for why the FVF+regulatory rounding happens
    before, not combined with, the VAT step.
    """
    fvf = fvf_bp(leaf_category_id, top_level_of)
    variable = bp(bp(price_pence, fvf + REGULATORY_BP), knobs.vat_on_fees_bp)
    order = bp(per_order_pence(price_pence), knobs.vat_on_fees_bp)
    ads = bp(price_pence, knobs.ad_rate_bp)
    import_vat = bp(price_pence, knobs.import_vat_term_bp)
    return variable + order + ads + import_vat


def max_cost(price_pence: int, leaf_category_id: str | None, knobs: FeeKnobs, *,
             top_level_of: Mapping[str, str] | None = None) -> int:
    """Highest landed supplier cost that still clears the margin knob.

    target = max(bp(price, min_margin_pct_bp), min_margin_fixed_pence)
    max_cost = price - fees(price, ...) - target

    Deliberately **not** clamped at zero. A negative result is the honest
    answer -- "no supplier price clears the margin at this sale price" --
    and clamping it to 0 would read as "free stock works", which is exactly
    the direction that loses money if acted on.
    """
    total_fees = fees(price_pence, leaf_category_id, knobs, top_level_of=top_level_of)
    target = max(bp(price_pence, knobs.min_margin_pct_bp), knobs.min_margin_fixed_pence)
    return price_pence - total_fees - target
