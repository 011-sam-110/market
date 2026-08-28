"""ebay_item.py, against the six real item pages recorded live by
scripts/record_ebay.py on 2026-08-27 -- read through the harness at
PHONELOOP_FIXTURES, the way the build contract requires (§2.6, §4/s2).

None of the six real pages carried an epid, or the "More than N available"
floor phrasing, or a genuinely junk seller-typed MPN in the design spec's
own sense ("Wireless-charge-carphonr" is from a different product page in
the spec's dataset, not one of these six phone listings). Those cases are
called out explicitly, in the module they matter, rather than silently
skipped.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from phoneloop.ebay_item import EbayItem, parse_item, to_row_updates
from phoneloop.harness import Cassette

# The build contract names this exact path (§3); PHONELOOP_FIXTURES overrides
# it for anyone who has recorded somewhere else.
FIXTURES_ROOT = Path(os.environ.get("PHONELOOP_FIXTURES",
                                    "C:/Users/sampo/.phoneloop/fixtures"))

pytestmark = pytest.mark.skipif(
    not (FIXTURES_ROOT / "ebay_item").exists(),
    reason=f"real item-page recordings not present at {FIXTURES_ROOT} on "
           "this machine -- run scripts/record_ebay.py first",
)

# Keys from the 2026-08-27 recording run.
IOUTLET = "item-https-www-ebay-co-uk-itm-225236871939-edfdd51f"        # business seller, MPN "Does Not Apply", no EAN
STAY_FREE = "item-https-www-ebay-co-uk-itm-128047213718-a1244de2"      # private seller, has EAN + a multi-value MPN
PRIVATE_LOW_FEEDBACK = "item-https-www-ebay-co-uk-itm-318787883180-9cd90d45"  # 1 feedback, explicit private marker
SANDS_WITH_QTY = "item-https-www-ebay-co-uk-itm-406124792115-4e07afb5"
SANDS_NO_QTY = "item-https-www-ebay-co-uk-itm-407173705482-605c99fd"   # no "Quantity:" block at all


def _page(key: str) -> dict:
    rec = Cassette(FIXTURES_ROOT).read("ebay_item", key)
    assert rec is not None, f"missing recording: ebay_item/{key}"
    return rec.data


def _parse(key: str) -> EbayItem:
    data = _page(key)
    item = parse_item(data["text"], html=data["html"], url=data["url"])
    assert item is not None
    return item


class TestItemId:
    def test_reads_the_ebay_item_number_line(self):
        assert _parse(IOUTLET).item_id == "225236871939"

    def test_falls_back_to_the_url_if_the_text_line_is_absent(self):
        assert parse_item("no item number line here\n£10.00",
                          url="https://www.ebay.co.uk/itm/999888777").item_id == "999888777"

    def test_no_text_at_all_is_none(self):
        assert parse_item("") is None

    def test_no_item_id_anywhere_is_none(self):
        assert parse_item("just some text with no identity") is None


class TestSoldAndAvailable:
    """"3 available2,858 sold" -- eBay renders these with no separator at
    all between them (measured on the real page)."""

    def test_a_real_available_and_sold_pair(self):
        item = _parse(IOUTLET)
        assert item.qty_available == 3
        assert item.qty_available_is_floor is False
        assert item.sold_qty == 2858

    def test_a_different_real_pair(self):
        item = _parse(SANDS_WITH_QTY)
        assert item.qty_available == 3
        assert item.sold_qty == 76

    def test_no_quantity_block_at_all_is_none_not_zero(self):
        item = _parse(SANDS_NO_QTY)
        assert item.qty_available is None
        assert item.sold_qty is None

    def test_sold_today_is_never_mistaken_for_lifetime_sold(self):
        """The same real page also shows "6 SOLD TODAY" (today's velocity,
        a different figure) earlier in the text than "2,858 sold" (the
        lifetime total). sold_qty must be the lifetime figure, not 6."""
        assert _parse(IOUTLET).sold_qty == 2858


# None of the six real item pages this build recorded showed "More than N
# available" -- eBay renders it only above some inventory threshold none of
# the six crossed. Hand-typed against eBay's own documented phrasing (build
# contract §2.5, spec's worked example), not lifted from a page recording --
# named SYNTHETIC_* per the standing rule; see unverified[] in the build
# report.
SYNTHETIC_MORE_THAN_AVAILABLE_PAGE = (
    "A listing\neBay item number:111222333444\n"
    "Quantity:\nMore than 10 available\nBuy It Now\n"
)


class TestMoreThanAvailableFloor:
    """None of the six real item pages this build recorded showed "More
    than N available" -- eBay renders it only above some inventory
    threshold none of the six crossed. This exercises the regex directly
    against eBay's own documented phrasing (build contract §2.5, spec's
    worked example) rather than a page recording; see unverified[] in the
    build report."""

    def test_synthetic_the_floor_flag_is_set(self):
        item = parse_item(SYNTHETIC_MORE_THAN_AVAILABLE_PAGE)
        assert item.qty_available == 10
        assert item.qty_available_is_floor is True

    def test_a_plain_available_count_is_not_a_floor(self):
        assert _parse(IOUTLET).qty_available_is_floor is False


class TestSellerFeedback:
    """The seller's top-of-page block, e.g. "The_iOutlet\n(76581)\n
    Business\n99.9% positive" -- feedback COUNT lives here; the later
    "About this seller" section repeats only the percentage."""

    def test_a_business_seller_with_a_large_feedback_count(self):
        item = _parse(IOUTLET)
        assert item.seller_feedback_bp == 9990
        assert item.seller_feedback_n == 76581

    def test_a_private_seller_with_a_single_feedback(self):
        item = _parse(PRIVATE_LOW_FEEDBACK)
        assert item.seller_feedback_bp == 10000
        assert item.seller_feedback_n == 1

    def test_the_sellerUserName_from_SEMANTIC_DATA_not_the_display_name(self):
        """The display name ("The_iOutlet") and the real username
        ("the_ioutlet_extra") differ in case and punctuation on every one
        of the six real pages -- the username is what a URL uses."""
        item = _parse(IOUTLET)
        assert item.seller_id == "the_ioutlet_extra"


class TestSellerType:
    """"Registered as a business seller" / "...private seller" is the one
    explicit marker this module trusts (build contract §2.5). Its absence
    is not evidence of "private"."""

    def test_the_business_marker(self):
        assert _parse(IOUTLET).seller_type == "business"

    def test_the_private_marker(self):
        assert _parse(PRIVATE_LOW_FEEDBACK).seller_type == "private"

    def test_neither_marker_present_stays_empty_not_a_guess(self):
        item = parse_item("x\neBay item number:1\nno marker of either kind")
        assert item.seller_type == ""


class TestStoreJoinedAndItemsSold:
    def test_joined_month_and_year_month_precision_only(self):
        assert _parse(IOUTLET).store_joined == date(2007, 10, 1)

    def test_a_different_real_join_date(self):
        assert _parse(PRIVATE_LOW_FEEDBACK).store_joined == date(2025, 7, 1)

    def test_items_sold_k_abbreviated(self):
        assert _parse(IOUTLET).store_items_sold == 273_000

    def test_items_sold_absent_is_none(self):
        """A seller with 0 lifetime sales renders no "...items sold" bullet
        at all (real page: "100% positive Feedback" with nothing after
        it) -- None, not 0."""
        assert _parse(PRIVATE_LOW_FEEDBACK).store_items_sold is None


class TestSpecifics:
    """Brand / MPN / EAN from the item specifics block."""

    def test_brand(self):
        assert _parse(IOUTLET).brand == "Apple"

    def test_ean_becomes_gtin_digits_only(self):
        item = _parse(SANDS_WITH_QTY)
        assert item.gtin == "0194252430071"
        assert item.gtin.isdigit()

    def test_no_ean_on_the_page_is_an_empty_gtin(self):
        assert _parse(IOUTLET).gtin == ""

    def test_a_multi_value_mpn_is_recorded_verbatim(self):
        """Apple lists several manufacturer part numbers for the same
        colour/storage combination; recorded exactly as printed, comma-
        separated string and all -- this module does not parse or split
        it."""
        assert _parse(STAY_FREE).mpn == "MNGK3B/A, MLPJ3B/A, MLPH3B/A|MLPH3BA"


class TestJunkMpnIsRecordedNotJudged:
    """ebay_item.py never decides whether an MPN is junk -- is_junk_mpn is
    identity.py's (s4), the sole authority on that rule (build contract
    §2.5). "Does Not Apply" is a real captured value that is itself
    uninformative; it is recorded exactly as printed, with no verdict
    attached either way."""

    def test_does_not_apply_is_recorded_verbatim(self):
        assert _parse(IOUTLET).mpn == "Does Not Apply"


class TestListedAt:
    """From the embedded SEMANTIC_DATA "startDate", to the second -- present
    exactly once on all six real pages."""

    def test_a_real_listing_start_date(self):
        item = _parse(IOUTLET)
        assert item.listed_at == datetime(2022, 11, 4, 15, 43, 26, tzinfo=UTC)

    def test_a_different_real_listing_start_date(self):
        item = _parse(SANDS_WITH_QTY)
        assert item.listed_at == datetime(2025, 8, 11, 21, 44, 47, tzinfo=UTC)

    def test_no_html_supplied_is_none(self):
        assert parse_item("x\neBay item number:1").listed_at is None


class TestLeafCategoryId:
    """No numeric category id is anywhere in the rendered text (only
    breadcrumb titles); the source that IS on all six real pages is the
    description-iframe URL's own "category=" query parameter."""

    def test_a_real_leaf_category_id(self):
        assert _parse(IOUTLET).leaf_category_id == "9355"

    def test_every_one_of_the_six_real_pages_agrees(self):
        for key in (IOUTLET, STAY_FREE, PRIVATE_LOW_FEEDBACK,
                    SANDS_WITH_QTY, SANDS_NO_QTY):
            assert _parse(key).leaf_category_id == "9355"


