"""eBay row-parsing tests.

Every fixture below is copied verbatim from a live eBay UK search on
2026-08-27. That rule is not decoration here -- this project has already
shipped a photo counter that returned zero for every real listing because its
fixture claimed to be real and was invented.
"""

from __future__ import annotations

import pytest

from phoneloop.ebay import (
    EbayListing,
    parse_row,
    parse_rows,
    retail_ceiling,
    saturation,
    to_pence,
)

# --- verbatim rows ----------------------------------------------------------

UK_USED = """Apple iPhone 13 128GB White Unlocked 5.4in iOS 12MP 5G Smartphone
Opens in a new window or tab
Pre-owned · Apple iPhone 13 · Unlocked · 128 GB
5.0 out of 5 stars.
2 product ratings
- Apple iPhone 13 128GB White Unlocked 5.4in iOS 12MP 5G Smartphone
£198.98
or Best Offer
+£4.50 delivery
reshi-1723 0% positive (0)"""

UK_WATCHED = """Apple iPhone 13 mini - 128 GB - Midnight (Unlocked) Battery Health 77%
Opens in a new window or tab
Pre-owned · Apple iPhone 13 mini · Unlocked · 128 GB
£150.42
or Best Offer
+£20.00 delivery
11 watchers
pngadget_0 99.4% positive (249)"""

OVERSEAS = """Fully Fresh Iphone 13 With Open Box Total Wireless Lock
Opens in a new window or tab
New (other) · Apple iPhone 13 · 128 GB · TracFone
4.0 out of 5 stars.
1 product rating
£292.33
or Best Offer
+£24.77 delivery
from United States
20 watchers
eu_6414 100% positive (67)"""

NO_POSTAGE = """Apple iPhone 13 128GB Blue Unlocked
Opens in a new window or tab
Pre-owned · Apple iPhone 13 · Unlocked · 128 GB
£175.00
Postage not specified
someone_2024 98% positive (12)"""

# About two thirds of a live results page looks like this: a real listing with
# a real price and no catalogue match line at all.
NO_ASPECTS = """Apple iPhone 13 maybe 128gb see photos
Opens in a new window or tab
£200.00
Free postage"""

FREE_POST = """Apple iPhone 13 128GB Starlight Unlocked
Opens in a new window or tab
Pre-owned · Apple iPhone 13 · Unlocked · 128 GB
£205.00
Free international postage
from Germany
3 watchers"""


class TestMoney:
    def test_reads_pounds_and_pence(self):
        assert to_pence("£198.98") == 19898
        assert to_pence("£1,250.00") == 125000

    def test_no_price_is_none(self):
        assert to_pence("or Best Offer") is None


class TestRow:
    def test_reads_a_uk_used_listing(self):
        row = parse_row(UK_USED)
        assert row.price_pence == 19898
        assert row.delivery_pence == 450
        assert row.landed_pence == 20348
        assert row.condition == "Pre-owned"
        assert row.storage_gb == 128
        assert row.is_uk is True
        assert row.is_used is True

    def test_the_title_is_the_first_line_not_the_repeat(self):
        assert parse_row(UK_USED).title.startswith("Apple iPhone 13 128GB White")

    def test_reads_watchers(self):
        assert parse_row(UK_WATCHED).watchers == 11
        assert parse_row(UK_USED).watchers == 0

    def test_detects_an_overseas_seller(self):
        row = parse_row(OVERSEAS)
        assert row.ships_from == "United States"
        assert row.is_uk is False

    def test_free_postage_is_zero_not_unknown(self):
        row = parse_row(FREE_POST)
        assert row.delivery_pence == 0
        assert row.landed_pence == 20500
        assert row.ships_from == "Germany"

    def test_unstated_postage_makes_landed_cost_unknown(self):
        """Not zero. "Postage not specified" on a heavy or overseas item is
        routinely the difference between a bargain and a loss, and 4 of the
        first 60 live rows said exactly that."""
        row = parse_row(NO_POSTAGE)
        assert row.delivery_pence is None
        assert row.landed_pence is None

    def test_the_aspects_line_supplies_storage_the_title_lacks(self):
        """eBay's own catalogue match beats a regex over the seller's title."""
        row = parse_row(UK_WATCHED)
        assert row.aspects[1] == "Apple iPhone 13 mini"
        assert row.storage_gb == 128

    def test_a_row_with_no_price_is_skipped(self):
        assert parse_row("Some banner\nShop now") is None
        assert parse_row("") is None

    def test_parse_rows_skips_the_unusable_without_raising(self):
        rows = [{"text": UK_USED, "href": "https://ebay.co.uk/itm/1"},
                {"text": "advert, no price"},
                {"text": OVERSEAS}]
        parsed = parse_rows(rows)
        assert len(parsed) == 2
        assert parsed[0].url.endswith("/itm/1")


