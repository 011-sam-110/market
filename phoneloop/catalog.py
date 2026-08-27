"""The categories we track, and how to enumerate past the 960-result cap.

Vinted answers any catalogue query with at most 960 items — ten pages of 96,
and page 11 is an HTTP 400. Measured, not documented. `total_entries` reports
960 even for a query with far fewer real matches, so it cannot be used to tell
"this shard is exhausted" from "this shard is truncated".

That leaves one honest way to enumerate a category: **shard until the shards
stop hitting the cap**. Ask for a price range; if it comes back saturated, the
range is hiding items, so split it and ask again. A range that returns fewer
than the cap has been seen whole. This is self-calibrating — it needs no guess
about how a category's prices are distributed, and it *demonstrates* coverage
rather than assuming it.

The leaf categories matter for the same reason: the cap is per query, so every
leaf carries its own 960. "Mobile phones" (3661) is one leaf of fifteen under
"Mobile phones & communication" (3565) — and the only one holding handsets.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# Vinted's result cap per query. Ten pages of 96. Page 11 returns HTTP 400.
RESULT_CAP = 960

# A shard is treated as truncated at this fraction of the cap. Not 1.0: pages
# routinely return 94 or 95 rather than 96 (promoted slots, items pulled
# mid-query), so a genuinely saturated shard lands a little under 960 and an
# exact test would call it complete and silently drop the remainder.
SATURATED_AT = 0.94

# Stop splitting here. Below a pound the bands stop separating anything, and a
# category with 960+ items inside one pound is a category we cannot enumerate
# by price at all -- worth knowing about explicitly rather than looping forever.
MIN_BAND_PENCE = 100

TREE_PATH = Path(__file__).resolve().parent.parent / "catalog_tree.json"


@dataclass(frozen=True)
class Category:
    """One tracked category: our slug, its Vinted subtree, its eBay queries."""

    slug: str
    vinted_root: int
    ebay_queries: tuple[str, ...]
    price_from: int = 3          # pounds; below this it is packaging and tat
    price_to: int = 3000
    note: str = ""
    # Leaves worth spending budget on. Empty means "every leaf under the root".
    vinted_leaves: tuple[int, ...] = field(default_factory=tuple)


# The fifteen. Chosen for: identifiable from a title, cheap to post, a real
# second-hand market on both platforms, and low counterfeit risk. Designer
# clothing and sneakers are deliberately absent -- both are dominated by fakes,
# which no title parser can detect and which turn a spread into a loss.
CATEGORIES: tuple[Category, ...] = (
    Category("phones", 3565,
             ("iphone", "samsung galaxy s", "google pixel", "samsung galaxy a",
              "oneplus phone", "iphone unlocked"),
             price_from=15, price_to=2000,
             note="the original target; leaf 3661 holds the handsets"),
    Category("consoles", 3002,
             ("nintendo switch", "playstation 5", "xbox series", "ps4 console",
              "nintendo 3ds", "retro console"),
             price_from=5, price_to=1500),
    Category("computers", 3564,
             ("laptop", "macbook", "gaming pc", "graphics card", "thinkpad"),
             price_from=10, price_to=3000),
    Category("tablets", 3567,
             ("ipad", "samsung tab", "kindle", "android tablet"),
             price_from=10, price_to=2000),
    Category("audio", 3566,
             ("headphones", "airpods", "bluetooth speaker", "hifi amplifier",
              "sony wh-1000"),
             price_from=5, price_to=2000),
    Category("cameras", 3054,
             ("dslr camera", "camera lens", "mirrorless camera", "gopro",
              "film camera"),
             price_from=10, price_to=3000),
    Category("wearables", 3004,
             ("apple watch", "garmin watch", "fitbit", "smartwatch"),
             price_from=10, price_to=1500),
    Category("trading_cards", 4874,
             ("pokemon cards", "yugioh cards", "magic the gathering",
              "football cards"),
             price_from=3, price_to=1000),
    Category("board_games", 4881,
             ("board game", "warhammer", "lego set"),
             price_from=5, price_to=500),
    Category("instruments", 4825,
             ("electric guitar", "keyboard piano", "audio interface",
              "guitar pedal", "dj controller"),
             price_from=10, price_to=3000),
    Category("kitchen", 3474,
             ("kitchenaid", "nespresso machine", "ninja air fryer",
              "coffee machine", "kitchen mixer"),
             price_from=10, price_to=1000),
    Category("tools", 3811,
             ("dewalt", "makita", "power drill", "bosch tools", "milwaukee tool"),
             price_from=10, price_to=2000),
    Category("cycling", 4333,
             ("bicycle", "bike wheels", "cycling helmet", "turbo trainer"),
             price_from=10, price_to=3000),
    Category("toys", 1499,
             ("lego", "funko pop", "action figure", "model kit"),
             price_from=3, price_to=1000),
    Category("tv_cinema", 3568,
             ("smart tv", "projector", "soundbar", "av receiver"),
             price_from=10, price_to=2000),
)

BY_SLUG = {c.slug: c for c in CATEGORIES}


# --- the Vinted tree ------------------------------------------------------

def load_tree(path: Path | None = None) -> list[dict]:
    p = path or TREE_PATH
    if not p.exists():
        raise FileNotFoundError(
            f"{p} missing - run scripts/refresh_catalog_tree.py to rebuild it")
    return json.loads(p.read_text(encoding="utf-8"))


def find_node(tree: list[dict], want: int) -> dict | None:
    for n in tree:
        if n["id"] == want:
            return n
        hit = find_node(n.get("catalogs") or [], want)
        if hit:
            return hit
    return None


def leaves_of(tree: list[dict], root_id: int) -> list[tuple[int, str]]:
    """Every childless node under a root: (id, title).

    Leaves rather than the root because the 960 cap is per query. Asking the
    root once gets 960 items for the whole of "Computers & accessories";
    asking its seventy leaves gets up to 960 each.
    """
    node = find_node(tree, root_id)
    if node is None:
        return []

    out: list[tuple[int, str]] = []

    def walk(n: dict) -> None:
        kids = n.get("catalogs") or []
        if not kids:
            out.append((n["id"], n["title"]))
            return
        for k in kids:
            walk(k)

    walk(node)
    return out


# --- adaptive price sharding ----------------------------------------------

def is_saturated(unique_seen: int) -> bool:
    """Did this shard hit the cap, i.e. is it hiding items we never saw?"""
    return unique_seen >= RESULT_CAP * SATURATED_AT


def split_band(lo_pence: int, hi_pence: int) -> list[tuple[int, int]]:
    """Halve a price band, geometrically.

    Geometric rather than arithmetic because listing prices are heavily
    right-skewed: an arithmetic midpoint of a 15-2000 pound band lands at
    1007, leaving essentially every real listing on one side and splitting
    nothing. The geometric midpoint lands near 173 and actually divides the
    population.
    """
    if hi_pence - lo_pence <= MIN_BAND_PENCE:
        return []
    mid = int((lo_pence * hi_pence) ** 0.5)
    if mid <= lo_pence or mid >= hi_pence:
        mid = (lo_pence + hi_pence) // 2
    if mid <= lo_pence or mid >= hi_pence:
        return []
    return [(lo_pence, mid), (mid, hi_pence)]
