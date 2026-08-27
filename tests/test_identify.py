"""Vision identification tests.

None of these need a key, a network, or quota -- which is the point of injecting
the transport. The gateway was rate-limited across every provider at the time
this was written, and the decision logic still had to be provably correct.

The behaviour under test is mostly *refusal*. This module spends money and can
be confidently wrong, so almost every test here asserts that it declines.
"""

from __future__ import annotations

import json

import pytest

from phoneloop.identify import (
    Cache,
    Identification,
    VisionIdentifier,
    build_messages,
    needs_vision,
    parse_reply,
    photo_urls,
    slugify,
)
from phoneloop.parser import parse

GOOD = json.dumps({
    "make": "Apple", "model": "iPhone 13 Pro", "storage_gb": 256,
    "confidence": 0.92, "evidence": "Three rear lenses in a square array, "
                                    "stainless frame, notch not Dynamic Island",
})

ITEM = {
    "id": 123,
    "title": "iPhone",
    "photos": [
        {"full_size_url": "https://images1.vinted.net/t/a/f800/1.webp"},
        {"full_size_url": "https://images1.vinted.net/t/b/f800/2.webp"},
    ],
}


def stub(reply=GOOD, exc=None):
    """A chat callable that records how often it was asked."""
    calls = []

    def chat(model, messages):
        calls.append((model, messages))
        if exc:
            raise exc
        return reply

    chat.calls = calls
    return chat


class TestParseReply:
    def test_reads_a_clean_json_reply(self):
        ident = parse_reply(GOOD)
        assert ident.make == "apple"
        assert ident.model == "iphone-13-pro"
        assert ident.storage_gb == 256
        assert ident.confidence == pytest.approx(0.92)

    def test_digs_json_out_of_prose_and_fences(self):
        """Models wrap JSON however firmly you ask them not to."""
        wrapped = f"Sure! Here is the identification:\n```json\n{GOOD}\n```\nHope that helps."
        assert parse_reply(wrapped).model == "iphone-13-pro"

    @pytest.mark.parametrize("text", [
        "", "I am not able to determine the model from these photos.",
        "{not json at all}", "[1,2,3]", '{"confidence":0.9}',
    ])
    def test_anything_unusable_is_none_not_a_guess(self, text):
        assert parse_reply(text) is None

    def test_a_non_numeric_confidence_reads_as_zero(self):
        """Which then falls below the floor and is discarded, as it should."""
        reply = json.dumps({"model": "iPhone 13", "confidence": "high"})
        assert parse_reply(reply).confidence == 0.0

    def test_storage_that_is_not_a_number_becomes_none(self):
        reply = json.dumps({"model": "iPhone 13", "storage_gb": "maybe 128",
                            "confidence": 0.9})
        assert parse_reply(reply).storage_gb is None


class TestSlug:
    def test_matches_the_rules_parser_slug(self):
        """Two spellings of one handset would key two half-empty buckets."""
        assert slugify("iPhone 13 Pro") == parse("iPhone 13 Pro").model
        assert slugify("Samsung Galaxy S24 Ultra").endswith("s24-ultra")

    def test_punctuation_and_repeats_collapse(self):
        assert slugify("iPhone  13 -- Pro!!") == "iphone-13-pro"


class TestIdentify:
    def test_a_confident_answer_comes_back(self):
        ident = VisionIdentifier(stub(), "m").identify(ITEM)
        assert ident.model == "iphone-13-pro"

    def test_low_confidence_is_refused(self):
        reply = json.dumps({"model": "iPhone 13", "confidence": 0.4})
        assert VisionIdentifier(stub(reply), "m").identify(ITEM) is None

    def test_the_floor_is_inclusive_at_the_safe_edge(self):
        exactly = json.dumps({"model": "iPhone 13", "confidence": 0.75})
        assert VisionIdentifier(stub(exactly), "m").identify(ITEM) is not None

    def test_a_transport_failure_is_none_never_a_guess(self):
        """Quota, network, provider outage -- all the same answer: unknown.

        This is the rule the project has already broken twice, once by scoring
        an unreadable page as a sale and once by passing an activation-locked
        handset as the best margin on the board.
        """
        idf = VisionIdentifier(stub(exc=RuntimeError("429 exhausted")), "m")
        assert idf.identify(ITEM) is None

    def test_a_listing_with_no_photos_is_not_even_asked(self):
        chat = stub()
        assert VisionIdentifier(chat, "m").identify({"id": 1, "title": "iPhone"}) is None
        assert chat.calls == [], "must not spend a call with nothing to look at"


class TestCache:
    def test_a_second_look_costs_nothing(self, tmp_path):
        chat = stub()
        idf = VisionIdentifier(chat, "m", cache=Cache(tmp_path / "c.json"))
        first = idf.identify(ITEM)
        second = idf.identify(ITEM)
        assert first == second
        assert len(chat.calls) == 1, "quota is the scarce resource"
        assert idf.cache_hits == 1

    def test_it_survives_a_restart(self, tmp_path):
        path = tmp_path / "c.json"
        cache = Cache(path)
        first = VisionIdentifier(stub(), "m", cache=cache).identify(ITEM)
        cache.save()

        chat = stub()
        reloaded = VisionIdentifier(chat, "m", cache=Cache(path)).identify(ITEM)
        assert reloaded == first
        assert chat.calls == []

    def test_a_corrupt_cache_starts_empty_rather_than_crashing(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text("{ this is not json", encoding="utf-8")
        assert len(Cache(path)) == 0

    def test_a_refused_identification_is_not_cached(self, tmp_path):
        """Otherwise one bad afternoon of quota errors poisons the store."""
        cache = Cache(tmp_path / "c.json")
        VisionIdentifier(stub(exc=RuntimeError("429")), "m", cache=cache).identify(ITEM)
        assert len(cache) == 0


class TestScope:
    def test_a_missing_model_needs_vision(self):
        assert needs_vision(parse("iPhone")) is True

    def test_a_missing_storage_also_needs_vision(self):
        """Storage keys a bucket. Pricing a 512GB against the 64GB tier is a
        hundred-pound error, which is why 59 of 96 live listings are in scope
        here rather than the 8 that fail to parse outright."""
        spec = parse("iPhone 13 Pro")
        assert spec.model is not None and spec.storage_gb is None
        assert needs_vision(spec) is True

    def test_a_fully_resolved_listing_does_not(self):
        assert needs_vision(parse("iPhone 13 Pro 256GB unlocked")) is False


class TestMessages:
    def test_photos_and_title_both_reach_the_model(self):
        messages = build_messages(ITEM, photo_urls(ITEM))
        content = messages[0]["content"]
        assert content[0]["type"] == "text"
        assert "iPhone" in content[0]["text"]
        assert [c["type"] for c in content[1:]] == ["image_url", "image_url"]

    def test_photo_count_is_capped(self):
        many = {"photos": [{"full_size_url": f"u{i}"} for i in range(9)]}
        assert len(photo_urls(many)) == 4

    def test_falls_back_to_the_single_main_photo(self):
        item = {"photo": {"full_size_url": "https://images1.vinted.net/t/x/f800/1.webp"}}
        assert photo_urls(item) == ["https://images1.vinted.net/t/x/f800/1.webp"]