class TestToRowUpdates:
    def test_known_fields_populate(self):
        item = EbayItem(
            item_id="1", sold_qty=5, seller_type="business",
            seller_id="x", seller_feedback_bp=9980, seller_feedback_n=100,
            brand="Apple", mpn="Does Not Apply", gtin="123456789012",
            leaf_category_id="9355", store_items_sold=10,
            store_joined=date(2020, 1, 1),
            listed_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
        updates = to_row_updates(item)
        assert updates["sold_qty"] == 5
        assert updates["seller_type"] == "business"
        assert updates["seller_id"] == "x"
        assert updates["seller_feedback_bp"] == 9980
        assert updates["brand"] == "Apple"
        assert updates["mpn"] == "Does Not Apply"
        assert updates["gtin"] == "123456789012"
        assert updates["leaf_category_id"] == "9355"
        assert updates["store_items_sold"] == 10
        assert updates["store_joined"] == date(2020, 1, 1)
        assert updates["listed_at"] == datetime(2020, 1, 1, tzinfo=UTC)

    def test_unknown_fields_are_simply_absent_not_null(self):
        """A blank EbayItem must not clobber a sweep row's good data with a
        pile of explicit Nones -- the caller does row.update(updates)."""
        assert to_row_updates(EbayItem(item_id="1")) == {}

    def test_an_empty_epid_never_reaches_the_update_dict(self):
        item = EbayItem(item_id="1", epid="")
        assert "epid" not in to_row_updates(item)

    def test_the_floor_flag_rides_in_extra_as_its_own_json_not_a_column(self):
        item = EbayItem(item_id="1", qty_available=10, qty_available_is_floor=True)
        updates = to_row_updates(item)
        assert "qty_available_is_floor" not in updates
        assert json.loads(updates["extra"]) == {"qty_available_is_floor": True}

    def test_real_parsed_items_feed_it_cleanly(self):
        updates = to_row_updates(_parse(IOUTLET))
        assert updates["sold_qty"] == 2858
        assert updates["seller_id"] == "the_ioutlet_extra"
        assert updates["leaf_category_id"] == "9355"
