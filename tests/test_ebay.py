"""eBay row-parsing tests.

Every fixture below is copied verbatim from a live eBay UK search on
2026-08-27. That rule is not decoration here -- this project has already
shipped a photo counter that returned zero for every real listing because its
fixture claimed to be real and was invented.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

import phoneloop.market_store as market_store
from phoneloop.ebay import (
    DECOY_ITEM_IDS,
    EbayListing,
    expand_abbreviated_count,
    is_decoy,
    looks_geolocated_abroad,
    pct_to_bp,
    uk_share,
    parse_row,
    parse_rows,
    retail_ceiling,
    saturation,
    to_pence,
    to_row,
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

# --- rows recorded through scripts/record_ebay.py, live, 2026-08-27 --------
#
# The fixtures above predate this build; everything below was captured for it
# and lives on disk (gzipped) under PHONELOOP_FIXTURES as
# cassette "ebay" / key "search-iphone-13-128gb-p1-...". These six are copied
# out verbatim rather than read through the harness in every test, the same
# way the fixtures above are: no test here should depend on the fixtures
# directory being present, and the values are pinned so a change shows up as
# a diff instead of a different day's search results.

SOLD_AND_PROMOTED_ROW = "Apple iPhone 13 128GB/256/512 - ALL COLOURS - UNLOCKED - GOOD CONDITION\nOpens in a new window or tab\n12 Months Warranty - Fast & Free Delivery - Unlocked\nGood - Refurbished\n4.5 out of 5 stars.\n76 product ratings\n- Apple iPhone 13 128GB/256/512 - ALL COLOURS - UNLOCKED - GOOD CONDITION\n£209.99£779.99(£209.99/Unit)\nBuy It Now\n+£30.00 delivery\n552+ sold\nthe_ioutlet_extra 99.9% positive (76.5K)\nRefurbished\nderosnopS"
SOLD_AND_PROMOTED_ROW_URL = "https://www.ebay.co.uk/itm/225236871939?_skw=iphone+13+128gb&epid=12049274306&itmmeta=01M12MQ4NJYKAQQ4FHQX5CMP67&hash=item347129eb03:g:eSUAAOSwVrljZS8p"

ALMOST_GONE_ROW = "Apple iPhone 13 / 128GB /Midnight Color/Unlocked Excellent Condition ✅ A++ Grade\nOpens in a new window or tab\nPre-owned · Apple iPhone 13 · Unlocked · 128 GB\n4.5 out of 5 stars.\n12 product ratings\n- Apple iPhone 13 / 128GB /Midnight Color/Unlocked Excellent Condition ✅ A++ Grade\n£189.99\nBuy It Now\n+ £17.52 postage estimate\nAlmost gone\n76 sold\nCustoms services and international tracking provided\nsands-phones 99.8% positive (1K)\nderosnopS"
ALMOST_GONE_ROW_URL = "https://www.ebay.co.uk/itm/406124792115?_skw=iphone+13+128gb&epid=8050688139&itmmeta=01M12MQ4NJ3HAKCRGBJV4WKYR7&hash=item5e8eec8933:g:6SEAAOSwciFnfYaD"

LAST_ONE_ROW = "NEW LOW PRICE\nApple iPhone 13 128GB Blue Unlocked\nOpens in a new window or tab\nPre-owned · Apple iPhone 13 · Unlocked · 128 GB\n4.5 out of 5 stars.\n76 product ratings\n- Apple iPhone 13 128GB Blue Unlocked\n£198.30\nor Best Offer\n+£20.00 delivery\nLast one\n5 watchers\nkeaah0 100% positive (8)\nderosnopS"
LAST_ONE_ROW_URL = "https://www.ebay.co.uk/itm/128015361583?_skw=iphone+13+128gb&epid=12049274306&itmmeta=01M12MQ4NMXCND8FFM68F0EW9N&hash=item1dce4f662f:g:Ri8AAeSwCQJqdcFA"

ZERO_FEEDBACK_ROW = "Apple iPhone 13 128GB White Unlocked 5.4in iOS 12MP 5G Smartphone\nOpens in a new window or tab\nPre-owned · Apple iPhone 13 · Unlocked · 128 GB\n5.0 out of 5 stars.\n2 product ratings\n- Apple iPhone 13 128GB White Unlocked 5.4in iOS 12MP 5G Smartphone\n£200.38\nor Best Offer\n+£4.50 delivery\nreshi-1723 0% positive (0)\nderosnopS"
ZERO_FEEDBACK_ROW_URL = "https://www.ebay.co.uk/itm/137649672983?_skw=iphone+13+128gb&epid=6052543130&itmmeta=01M12MQ4NNAG37PWQDNTDX68DA&hash=item200c8f5317:g:xnAAAeSwmX1qiao1"

# China is the second most common ships_from on the 528 real rows (27, after
# United States' 49) -- a real "from China" row, for the origin_country
# mapping guard below.
CHINA_ORIGIN_ROW = "IPhone 11 6.1'' GSM CDMA 64GB/128GB/256GB 12MP Mobile Phone Unlocked\nOpens in a new window or tab\nPre-owned\n5.0 out of 5 stars.\n4 product ratings\n- IPhone 11 6.1'' GSM CDMA 64GB/128GB/256GB 12MP Mobile Phone Unlocked\n£165.00 to £210.00(£165.00/Unit)\nBuy It Now\n+£9.99 delivery\nfrom China\ndollmascot 98.8% positive (1.7K)\nderosnopS"
CHINA_ORIGIN_ROW_URL = "https://www.ebay.co.uk/itm/126637082291?_skw=iphone+13+128gb&epid=11034720500&itmmeta=01M12NBZRZPMW5XNF4RDZ1EV0F&hash=item1d7c2886b3:g:z0EAAOSwjQ5mxiHG"

# The percentage-then-count ordering is the contract's documented shape (409
# of 528 real rows, measured); this one carries the reverse ordering, seen on
# 65 of the same 528.
ALT_SELLER_ORDER_ROW = "Redmi\nREDMI 10 Power 128 GB 8 GB RAM Orange-Factory Unlocked Phone-NO TARIFF TO USA\nOpens in a new window or tab\nBrand new\n£114.75\nLast one\none.stop.shoppee (3,503) 98.6%\n+£25.51 delivery\nderosnopS"
ALT_SELLER_ORDER_ROW_URL = "https://www.ebay.co.uk/itm/177815541337?_skw=iphone+13+128gb&itmmeta=01M12MQ4PVC9YK3R81ZD0QQ3BW&hash=item2966a1d659:g:C8IAAeSwJjtpJXa-"

# A "people also viewed"-style row with no seller line at all -- a real
# absence (54 of 528 real rows), not a parse failure.
NO_SELLER_LINE_ROW = "Apple iPhone 13 128GB 256GB 512GB Unlocked Refurb Smartphone Excellent Condition\nOpens in a new window or tab\nExcellent - Refurbished\n£309.89\n+ £21.66 postage estimate\nRefurbished\nderosnopS"
NO_SELLER_LINE_ROW_URL = "https://www.ebay.co.uk/itm/389156180455?_skw=iphone+13+128gb&epid=15049284953&itmmeta=01M12MQ4PVXWCMWPDWYZ3QMZGD&hash=item5a9b8411e7:g:aRUAAOSwDMJnZZ4N"


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
    def _pool(self, n=7, base=20000, step=500):
        return [
            EbayListing(f"iPhone 13 {i}", base + i * step, 0,
                        condition="Pre-owned", storage_gb=128)
            for i in range(n)
        ]

    def test_median_of_landed_uk_prices(self):
        c = retail_ceiling(self._pool(), storage_gb=128)
        assert c.samples == 7
        assert c.median_pence == 21500

    def test_overseas_listings_are_excluded_and_counted(self):
        """They skew cheap, which makes them look like the bargains, and they
        carry customs, VAT and weeks of delivery."""
        pool = self._pool() + [
            EbayListing("cheap import", 9000, 0, condition="Pre-owned",
                        storage_gb=128, ships_from="Germany")
        ]
        c = retail_ceiling(pool, storage_gb=128)
        assert c.overseas_excluded == 1
        assert c.median_pence == 21500, "the import must not drag the median"

    def test_listings_with_unknown_postage_are_excluded_and_counted(self):
        pool = self._pool() + [
            EbayListing("no postage", 15000, None, condition="Pre-owned",
                        storage_gb=128)
        ]
        c = retail_ceiling(pool, storage_gb=128)
        assert c.unpriceable_excluded == 1
        assert c.samples == 7

    def test_a_different_capacity_is_not_comparable(self):
        pool = self._pool() + [
            EbayListing("512GB", 40000, 0, condition="Pre-owned", storage_gb=512)
        ]
        assert retail_ceiling(pool, storage_gb=128).samples == 7

    def test_too_few_samples_is_none_not_a_number(self):
        """Two listings are two people's opinions, not a market rate."""
        assert retail_ceiling(self._pool(n=5), storage_gb=128) is None
        assert retail_ceiling([], storage_gb=128) is None

    def test_an_outlier_does_not_move_the_median(self):
        pool = self._pool() + [
            EbayListing("mispriced", 150000, 0, condition="Pre-owned",
                        storage_gb=128)
        ]
        assert retail_ceiling(pool, storage_gb=128).median_pence == 21750


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
            for i in range(7)
        ] + [
            EbayListing("iPhone 13 mini", 9000, 0, condition="Pre-owned",
                        aspects=("Pre-owned", "Apple iPhone 13 mini", "128 GB"),
                        storage_gb=128)
        ]
        c = retail_ceiling(pool, storage_gb=128, model="iphone-13")
        assert c.variant_excluded == 1
        assert c.samples == 7
        assert c.median_pence == 22500, "the mini must not drag the median"

    def test_uncatalogued_rows_are_dropped_when_a_model_is_demanded(self):
        """Roughly two thirds of a live page has no catalogue line. Assuming
        those match would readmit exactly the contamination this prevents."""
        pool = [
            EbayListing(f"iPhone 13 {i}", 21000, 0, condition="Pre-owned",
                        aspects=("Pre-owned", "Apple iPhone 13"), storage_gb=128)
            for i in range(6)
        ] + [
            EbayListing("no aspects", 5000, 0, condition="Pre-owned",
                        storage_gb=128)
        ]
        c = retail_ceiling(pool, storage_gb=128, model="iphone-13")
        assert c.variant_excluded == 1
        assert c.samples == 6

    def test_without_a_model_nothing_is_excluded(self):
        """Back-compatible: the filter only applies when asked for."""
        pool = [
            EbayListing(f"x{i}", 20000, 0, condition="Pre-owned", storage_gb=128)
            for i in range(6)
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


class TestPoolPurity:
    """Two guards against a ceiling that describes no real handset.

    Both were added after live data produced an iPhone 11 128GB ceiling of
    28800 while the model-wide used figure for the same phone was 12316. The
    pool was five listings, some of them sealed retail stock.
    """

    def test_new_other_is_not_a_used_comparable(self):
        """eBay applies "New (other)" to sealed and shop-soiled retail stock.
        It prices like new, and pooling it inflates the ceiling -- which is the
        direction that loses money."""
        assert parse_row(OVERSEAS).is_used is False
        assert parse_row(UK_USED).is_used is True

    def test_a_scattered_pool_has_no_median_worth_reporting(self):
        """These seven prices range over 5x. Something in the pool is not the
        handset being priced, and reporting their midpoint would launder that
        into a number that looks authoritative."""
        pool = [
            EbayListing(f"x{i}", p, 0, condition="Pre-owned", storage_gb=128)
            for i, p in enumerate([9000, 11000, 15000, 20000, 28000, 41000, 46000])
        ]
        assert retail_ceiling(pool, storage_gb=128) is None

    def test_a_tight_pool_still_reports(self):
        pool = [
            EbayListing(f"x{i}", p, 0, condition="Pre-owned", storage_gb=128)
            for i, p in enumerate([20500, 21000, 21200, 21500, 21800, 22000, 22400])
        ]
        c = retail_ceiling(pool, storage_gb=128)
        assert c is not None
        assert c.median_pence == 21500

    def test_six_is_the_floor(self):
        def pool(n):
            return [EbayListing(f"x{i}", 21000 + i * 100, 0,
                                condition="Pre-owned", storage_gb=128)
                    for i in range(n)]
        assert retail_ceiling(pool(5), storage_gb=128) is None
        assert retail_ceiling(pool(6), storage_gb=128) is not None


class TestEgressGeography:
    """A UK ceiling is only meaningful if the search was answered as a UK user.

    Captured through a rotating residential gateway, which takes a fresh exit
    per navigation, the UK share swung between queries inside one run: 79 of 88
    for one handset, 28 of 84 for the next. eBay reorders results by shipping
    proximity, so a query answered from Hong Kong returns a thin and
    unrepresentative UK pool. Nothing downstream can see that -- the ceiling
    comes out looking perfectly ordinary.
    """

    def _pool(self, uk, abroad):
        return (
            [EbayListing(f"uk{i}", 20000, 0, condition="Pre-owned")
             for i in range(uk)]
            + [EbayListing(f"ab{i}", 9000, 0, condition="Pre-owned",
                           ships_from="Hong Kong") for i in range(abroad)]
        )

    def test_a_uk_served_page_passes(self):
        assert uk_share(self._pool(79, 9)) > 0.55
        assert looks_geolocated_abroad(self._pool(79, 9)) is False

    def test_the_real_bad_capture_is_caught(self):
        """iPhone 13 Pro: 28 UK of 84 rows, captured via a non-UK exit."""
        assert looks_geolocated_abroad(self._pool(28, 56)) is True

    def test_an_empty_result_set_is_not_evidence_of_geolocation(self):
        """Nothing matched is a different problem, and conflating them would
        send someone to debug the proxy over an unstocked query."""
        assert looks_geolocated_abroad([]) is False
        assert uk_share([]) == 0.0

    def test_the_boundary_is_inclusive_of_the_threshold(self):
        assert looks_geolocated_abroad(self._pool(55, 45)) is False
        assert looks_geolocated_abroad(self._pool(54, 46)) is True


class TestShipsFromIsNotAPriceLine:
    """A multi-variant listing opens with "from £107.98", not a country.

    The first version of _FROM_RE matched any line starting with "from", so a
    UK listing with variant pricing was recorded as shipping from
    "£107.98 with coupon" and dropped out of the UK pool. That is the
    expensive direction: it discards good comps and shrinks exactly the sample
    the ceiling is computed from. Verbatim from a live eBay UK page,
    2026-08-27.
    """

    PRICE_LINE = (
        "Apple iPhone 13 128GB Unlocked Smartphone Various Colours\n"
        "Pre-owned \u00b7 Apple iPhone 13 \u00b7 Unlocked \u00b7 128 GB\n"
        "from \u00a3107.98 with coupon\n"
        "Free postage\n"
    )

    OVERSEAS = (
        "Apple iPhone 13 128GB Unlocked Smartphone\n"
        "Pre-owned \u00b7 Apple iPhone 13 \u00b7 Unlocked \u00b7 128 GB\n"
        "\u00a3189.99\n"
        "from Germany\n"
        "+ \u00a312.99 postage\n"
    )

    def test_a_from_price_line_is_not_a_shipping_country(self):
        listing = parse_row(self.PRICE_LINE)
        assert listing is not None
        assert listing.ships_from == ""
        assert listing.is_uk is True

    def test_a_real_origin_is_still_read(self):
        """The fix must not blind the parser to genuine overseas sellers --
        they skew cheap, look like the bargains, and carry customs and VAT."""
        listing = parse_row(self.OVERSEAS)
        assert listing is not None
        assert listing.ships_from == "Germany"
        assert listing.is_uk is False


class TestDecoy:
    """eBay's search results page renders a hidden template <li> ahead of the
    real rows, pointing at /itm/123456 -- nothing in ITS TEXT marks it as a
    decoy (it parses like a perfectly good row), only the item id does. None
    of the 528 real rows this build recorded (2026-08-27) were a decoy: the
    JS extraction those rows came through already requires an /itm/ href with
    real digits, so a genuine decoy row was never itself captured. This pairs
    a real row's own text with the documented decoy id (build contract §2.4)
    to prove the id alone is what triggers the drop, regardless of content.
    """

    def test_the_hidden_template_row_is_dropped(self):
        assert parse_row(SOLD_AND_PROMOTED_ROW,
                          "https://www.ebay.co.uk/itm/123456?_skw=x") is None

    def test_is_decoy_reads_the_item_id_not_the_whole_url(self):
        assert is_decoy("https://www.ebay.co.uk/itm/123456") is True
        assert is_decoy("https://www.ebay.co.uk/itm/123456?foo=bar") is True
        assert is_decoy("https://www.ebay.co.uk/itm/225236871939") is False
        assert is_decoy("") is False
        assert "123456" in DECOY_ITEM_IDS


class TestSoldCount:
    def test_a_plus_suffixed_count_drops_the_plus_keeps_the_digits(self):
        row = parse_row(SOLD_AND_PROMOTED_ROW, SOLD_AND_PROMOTED_ROW_URL)
        assert row.sold_qty == 552

    def test_a_row_with_no_sold_count_is_none_not_zero(self):
        row = parse_row(UK_USED)
        assert row.sold_qty is None


class TestSellerLine:
    """Two orderings were measured live across the 528 rows this build
    recorded, 2026-08-27: "name pct% positive (n)" -- the build contract's
    documented shape -- on 409 of them, and "name (n) pct%" on 65. The
    remaining 54 carry no seller line at all."""

    def test_the_documented_ordering(self):
        row = parse_row(SOLD_AND_PROMOTED_ROW, SOLD_AND_PROMOTED_ROW_URL)
        assert row.seller_id == "the_ioutlet_extra"
        assert row.seller_feedback_bp == 9990
        assert row.seller_feedback_n == 76500

    def test_zero_percent_positive_is_a_real_zero_not_unknown(self):
        row = parse_row(ZERO_FEEDBACK_ROW, ZERO_FEEDBACK_ROW_URL)
        assert row.seller_id == "reshi-1723"
        assert row.seller_feedback_bp == 0
        assert row.seller_feedback_n == 0

    def test_the_reversed_ordering(self):
        row = parse_row(ALT_SELLER_ORDER_ROW, ALT_SELLER_ORDER_ROW_URL)
        assert row.seller_id == "one.stop.shoppee"
        assert row.seller_feedback_bp == 9860
        assert row.seller_feedback_n == 3503

    def test_no_seller_line_is_empty_not_a_crash(self):
        row = parse_row(NO_SELLER_LINE_ROW, NO_SELLER_LINE_ROW_URL)
        assert row is not None
        assert row.seller_id == ""
        assert row.seller_feedback_bp is None
        assert row.seller_feedback_n is None


class TestAbbreviatedCounts:
    def test_k_expansion(self):
        assert expand_abbreviated_count("74.6K") == 74600
        assert expand_abbreviated_count("1K") == 1000

    def test_m_expansion(self):
        assert expand_abbreviated_count("13.9M") == 13_900_000

    def test_a_plain_comma_count_needs_no_abbreviation(self):
        assert expand_abbreviated_count("3,503") == 3503

    def test_percent_to_basis_points(self):
        assert pct_to_bp("99.8") == 9980
        assert pct_to_bp("100") == 10000
        assert pct_to_bp("0") == 0


class TestPromoted:
    """eBay renders "Sponsored" reversed ("derosnopS") so it cannot be
    selected as page text, but it is still there in the DOM's innerText."""

    def test_the_reversed_marker_is_read(self):
        assert parse_row(SOLD_AND_PROMOTED_ROW, SOLD_AND_PROMOTED_ROW_URL).promoted is True

    def test_a_promoted_row_is_kept_not_dropped(self):
        row = parse_row(SOLD_AND_PROMOTED_ROW, SOLD_AND_PROMOTED_ROW_URL)
        assert row is not None
        assert row.price_pence == 20999

    def test_a_row_without_the_marker_is_not_promoted(self):
        """Every one of the 528 real rows this build captured, 2026-08-27,
        carried the marker (see the module docstring in phoneloop/ebay.py) --
        so the promoted=False case is the pre-existing fixtures above,
        captured the same day without it."""
        assert parse_row(UK_USED).promoted is False
        assert parse_row(UK_WATCHED).promoted is False


class TestStockHint:
    def test_almost_gone(self):
        assert parse_row(ALMOST_GONE_ROW, ALMOST_GONE_ROW_URL).stock_hint == "Almost gone"

    def test_last_one(self):
        assert parse_row(LAST_ONE_ROW, LAST_ONE_ROW_URL).stock_hint == "Last one"

    def test_no_hint_is_an_empty_string(self):
        assert parse_row(UK_USED).stock_hint == ""


class TestWatchersWithAPlus:
    def test_a_plus_suffixed_watcher_count_is_read(self):
        """"48+ watchers" (measured live, 2026-08-27) read as 0 watchers
        before this fix: the original pattern required whitespace
        immediately after the digits, and did not expect a "+"."""
        assert parse_row("x\n£10.00\n48+ watchers").watchers == 48


class TestFreeDeliveryBannerIsNotAChargeLine:
    """A live listing template (the_ioutlet_extra's, measured 2026-08-27, 11
    of the 528 real rows this build recorded) opens with a marketing banner
    -- "12 Months Warranty - Fast & Free Delivery - Unlocked" -- ahead of a
    REAL "+£30.00 delivery" charge three lines later. Matching "free" anywhere
    in the row (the pre-existing behaviour) found the banner first and booked
    the charge as 0 on all 11; a genuine free-delivery statement always opens
    its own line, so the fix anchors the match to the start of a line.
    """

    def test_the_banner_does_not_shadow_the_real_charge(self):
        row = parse_row(SOLD_AND_PROMOTED_ROW, SOLD_AND_PROMOTED_ROW_URL)
        assert row.delivery_pence == 3000
        assert row.landed_pence == 23999

    def test_a_genuine_free_postage_line_is_still_zero(self):
        assert parse_row(FREE_POST).delivery_pence == 0


# None of the 528 real rows this build captured, 2026-08-27, carried a
# delivery-ETA line at all -- eBay did not show one anywhere on this sweep.
# These four rows are hand-typed against the three ETA phrasings the design
# spec and build contract document verbatim (contract §2.4), not lifted from
# a page recording -- named SYNTHETIC_* per the standing rule; see
# unverified[] in the build report.
SYNTHETIC_ETA_IN_N_DAYS_ROW = "x\n£10.00\nFree delivery in 3 days"
SYNTHETIC_ETA_HYPHEN_RANGE_ROW = "x\n£10.00\nEstimated delivery 3-5 days"
SYNTHETIC_ETA_EN_DASH_RANGE_ROW = "x\n£10.00\nEstimated delivery 3–5 days"
SYNTHETIC_ETA_GET_IT_BY_ROW = "x\n£10.00\nGet it by Tue 1 Sep"
SYNTHETIC_ETA_GET_IT_BY_NEW_YEAR_ROW = "x\n£10.00\nGet it by Thu 1 Jan"


class TestEta:
    """None of the 528 real rows this build captured, 2026-08-27, carried a
    delivery-ETA line at all -- eBay did not show one anywhere on this sweep.
    These exercise the parser directly against the three phrasings the
    design spec and build contract document verbatim (contract §2.4), not
    against a page recording; see unverified[] in the build report."""

    def test_synthetic_in_n_days(self):
        assert parse_row(SYNTHETIC_ETA_IN_N_DAYS_ROW).eta_days == 3

    def test_synthetic_a_hyphen_range_takes_the_maximum(self):
        assert parse_row(SYNTHETIC_ETA_HYPHEN_RANGE_ROW).eta_days == 5

    def test_synthetic_an_en_dash_range_also_works(self):
        assert parse_row(SYNTHETIC_ETA_EN_DASH_RANGE_ROW).eta_days == 5

    def test_synthetic_get_it_by_with_no_reference_date_is_none(self):
        """parse_row must not read the clock itself."""
        assert parse_row(SYNTHETIC_ETA_GET_IT_BY_ROW).eta_days is None

    def test_synthetic_get_it_by_with_a_reference_date(self):
        row = parse_row(SYNTHETIC_ETA_GET_IT_BY_ROW, today=date(2026, 8, 27))
        assert row.eta_days == 5

    def test_synthetic_get_it_by_rolls_into_next_year(self):
        """1 Jan relative to a late-December "today" is next year, not a
        negative day count."""
        row = parse_row(SYNTHETIC_ETA_GET_IT_BY_NEW_YEAR_ROW, today=date(2026, 12, 28))
        assert row.eta_days == 4

    def test_no_eta_line_is_none(self):
        assert parse_row(UK_USED).eta_days is None


class TestParseRowsPassesTodayThrough:
    def test_synthetic_today_reaches_every_row(self):
        rows = [{"text": SYNTHETIC_ETA_GET_IT_BY_ROW}]
        listings = parse_rows(rows, today=date(2026, 8, 27))
        assert listings[0].eta_days == 5


class TestToRow:
    """`to_row` is built from market_store.empty_row() (build contract §2.4).

    build/s1_store_fees -- the sibling branch that defines the real
    ROW_FIELDS / empty_row() -- lands before s2 only at integration (contract
    §6.1); this worktree builds in parallel with it and does not merge it.
    So this monkeypatches phoneloop.market_store with a literal reproduction
    of the contract's §1.1 column list, verifying to_row's OWN mapping logic
    now. The real cross-check against s1's actual empty_row() is
    integration's job, not this stream's.
    """

    # Build contract §1.1, the 37 columns, in schema order.
    ROW_FIELDS = (
        "source", "source_id", "seen_at", "category", "shard", "title",
        "price_pence", "total_pence", "currency", "condition", "brand",
        "model", "storage_gb", "grade", "kind", "available", "region",
        "seller_id", "url", "extra", "content_hash",
        "sold_qty", "qty_available", "seller_type", "seller_feedback_bp",
        "seller_feedback_n", "listed_at", "epid", "gtin", "mpn",
        "identity_tier", "origin_country", "promoted", "leaf_category_id",
        "eta_days", "store_joined", "store_items_sold",
    )

    @staticmethod
    def _empty_row(source, source_id):
        row = {name: None for name in TestToRow.ROW_FIELDS}
        row["source"] = source
        row["source_id"] = str(source_id)
        return row

    @pytest.fixture(autouse=True)
    def _stub_market_store(self, monkeypatch):
        monkeypatch.setattr(market_store, "empty_row", self._empty_row, raising=False)
        monkeypatch.setattr(market_store, "ROW_FIELDS", self.ROW_FIELDS, raising=False)

    def test_the_output_has_exactly_the_row_fields_keys(self):
        listing = parse_row(SOLD_AND_PROMOTED_ROW, SOLD_AND_PROMOTED_ROW_URL)
        row = to_row(listing, category="phones", shard="iphone-13-128gb/p1")
        assert set(row) == set(self.ROW_FIELDS)

    def test_a_real_row_maps_onto_the_right_columns(self):
        listing = parse_row(SOLD_AND_PROMOTED_ROW, SOLD_AND_PROMOTED_ROW_URL)
        row = to_row(listing, category="phones", shard="s", leaf_category_id="9355")
        assert row["source"] == "ebay"
        assert row["source_id"] == "225236871939"
        assert row["price_pence"] == 20999
        assert row["total_pence"] == 23999
        assert row["currency"] == "GBP"
        assert row["kind"] == "ask"
        assert row["available"] is True
        assert row["sold_qty"] == 552
        assert row["seller_id"] == "the_ioutlet_extra"
        assert row["seller_feedback_bp"] == 9990
        assert row["seller_feedback_n"] == 76500
        assert row["promoted"] is True
        assert row["epid"] == "12049274306"
        assert row["identity_tier"] == "catalogued"
        assert row["leaf_category_id"] == "9355"
        assert row["origin_country"] == "GB"
        assert row["region"] == "GB"

    def test_the_url_has_its_query_string_stripped(self):
        listing = parse_row(SOLD_AND_PROMOTED_ROW, SOLD_AND_PROMOTED_ROW_URL)
        row = to_row(listing, category="phones", shard="s")
        assert row["url"] == "https://www.ebay.co.uk/itm/225236871939"

    def test_stock_hint_rides_in_extra_it_is_not_a_column(self):
        listing = parse_row(ALMOST_GONE_ROW, ALMOST_GONE_ROW_URL)
        row = to_row(listing, category="phones", shard="s")
        assert "stock_hint" not in row
        assert json.loads(row["extra"])["stock_hint"] == "Almost gone"

    def test_an_overseas_listing_is_not_gb(self):
        listing = parse_row(OVERSEAS)
        row = to_row(listing, category="phones", shard="s")
        assert row["region"] is None
        assert row["origin_country"] == "US"

    def test_a_china_origin_listing_maps_to_cn(self):
        """China is the second most common origin on the real recorded
        pages (27 of 528 rows) -- the column exists precisely so a
        China-sourced dropshipper can see China-shipped competitors (build
        contract §1.1, col 32), so this name must never silently return
        None."""
        listing = parse_row(CHINA_ORIGIN_ROW, CHINA_ORIGIN_ROW_URL)
        row = to_row(listing, category="phones", shard="s")
        assert row["origin_country"] == "CN"

    def test_a_row_with_no_epid_has_no_identity_tier(self):
        listing = parse_row(UK_USED)
        row = to_row(listing, category="phones", shard="s")
        assert row["epid"] is None
        assert row["identity_tier"] is None

    def test_leaf_category_id_is_a_caller_supplied_passthrough(self):
        """The row parser never sees a category id -- it comes from the
        sweep's own shard, not from anything on the page."""
        listing = parse_row(UK_USED)
        assert to_row(listing, category="phones", shard="s")["leaf_category_id"] is None
        assert to_row(listing, category="phones", shard="s",
                      leaf_category_id="139971")["leaf_category_id"] == "139971"
