"""catalog_ebay tests.

`leaves_for` / `top_level_of` are tested against `ebay_categories.json` --
the real tree recorded by `scripts/refresh_ebay_categories.py` on 2026-08-28,
egress DIRECT (see that script's docstring for exactly what was fetched and
`refresh_ebay_categories.py`'s module comment for the two page templates it
parses). Nothing here is synthetic: every leaf id below came off a live page.

The last class in this file, `TestRefreshParsing`, guards the two-template
HTML parser in `scripts/refresh_ebay_categories.py` directly, against the
same real fixture bytes -- not required by the contract's acceptance list,
but the parsing bug it fixed (an unscoped regex pulling promotional widget
links and an ancestor category into a department's own subcategory list) was
real and silent, and is exactly the kind of thing that regresses quietly.
"""

from __future__ import annotations

import gzip
import json

import pytest

from phoneloop.catalog_ebay import (
    EXCLUDED,
    IN_SCOPE,
    get_category_subtree,
    get_default_category_tree_id,
    leaves_for,
    load_tree,
    top_level_of,
)

# spec section 9's in-scope list, in eBay's own department titles -- the
# titles `catalog_ebay.IN_SCOPE` carries, read off the real
# /n/all-categories recording (contracts section 2.14: "Do not guess an id").
SPEC_IN_SCOPE_TITLES = (
    "Home, Furniture & DIY",
    "Garden & Patio",
    "Pet Supplies",
    "Sporting Goods",
    "Vehicle Parts & Accessories",
    "Mobile Phones & Communication",
    "Crafts",
    "Health & Beauty",
    "Toys & Games",
)

FIXTURES_ROOT = "C:/Users/sampo/.phoneloop/fixtures/ebay_categories"


def _load_fixture(stem: str) -> str:
    """One real recorded page's HTML, decompressed straight from the
    cassette -- not the repo's own fixtures/ (nothing is committed there for
    this stream; the recordings live under ~/.phoneloop/fixtures per the
    harness convention and the contract's explicit instruction)."""
    import glob
    matches = glob.glob(f"{FIXTURES_ROOT}/{stem}-*.json.gz")
    if not matches:
        pytest.skip(f"recording {stem}-*.json.gz not present on this machine")
    with gzip.open(matches[0], "rt", encoding="utf-8") as fh:
        return json.load(fh)["data"]


class TestScopeConfig:
    """spec section 9, verbatim: nine in scope, five excluded, every
    exclusion carrying its reason."""

    def test_in_scope_is_the_nine_spec_departments(self):
        assert tuple(d.title for d in IN_SCOPE) == SPEC_IN_SCOPE_TITLES

    def test_in_scope_slugs_are_unique(self):
        slugs = [d.slug for d in IN_SCOPE]
        assert len(slugs) == len(set(slugs))

    def test_excluded_is_the_five_spec_departments(self):
        assert len(EXCLUDED) == 5

    def test_every_excluded_department_carries_a_reason(self):
        for dept in EXCLUDED:
            assert dept.note, f"{dept.title} excluded with no reason recorded"

    def test_clothes_and_shoes_is_excluded_for_sizing_returns(self):
        clothes = next(d for d in EXCLUDED if d.slug == "clothes_shoes")
        assert "sizing" in clothes.note.lower()

    def test_no_slug_is_both_in_scope_and_excluded(self):
        in_scope_slugs = {d.slug for d in IN_SCOPE}
        excluded_slugs = {d.slug for d in EXCLUDED}
        assert not (in_scope_slugs & excluded_slugs)


class TestLeavesForRealRecording:
    """Against the live tree in ebay_categories.json -- run through
    `load_tree()`'s real default path, not a hand-built dict, so a stale or
    missing file fails these tests the same way it would fail a caller."""

    def test_every_in_scope_department_has_leaves(self):
        """The whole point of the refresh: a department with zero leaves
        cannot be swept. All nine came back with real subcategories on the
        2026-08-28 direct-connection run."""
        for dept in IN_SCOPE:
            leaves = leaves_for(dept.slug)
            assert len(leaves) > 0, f"{dept.title} has no recorded leaves"

    def test_every_leaf_id_is_numeric_as_string(self):
        for dept in IN_SCOPE:
            for leaf in leaves_for(dept.slug):
                assert leaf.category_id.isdigit(), (
                    f"{dept.title}: {leaf.title!r} has a non-numeric id "
                    f"{leaf.category_id!r}")

    def test_no_duplicate_leaf_ids_anywhere_in_the_tree(self):
        seen: dict[str, str] = {}
        for dept in IN_SCOPE:
            for leaf in leaves_for(dept.slug):
                assert leaf.category_id not in seen, (
                    f"{leaf.category_id} appears under both "
                    f"{seen.get(leaf.category_id)!r} and {dept.title!r}")
                seen[leaf.category_id] = dept.title

    def test_every_leaf_is_reachable_from_its_department_in_path(self):
        for dept in IN_SCOPE:
            for leaf in leaves_for(dept.slug):
                assert leaf.path[0] == dept.title
                assert leaf.path[-1] == leaf.title
                assert len(leaf.path) == 2

    def test_an_unknown_department_raises_key_error(self):
        with pytest.raises(KeyError):
            leaves_for("not-a-department")


