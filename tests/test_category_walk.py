"""The recursive category walk -- traversal only, never parsing.

`parse_department_subcats`, `parse_leaf_category_id` and `parse_leaf_title`
are exercised against real recorded eBay HTML in tests/test_catalog_ebay.py,
and nothing here re-tests them. What this file pins is the walk built on top:
which nodes it visits, which it refuses to visit twice, where it stops, and
what it counts on the way. Those are control-flow facts, so the pages below
are deliberately synthetic stand-ins keyed by bn_id, not eBay HTML.

The walk exists because the crawl was capped at five subcategories per
department and one level deep, which is where the board's 43 leaves came
from. 43 is not a market. It is a cap.
"""

from __future__ import annotations

import pytest

from scripts.refresh_ebay_categories import Blocked, walk


class FakeTree:
    """A category tree the walk can traverse, addressed the way eBay is.

    `nodes` maps a bn_id to (numeric category id, title, [child bn_ids]).
    `fetches` records every id actually requested, in order, so a test can
    assert that a node was visited once and not twice.
    """

    def __init__(self, nodes: dict, blocked: set[str] = frozenset()):
        self.nodes = nodes
        self.blocked = set(blocked)
        self.fetches: list[str] = []

    def rec(self, _kind: str, name: str, _produce):
        bn_id = name.removeprefix("leaf-")
        self.fetches.append(bn_id)
        if bn_id in self.blocked:
            raise Blocked(f"blocked - {bn_id}")
        return bn_id

    def parse_children(self, marker: str):
        return [{"bn_id": c, "title": f"title-{c}"}
                for c in self.nodes[marker][2]]

    def parse_id(self, marker: str):
        return self.nodes[marker][0]

    def parse_title(self, marker: str):
        return self.nodes[marker][1]


@pytest.fixture
def patched(monkeypatch):
    """Swap the three HTML parsers for the FakeTree's own lookups.

    The walk passes its fetched page straight to all three, so a page that is
    just its own bn_id lets the fake answer every question about it.
    """
    def _install(tree: FakeTree):
        import scripts.refresh_ebay_categories as mod
        monkeypatch.setattr(mod, "parse_department_subcats", tree.parse_children)
        monkeypatch.setattr(mod, "parse_leaf_category_id", tree.parse_id)
        monkeypatch.setattr(mod, "parse_leaf_title", tree.parse_title)
        return tree
    return _install


def new_stats() -> dict:
    return {"fetched": 0, "blocked": 0, "no_id": 0,
            "children_seen": 0, "children_not_walked": 0}


def run(tree: FakeTree, root: str, *, depth: int, max_children: int = 0,
        seen: set[str] | None = None, stats: dict | None = None):
    return walk(tree.rec, None, {"bn_id": root, "title": f"title-{root}"},
                ancestry=("Toys & Games",), depth=depth,
                max_children=max_children,
                seen=seen if seen is not None else set(),
                stats=stats if stats is not None else new_stats())


# --- where the walk stops ---------------------------------------------------

def test_depth_zero_records_the_node_itself_as_a_leaf(patched):
    """A bounded crawl means "childless in the tree we recorded". A node with
    children that this crawl did not descend into is still the leaf the sweep
    will shard on, and calling it anything else would overstate the crawl."""
    tree = patched(FakeTree({"bn_1": ("246", "Action Figures", ["bn_2", "bn_3"])}))

    leaves = run(tree, "bn_1", depth=0)

    assert leaves == [{"category_id": "246", "title": "Action Figures",
                       "path": ["Toys & Games", "Action Figures"], "depth": 1}]
    assert tree.fetches == ["bn_1"], "the children must not be fetched at depth 0"


def test_depth_one_returns_the_children_and_not_their_parent(patched):
    """A hub and its children must never both be swept. Sweeping both books
    the same listings under two leaves and counts them twice."""
    tree = patched(FakeTree({
        "bn_1": ("246", "Action Figures", ["bn_2", "bn_3"]),
        "bn_2": ("300", "Marvel", []),
        "bn_3": ("301", "Star Wars", []),
    }))

    leaves = run(tree, "bn_1", depth=1)

    assert [x["category_id"] for x in leaves] == ["300", "301"]
    assert "246" not in [x["category_id"] for x in leaves]


def test_a_node_with_no_children_is_a_leaf_at_any_depth(patched):
    """Departments are not uniformly deep. A branch that bottoms out early
    must not be dropped because the crawl had budget left."""
    tree = patched(FakeTree({
        "bn_1": ("246", "Action Figures", ["bn_2"]),
        "bn_2": ("300", "Marvel", []),
    }))

    leaves = run(tree, "bn_1", depth=5)

    assert [x["category_id"] for x in leaves] == ["300"]


def test_the_path_records_the_whole_ancestry(patched):
    """The board groups thousands of leaves, and a leaf id alone says
    nothing about where it sits."""
    tree = patched(FakeTree({
        "bn_1": ("246", "Action Figures", ["bn_2"]),
        "bn_2": ("300", "Marvel", []),
    }))

    leaves = run(tree, "bn_1", depth=1)

    assert leaves[0]["path"] == ["Toys & Games", "Action Figures", "Marvel"]
    assert leaves[0]["depth"] == 2


# --- what it refuses to visit twice -----------------------------------------

