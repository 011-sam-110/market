"""Second look at a candidate, from the public listing page.

Item detail via the API is auth-gated (403), but the public HTML page is not and
carries the description in embedded JSON. Pages run ~1.9 MB, so this is
candidates-only and never the hot path.

The description is where the expensive disqualifiers hide. iCloud lock in
particular rarely appears in a title, and missing it buys a brick.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from .parser import PhoneSpec, _find_disqualifiers, _normalise

log = logging.getLogger("phoneloop.verifier")

# The page embeds its state as JSON. Capture a full JSON string literal,
# escapes included, so an apostrophe or a newline in the seller's text does not
# truncate it halfway.
_DESCRIPTION_RE = re.compile(r'"description"\s*:\s*("(?:[^"\\]|\\.)*")')
_PHOTO_RE = re.compile(r'"(?:full_size_url|url)"\s*:\s*"https://images\d*\.vinted')


@dataclass(frozen=True)
class VerifyResult:
    description: str = ""
    photo_count: int = 0
    extra_disqualifiers: tuple[str, ...] = field(default_factory=tuple)
    ok: bool = False
    reason: str = ""


def extract_description(html: str) -> str:
    """Pull the description out of the page's embedded JSON."""
    match = _DESCRIPTION_RE.search(html)
    if not match:
        return ""
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return ""


def count_photos(html: str) -> int:
    """A listing with a single stock image is a different risk from one with
    eight photos of the actual handset."""
    return len(set(_PHOTO_RE.findall(html)))


def verify(client, item: dict, spec: PhoneSpec) -> VerifyResult:
    """Fetch and re-check a candidate.

    Never raises into the loop. A fetch that fails returns `ok=False`, because
    an unverified candidate is not a candidate — the rule everywhere here is
    that failure degrades toward silence, never toward a false alert.
    """
    path = item.get("path") or item.get("url") or ""
    if not path:
        return VerifyResult(ok=False, reason="listing has no path to verify")

    try:
        html = client.fetch_public_page(path)
    except Exception as exc:                      # noqa: BLE001 - deliberate
        log.warning("verify fetch failed for %s: %s", path, exc)
        return VerifyResult(ok=False, reason=f"fetch failed: {exc}")

    description = extract_description(html)
    photo_count = count_photos(html)

    # Re-scan with the description in hand. The title alone misses most of these.
    found = _find_disqualifiers(_normalise(f"{item.get('title', '')} {description}"))
    extra = tuple(d for d in found if d not in spec.disqualifiers)

    if found:
        return VerifyResult(
            description=description,
            photo_count=photo_count,
            extra_disqualifiers=extra,
            ok=False,
            reason=f"disqualified on description: {', '.join(found)}",
        )

    return VerifyResult(
        description=description,
        photo_count=photo_count,
        extra_disqualifiers=(),
        ok=True,
        reason="verified",
    )
