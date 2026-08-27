"""Two exits, one buy price, and the arithmetic between them.

The project now holds three prices for the same handset, from three different
kinds of source, and they are not interchangeable:

    Vinted ask      what a seller wants          measured, live
    CeX cash        what CeX will actually pay    measured, COMMITTED
    eBay median     what rivals are asking        measured, but an ASK

The distinction that decides everything here is committed versus asking. CeX
publishes a price it is contractually willing to hand over today. eBay's median
is what other sellers hope for, and hope is not an exit. Treating an asking
price as a sale price is the single most common way a paper margin evaporates,
so the eBay side is discounted twice below -- once to undercut the competition
enough to actually sell, once for fees -- and both discounts are named
parameters rather than buried constants.

What is measured and what is assumed
------------------------------------
Measured: every price above, plus the overseas and unpriceable exclusions.

Assumed, and requiring confirmation before money moves:

  FEE MODELS. eBay UK's fee position for private sellers changed and the two
  regimes differ enormously. `BUSINESS` is the conservative default. `PRIVATE`
  is what a fee-free private account looks like -- do not use it until the
  account's own fee schedule has been read, because being wrong here is the
  difference between a margin and a loss.

  UNDERCUT. To sell against the median you must sit under it. 5% is a guess.
  It becomes a measurement the day sold data exists, which today it does not.

  SHIP_PENCE. Sending a phone tracked and insured. Confirm against the actual
  service used; underinsuring a £200 handset is a false economy.

Nothing in this module fetches. Pure arithmetic, integer pence throughout.
"""

from __future__ import annotations

from dataclasses import dataclass

# Postage for one insured, tracked handset. An assumption -- see module docs.
SHIP_PENCE = 850

# How far under the median you must sit to actually sell. An assumption.
UNDERCUT = 0.05


@dataclass(frozen=True)
class FeeModel:
    """A marketplace's cut. `rate` is a fraction, `fixed` is pence."""

    name: str
    rate: float
    fixed: int = 0

    def on(self, gross_pence: int) -> int:
        return round(gross_pence * self.rate) + self.fixed


# Conservative default: a business seller's final value fee plus the per-order
# charge. Use this unless the account's real schedule has been checked.
BUSINESS = FeeModel("ebay-business", 0.128, 30)

# A fee-free private account. Verify before trusting -- see module docs.
PRIVATE = FeeModel("ebay-private", 0.0, 0)

# CeX takes no cut; you are selling TO them, not through them.
NO_FEE = FeeModel("none", 0.0, 0)


@dataclass(frozen=True)
class Exit:
    """One way of turning a handset back into money."""

    venue: str
    gross_pence: int
    fees_pence: int
    ship_pence: int
    committed: bool          # is this price contractual, or someone's hope?

    @property
    def net_pence(self) -> int:
        return self.gross_pence - self.fees_pence - self.ship_pence


def cex_exit(cash_pence: int, ship_pence: int = 0) -> Exit:
    """Sell to CeX. Same day, no fee, no buyer to satisfy.

    ship_pence defaults to zero because the usual route is walking into a
    branch. Set it if posting.
    """
    return Exit("cex", cash_pence, 0, ship_pence, committed=True)


def ebay_exit(ceiling_pence: int, fees: FeeModel = BUSINESS,
              undercut: float = UNDERCUT, ship_pence: int = SHIP_PENCE) -> Exit:
    """List on eBay just under the going rate.

    Not committed: this is what you would ask, not what you would get.
    """
    gross = round(ceiling_pence * (1 - undercut))
    return Exit("ebay", gross, fees.on(gross), ship_pence, committed=False)


@dataclass(frozen=True)
class Spread:
    buy_pence: int
    exits: tuple[Exit, ...]

    @property
    def best(self) -> Exit:
        return max(self.exits, key=lambda e: e.net_pence)

    @property
    def floor(self) -> Exit | None:
        """The best exit you are actually guaranteed.

        This is the number that decides whether a buy is safe. If the committed
        floor already clears the buy price, the trade cannot lose -- the eBay
        upside is then a bonus rather than the whole thesis.
        """
        committed = [e for e in self.exits if e.committed]
        return max(committed, key=lambda e: e.net_pence) if committed else None

    @property
    def margin_pence(self) -> int:
        return self.best.net_pence - self.buy_pence

    @property
    def floor_margin_pence(self) -> int | None:
        """Margin if the speculative exit never happens. Can be negative."""
        return None if self.floor is None else self.floor.net_pence - self.buy_pence

    @property
    def downside_covered(self) -> bool:
        """True when even the guaranteed exit returns the purchase price."""
        fm = self.floor_margin_pence
        return fm is not None and fm >= 0

    @property
    def roi(self) -> float:
        return self.margin_pence / self.buy_pence if self.buy_pence else 0.0


def evaluate(buy_pence: int, cex_cash_pence: int | None = None,
             ebay_ceiling_pence: int | None = None,
             fees: FeeModel = BUSINESS,
             undercut: float = UNDERCUT,
             ship_pence: int = SHIP_PENCE) -> Spread | None:
    """Price one purchase against every exit that has a number.

    Returns None when no exit is priceable. That is the honest answer for a
    handset with no CeX box and too few eBay comps: not a zero, not a guess,
    no result at all.
    """
    exits: list[Exit] = []
    if cex_cash_pence is not None:
        exits.append(cex_exit(cex_cash_pence))
    if ebay_ceiling_pence is not None:
        exits.append(ebay_exit(ebay_ceiling_pence, fees, undercut, ship_pence))
    return Spread(buy_pence, tuple(exits)) if exits else None
