"""Parser tests.

Every title below except the synthetic ones was pulled from the live Vinted feed
on 2026-08-27. Real titles are the only fixtures worth testing against — invented
ones are always tidier than reality.
"""

import pytest

from phoneloop.parser import parse

# (title, expected_model, expected_storage, expected_disqualifiers)
LIVE_CASES = [
    ("iPhone 13 128gb Midnight  Excellent Condition", "iphone-13", 128, ()),
    ("Apple iPhone 13 128GB Black Network Unlocked Smartphone", "iphone-13", 128, ()),
    ("iPhone 13 White", "iphone-13", None, ()),
    ("Apple I-Phone 13 Green Unlocked", "iphone-13", None, ()),
    ("iphone 13pro", "iphone-13-pro", None, ()),
    ("Apple IPhone 13 Pro Max 128 GB", "iphone-13-pro-max", 128, ()),
    ("Iphone 13 pro max 256GB", "iphone-13-pro-max", 256, ()),
    ("iPhone 13 , 128GB , Unlocked", "iphone-13", 128, ()),
    ("iPhone 13.Excellent Condition", "iphone-13", None, ()),
    ("apple iphone 13 RED - 128GB - Spares & Repair /", "iphone-13", 128, ("spares_repair",)),
]


@pytest.mark.parametrize("title,model,storage,disq", LIVE_CASES)
def test_live_titles_parse(title, model, storage, disq):
    spec = parse(title)
    assert spec.model == model, f"model wrong for {title!r}"
    assert spec.storage_gb == storage, f"storage wrong for {title!r}"
    assert spec.disqualifiers == disq, f"disqualifiers wrong for {title!r}"


def test_pro_max_not_misread_as_pro():
    """Longest-variant-first matching. If 'pro' matched before 'pro max', every
    Pro Max would be priced against the cheaper Pro bucket and look like a steal."""
    assert parse("iPhone 13 Pro Max 256GB").model == "iphone-13-pro-max"
    assert parse("iPhone 13 Pro 256GB").model == "iphone-13-pro"
    assert parse("iPhone 13 mini 256GB").model == "iphone-13-mini"
    assert parse("iPhone 13 256GB").model == "iphone-13"


@pytest.mark.parametrize("title", [
    "iphone 13 pro max case",
    "iPhone 13 Screen Protector Tempered Glass",
    "Apple iPhone 13 Charger Cable",
    "iPhone 13 Pro silicone cover",
    "iphone 13 battery replacement",
])
def test_accessories_rejected(title):
    """A free-text phone search returns mostly accessories. Anything that isn't a
    handset must yield model=None so it never reaches the pricebook."""
    spec = parse(title)
    assert spec.model is None, f"{title!r} should not parse as a phone"


def test_icloud_lock_found_in_description_only():
    """The worst disqualifier rarely appears in the title. Missing it buys a brick."""
    spec = parse("iPhone 13 128GB Excellent", description="Great phone but it is iCloud locked, sold as seen")
    assert "icloud_locked" in spec.disqualifiers


@pytest.mark.parametrize("text,expected", [
    ("iPhone 13 - spares or repair", "spares_repair"),
    ("iPhone 13 for parts only", "spares_repair"),
    ("iPhone 13, cracked screen", "damaged"),
    ("iPhone 13 faulty battery", "faulty"),
    ("iPhone 13 - Find My is on", "icloud_locked"),
])
def test_each_disqualifier_detected(text, expected):
    assert expected in parse(text).disqualifiers


def test_storage_requires_a_unit():
    """'iPhone 13' must not read 13 as 13GB. Storage only counts with gb/tb attached."""
    assert parse("iPhone 13").storage_gb is None
    assert parse("iPhone 12 Pro").storage_gb is None


def test_implausible_storage_rejected():
    """OCR-ish noise and marketing numbers shouldn't become capacities."""
    assert parse("iPhone 13 7gb").storage_gb is None
    assert parse("iPhone 13 1tb").storage_gb == 1024


def test_network_lock_extracted():
    assert parse("iPhone 13 128GB Unlocked").network_lock == "unlocked"
    assert parse("iPhone 13 128GB locked to EE").network_lock == "ee"
    assert parse("iPhone 13 128GB on Vodafone").network_lock == "vodafone"


def test_unknown_make_returns_empty_spec():
    spec = parse("Nokia 3310 classic")
    assert spec.model is None
    assert spec.make is None


def test_spec_is_hashable_for_bucket_keys():
    """PhoneSpec is used as a dict key in the pricebook, so it must be hashable."""
    a = parse("iPhone 13 128GB")
    b = parse("iPhone 13 128GB")
    assert hash(a.bucket_key()) == hash(b.bucket_key())
    assert a.bucket_key() == b.bucket_key()


# --- Samsung: all titles below are live, from the same 2026-08-27 sample ------

SAMSUNG_CASES = [
    ("Samsung Galaxy S25 - 128gb mint unlocked", "samsung", "galaxy-s25", 128),
    ("Samsung Galaxy s24 ultra", "samsung", "galaxy-s24-ultra", None),
    ("Samsung s24 ultra 256gb", "samsung", "galaxy-s24-ultra", 256),
    ("Samsung galaxy s10", "samsung", "galaxy-s10", None),
    ("Samsung S23", "samsung", "galaxy-s23", None),
    ("Samsung Galaxy S22 Ultra Unlocked Pristine", "samsung", "galaxy-s22-ultra", None),
    ("Samsung  galaxy  s24", "samsung", "galaxy-s24", None),
    ("Samsung Galaxy S21 Plus 256GB", "samsung", "galaxy-s21-plus", 256),
    ("Galaxy Z Flip 4 128gb", "samsung", "galaxy-z-flip-4", 128),
    ("Samsung Galaxy A54 128GB", "samsung", "galaxy-a54", 128),
]


@pytest.mark.parametrize("title,make,model,storage", SAMSUNG_CASES)
def test_samsung_titles_parse(title, make, model, storage):
    spec = parse(title)
    assert spec.make == make, f"make wrong for {title!r}"
    assert spec.model == model, f"model wrong for {title!r}"
    assert spec.storage_gb == storage, f"storage wrong for {title!r}"


def test_samsung_ultra_not_misread_as_base():
    """Same longest-variant-first hazard as iPhone Pro Max: an Ultra priced
    against the base bucket would look like a bargain it isn't."""
    assert parse("Samsung Galaxy S24 Ultra").model == "galaxy-s24-ultra"
    assert parse("Samsung Galaxy S24").model == "galaxy-s24"


def test_samsung_accessory_still_rejected():
    assert parse("Samsung Galaxy S24 Ultra case").model is None


def test_apple_and_samsung_do_not_collide():
    assert parse("iPhone 13 Pro").make == "apple"
    assert parse("Samsung Galaxy S23").make == "samsung"


def test_unspaced_variants_normalised():
    """Live titles omit spaces constantly. These were missing from the first
    fixture set and only surfaced when a patch to _normalise silently failed."""
    assert parse("Samsung Galaxy s24ultra 256gb").model == "galaxy-s24-ultra"
    assert parse("Samsung Galaxy S21+ 128GB").model == "galaxy-s21-plus"
    assert parse("iphone 15promax").model == "iphone-15-pro-max"
