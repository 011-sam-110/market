"""Collapse listings into PRODUCTS, so competition and demand can be measured.

A store keyed on listings answers "what is for sale". It cannot answer the
question this project actually exists for -- *what sells well that few people
are selling* -- because both halves of that are per-product measures:

    demand      how many of this product sell, and how fast
    competition how many distinct sellers and live listings this product has

Five thousand phone listings collapse to a few hundred products. Until that
collapse exists on disk, the competition axis cannot be computed at all.

**The key is deliberately coarse.** `iPhone 13 128GB` and `iPhone 13 256GB` are
one product here, not two. A key that splits on every attribute fragments the
population until every product has one seller and the competition signal
becomes noise. Capacity is kept as a separate `variant` field so a narrower
join is still possible where the sample supports it.

**Brand and model must agree, or the model is dropped.** This is the guard that
matters. The phone parser slugged "Smartphone Oppo a5 2020" as `galaxy-a5` --
a Samsung slug on an OPPO handset -- and a live sample of 25 rows contained two
such collisions. Pricing an OPPO A5 against Galaxy A5 comps is the same
variant-contamination failure that has already cost this project two wrong
answers (an iPhone 12 ceiling overstated 28%, and a CeX table where grade C
priced above grade A). An impossibility is the cheapest detector there is, so
it is checked rather than hoped for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Slug prefixes that imply a manufacturer. If the source states a different
# brand, the two disagree and the slug is not trustworthy for that listing.
SLUG_BRAND = {
    "iphone": "apple",
    "ipad": "apple",
    "macbook": "apple",
    "galaxy": "samsung",
    "pixel": "google",
    "redmi": "xiaomi",
    "oneplus": "oneplus",
}

# Brands that legitimately appear under another brand's name in the wild.
# Redmi and POCO are Xiaomi sub-brands; a listing naming either is not in
# conflict with the other.
BRAND_ALIASES = {
    "redmi": "xiaomi",
    "poco": "xiaomi",
    "mi": "xiaomi",
    "oppo": "oppo",
    "realme": "oppo",
    "honor": "honor",
    "moto": "motorola",
    "hp": "hp",
    "hewlett-packard": "hp",
}

# Words that describe the listing rather than the product. Stripping them is
# what lets "iPhone 13 Pro 128GB Graphite Excellent Condition Boxed" and
# "Apple iPhone 13 Pro - unlocked, 128gb" reach the same key.
_NOISE = frozenset("""
new used boxed unboxed unlocked sim free simfree genuine original authentic
excellent very good great mint pristine perfect immaculate fair poor faulty
spares repairs parts working condition tested fully refurbished refurb grade
with without includes included box case cover charger cable bundle set
fast free postage delivery shipping ship uk gb royal mail next day
black white silver grey gray gold rose blue red green purple pink yellow
graphite midnight starlight space sierra pacific titanium natural desert
cheap bargain rare vintage retro job lot joblot bulk wholesale
please read description see photos pics photo picture ltd limited
smartphone phone mobile handset android ios
""".split())

_TOKEN = re.compile(r"[a-z0-9]+")
_HAS_DIGIT = re.compile(r"\d")
# Capacity anywhere in the title: 128gb, 1tb, 512 gb.
_CAPACITY = re.compile(r"\b(\d{1,4})\s*(gb|tb)\b", re.I)


def normalise_brand(raw: str | None) -> str:
    """Lowercase, alias-folded brand. Empty string when unknown."""
    if not raw:
        return ""
    b = re.sub(r"[^a-z0-9 ]", "", raw.strip().lower()).strip()
    if not b:
        return ""
    first = b.split()[0]
    return BRAND_ALIASES.get(first, first)


def brand_of_slug(slug: str | None) -> str:
    """The manufacturer a model slug implies, or '' if it implies none."""
    if not slug:
        return ""
    head = slug.split("-")[0]
    return SLUG_BRAND.get(head, "")


def brands_conflict(stated: str, slug: str | None) -> bool:
    """Does the source's brand contradict the model slug's manufacturer?

    Only a conflict when BOTH are known. An unknown brand is not evidence
    against a slug, and treating it as one would discard most of the feed.
    """
    implied = brand_of_slug(slug)
    if not implied or not stated:
        return False
    return normalise_brand(stated) != implied


def capacity_of(title: str) -> str:
    """Storage as a normalised variant string: '128gb', '1tb', or ''."""
    m = _CAPACITY.search(title or "")
    if not m:
        return ""
    return f"{int(m.group(1))}{m.group(2).lower()}"


def family_tokens(title: str, brand: str, limit: int = 3) -> tuple[str, ...]:
    """The tokens that name the model, in title order.

    Model numbers -- tokens containing a digit -- are what identify a product
    across two sellers' very different titles, so they are kept preferentially.
    Words are kept only to fill remaining slots, and only if they survived the
    noise list.
    """
    words = _TOKEN.findall((title or "").lower())
    brand_words = set(brand.split()) | {brand}
    kept: list[str] = []
    for w in words:
        if w in brand_words or w in _NOISE or len(w) < 2:
            continue
        # A bare capacity is a variant, not an identity.
        if re.fullmatch(r"\d{1,4}(gb|tb)?", w) and w in {
                capacity_of(title), capacity_of(title).rstrip("gbt")}:
            continue
        kept.append(w)

    numbered = [w for w in kept if _HAS_DIGIT.search(w)]
    plain = [w for w in kept if not _HAS_DIGIT.search(w)]
    out: list[str] = []
    for w in plain[:1] + numbered + plain[1:]:
        if w not in out:
            out.append(w)
        if len(out) >= limit:
            break
    return tuple(out)


@dataclass(frozen=True)
class ProductKey:
    category: str
    brand: str
    family: str
    variant: str
    confidence: float
    conflict: bool = False

    @property
    def key(self) -> str:
        """The canonical string products group on. Excludes variant by design."""
        return f"{self.category}:{self.brand or '?'}:{self.family or '?'}"

    @property
    def variant_key(self) -> str:
        return f"{self.key}:{self.variant}" if self.variant else self.key

    @property
    def usable(self) -> bool:
        """Is this key specific enough to group on without merging products?

        A key with neither a brand nor a family names nothing. Grouping on it
        would pile unrelated listings into one bucket and report a competition
        figure for a product that does not exist.
        """
        return bool(self.brand and self.family)


def key_for(title: str, category: str, brand: str | None = None,
            model_slug: str | None = None, storage_gb: int | None = None
            ) -> ProductKey:
    """Canonical product key for one listing.

    Args:
        title: the seller's title.
        category: our category slug.
        brand: the marketplace's own brand field, when it has one. Vinted
            supplies this on every item and it is far better than guessing
            the brand out of the title.
        model_slug: the phone parser's slug, used only when it does not
            contradict `brand`.
        storage_gb: parsed capacity, preferred over re-reading the title.
    """
    stated = normalise_brand(brand)
    conflict = brands_conflict(stated, model_slug)

    # A slug the brand contradicts is worse than no slug: it is a confident
    # wrong answer that joins this listing to another product's comps.
    slug = None if conflict else model_slug
    resolved_brand = stated or brand_of_slug(slug)

    if slug:
        family = slug
        confidence = 0.9 if stated else 0.7
    else:
        tokens = family_tokens(title, resolved_brand)
        family = "-".join(tokens)
        # Model numbers identify; bare words often do not.
        confidence = 0.6 if any(_HAS_DIGIT.search(t) for t in tokens) else 0.35

    variant = f"{storage_gb}gb" if storage_gb else capacity_of(title)
    if conflict:
        confidence = min(confidence, 0.3)

    return ProductKey(category=category, brand=resolved_brand, family=family,
                      variant=variant, confidence=confidence, conflict=conflict)