class TestCeiling:
    def _pool(self, n=6, base=20000, step=500):
        return [
            EbayListing(f"iPhone 13 {i}", base + i * step, 0,
                        condition="Pre-owned", storage_gb=128)
            for i in range(n)
        ]

    def test_median_of_landed_uk_prices(self):
        c = retail_ceiling(self._pool(), storage_gb=128)
        assert c.samples == 6
        assert c.median_pence == 21250

    def test_overseas_listings_are_excluded_and_counted(self):
        """They skew cheap, which makes them look like the bargains, and they
        carry customs, VAT and weeks of delivery."""
        pool = self._pool() + [
            EbayListing("cheap import", 9000, 0, condition="Pre-owned",
                        storage_gb=128, ships_from="Germany")
        ]
        c = retail_ceiling(pool, storage_gb=128)
        assert c.overseas_excluded == 1
        assert c.median_pence == 21250, "the import must not drag the median"

    def test_listings_with_unknown_postage_are_excluded_and_counted(self):
        pool = self._pool() + [
            EbayListing("no postage", 15000, None, condition="Pre-owned",
                        storage_gb=128)
        ]
        c = retail_ceiling(pool, storage_gb=128)
        assert c.unpriceable_excluded == 1
        assert c.samples == 6

    def test_a_different_capacity_is_not_comparable(self):
        pool = self._pool() + [
            EbayListing("512GB", 40000, 0, condition="Pre-owned", storage_gb=512)
        ]
        assert retail_ceiling(pool, storage_gb=128).samples == 6

    def test_too_few_samples_is_none_not_a_number(self):
        """Two listings are two people's opinions, not a market rate."""
        assert retail_ceiling(self._pool(n=3), storage_gb=128) is None
        assert retail_ceiling([], storage_gb=128) is None

    def test_an_outlier_does_not_move_the_median(self):
        pool = self._pool() + [
            EbayListing("mispriced", 150000, 0, condition="Pre-owned",
                        storage_gb=128)
        ]
        assert retail_ceiling(pool, storage_gb=128).median_pence == 21500


class TestVariantContamination:
    """A search for "iphone 13" returns the mini, the Pro and the Pro Max.

    This is not a rare edge: the live "iphone 13" page returned 88 listings
    and the resulting unfiltered median sat 6.63 pounds from the iPhone 12's,
    which is not a credible gap between two generations. It was variant
    contamination, and it would have priced every buy on the board.
    """

    def test_it_reads_the_model_from_ebays_own_catalogue_line(self):
        assert parse_row(UK_USED).catalogue_model == "iphone-13"
        assert parse_row(UK_WATCHED).catalogue_model == "iphone-13-mini"

    def test_no_catalogue_line_means_unknown_not_a_guess(self):
        row = parse_row(NO_ASPECTS)
        assert row.catalogue_model is None
        assert row.price_pence == 20000, "the row is otherwise perfectly usable"

    def test_a_mini_does_not_price_a_standard(self):
        pool = [
            EbayListing(f"iPhone 13 {i}", 21000 + i * 500, 0,
                        condition="Pre-owned",
                        aspects=("Pre-owned", "Apple iPhone 13", "128 GB"),
                        storage_gb=128)
            for i in range(5)
        ] + [
            EbayListing("iPhone 13 mini", 9000, 0, condition="Pre-owned",
                        aspects=("Pre-owned", "Apple iPhone 13 mini", "128 GB"),
                        storage_gb=128)
        ]
        c = retail_ceiling(pool, storage_gb=128, model="iphone-13")
        assert c.variant_excluded == 1
        assert c.samples == 5
        assert c.median_pence == 22000, "the mini must not drag the median"

    def test_uncatalogued_rows_are_dropped_when_a_model_is_demanded(self):
        """Roughly two thirds of a live page has no catalogue line. Assuming
        those match would readmit exactly the contamination this prevents."""
        pool = [
            EbayListing(f"iPhone 13 {i}", 21000, 0, condition="Pre-owned",
                        aspects=("Pre-owned", "Apple iPhone 13"), storage_gb=128)
            for i in range(4)
        ] + [
            EbayListing("no aspects", 5000, 0, condition="Pre-owned",
                        storage_gb=128)
        ]
        c = retail_ceiling(pool, storage_gb=128, model="iphone-13")
        assert c.variant_excluded == 1
        assert c.samples == 4

    def test_without_a_model_nothing_is_excluded(self):
        """Back-compatible: the filter only applies when asked for."""
        pool = [
            EbayListing(f"x{i}", 20000, 0, condition="Pre-owned", storage_gb=128)
            for i in range(4)
        ]
        assert retail_ceiling(pool, storage_gb=128).variant_excluded == 0


class TestSaturation:
    def test_counts_the_competition_and_the_interest(self):
        pool = [
            EbayListing("a", 100, 0, watchers=5),
            EbayListing("b", 100, 0, watchers=1),
            EbayListing("c", 100, 0, ships_from="Germany", watchers=0),
        ]
        s = saturation(pool)
        assert s["live_listings"] == 3
        assert s["uk_listings"] == 2
        assert s["overseas_listings"] == 1
        assert s["total_watchers"] == 6
        assert s["watchers_per_listing"] == 2.0

    def test_empty_does_not_divide_by_zero(self):
        assert saturation([])["watchers_per_listing"] == 0.0