def test_a_node_reached_from_two_parents_is_walked_once(patched):
    """The panels cross-link. Without the shared `seen` set the walk fetches
    the shared node twice and writes the same leaf twice, and a duplicated
    leaf is a duplicated shard.

    bn_3 contributes nothing here, and that is the point. Its only child is
    already recorded under bn_2, so bn_3 is a hub whose whole subtree is
    covered -- not a childless category. Recording it as a leaf would sweep
    a category that contains listings already swept under bn_4.
    """
    tree = patched(FakeTree({
        "bn_1": ("246", "Action Figures", ["bn_2", "bn_3"]),
        "bn_2": ("300", "Marvel", ["bn_4"]),
        "bn_3": ("301", "Star Wars", ["bn_4"]),
        "bn_4": ("400", "Figurines", []),
    }))

    leaves = run(tree, "bn_1", depth=3)

    assert tree.fetches.count("bn_4") == 1
    assert [x["category_id"] for x in leaves] == ["400"]


def test_a_hub_still_walks_the_children_that_are_new(patched):
    """The rule is "contributes nothing new", not "touched a seen child".
    A hub with three children, one of them already recorded elsewhere, must
    still yield the other two."""
    tree = patched(FakeTree({
        "bn_1": ("246", "A", ["bn_2", "bn_3"]),
        "bn_2": ("300", "B", ["bn_9"]),
        "bn_3": ("301", "C", ["bn_9", "bn_4", "bn_5"]),
        "bn_4": ("400", "D", []),
        "bn_5": ("500", "E", []),
        "bn_9": ("900", "Shared", []),
    }))

    leaves = run(tree, "bn_1", depth=3)

    assert [x["category_id"] for x in leaves] == ["900", "400", "500"]
    assert "301" not in [x["category_id"] for x in leaves]


def test_a_self_link_does_not_recurse(patched):
    """Every browse panel links to the category it is already on. Unfiltered,
    that node walks into itself, finds one child it has already seen, and
    returns nothing -- so a real leaf would vanish."""
    tree = patched(FakeTree({"bn_1": ("246", "Action Figures", ["bn_1"])}))

    leaves = run(tree, "bn_1", depth=3)

    assert [x["category_id"] for x in leaves] == ["246"]
    assert tree.fetches == ["bn_1"]


def test_seen_is_shared_across_departments(patched):
    """"Home & Garden" sits above both Home, Furniture & DIY and Pet
    Supplies. A per-department set would land one leaf in two departments,
    and the board would count its listings twice."""
    tree = patched(FakeTree({
        "bn_1": ("246", "A", ["bn_9"]),
        "bn_2": ("247", "B", ["bn_9"]),
        "bn_9": ("900", "Shared", []),
    }))
    shared: set[str] = set()

    first = run(tree, "bn_1", depth=2, seen=shared)
    second = run(tree, "bn_2", depth=2, seen=shared)

    assert [x["category_id"] for x in first] == ["900"]
    assert second == []
    assert tree.fetches.count("bn_9") == 1


# --- what it survives -------------------------------------------------------

def test_a_blocked_node_drops_its_branch_and_is_counted(patched):
    """A block is not an empty category. The count is what tells a thin
    crawl apart from a thin market."""
    tree = patched(FakeTree({
        "bn_1": ("246", "A", ["bn_2", "bn_3"]),
        "bn_2": ("300", "B", []),
        "bn_3": ("301", "C", []),
    }, blocked={"bn_2"}))
    stats = new_stats()

    leaves = run(tree, "bn_1", depth=1, stats=stats)

    assert [x["category_id"] for x in leaves] == ["301"]
    assert stats["blocked"] == 1
    assert stats["fetched"] == 2


def test_a_leaf_with_no_numeric_id_is_dropped_not_guessed(patched):
    """The sweep filters on the classic numeric id. A leaf without one cannot
    be swept, and inventing one would sweep the wrong category."""
    tree = patched(FakeTree({
        "bn_1": ("246", "A", ["bn_2", "bn_3"]),
        "bn_2": (None, "B", []),
        "bn_3": ("301", "C", []),
    }))
    stats = new_stats()

    leaves = run(tree, "bn_1", depth=1, stats=stats)

    assert [x["category_id"] for x in leaves] == ["301"]
    assert stats["no_id"] == 1


# --- what it counts ---------------------------------------------------------

def test_the_children_it_did_not_walk_are_counted(patched):
    """The whole point of the count: it prices the next level from a crawl
    that did not run it. The HTML is already in hand, so this is free."""
    tree = patched(FakeTree({
        "bn_1": ("246", "A", ["bn_2", "bn_3"]),
        "bn_2": ("300", "B", ["bn_4", "bn_5"]),
        "bn_3": ("301", "C", ["bn_6"]),
    }))
    stats = new_stats()

    run(tree, "bn_1", depth=1, stats=stats)

    assert stats["children_not_walked"] == 3
    assert stats["fetched"] == 3


def test_max_children_caps_the_branching_but_still_counts_the_rest(patched):
    """A cap that also hid what it cut is how 43 leaves came to look like a
    market. Taking two of six is fine. Reporting six as two is not."""
    tree = patched(FakeTree({
        "bn_1": ("246", "A", ["bn_2", "bn_3", "bn_4"]),
        "bn_2": ("300", "B", []),
        "bn_3": ("301", "C", []),
        "bn_4": ("302", "D", []),
    }))
    stats = new_stats()

    leaves = run(tree, "bn_1", depth=1, max_children=2, stats=stats)

    assert [x["category_id"] for x in leaves] == ["300", "301"]
    assert stats["children_seen"] == 3
