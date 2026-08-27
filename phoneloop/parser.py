"""Title and description -> canonical phone spec.

Pure. No I/O, no config, no clock. This module holds the logic most likely to be
wrong, so it is kept exhaustively testable against real fixtures.

Rules first, model second: a deterministic pass resolves the great majority of
titles instantly and explainably. An LLM belongs only on the residue this cannot
resolve — never in the hot path, where a network call would defeat a loop whose
entire value is speed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --- model tables -----------------------------------------------------------
# Adding a manufacturer is a data change here, not a code change.

_APPLE_NUMBERS = [
    "17", "16", "15", "14", "13", "12", "11",
    "xs max", "xs", "xr", "x", "se", "8", "7", "6s", "6",
]
# Longest variant first. If "pro" matched before "pro max", every Pro Max would
# price against the cheaper Pro bucket and look like a bargain it isn't.
_APPLE_VARIANTS = ["pro max", "plus", "pro", "mini", "max", ""]

# Samsung. Same longest-first hazard: an Ultra priced against the base bucket
# looks like a steal. Sellers frequently drop either "samsung" or "galaxy" from
# the title, so presence of either is enough to establish the make.
_SAMSUNG_VARIANTS = ["ultra", "plus", "fe", ""]
_SAMSUNG_SERIES = [
    # (regex for the series token, slug template)
    (r"\bz\s*fold\s*(\d{1,2})\b", "galaxy-z-fold-{}"),
    (r"\bz\s*flip\s*(\d{1,2})\b", "galaxy-z-flip-{}"),
    (r"\bnote\s*(\d{1,2})\b", "galaxy-note-{}"),
    (r"\bs(\d{1,2})\b", "galaxy-s{}"),
    (r"\ba(\d{1,3})\b", "galaxy-a{}"),
]

# Capacities Apple has actually shipped. Anything else is noise, not storage.
_VALID_STORAGE_GB = {16, 32, 64, 128, 256, 512, 1024}

_NETWORKS = {
    "unlocked": "unlocked",
    "ee": "ee",
    "o2": "o2",
    "vodafone": "vodafone",
    "three": "three",
    "tesco": "tesco",
    "giffgaff": "giffgaff",
    "sky": "sky",
}

# Anything matching these is not a handset. A free-text phone search on Vinted
# returns mostly these, so the filter carries real weight.
_ACCESSORY_PATTERNS = [
    r"\bcase\b", r"\bcases\b", r"\bcover\b", r"\bcovers\b", r"\bskin\b",
    r"screen protector", r"tempered glass", r"\bglass\b",
    r"\bcharger\b", r"\bcable\b", r"\blead\b", r"\badapter\b", r"\badaptor\b",
    r"\bholder\b", r"\bmount\b", r"\bstrap\b", r"\bpouch\b", r"\bwallet\b",
    r"\bbumper\b", r"\bgrip\b", r"\bstand\b", r"\blens\b",
    r"battery replacement", r"replacement battery",
    r"\bairpods\b", r"\bwatch\b", r"\bearphones\b", r"\bheadphones\b",
]

# (canonical name, pattern). Order is irrelevant — all are scanned.
_DISQUALIFIERS = [
    ("spares_repair", r"spares?\s*(or|and|&|/)?\s*repair|for\s*parts|\bparts only\b|\bspares\b"),
    ("icloud_locked", r"icloud\s*lock|icloud\s*locked|find\s*my\s*(is\s*)?on|activation\s*lock|\bicloud\b"),
    ("damaged", r"\bcracked\b|\bsmashed\b|\bshattered\b|screen\s*burn|\bdamaged\b|\bchipped\b"),
    ("faulty", r"\bfaulty\b|\bnot\s*working\b|\bdoesn'?t\s*work\b|\bbroken\b|\bdead\b"),
]

_STORAGE_RE = re.compile(r"\b(\d{1,4})\s*(gb|tb)\b")


@dataclass(frozen=True)
class PhoneSpec:
    """A parsed handset identity. Frozen so it can key a pricebook bucket."""

    make: str | None = None
    model: str | None = None
    storage_gb: int | None = None
    network_lock: str | None = None
    disqualifiers: tuple[str, ...] = field(default_factory=tuple)
    parse_method: str = "rules"
    confidence: float = 0.0

    @property
    def is_phone(self) -> bool:
        return self.model is not None

    @property
    def is_clean(self) -> bool:
        """No disqualifiers found. Not a guarantee of condition — only that we
        did not spot a reason to walk away."""
        return not self.disqualifiers

    def bucket_key(self) -> tuple:
        """Key for the pricebook. Condition is added by the caller, since it
        comes from Vinted's own field rather than from the title."""
        return (self.make, self.model, self.storage_gb)


