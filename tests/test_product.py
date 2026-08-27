"""The product key: it must collapse listings without merging products.

Every title here is copied verbatim from a live Vinted or eBay listing on
2026-08-27. The ones this file exists for are the brand/model conflicts --
"Smartphone Oppo a5 2020" was being keyed as `galaxy-a5`, a Samsung slug on an
OPPO handset, and 173 of 6,191 live rows (2.8%) carried a collision like it.
Pricing an OPPO A5 against Galaxy A5 comps is the same variant contamination
that already produced an iPhone 12 ceiling 28% too high.
"""

from __future__ import annotations

import pytest

from phoneloop.product import (
    ProductKey,
    brands_conflict,
    capacity_of,
    family_tokens,
    key_for,
    normalise_brand,
)


class TestBrandModelConflict:
    """The guard that stops one manufacturer's comps pricing another's phone."""

    def test_oppo_titled_with_a_samsung_slug_is_a_conflict(self):
        assert brands_conflict("OPPO", "galaxy-a5") is True

    def test_the_conflicting_slug_is_dropped_not_used(self):
        pk = key_for("Smartphone Oppo a5 2020", "phones",
                     brand="OPPO", model_slug="galaxy-a5")
        assert pk.conflict is True
        assert "galaxy" not in pk.family
        assert pk.brand == "oppo"

    def test_a_conflict_is_marked_low_confidence(self):
        pk = key_for("Smartphone Oppo a5 2020", "phones",
                     brand="OPPO", model_slug="galaxy-a5")
        assert pk.confidence <= 0.3

    def test_agreeing_brand_and_slug_are_not_a_conflict(self):
        assert brands_conflict("Apple", "iphone-13-pro") is False
        assert brands_conflict("Samsung", "galaxy-s21") is False

    def test_an_unknown_brand_is_not_evidence_against_a_slug(self):
        """Most of the feed has no brand field. Treating blank as a conflict
        would discard it wholesale."""
        assert brands_conflict("", "iphone-13") is False
        assert brands_conflict("Apple", None) is False

    def test_a_sub_brand_does_not_conflict_with_its_parent(self):
        assert brands_conflict("Redmi", "redmi-note-11") is False
        assert normalise_brand("Redmi") == "xiaomi"
        assert normalise_brand("POCO") == "xiaomi"


class TestCollapse:
    """Different sellers describing one product must reach one key."""

    @pytest.mark.parametrize("title", [
        "iPhone 13 128GB",
        "Apple iPhone 13 - 128gb, unlocked",
        "iPhone 13 128GB Midnight Excellent Condition Boxed",
        "APPLE IPHONE 13 128 GB BLACK UNLOCKED FREE POSTAGE",
    ])
    def test_one_product_one_key(self, title):
        pk = key_for(title, "phones", brand="Apple", model_slug="iphone-13",
                     storage_gb=128)
        assert pk.key == "phones:apple:iphone-13"

    def test_capacity_is_a_variant_not_an_identity(self):
        """Splitting on capacity fragments the population until every product
        has one seller and the competition signal becomes noise."""
        a = key_for("iPhone 13 128GB", "phones", brand="Apple",
                    model_slug="iphone-13", storage_gb=128)
        b = key_for("iPhone 13 256GB", "phones", brand="Apple",
                    model_slug="iphone-13", storage_gb=256)
        assert a.key == b.key
        assert a.variant_key != b.variant_key

    def test_distinct_products_stay_distinct(self):
        """The failure that cost 28%: a Pro Max is not a base model."""
        base = key_for("iPhone 13", "phones", brand="Apple",
                       model_slug="iphone-13")
        pro = key_for("iPhone 13 Pro Max", "phones", brand="Apple",
                      model_slug="iphone-13-pro-max")
        assert base.key != pro.key


class TestFamilyWithoutASlug:
    """Fourteen of fifteen categories have no model parser at all."""

    def test_a_model_number_survives_the_noise(self):
        tokens = family_tokens(
            "DeWalt DCD796 Brushless Combi Drill Body Only Excellent Condition",
            "dewalt")
        assert "dcd796" in tokens

    def test_marketing_and_colour_are_stripped(self):
        tokens = family_tokens(
            "KitchenAid 5KSM175 Artisan Mixer Red Boxed Free Postage UK",
            "kitchenaid")
        assert "5ksm175" in tokens
        for noise in ("red", "boxed", "free", "postage", "uk"):
            assert noise not in tokens

    def test_the_brand_is_not_repeated_in_the_family(self):
        tokens = family_tokens("Makita DHP484 Hammer Driver Drill", "makita")
        assert "makita" not in tokens


class TestUsability:
    """A key naming nothing must not be grouped on."""

    def test_a_key_with_no_brand_and_no_family_is_unusable(self):
        pk = ProductKey(category="phones", brand="", family="",
                        variant="", confidence=0.1)
        assert pk.usable is False

    def test_a_bare_generic_title_is_not_forced_into_a_product(self):
        """'Android phone' names no product. Bucketing it would report a
        competition figure for something that does not exist."""
        pk = key_for("Android phone", "phones", brand="", model_slug=None)
        assert pk.usable is False

    def test_a_branded_model_is_usable(self):
        pk = key_for("Samsung Galaxy A14 64GB", "phones", brand="Samsung",
                     model_slug="galaxy-a14", storage_gb=64)
        assert pk.usable is True


class TestCapacity:
    @pytest.mark.parametrize("title,want", [
        ("iPhone 13 128GB", "128gb"),
        ("iPhone 13 128 GB", "128gb"),
        ("iPhone 13 1TB", "1tb"),
        ("iPhone 13 no capacity given", ""),
    ])
    def test_capacity_parsing(self, title, want):
        assert capacity_of(title) == want
