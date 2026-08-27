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
    """Nokia used to sit here; it is supported now. An unsupported make must
    still yield an empty spec rather than a guess."""
    spec = parse("Blackberry Curve 8520")
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


# --- the unresolved tail, measured live 2026-08-27 ---------------------------
# Every title here was in the 18% the parser could not resolve.

TAIL_CASES = [
    # Samsung written without "samsung" or "galaxy" — my own matcher's gap,
    # and the single biggest group in the tail.
    ("S24 ultra 256GB",                     "samsung", "galaxy-s24-ultra", 256),
    ("A55 5G",                              "samsung", "galaxy-a55",       None),
    ("Z flip 5 256gb",                      "samsung", "galaxy-z-flip-5",  256),
    # Google
    ("Pixel 6a",                            "google",  "pixel-6a",         None),
    ("Google pixel 10",                     "google",  "pixel-10",         None),
    ("Google pixel 11 pro 5G 256GB AI sim-free olive phone",
                                            "google",  "pixel-11-pro",     256),
    # Motorola
    ("Motorola g85",                        "motorola", "moto-g85",        None),
    ("Motorola Moto G35 5G 128GB 4GB Ram Midnight Black",
                                            "motorola", "moto-g35",        128),
    ("Motorola Edge 30 Ultra 256GB 12GB Black",
                                            "motorola", "edge-30-ultra",   256),
    # Oppo
    ("Oppo Find X9 Pro 16/512GB Titanium Charcoal",
                                            "oppo",    "find-x9-pro",      512),
    # Sony
    ("Sony Xperia Z3",                      "sony",    "xperia-z3",        None),
    # Nokia
    ("Nokia 105 dualsim unlocked mobile phone",
                                            "nokia",   "nokia-105",        None),
]


@pytest.mark.parametrize("title,make,model,storage", TAIL_CASES)
def test_unresolved_tail_now_parses(title, make, model, storage):
    spec = parse(title)
    assert spec.make == make, f"make wrong for {title!r}"
    assert spec.model == model, f"model wrong for {title!r}"
    assert spec.storage_gb == storage, f"storage wrong for {title!r}"


@pytest.mark.parametrize("title", [
    "4x Sony Xperia Z3 BUNDLE",
    "Job lot of 3 iPhones",
    "iPhone 13 joblot spares",
    "Bundle of 5 Samsung Galaxy S8",
])
def test_multi_item_lots_are_disqualified(title):
    """A bundle is a different product at a different price. Letting one into a
    bucket would drag the median and make single handsets look overpriced."""
    assert "bundle" in parse(title).disqualifiers


def test_bare_iphone_with_no_model_stays_unresolved():
    """'iPhone' alone is genuinely unparseable — there is no model to price
    against, so refusing is correct rather than a gap."""
    assert parse("iPhone").model is None


def test_makes_do_not_steal_each_others_titles():
    """The bare-Samsung patterns are greedy, so every other make is checked
    first. This pins that ordering."""
    assert parse("Sony Xperia Z3").make == "sony"
    assert parse("Motorola Edge 30 Ultra").make == "motorola"
    assert parse("Google Pixel 8 Pro").make == "google"
    assert parse("iPhone 13 Pro").make == "apple"
    assert parse("Samsung Galaxy S24").make == "samsung"


def test_bare_pro_max_infers_apple():
    """Live title 'locked 17 pro max' never says iPhone. 'Pro Max' is Apple-only
    naming, so a bare generation number plus it is safely an iPhone."""
    assert parse("locked 17 pro max").model == "iphone-17-pro-max"
    assert parse("locked 17 pro max").make == "apple"


def test_bare_pro_max_never_steals_a_branded_title():
    """The fallback runs last. Anything with a real brand token must be claimed
    by its own matcher first."""
    assert parse("Google pixel 11 pro 5G").make == "google"
    assert parse("Oppo Find X9 Pro").make == "oppo"
    assert parse("Samsung Galaxy S24 Ultra").make == "samsung"
    assert parse("Motorola Edge 30 Ultra").make == "motorola"


def test_bare_pro_alone_is_not_enough():
    """Only 'pro max' is Apple-distinctive. Bare 'pro' is used by everyone, so
    inferring from it would misfile other manufacturers into iPhone buckets."""
    assert parse("15 pro").model is None
