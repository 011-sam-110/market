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
# looks like a steal.
#
# These patterns are deliberately allowed to match WITHOUT "samsung" or "galaxy"
# present — live titles like "S24 ultra 256GB", "A55 5G" and "Z flip 5 256gb"
# were the largest single group the parser was missing. Bare "s24"/"a55" is only
# safe because everything here arrives from the phones category; the greediness
# is why Samsung is checked LAST in _MAKE_MATCHERS.
_SAMSUNG_VARIANTS = ["ultra", "plus", "fe", ""]
_SAMSUNG_SERIES = [
    # (regex for the series token, slug template)
    (r"\bz\s*fold\s*(\d{1,2})\b", "galaxy-z-fold-{}"),
    (r"\bz\s*flip\s*(\d{1,2})\b", "galaxy-z-flip-{}"),
    (r"\bnote\s*(\d{1,2})\b", "galaxy-note-{}"),
    (r"\bs(\d{1,2})\b", "galaxy-s{}"),
    (r"\ba(\d{1,3})\b", "galaxy-a{}"),
]

# Everything else is pure data: (make, brand tokens, series patterns, variants).
# Adding a manufacturer means adding a row here and nothing else.
_BRAND_RULES = [
    ("google", ("pixel", "google"), [
        (r"\bpixel\s*(\d{1,2}\s*a|\d{1,2})\b", "pixel-{}"),
    ], ["pro xl", "pro", "xl"]),

    ("motorola", ("motorola", "moto "), [
        (r"\bedge\s*(\d{1,3})\b", "edge-{}"),
        (r"\b(?:moto\s*)?g(\d{2,3})\b", "moto-g{}"),
        (r"\brazr\s*(\d{2,4})\b", "razr-{}"),
    ], ["ultra", "pro", "plus", "neo", "fusion"]),

    ("oppo", ("oppo",), [
        (r"\bfind\s*([xn]\d{1,2})\b", "find-{}"),
        (r"\breno\s*(\d{1,2})\b", "reno-{}"),
        (r"\b([xn]\d{1,2})\s*flip\b", "{}-flip"),
    ], ["pro plus", "pro", "flip", "lite"]),

    ("sony", ("sony", "xperia"), [
        (r"\bxperia\s*([a-z]?\d{1,2}|1|5|10)\b", "xperia-{}"),
    ], ["mark v", "iv", "v", "iii", "ii", "plus"]),

    ("oneplus", ("oneplus", "one plus"), [
        (r"\bone\s*plus\s*(\d{1,2})\b", "oneplus-{}"),
    ], ["pro", "t", "r"]),

    ("xiaomi", ("xiaomi", "redmi", "poco"), [
        (r"\bredmi\s*note\s*(\d{1,2})\b", "redmi-note-{}"),
        (r"\bredmi\s*(\d{1,2})\b", "redmi-{}"),
        (r"\bpoco\s*([a-z]\d{1,2})\b", "poco-{}"),
        (r"\bxiaomi\s*(\d{1,2})\b", "xiaomi-{}"),
    ], ["pro plus", "pro", "ultra", "lite"]),

    ("realme", ("realme", "real me"), [
        (r"\breal\s*me\s*(gt|\d{1,2})\b", "realme-{}"),
    ], ["pro", "master edition", "master"]),

    ("honor", ("honor",), [
        (r"\bhonor\s*(\d{1,3}[a-z]?)\b", "honor-{}"),
    ], ["pro", "lite"]),

    ("huawei", ("huawei",), [
        (r"\bp(\d{1,2})\b", "huawei-p{}"),
        (r"\bmate\s*(\d{1,2})\b", "huawei-mate-{}"),
    ], ["pro", "lite"]),

    ("nokia", ("nokia",), [
        (r"\bnokia\s*(\d{1,4})\b", "nokia-{}"),
    ], ["plus", "pro"]),
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
    # An activation-locked handset is worth close to nothing and CeX will not
    # take it, so this is the most expensive thing the parser can miss.
    #
    # Keying on the word "icloud" was not enough. A live listing read "Locked
    # and I don't know the password but would be good once gets unlocked by
    # professional" -- an activation lock described without ever using the term,
    # which the verifier passed as fine at the best apparent margin on the board.
    # So the rules below describe the *situation*, not the vocabulary.
    #
    # The negative lookbehinds and lookahead carry equal weight: "no icloud" and
    # "icloud removed" are sellers saying the phone is CLEAN, and treating them
    # as locks would mute most of the good listings in the feed. Note that
    # \blocked\b cannot match inside "unlocked", which is what makes the
    # bare-"locked" clauses safe.
    ("activation_locked", (
        r"(?<!no )(?<!not )\bicloud\b(?!\s*(removed|cleared|free|off|"
        r"signed\s*out|logged\s*out|unlocked))"
        r"|activation\s*lock"
        r"|find\s*my\s*(iphone\s*)?(is\s*)?on"
        r"|apple\s*id\s*(lock|password)"
        r"|(don'?t|dont|do\s*not|doesn'?t)\s*know\s*(the\s*)?(password|passcode|pin|apple\s*id)"
        r"|forgot(ten)?\s*(the\s*)?(password|passcode|pin|apple\s*id)"
        r"|locked\s*to\s*(a\s*|the\s*)?(previous|old|last)\s*owner"
        r"|previous\s*owner'?s?\s*(icloud|apple\s*id|account)"
        r"|needs?\s*unlock"
        r"|\bdisabled\b"
        r"|\blocked\b.{0,40}(password|passcode|\bpin\b)"
    )),
    # A barred IMEI is unsellable through any legitimate channel. "not
    # blacklisted" is a clean-stock claim, so it must not trip this.
    ("blacklisted", (
        r"(?<!not )(?<!never )\bblacklist(ed)?\b"
        r"|imei\s*(is\s*)?(blocked|barred|blacklisted)"
        r"|\bbarred\b"
        r"|reported\s*(lost|stolen)"
    )),
    # Nouns as well as adjectives. The rule caught "cracked" and missed "has a
    # crack on the back as shown" -- a listing that then ranked FIRST on the
    # board as the only guaranteed-safe buy the project had ever found. Sellers
    # describe damage both ways and the noun form is not the rarer one.
    #
    # Negation is handled separately in _negated(), because "no cracks" and
    # "no scratches" are among the commonest phrases in a CLEAN listing and a
    # fixed-width lookbehind cannot see past "without any" or "free from".
    ("damaged", (
        r"\bcrack(s|ed|ing)?\b|\bsmash(ed)?\b|\bshatter(ed)?\b"
        r"|screen\s*burn|\bdamaged?\b|\bchip(s|ped)?\b"
        r"|\bdent(s|ed)?\b|\bspider\s*web"
    )),
    ("faulty", r"\bfaulty\b|\bnot\s*working\b|\bdoesn'?t\s*work\b|\bbroken\b|\bdead\b"),
    # A bundle is a different product at a different price. One in a bucket
    # drags the median and makes single handsets look overpriced.
    #
    # The quantity forms have to be narrow. A bare `\bx\d+\b` reads the X9 in
    # "Oppo Find X9" as a quantity and threw away a £788 listing -- and would do
    # the same to every Poco X6 and Nothing Phone X2. Real quantities are either
    # leading ("4x Sony", "3 x iPhone") or trailing ("cases x3"); a model name
    # puts its X-number in the middle, after a brand word.
    ("bundle", (
        r"\bbundle\b|\bjob\s*lot\b|\bjoblot\b|\bbulk\b"
        r"|\b\d+\s*x\s+\w"
        r"|\bx\s*\d{1,2}\s*$"
    )),
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
    # Separators that carry no clause meaning just become whitespace.
    t = re.sub(r"[/\\|–—]+", " ", t)
    # Sentence punctuation becomes a spaced semicolon rather than vanishing.
    # It still tokenises cleanly -- "iPhone 13.Excellent" and "iPhone 13 , 128GB"
    # both split on whitespace as before, and every rule here is \b-anchored so
    # a free-standing ";" reads as a gap. But it SURVIVES, which is what lets
    # _negated() tell "no charger, cracked back" from "no cracks".
    # Erasing it is why the rules once passed a phone whose description read
    # "Nothing wrong with the phone itself but has a crack on the back".
    t = re.sub(r"[,.;!?]+", " ; ", t)
    return re.sub(r"\s+", " ", t).strip(" ;")


# Words that turn a damage mention into a reassurance. "No cracks or
# scratches" is one of the commonest sentences in a genuinely clean listing, so
# matching the damage word alone would bin most of the good stock.
_NEGATORS = re.compile(
    r"\b(no|not|none|never|without|zero|nothing"
    r"|free\s+from|free\s+of"
    r"|doesn'?t\s+have|does\s+not\s+have|hasn'?t\s+got)\b"
)

# How far back to look for one. Long enough to cover "without any visible",
# short enough that a negation about something else earlier in the sentence
# does not reach across and excuse real damage.
_NEGATION_WINDOW = 28


def _negated(text: str, start: int) -> bool:
    """Is this match inside the scope of a negation immediately before it?

    A fixed-width lookbehind cannot express this -- "no cracks", "without any
    cracks" and "free from cracks" are three different widths -- so the window
    before the match is inspected directly.
    """
    window = text[max(0, start - _NEGATION_WINDOW):start]
    # A clause boundary ends a negation's reach. In "no charger, cracked back"
    # the "no" governs the charger and says nothing about the crack.
    #
    # "and" is NOT a boundary: it continues a list under the same negation, as
    # in "free from cracks and chips". "but" is, because it reverses -- which
    # is exactly the shape of a misleading listing: reassure, then admit.
    window = re.split(r";|\bbut\b|\bhowever\b|\bthough\b", window)[-1]
    return bool(_NEGATORS.search(window))


def _find_disqualifiers(text: str) -> tuple[str, ...]:
    found = []
    for name, pat in _DISQUALIFIERS:
        # Every occurrence, not just the first: "no scratches but cracked back"
        # must still disqualify, and the first match there is the negated one.
        for match in re.finditer(pat, text):
            if not _negated(text, match.start()):
                found.append(name)
                break
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


def _match_series(text: str, series, variants, brand_tokens=None) -> str | None:
    """Shared series+variant matcher.

    Args:
        series: (pattern, slug_template) pairs, most specific first.
        variants: Suffixes to look for immediately after the series token,
            longest first. A variant found anywhere else in the title is
            ignored — "s24 ultra" and "ultra clean condition" must not look
            the same.
        brand_tokens: If given, at least one must appear or the make is not
            claimed at all.
    """
    if brand_tokens and not any(t in text for t in brand_tokens):
        return None
    for pattern, template in series:
        m = re.search(pattern, text)
        if not m:
            continue
        base = template.format(re.sub(r"\s+", "", m.group(1)))
        tail = text[m.end():]
        for variant in variants:
            if variant and re.match(rf"\s+{re.escape(variant)}\b", tail):
                return f"{base}-{variant.replace(' ', '-')}"
        return base
    return None


def _match_samsung_model(text: str) -> str | None:
    """Samsung, matched WITHOUT requiring a brand token — see _SAMSUNG_SERIES."""
    return _match_series(text, _SAMSUNG_SERIES, _SAMSUNG_VARIANTS)


def _match_bare_apple(text: str) -> str | None:
    """Last-resort Apple match for titles that never say "iPhone".

    "Pro Max" is an Apple-only naming convention — Samsung uses Ultra, Google
    uses XL, Motorola uses Ultra. So a bare generation number followed by
    "pro max", with no other make having claimed the title first, is safely
    an iPhone. Live example: "locked 17 pro max".

    Deliberately narrow: only "pro max", never bare "pro", which every
    manufacturer uses.
    """
    m = re.search(r"\b(1[0-9]|[6-9])\s*pro\s*max\b", text)
    return f"iphone-{m.group(1)}-pro-max" if m else None


# Order matters. Apple first, then every brand-token-gated make, then the two
# greedy fallbacks LAST: Samsung's patterns match bare "s24"/"a55", and the bare
# Apple rule matches a naked generation number. Either would otherwise claim
# titles belonging to another manufacturer.
_MAKE_MATCHERS = (
    [("apple", _match_apple_model)]
    + [
        (make, (lambda t, s=series, v=variants, b=tokens: _match_series(t, s, v, b)))
        for make, tokens, series, variants in _BRAND_RULES
    ]
    + [
        ("samsung", _match_samsung_model),
        ("apple", _match_bare_apple),
    ]
)


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