def _normalise(text: str) -> str:
    """Lowercase and repair the spellings sellers actually use."""
    t = text.lower()
    t = t.replace("’", "'").replace("–", "-").replace("—", "-")
    # "i-phone", "i phone", "iphone" all mean the same thing.
    t = re.sub(r"\bi[\s\-_.]*phone\b", "iphone", t)
    # "15promax" -> "15 pro max". Has to run before the single-variant rule:
    # there is no word boundary between "pro" and "max", so a \b-anchored
    # pattern never fires on the concatenated form.
    t = re.sub(r"(\d)\s*pro\s*max\b", r"\1 pro max", t)
    # "13pro" / "s24ultra" -> spaced; sellers routinely omit the space.
    t = re.sub(r"(\d)(pro|plus|mini|max|ultra|fe)\b", r"\1 \2", t)
    # "S21+" is standard shorthand for the Plus model.
    t = re.sub(r"(\d)\s*\+", r"\1 plus", t)
    # Punctuation used as a separator becomes whitespace, so "iPhone 13.Excellent"
    # and "iPhone 13 , 128GB ," both tokenise cleanly.
    t = re.sub(r"[,./\\|–—]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _find_disqualifiers(text: str) -> tuple[str, ...]:
    found = [name for name, pat in _DISQUALIFIERS if re.search(pat, text)]
    return tuple(dict.fromkeys(found))  # de-duped, order preserved


def _is_accessory(text: str) -> bool:
    return any(re.search(p, text) for p in _ACCESSORY_PATTERNS)


def _match_apple_model(text: str) -> str | None:
    """Return a normalised Apple model slug, or None.

    Numbers are tried longest-first so "iphone 13" cannot shadow a hypothetical
    "iphone 13x", and variants longest-first so "pro max" beats "pro".
    """
    if "iphone" not in text:
        return None
    for number in _APPLE_NUMBERS:
        # Require the number to follow "iphone", optionally with words between,
        # so "iphone 13" matches but a stray "13" elsewhere does not.
        if not re.search(rf"iphone\s*{re.escape(number)}\b", text):
            continue
        for variant in _APPLE_VARIANTS:
            if not variant:
                slug = f"iphone-{number}".replace(" ", "-")
                return slug
            if re.search(rf"iphone\s*{re.escape(number)}\s+{re.escape(variant)}\b", text):
                return f"iphone-{number} {variant}".replace(" ", "-")
    return None


def _match_samsung_model(text: str) -> str | None:
    """Return a normalised Samsung model slug, or None.

    Either "samsung" or "galaxy" establishes the make — sellers routinely write
    "Samsung s24 ultra" or "Galaxy Z Flip 4" and drop the other token.
    """
    if "samsung" not in text and "galaxy" not in text:
        return None
    for pattern, template in _SAMSUNG_SERIES:
        m = re.search(pattern, text)
        if not m:
            continue
        base = template.format(m.group(1))
        # Variant must appear after the series token, or "s24 ultra" and a
        # stray "ultra" elsewhere in the title would be indistinguishable.
        tail = text[m.end():]
        for variant in _SAMSUNG_VARIANTS:
            if variant and re.match(rf"\s+{re.escape(variant)}\b", tail):
                return f"{base}-{variant}"
        return base
    return None


# Tried in order. Adding a manufacturer means adding a matcher here and a table
# above — no change to parse() itself.
_MAKE_MATCHERS = [
    ("apple", _match_apple_model),
    ("samsung", _match_samsung_model),
]


def _match_model(text: str) -> tuple[str, str] | None:
    """Return (make, model_slug) for the first manufacturer that claims the text."""
    for make, matcher in _MAKE_MATCHERS:
        slug = matcher(text)
        if slug is not None:
            return make, slug
    return None


def _extract_storage(text: str) -> int | None:
    """Capacity only counts when a unit is attached.

    Without this, "iPhone 13" would read 13 as 13GB and the whole pricebook
    would bucket on nonsense.
    """
    for value, unit in _STORAGE_RE.findall(text):
        gb = int(value) * 1024 if unit == "tb" else int(value)
        if gb in _VALID_STORAGE_GB:
            return gb
    return None


def _extract_network(text: str) -> str | None:
    if re.search(r"\bunlock(ed)?\b", text):
        return "unlocked"
    for token, canonical in _NETWORKS.items():
        if token == "unlocked":
            continue
        if re.search(rf"\b{re.escape(token)}\b", text):
            return canonical
    return None


def parse(title: str, description: str = "") -> PhoneSpec:
    """Parse a listing into a PhoneSpec.

    Args:
        title: The raw listing title.
        description: Optional body text. Disqualifiers hide here far more often
            than in the title — iCloud lock especially.

    Returns:
        A PhoneSpec. `model is None` means "not a handset we can price", which
        is the correct outcome for accessories and unknown makes alike.
    """
    haystack = _normalise(f"{title} {description}")
    title_only = _normalise(title)

    # Disqualifiers are scanned across both fields and reported even for
    # accessories, so callers can log why something was skipped.
    disqualifiers = _find_disqualifiers(haystack)

    if _is_accessory(title_only):
        return PhoneSpec(disqualifiers=disqualifiers, confidence=1.0)

    matched = _match_model(title_only)
    if matched is None:
        return PhoneSpec(disqualifiers=disqualifiers, confidence=0.0)
    make, model = matched

    storage = _extract_storage(title_only) or _extract_storage(haystack)

    # Confidence reflects how much of the identity we actually pinned down.
    # Storage is the field that most often decides price, so its absence costs
    # the most.
    confidence = 0.6 + (0.4 if storage is not None else 0.0)

    return PhoneSpec(
        make=make,
        model=model,
        storage_gb=storage,
        network_lock=_extract_network(haystack),
        disqualifiers=disqualifiers,
        parse_method="rules",
        confidence=confidence,
    )
