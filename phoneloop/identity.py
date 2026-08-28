"""Product identity at three honest tiers, plus title-shingle clustering
(spec section 5, contracts section 2.9).

eBay carries an `epid` on roughly 5% of search rows (13/248 measured) and no
manufacturer part number worth trusting anywhere on the search page. Rather
than pretend that is enough to identify every product, or too little to
identify any, every row is tagged with the best-supported tier and the
downstream competitor count is computed at that tier:

    catalogued  -- a true epid or gtin. Exact.
    inferred    -- eBay's own inferredEpid (getItem's title+aspects match).
                   Near-exact, and labelled as eBay's guess, not a seller's.
    cluster     -- no catalogue match, but title shingles cluster tightly
                   with other rows in the same leaf. Approximate.
    none        -- nothing to group on. Niche-level only.

Two rules hold the line on honesty:

**A seller-typed MPN is never identity.** One measured example --
`"mpn":"Wireless-charge-carphonr"` -- is plainly a mangled product
description, not a manufacturer code, and there is no field on the row that
distinguishes a good MPN from a bad one except looking at the string. That
judgement lives here, once, in `is_junk_mpn`, so no other module reinvents
it and disagrees.

**Clustering never crosses a niche.** Two clothes irons and a "wireless
doorbell" that happens to share the word "wireless" three times over must
never merge into one product just because they were swept together --
`cluster()` refuses rows that span more than one `leaf_category_id`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

TIERS: tuple[str, ...] = ("catalogued", "inferred", "cluster", "none")

JACCARD_MIN: float = 0.6
SHINGLE_N: int = 3

# Splits a raw MPN into candidate "words". Real manufacturer part numbers are
# a single alphanumeric token (or a token pair like "SM-G991B") with a digit
# somewhere in it; a seller who typed a description instead produces two or
# more all-letters words joined by punctuation, which this pattern isolates.
_MPN_WORD_SEP = re.compile(r"[-_\s/]+")

# What a title normalises to before shingling: lowercase alphanumeric runs.
# Punctuation and casing carry no identity signal and only fragment matches
# that should hit ("Earbuds," vs "Earbuds").
_TITLE_TOKEN = re.compile(r"[a-z0-9]+")


def is_junk_mpn(mpn: str | None) -> bool:
    """Is this MPN seller-typed noise rather than a manufacturer part number?

    Measured junk: "Wireless-charge-carphonr". The rule: split on hyphen,
    underscore, space or slash; if that yields two or more tokens, every one
    of them is plain alphabetic (length >= 3), and none of them carries a
    digit, it reads as a typed description rather than a code -- real MPNs
    are either one compact token ("A2482") or a short token pair where at
    least one side carries a digit ("SM-G991B", "GN-123X"). A single token,
    or any token with a digit in it, is given the benefit of the doubt and
    is NOT flagged junk; this rule only catches the pattern actually
    measured, not every conceivable bad MPN.

    Junk MPNs are recorded (never discarded) and never used for identity --
    this is the sole authority on that rule; nothing else in the codebase
    re-implements it.
    """
    if not mpn:
        return False
    tokens = [t for t in _MPN_WORD_SEP.split(mpn.strip()) if t]
    if len(tokens) < 2:
        return False
    if any(any(ch.isdigit() for ch in t) for t in tokens):
        return False
    return all(t.isalpha() and len(t) >= 3 for t in tokens)


def tier_for(row: Mapping) -> str:
    """Best identity tier this row supports: catalogued | inferred | cluster
    | none (spec section 5).

    Order matters: a gtin always wins (it is exact regardless of what the
    epid field holds), then an epid -- split into catalogued or inferred by
    whether the transport marked it as eBay's own inferredEpid rather than a
    true catalogue match -- and only once both are absent does a title+leaf
    fall back to the approximate cluster tier.
    """
    gtin = row.get("gtin")
    epid = row.get("epid")

    if gtin:
        return "catalogued"
    if epid:
        return "inferred" if row.get("identity_tier") == "inferred" else "catalogued"
    if row.get("title") and row.get("leaf_category_id"):
        return "cluster"
    return "none"


def product_key(row: Mapping, tier: str, *, cluster_id: int | None = None) -> str | None:
    """The string listings group on at this tier. None for tier 'none' -- a
    nameless product is niche-level only.

    `mpn` never appears here, at any tier -- see the module docstring.
    """
    if tier == "catalogued":
        gtin = row.get("gtin")
        if gtin:
            return f"gtin:{gtin}"
        return f"epid:{row.get('epid')}"
    if tier == "inferred":
        # The tilde marks this as eBay's own match, not a seller's declared
        # catalogue entry -- keeping the two namespaces apart means a
        # catalogued and an inferred key for the same epid never collide.
        return f"epid~:{row.get('epid')}"
    if tier == "cluster":
        if cluster_id is None:
            raise ValueError(
                "product_key(tier='cluster') needs a cluster_id -- call "
                "cluster() over the niche first and pass its result back in"
            )
        return f"cluster:{row.get('leaf_category_id')}:{cluster_id}"
    if tier == "none":
        return None
    raise ValueError(f"unknown identity tier: {tier!r}; expected one of {TIERS}")


def shingles(title: str, n: int = SHINGLE_N) -> frozenset[str]:
    """Overlapping n-word shingles of a normalised title, the unit clustering
    compares.

    Normalisation is lowercase alphanumeric tokens only -- "Earbuds," and
    "earbuds" must shingle identically, or punctuation noise fragments
    matches that should hit. A title shorter than n words has no shingles.
    """
    words = _TITLE_TOKEN.findall((title or "").lower())
    if len(words) < n:
        return frozenset()
    return frozenset(" ".join(words[i:i + n]) for i in range(len(words) - n + 1))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """|A n B| / |A u B|. Two empty sets score 0.0, not 1.0 -- nothing in
    common is not identity, and a pair of unshingled (too-short) titles must
    never look like a perfect match by that accident.
    """
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def cluster(rows: Sequence[Mapping], *, threshold: float = JACCARD_MIN) -> list[int]:
    """Cluster ids parallel to `rows`, by title shingle Jaccard, INSIDE one
    niche.

    Greedy single-pass assignment: each row joins the first existing cluster
    whose representative (its first member's shingle set) scores >=
    threshold against it, else starts a new cluster. Cluster ids are
    assigned in order of first appearance (0, 1, 2...), so the same input
    list always yields the same ids -- deterministic, and cheap enough to
    run over a whole niche on every pass.

    Raises ValueError if `rows` span more than one leaf_category_id:
    clustering across niches is how "wireless charger" and "wireless
    doorbell" become one product. Rows with no leaf_category_id at all are
    not counted toward that check -- every row that HAS one must agree.
    """
    leaves = {row.get("leaf_category_id") for row in rows}
    leaves.discard(None)
    leaves.discard("")
    if len(leaves) > 1:
        raise ValueError(
            f"cluster() called across {len(leaves)} leaf categories "
            f"{sorted(leaves)}; clustering must run inside one niche"
        )

    row_shingles = [shingles(row.get("title") or "") for row in rows]
    representatives: list[frozenset[str]] = []
    ids: list[int] = []

    for sset in row_shingles:
        matched_id = None
        for cid, rep in enumerate(representatives):
            if jaccard(sset, rep) >= threshold:
                matched_id = cid
                break
        if matched_id is None:
            representatives.append(sset)
            ids.append(len(representatives) - 1)
        else:
            ids.append(matched_id)

    return ids
