"""A paper-money forecaster, scored against the market price.

The package answers one question: does a cheap model with some evidence
forecast two-outcome Polymarket markets better than the market price does?
It answers with a Brier score over resolved markets, and it prints the count
under every figure.

What it does NOT do, by construction:

- It places no order. It has no wallet, no Polymarket API key and no signing
  code. Every stake is a paper number in the local ledger.
- It reads only the public Gamma API (no key). Polymarket is close-only in
  GB, so nothing here may route around that.
- It makes at most HARD_DAILY_CALLS model calls in one UTC day. The ledger
  counts each call before it is sent, so a crash cannot reset the count.

The modules, in run order:

    gamma.py     read and filter open markets (public API, no key)
    ledger.py    the SQLite store, outside the repo, and the call counter
    evidence.py  Reddit hits (Prospector CLI) and NewsScraper headlines
    model.py     one DeepSeek call, and the prompt
    agent.py     one forecast per market, with a quarter-Kelly paper stake
    score.py     resolution, Brier against the market, paper P&L, WITHHELD
"""

from __future__ import annotations

# The hard ceiling on model calls per UTC day. A flag or an env var can set a
# LOWER number. Nothing can set a higher one: ledger.reserve_call() clamps to
# this constant.
HARD_DAILY_CALLS = 20

# The scorer prints its comparison only at or above this many resolved
# forecasts. Below it, every comparison figure is stamped WITHHELD with its n.
WITHHOLD_BELOW = 100

# Paper money only. The unit is a paper dollar. It is never converted.
PAPER_CAPITAL = 1000.0
KELLY_MULTIPLIER = 0.25  # quarter-Kelly
STAKE_CAP = 0.06  # no stake above 6% of free paper capital

__all__ = [
    "HARD_DAILY_CALLS",
    "KELLY_MULTIPLIER",
    "PAPER_CAPITAL",
    "STAKE_CAP",
    "WITHHOLD_BELOW",
]
