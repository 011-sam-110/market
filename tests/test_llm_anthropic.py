"""The Anthropic backend, tested without a key, a network or a penny of quota.

Same reason llm.py and identify.py were split in the first place: the decisions
worth pinning are message translation and error classification, and neither
needs a live provider.
"""

from __future__ import annotations

import json

import pytest

from phoneloop import llm


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or json.dumps(payload or {})

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


OPENAI_SHAPED = [{
    "role": "user",
    "content": [
        {"type": "text", "text": "what handset is this?"},
        {"type": "image_url", "image_url": {"url": "https://img/a.jpg"}},
        {"type": "image_url", "image_url": {"url": "https://img/b.jpg"}},
    ],
}]


class TestTranslation:
    def test_images_become_source_blocks(self):
        out = llm.to_anthropic(OPENAI_SHAPED)
        blocks = out[0]["content"]
        assert blocks[0] == {"type": "text", "text": "what handset is this?"}
        assert blocks[1] == {"type": "image",
                             "source": {"type": "url", "url": "https://img/a.jpg"}}

    def test_every_photo_survives_translation(self):
        """Dropping one silently would degrade identification in a way that
        looks like the model being wrong rather than the request being short."""
        blocks = llm.to_anthropic(OPENAI_SHAPED)[0]["content"]
        assert sum(1 for b in blocks if b["type"] == "image") == 2

    def test_a_plain_string_message_passes_through(self):
        assert llm.to_anthropic([{"role": "user", "content": "hi"}]) == [
            {"role": "user", "content": "hi"}
        ]


class TestChat:
    def _chat(self, response):
        session = FakeSession(response)
        return llm.make_anthropic_chat("sk-test", session), session

    def test_it_returns_the_text(self):
        chat, _ = self._chat(FakeResponse(
            200, {"content": [{"type": "text", "text": '{"model":"iphone-13"}'}]}))
        assert chat("m", OPENAI_SHAPED) == '{"model":"iphone-13"}'

    def test_text_split_across_blocks_is_joined_not_truncated(self):
        chat, _ = self._chat(FakeResponse(
            200, {"content": [{"type": "text", "text": '{"model":'},
                              {"type": "text", "text": '"iphone-13"}'}]}))
        assert chat("m", OPENAI_SHAPED) == '{"model":"iphone-13"}'

    def test_non_text_blocks_are_ignored(self):
        chat, _ = self._chat(FakeResponse(
            200, {"content": [{"type": "thinking", "thinking": "hmm"},
                              {"type": "text", "text": "answer"}]}))
        assert chat("m", OPENAI_SHAPED) == "answer"

    def test_it_sends_the_key_as_x_api_key_not_a_bearer(self):
        chat, session = self._chat(FakeResponse(
            200, {"content": [{"type": "text", "text": "ok"}]}))
        chat("claude-haiku-4-5-20251001", OPENAI_SHAPED)
        headers = session.calls[0]["headers"]
        assert headers["x-api-key"] == "sk-test"
        assert "Authorization" not in headers
        assert headers["anthropic-version"] == llm.ANTHROPIC_VERSION

    def test_max_tokens_is_always_sent(self):
        """Anthropic rejects a request without it, and the failure would look
        like a bad key rather than a missing field."""
        chat, session = self._chat(FakeResponse(
            200, {"content": [{"type": "text", "text": "ok"}]}))
        chat("m", OPENAI_SHAPED)
        assert session.calls[0]["json"]["max_tokens"] == 300

    @pytest.mark.parametrize("status,expected", [
        (401, "key rejected"),
        (403, "key rejected"),
        (429, "rate limited"),
        (500, "HTTP 500"),
    ])
    def test_failures_are_named_not_swallowed(self, status, expected):
        chat, _ = self._chat(FakeResponse(status, {}, text="boom"))
        with pytest.raises(llm.LLMUnavailable) as exc:
            chat("m", OPENAI_SHAPED)
        assert expected in str(exc.value)

    def test_a_transport_error_is_unavailable_not_a_crash(self):
        import requests
        chat, _ = self._chat(requests.ConnectionError("no route"))
        with pytest.raises(llm.LLMUnavailable):
            chat("m", OPENAI_SHAPED)


class TestResolve:
    def test_anthropic_wins_when_a_key_is_present(self):
        conf = {"anthropic_key": "sk-x", "anthropic_model": "claude-haiku-4-5-20251001",
                "base_url": "http://localhost:3011/v1", "key": "gw"}
        _, model, provider = llm.resolve(conf)
        assert provider == "anthropic"
        assert model == "claude-haiku-4-5-20251001"

    def test_it_falls_back_to_the_gateway(self):
        conf = {"anthropic_key": "", "base_url": "http://localhost:3011/v1",
                "key": "gw", "model": "gemini-2.5-flash"}
        _, model, provider = llm.resolve(conf)
        assert provider == "gateway"
        assert model == "gemini-2.5-flash"

    def test_nothing_configured_raises_rather_than_failing_later(self):
        """A callable that fails at call time would make "no backend" look
        identical to "found nothing to identify"."""
        with pytest.raises(llm.LLMUnavailable) as exc:
            llm.resolve({"anthropic_key": "", "base_url": "", "key": ""})
        assert "PHONELOOP_ANTHROPIC_KEY" in str(exc.value)
