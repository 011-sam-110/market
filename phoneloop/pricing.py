"""Looking a handset up in the two price tables.

Small on purpose. It was inline in scripts/board.py, where nothing could test
it and the reviewer could not share it -- and the two lookups behave
differently in a way that is easy to get backwards, so it is worth stating once
in a place that has tests.

    CeX      a model-wide figure is the cheapest capacity CeX stocks, so it
             UNDERSTATES the exit. Safe to fall back to.

    eBay     a model-wide median is not a bound in either direction. Measured
             against capacity-specific queries on 2026-08-27: -1%, +3%, +27%,
             +70%. Usable, but only when the caller is told it is approximate.
"""

from __future__ import annotations

import json
from pathlib import Path


def load_table(path: Path) -> dict:
    """A price table, or an empty one. A malformed file is not silently empty:
    it raises, because an empty table reads on the board as "nothing is
    priceable" rather than "the file is broken"."""
    if not Path(path).exists():
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def cex_cash(table: dict, model: str, storage_gb: int | None,
             grade: str = "B") -> tuple[int | None, bool]:
    """Cash CeX will pay. Returns (pence, exact).

    Falls back to the model-wide figure when the capacity is not covered,
    because that errs low and a low exit only ever makes a buy look worse.
    """
    for entry in table.values():
        if entry.get("model") == model and entry.get("storage_gb") == storage_gb:
            return (entry.get("cash_pence") or {}).get(grade), storage_gb is not None
    for entry in table.values():
        if entry.get("model") == model and entry.get("storage_gb") is None:
            return (entry.get("cash_pence") or {}).get(grade), False
    return None, False


def ebay_ceiling(table: dict, model: str,
                 storage_gb: int | None) -> tuple[int | None, bool]:
    """What comparable used UK stock is asking. Returns (pence, exact).

    `exact` is False whenever the figure mixes capacities -- including when
    both sides are None, which is two unknowns agreeing rather than a match,
    and is the widest uncertainty on the board precisely because it looks like
    the cleanest row.
    """
    for entry in table.values():
        if entry.get("model") == model and entry.get("storage_gb") == storage_gb:
            return entry.get("ceiling_pence"), storage_gb is not None
    for entry in table.values():
        if entry.get("model") == model and entry.get("storage_gb") is None:
            return entry.get("ceiling_pence"), False
    return None, False


def coverage(table: dict) -> set[str]:
    """Which model slugs a table can price at all."""
    return {e["model"] for e in table.values() if e.get("model")}
