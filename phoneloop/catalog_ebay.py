"""eBay UK category scope: which departments this stage sweeps, and the leaves
under them, from a recorded category tree.

**In scope vs excluded is a business decision (spec section 9), not a technical
one.** The nine in-scope departments are where dropship goods live and returns
are cheap: nothing sized, nothing branded enough to attract counterfeits,
nothing fragile, nothing regulated, nothing safety-critical. The five excluded
groups each carry the reason they were cut, because "excluded" without a reason
rots into "forgotten" the first time someone re-derives this list.

**Department ids are read off a live page, never guessed.** eBay UK's
"Advanced search" category picker (`/n/all-categories`) embeds a `<select>`
whose `<option value="...">` is the department's real, classic numeric
category id -- the same id space `_sacat=` filters on. Recorded live on
2026-08-27 (`scripts/refresh_ebay_categories.py`); see that script's docstring
for the exact ids measured. Guessing would have been wrong here: "Vehicle
Parts & Accessories" is 131090, not the small id a quick guess would reach for.

**Leaves are a different id space, and a shallower crawl than the word "leaf"
usually promises.** A department's own browse page
(`https://www.ebay.co.uk/b/x/<category_id>`) lists its immediate
subcategories as links to `/b/bn_<n>` -- eBay's newer "browse node" scheme,
used for navigation, NOT the classic numeric id. Getting a classic numeric id
back requires one more fetch per subcategory (that subcategory's own page
embeds its `categoryId` inline). A full recursive crawl to eBay's true leaf
categories would run to hundreds of live fetches across nine departments and
was not attempted; `scripts/refresh_ebay_categories.py` resolves a capped
number per department instead, live, and reports the real count. So
`leaves_for()` returns "every childless category IN THE TREE WE RECORDED",
which for most departments is a second-level browse category, not
necessarily an eBay taxonomy leaf. Good enough to shard a sweep by; not a
claim of exhaustive coverage.

**The Taxonomy API is unverified.** `get_default_category_tree_id` and
`get_category_subtree` are shaped from eBay's Commerce Taxonomy API docs and
have never been called against a live keyset -- none exists yet (spec
section 3). They exist so that when a keyset arrives, filling in the real
tree is a config change, not new code; calling either without a client raises
rather than fabricating a response.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Department:
    slug: str
    title: str
    category_id: str          # eBay's top-level id, as a string; "" until read off a real page
    note: str = ""


# The nine in-scope departments (spec section 9), with the classic numeric
# category id read off https://www.ebay.co.uk/n/all-categories's "Advanced
# search" category `<select>` on 2026-08-27 -- see
# scripts/refresh_ebay_categories.py's docstring for the raw capture. A
# qualifier in `note` ("accessories only", "non-branded only"...) narrows the
# department in the design's intent; `leaves_for()` does not filter by it --
# it returns every recorded leaf under the whole department, because the HTML
# tree carries no reliable "is this an accessory vs a part" signal to filter
# on, and a wrong filter would silently drop real candidates.
IN_SCOPE: tuple[Department, ...] = (
    Department("home_furniture_diy", "Home, Furniture & DIY", "11700"),
    Department("garden_patio", "Garden & Patio", "159912"),
    Department("pet_supplies", "Pet Supplies", "1281"),
    Department("sporting_goods", "Sporting Goods", "888"),
    Department("vehicle_accessories", "Vehicle Parts & Accessories", "131090",
                note="accessories only, not parts"),
    Department("mobile_phone_accessories", "Mobile Phones & Communication", "15032",
                note="accessories only, not handsets"),
    Department("crafts", "Crafts", "14339"),
    Department("health_beauty", "Health & Beauty", "26395",
                note="non-consumable devices and tools only"),
    Department("toys_games", "Toys & Games", "220",
                note="non-branded only"),
)

# Excluded on purpose (spec section 9). "Clothes, Shoes & Accessories" is a
# real eBay department and carries the same kind of id as IN_SCOPE, read off
# the same page. The other four are cross-cutting concerns rather than single
# eBay departments -- "branded electronics" cuts across several departments,
# "anything with safety compliance" is a product property, not a category --
# so their `category_id` stays "" and they exist here purely as documented
# reasons a department or a listing gets rejected downstream, not as
# something `leaves_for()` can enumerate.
EXCLUDED: tuple[Department, ...] = (
    Department("clothes_shoes", "Clothes, Shoes & Accessories", "11450",
                note="sizing returns"),
    Department("branded_electronics", "Branded electronics", "",
                note="counterfeit exposure"),
    Department("fragile_goods", "Fragile goods", "",
                note="breakage and return-shipping risk"),
    Department("consumables_cosmetics", "Consumables and cosmetics", "",
                note="regulation"),
    Department("safety_compliance", "Safety-compliance items (car seats, mains electrical)", "",
                note="safety compliance exposure"),
)


@dataclass(frozen=True)
class Leaf:
    category_id: str
    title: str
    path: tuple[str, ...]     # department -> ... -> leaf, titles


# ebay_categories.json at the repo root, beside catalog_tree.json (the Vinted
# tree). Generated by scripts/refresh_ebay_categories.py, committed because it
# is small (a capped number of leaves per department, not eBay's full
# taxonomy) and because every downstream stream needs it without re-recording.
TREE_PATH: Path = Path(__file__).resolve().parent.parent / "ebay_categories.json"


def load_tree(path: Path | None = None) -> dict:
    """The recorded eBay UK category tree.

    Raises rather than returning {}, same reasoning as catalog.load_tree: an
    empty tree and a missing file both fail the caller, but only one of them
    fails loudly enough to get fixed.
    """
    p = path or TREE_PATH
    if not p.exists():
        raise FileNotFoundError(
            f"{p} missing - run scripts/refresh_ebay_categories.py to rebuild it")
    return json.loads(p.read_text(encoding="utf-8"))


def _department_node(tree: dict, department: str) -> dict:
    node = tree.get("departments", {}).get(department)
    if node is None:
        raise KeyError(department)
    return node


def leaves_for(department: str, *, tree: dict | None = None) -> tuple[Leaf, ...]:
    """Every childless category under one in-scope department, from the
    recorded tree. Unknown department raises KeyError rather than returning
    an empty tuple, so a typo'd slug fails at the call site instead of
    silently sweeping nothing.
    """
    t = tree if tree is not None else load_tree()
    node = _department_node(t, department)
    dept_title = node.get("title", department)
    return tuple(
        Leaf(category_id=str(leaf["category_id"]), title=leaf["title"],
             path=(dept_title, leaf["title"]))
        for leaf in node.get("leaves", [])
    )


def top_level_of(tree: dict | None = None) -> dict[str, str]:
    """leaf category id -> top-level category id, over every department in
    the recorded tree. This is what fees.max_cost needs to price a leaf: the
    fee table is keyed by top-level department, and a listing only ever
    carries its leaf id.
    """
    t = tree if tree is not None else load_tree()
    out: dict[str, str] = {}
    for node in t.get("departments", {}).values():
        top_id = node.get("category_id") or ""
        if not top_id:
            continue
        for leaf in node.get("leaves", []):
            out[str(leaf["category_id"])] = top_id
    return out


# --- Taxonomy API, UNVERIFIED: shaped from the docs, never called without a client ---
#
# https://developer.ebay.com/api-docs/commerce/taxonomy/resources/methods --
# read for the request/response shape, never exercised, because no production
# keyset exists yet (spec section 3). A `client` is anything exposing `.get`
# the way phoneloop.harness.HarnessSession or a requests.Session does; passing
# None is the "no keyset configured" case and raises rather than reaching the
# network with nothing to authenticate with.

def get_default_category_tree_id(client, marketplace_id: str = "EBAY_GB") -> str:
    """UNVERIFIED: Commerce Taxonomy getDefaultCategoryTreeId.

    GET /commerce/taxonomy/v1/get_default_category_tree_id?marketplace_id=...
    -> {"categoryTreeId": "3", "categoryTreeVersion": "..."} per the OAS.
    Never exercised against a live response.
    """
    if client is None:
        raise RuntimeError(
            "no Taxonomy API client - get_default_category_tree_id is "
            "unverified (shaped from the docs, never exercised) and must not "
            "be called without one")
    resp = client.get(
        "https://api.ebay.com/commerce/taxonomy/v1/get_default_category_tree_id",
        params={"marketplace_id": marketplace_id})
    resp.raise_for_status()
    return resp.json()["categoryTreeId"]


def get_category_subtree(client, tree_id: str, category_id: str) -> dict:
    """UNVERIFIED: Commerce Taxonomy getCategorySubtree.

    GET /commerce/taxonomy/v1/category_tree/{tree_id}/get_category_subtree
        ?category_id=... -> a nested categorySubtreeNode tree per the OAS.
    Never exercised against a live response.
    """
    if client is None:
        raise RuntimeError(
            "no Taxonomy API client - get_category_subtree is unverified "
            "(shaped from the docs, never exercised) and must not be called "
            "without one")
    resp = client.get(
        f"https://api.ebay.com/commerce/taxonomy/v1/category_tree/{tree_id}"
        "/get_category_subtree",
        params={"category_id": category_id})
    resp.raise_for_status()
    return resp.json()
