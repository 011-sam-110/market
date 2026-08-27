"""Pricebook tests.

The outlier-resistance guarantee is the one that matters: a single delusional
listing must not move the reference, because the reference is what every buy
decision is measured against.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from phoneloop import config
from phoneloop.parser import parse
from phoneloop.pricebook import Pricebook
from phoneloop.store import Store

CFG = config.load(env={})


def obs(item_id, title, total, condition="Very good"):
    return {
        "item_id": item_id,
        "seen_at": datetime.now(timezone.utc),
        "title": title,
        "price": Decimal(str(total)),
        "total_item_price": Decimal(str(total)),
        "service_fee": Decimal("0"),
        "currency": "GBP",
        "condition": condition,
        "brand_title": "Apple",
        "favourite_count": 0,
        "view_count": 0,
        "photo_count": 3,
        "seller_id": 1,
        "url": "u",
    }


@pytest.fixture
def book(tmp_path):
    return Pricebook(Store(tmp_path), CFG)


def fill(book, title, prices, condition="Very good", start=1000):
    book.store.record([obs(start + i, title, p, condition) for i, p in enumerate(prices)])
    book.refresh()


def test_thin_bucket_returns_none(book):
    fill(book, "iPhone 13 128GB", [200] * (CFG.MIN_BUCKET_SAMPLES - 1))
    assert book.reference(parse("iPhone 13 128GB"), "Very good") is None


def test_bucket_at_threshold_is_priced(book):
    fill(book, "iPhone 13 128GB", [200] * CFG.MIN_BUCKET_SAMPLES)
    assert book.reference(parse("iPhone 13 128GB"), "Very good").median == Decimal("200")


def test_uses_median_not_mean(book):
    """Eight listings at 100 and one delusional at 1000. The mean is 200 and
    would make every genuine listing look like a bargain."""
    fill(book, "iPhone 13 128GB", [100] * 8 + [1000])
    ref = book.reference(parse("iPhone 13 128GB"), "Very good")
    assert ref.median == Decimal("100"), "a single outlier moved the reference"
    assert ref.samples == 9


def test_scam_lowball_does_not_drag_the_median(book):
    """The other direction: cheap fakes must not lower the bar for everyone."""
    fill(book, "iPhone 13 128GB", [200] * 8 + [10, 15])
    assert book.reference(parse("iPhone 13 128GB"), "Very good").median == Decimal("200")


def test_disqualified_listings_excluded_from_buckets(book):
    """A spares-or-repair handset is a different product. Including them would
    drag the median down and make working phones look overpriced."""
    good = [obs(i, "iPhone 13 128GB", 200) for i in range(8)]
    broken = [obs(100 + i, "iPhone 13 128GB spares or repair", 40) for i in range(8)]
    book.store.record(good + broken)
    book.refresh()
    assert book.reference(parse("iPhone 13 128GB"), "Very good").median == Decimal("200")


def test_condition_separates_buckets(book):
    book.store.record(
        [obs(i, "iPhone 13 128GB", 200, "Very good") for i in range(8)]
        + [obs(100 + i, "iPhone 13 128GB", 120, "Satisfactory") for i in range(8)]
    )
    book.refresh()
    assert book.reference(parse("iPhone 13 128GB"), "Very good").median == Decimal("200")
    assert book.reference(parse("iPhone 13 128GB"), "Satisfactory").median == Decimal("120")


def test_null_storage_prices_against_lowest_tier(book):
    """55% of live listings omit storage. They price against the cheapest tier,
    so their discount reads smaller than it may be — erring toward missing a
    deal rather than buying a disappointment."""
    book.store.record(
        [obs(i, "iPhone 13 128GB", 200) for i in range(8)]
        + [obs(100 + i, "iPhone 13 256GB", 300) for i in range(8)]
    )
    book.refresh()
    spec = parse("iPhone 13 White")
    assert spec.storage_gb is None
    ref = book.reference(spec, "Very good")
    assert ref.median == Decimal("200"), "must use the 128GB tier, not the 256GB one"
    assert ref.inferred_from_lowest_tier is True


def test_exact_storage_match_is_not_flagged_as_inferred(book):
    fill(book, "iPhone 13 128GB", [200] * 8)
    assert book.reference(parse("iPhone 13 128GB"), "Very good").inferred_from_lowest_tier is False


def test_unknown_model_has_no_reference(book):
    fill(book, "iPhone 13 128GB", [200] * 8)
    assert book.reference(parse("iPhone 15 Pro Max 512GB"), "Very good") is None


def test_non_phone_has_no_reference(book):
    fill(book, "iPhone 13 128GB", [200] * 8)
    assert book.reference(parse("iphone 13 case"), "Very good") is None


def test_mad_reflects_spread(book):
    fill(book, "iPhone 13 128GB", [190, 195, 200, 200, 200, 205, 210, 200])
    ref = book.reference(parse("iPhone 13 128GB"), "Very good")
    assert ref.median == Decimal("200")
    assert ref.mad > 0, "a spread of 190-210 should not report zero deviation"


def test_stats_orders_by_sample_density(book):
    book.store.record(
        [obs(i, "iPhone 13 128GB", 200) for i in range(20)]
        + [obs(100 + i, "iPhone 12 128GB", 150) for i in range(8)]
    )
    book.refresh()
    rows = book.stats()
    assert rows[0]["model"] == "iphone-13"
    assert rows[0]["samples"] == 20
