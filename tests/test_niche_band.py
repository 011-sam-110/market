"""A niche is "leaf x price band" (spec section 5) -- but only the leaf was
ever stored, so two bands of one leaf would silently become one niche.

The sweep filters on a price band (`_udlo`/`_udhi`, the `15-55` in a
search_key), and the ASP band is one of the six knobs, so sweeping a second
band is a planned move, not a hypothetical. The row it wrote recorded the
shard as `16086/p1` -- category and page, no band -- and board.rank_niches
groups on `leaf_category_id` alone.

Nothing about that goes red on its own. Every niche-level figure (median
price, sell-through, competitor count) would simply start describing a blend
of two price bands, which is the shape of failure this project keeps hitting:
a number that is wrong rather than missing.

These tests pin the band onto the row and into the niche key.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from phoneloop.ebay import parse_row, to_row

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "bulk_ebay.py"
_spec = importlib.util.spec_from_file_location("bulk_ebay_under_test_band", _SCRIPT)
bulk_ebay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bulk_ebay)

# A real recorded row (sacat 139956, 2026-08-28), so the shard string under
# test is attached to a listing that genuinely exists.
ROW = ("NEW Genuine Toomax Storaway Spare Replacement Parts\n"
       "Opens in a new window or tab\nBrand new\n£3.99 to £19.99\nBuy It Now\n"
       "+£2.99 delivery\ncharmainesellsitall 99.8% positive (10.7K)\nderosnopS")
URL = "https://www.ebay.co.uk/itm/127154756630"


class TestShardCarriesTheBand:
    def test_shard_for_names_category_band_and_page(self):
        assert bulk_ebay.shard_for("139956", 15, 55, 1) == "139956/15-55/p1"

    def test_two_bands_of_one_category_get_different_shards(self):
        a = bulk_ebay.shard_for("139956", 15, 55, 1)
        b = bulk_ebay.shard_for("139956", 55, 150, 1)
        assert a != b

    def test_the_band_survives_onto_the_stored_row(self):
        listing = parse_row(ROW, URL)
        row = to_row(listing, category="garden_patio",
                     shard=bulk_ebay.shard_for("139956", 15, 55, 1),
                     leaf_category_id="139956")
        assert row["shard"] == "139956/15-55/p1"


class TestNicheKeyIsLeafTimesBand:
    """The key board.rank_niches groups on."""

    def test_band_is_read_back_off_the_shard(self):
        assert bulk_ebay.band_of("139956/15-55/p1") == "15-55"

    def test_an_old_format_shard_reports_no_band(self):
        """Rows written before this change say 16086/p1. They must come back
        as "band unknown", not crash and not claim a band they never had."""
        assert bulk_ebay.band_of("16086/p1") is None

    def test_two_bands_do_not_collapse_into_one_niche(self):
        keys = {bulk_ebay.niche_key("139956", "139956/15-55/p1"),
                bulk_ebay.niche_key("139956", "139956/55-150/p2")}
        assert len(keys) == 2, "two price bands merged into a single niche"

    def test_pages_of_the_same_band_do_collapse(self):
        """Sharding by page is a fetch detail, never a niche boundary."""
        keys = {bulk_ebay.niche_key("139956", "139956/15-55/p1"),
                bulk_ebay.niche_key("139956", "139956/15-55/p3")}
        assert len(keys) == 1
