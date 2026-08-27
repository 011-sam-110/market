"""Identify a listing from its photos when the title cannot.

This is the part that is not a commodity. Every competing tool prices by
matching a title against a catalogue, which means it must first *read* the
title -- so it silently skips whatever it cannot parse. On a live sample of 96
Vinted phone listings, **59 had no model or no storage in the title**. Those are
invisible to every price-based scanner in the market, including the one in
parser.py, and two of them were listed as literally "iPhone" at £190 and £200:
anywhere from an iPhone 11 (~£100) to a 15 (~£400).

A seller who cannot describe what they have prices it by guesswork. That is the
whole opportunity, and photographs are the evidence the title is missing.

Design rules, all inherited from things that have already gone wrong here:

- **Never guess.** Below CONFIDENCE_FLOOR, or on any error, this returns None.
  An unidentified listing is simply not a candidate. The project has twice been
  bitten by treating "could not tell" as a result -- an unreadable page scored
  as a sale, and an activation-locked handset scored as the best margin on the
  board -- and this is the same mistake with a bigger budget.
- **Never in the hot path.** parser.py's docstring already says it: rules first,
  model second, on the residue only. A network round-trip per listing would
  defeat a loop whose value is latency.
- **Cache by item id.** Identification of a given listing is stable, and quota
  is the scarce resource. Re-running the pipeline must cost nothing for
  listings already seen.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, asdict
from pathlib import Path

log = logging.getLogger("phoneloop.identify")

# Below this, the answer is discarded. Chosen high deliberately: a wrong model
# means a wrong bucket means a wrong reference price, and the cost of missing a
# deal is zero while the cost of inventing one is real money.
CONFIDENCE_FLOOR = 0.75

MAX_PHOTOS = 4          # more adds latency and tokens, not accuracy
MAX_TOKENS = 300

PROMPT = (
    "You are identifying a second-hand phone so it can be priced for resale.\n"
    'The seller titled it: "{title}"\n\n'
    "From the photographs, identify the exact model and storage capacity. "
    "Useful evidence: the camera layout and lens count, the body shape and "
    "materials, the notch or Dynamic Island, the port, and any visible settings "
    "or About screen showing a model name or capacity.\n\n"
    "Rules:\n"
    "- If you cannot tell the model apart from its siblings, say so with low "
    "confidence. A wrong answer is far worse than no answer.\n"
    "- storage_gb must be null unless you can actually see it stated. Do not "
    "infer a capacity from the model.\n\n"
    'Reply with JSON only: {{"make": str, "model": str, "storage_gb": int|null, '
    '"confidence": 0.0-1.0, "evidence": str}}'
)

_JSON_RE = re.compile(r"\{.*\}", re.S)


@dataclass(frozen=True)
class Identification:
    make: str
    model: str
    storage_gb: int | None
    confidence: float
    evidence: str
    source: str = "vision"


def parse_reply(text: str) -> Identification | None:
    """Pull an Identification out of a model reply.

    Models wrap JSON in prose and fences no matter how firmly asked not to, so
    the object is extracted rather than assumed to be the whole reply. Anything
    that does not yield a usable make and model returns None.
    """
    if not text:
        return None
    match = _JSON_RE.search(text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    model = str(data.get("model") or "").strip()
    if not model:
        return None

    try:
        confidence = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0

    storage = data.get("storage_gb")
    try:
        storage = int(storage) if storage is not None else None
    except (TypeError, ValueError):
        storage = None

    return Identification(
        make=str(data.get("make") or "").strip().lower(),
        model=slugify(model),
        storage_gb=storage,
        confidence=confidence,
        evidence=str(data.get("evidence") or "").strip()[:300],
    )


def slugify(model: str) -> str:
    """Match parser.py's slug shape so both feed the same pricebook bucket.

    If vision returned "iPhone 13 Pro" while the rules parser emits
    "iphone-13-pro", they would key two different buckets for one handset and
    each would look under-sampled.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")
    return re.sub(r"-+", "-", slug)


def photo_urls(item: dict, limit: int = MAX_PHOTOS) -> list[str]:
    """Full-size photo URLs from a catalogue item, best first."""
    urls = []
    for photo in (item.get("photos") or [])[:limit]:
        url = photo.get("full_size_url") or photo.get("url")
        if url:
            urls.append(url)
    if not urls:
        main = (item.get("photo") or {}).get("full_size_url")
        if main:
            urls.append(main)
    return urls


def build_messages(item: dict, urls: list[str]) -> list[dict]:
    """OpenAI-shaped chat messages carrying the title and the photos."""
    content: list[dict] = [
        {"type": "text", "text": PROMPT.format(title=item.get("title", ""))}
    ]
    content += [{"type": "image_url", "image_url": {"url": u}} for u in urls]
    return [{"role": "user", "content": content}]


class Cache:
    """Identifications keyed by listing id, as one JSON file.

    Quota is the scarce resource, and a listing's identity does not change. A
    flat file is right at this scale and keeps the store free of a second
    database.
    """

    def __init__(self, path: Path):
        self.path = path
        self._data: dict[str, dict] = {}
        if path.exists():
            try:
                self._data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                log.warning("identification cache unreadable, starting empty")

    def get(self, item_id) -> Identification | None:
        row = self._data.get(str(item_id))
        return Identification(**row) if row else None

    def put(self, item_id, ident: Identification) -> None:
        self._data[str(item_id)] = asdict(ident)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=1), encoding="utf-8")

    def __len__(self) -> int:
        return len(self._data)


class VisionIdentifier:
    """Identify listings from photographs, via any OpenAI-compatible endpoint.

    `chat` is injected: it takes (model, messages) and returns the reply text,
    or raises. Keeping the transport out of here means the decision logic is
    testable without a network, a key, or quota -- which matters, because the
    gateway this was written against was rate-limited at the time.
    """

    def __init__(self, chat, model: str, cache: Cache | None = None,
                 confidence_floor: float = CONFIDENCE_FLOOR):
        self.chat = chat
        self.model = model
        self.cache = cache
        self.confidence_floor = confidence_floor
        self.calls = 0
        self.cache_hits = 0

    def identify(self, item: dict) -> Identification | None:
        """Identify one listing. None means "not known", never a guess."""
        item_id = item.get("id")
        if self.cache is not None and item_id is not None:
            cached = self.cache.get(item_id)
            if cached is not None:
                self.cache_hits += 1
                return cached

        urls = photo_urls(item)
        if not urls:
            return None

        try:
            self.calls += 1
            reply = self.chat(self.model, build_messages(item, urls))
        except Exception as exc:                  # noqa: BLE001 - deliberate
            # Quota, network, a provider outage. All the same answer: we do not
            # know what this is, so it is not a candidate.
            log.warning("identify failed for %s: %s", item_id, str(exc)[:120])
            return None

        ident = parse_reply(reply)
        if ident is None:
            return None
        if ident.confidence < self.confidence_floor:
            log.debug("identification below floor (%.2f) for %s",
                      ident.confidence, item_id)
            return None

        if self.cache is not None and item_id is not None:
            self.cache.put(item_id, ident)
        return ident


def needs_vision(spec) -> bool:
    """Is this listing one the rules parser could not fully resolve?

    Storage counts. It keys a pricebook bucket, and a listing priced against the
    wrong capacity tier is mispriced by a hundred pounds or more -- which is why
    59 of 96 live listings, not 8, are in scope here.
    """
    return spec.model is None or spec.storage_gb is None