class TestTopLevelOf:
    def test_every_leaf_maps_to_a_recognised_top_level_id(self):
        known_top_ids = {d.category_id for d in (*IN_SCOPE, *EXCLUDED) if d.category_id}
        mapping = top_level_of()
        assert mapping, "top_level_of() returned nothing to check"
        for leaf_id, top_id in mapping.items():
            assert top_id in known_top_ids, (
                f"leaf {leaf_id} maps to {top_id!r}, not one of IN_SCOPE/EXCLUDED")

    def test_every_leaf_from_leaves_for_appears_in_the_mapping(self):
        mapping = top_level_of()
        for dept in IN_SCOPE:
            for leaf in leaves_for(dept.slug):
                assert mapping[leaf.category_id] == dept.category_id


class TestLoadTree:
    def test_missing_file_raises_with_the_refresh_command(self, tmp_path):
        missing = tmp_path / "no_such_tree.json"
        with pytest.raises(FileNotFoundError) as exc_info:
            load_tree(missing)
        assert "refresh_ebay_categories.py" in str(exc_info.value)

    def test_missing_file_never_returns_empty_dict(self, tmp_path):
        """The guard this stream watched go red: swap the raise for
        `return {}` and this is the test that catches it -- an empty tree
        would make `leaves_for` report "no leaves" for a department that
        was simply never loaded, indistinguishable from a real gap."""
        missing = tmp_path / "no_such_tree.json"
        with pytest.raises(FileNotFoundError):
            load_tree(missing)


class TestTaxonomyStubsUnverified:
    """Shaped from eBay's Commerce Taxonomy API docs, never exercised (spec
    section 3: no keyset exists). Calling either without a client must raise,
    never fabricate a tree that looks like a real API response."""

    def test_get_default_category_tree_id_raises_without_a_client(self):
        with pytest.raises(RuntimeError):
            get_default_category_tree_id(None)

    def test_get_category_subtree_raises_without_a_client(self):
        with pytest.raises(RuntimeError):
            get_category_subtree(None, "3", "11700")


class TestRefreshParsing:
    """Guards `scripts/refresh_ebay_categories.py`'s HTML parsing against
    the real recorded bytes -- see this file's module docstring."""

    def test_top_level_ids_read_off_the_real_all_categories_page(self):
        from scripts.refresh_ebay_categories import parse_top_level_ids
        html_text = _load_fixture("all-categories")
        ids = parse_top_level_ids(html_text)
        for dept in IN_SCOPE:
            assert ids.get(dept.title) == dept.category_id, (
                f"{dept.title}: /n/all-categories now says "
                f"{ids.get(dept.title)!r}, catalog_ebay says {dept.category_id!r}")

    def test_the_brw_template_department_finds_real_subcategories(self):
        """Home, Furniture & DIY: `section.brw-category-nav`."""
        from scripts.refresh_ebay_categories import parse_department_subcats
        html_text = _load_fixture("dept-home-furniture-diy")
        subcats = parse_department_subcats(html_text)
        titles = {s["title"] for s in subcats}
        assert "Kitchen" in titles
        assert len(subcats) > 10

    def test_the_su_template_department_finds_real_subcategories(self):
        """Pet Supplies: `su-category-tree`, not `brw-category-nav` --
        `_SUBCAT_RE` found nothing here before the fallback parser existed."""
        from scripts.refresh_ebay_categories import parse_department_subcats
        html_text = _load_fixture("dept-pet-supplies")
        subcats = parse_department_subcats(html_text)
        titles = {s["title"] for s in subcats}
        assert titles == {
            "Beekeeping", "Birds", "Cameras", "Cat Supplies", "Dog Supplies",
            "Fish & Aquarium", "Other Pet Supplies", "Pet Memorials",
            "Poultry & Waterfowl", "Reptiles", "Small Animal Supplies",
            "Spiders & Insects", "Trackers",
        }

    def test_promotional_widgets_never_pollute_the_brw_subcategory_list(self):
        """Vehicle Parts & Accessories carries five OTHER sections sharing
        the exact `brw-category-nav` class ("My Garage", "You're Protected",
        "Tyre Fitting Service", "Certified Recycled", "Sell on eBay"); one of
        them links a real bn_ id that is a booking flow, not a category. An
        earlier, unscoped version of this parser returned it as a
        "subcategory"."""
        from scripts.refresh_ebay_categories import parse_department_subcats
        html_text = _load_fixture("dept-vehicle-accessories")
        titles = {s["title"] for s in parse_department_subcats(html_text)}
        assert "Tyres, fitting, easy!" not in titles
        assert "Car care & cleaning" in titles

    def test_the_ancestor_link_is_not_returned_as_a_subcategory(self):
        """Crafts' nested brw list is headed by "Sports, Hobbies & Leisure"
        -- the parent one level up, not a Crafts subcategory -- immediately
        followed by a nested `<section class="brw-category-nav__nested-items">`,
        which is how it is told apart from a genuine leaf link."""
        from scripts.refresh_ebay_categories import parse_department_subcats
        html_text = _load_fixture("dept-crafts")
        titles = {s["title"] for s in parse_department_subcats(html_text)}
        assert "Sports, Hobbies & Leisure" not in titles
        assert "Sewing" in titles
