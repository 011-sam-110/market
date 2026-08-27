"""Thin transport to an OpenAI-compatible chat endpoint.

Kept apart from identify.py on purpose: that module holds the decisions worth
testing, and this one holds the HTTP. Splitting them is what lets the whole
identification path be tested with no key, no network and no quota -- which was
not a hypothetical, since every provider behind the gateway was returning 429
on the day it was written.

Configured from ~/.phoneloop/.env:

    PHONELOOP_LLM_BASE_URL   e.g. http://localhost:3011/v1
    PHONELOOP_LLM_KEY
    PHONELOOP_VISION_MODEL   pin a strong one; "auto" routes images to weak
                             models and returns confident nonsense
"""

from __future__ import annotations

import logging

import requests

from . import proxies

log = logging.getLogger("phoneloop.llm")

DEFAULT_MODEL = "gemini-2.5-flash"
TIMEOUT_S = 90

# Anthropic's own endpoint, used when PHONELOOP_ANTHROPIC_KEY is set. Preferred
# over the free gateway because the gateway returns 429 across every provider
# behind it under load, and an identifier that cannot answer is indistinguishable
# from one that found nothing.
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

# Haiku is the right default here and the choice is about volume, not capability.
# Identification runs on roughly sixty listings an hour, each carrying up to four
# photographs, and the question asked is a narrow one: read a model name and a
# capacity off a handset. Reach for a larger model only if measured accuracy on
# hand-graded listings proves inadequate.
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"


class LLMUnavailable(RuntimeError):
    """The endpoint could not answer. Callers treat this as "unknown"."""


def settings(env: dict[str, str] | None = None) -> dict[str, str]:
    env = proxies.load_env_file() if env is None else env
    return {
        "base_url": env.get("PHONELOOP_LLM_BASE_URL", "").rstrip("/"),
        "key": env.get("PHONELOOP_LLM_KEY", ""),
        "model": env.get("PHONELOOP_VISION_MODEL", DEFAULT_MODEL),
        "anthropic_key": env.get("PHONELOOP_ANTHROPIC_KEY", ""),
        "anthropic_model": env.get("PHONELOOP_ANTHROPIC_MODEL", ANTHROPIC_MODEL),
    }


def make_chat(base_url: str, key: str, session=None):
    """Build a `chat(model, messages) -> str` for VisionIdentifier.

    Note this session is NOT routed through the residential proxy. The gateway
    is on localhost; sending it out through an exit node in another country and
    back would be slower, less reliable, and would burn proxy bandwidth on
    traffic that never leaves the machine.
    """
    http = session or requests.Session()

    def chat(model: str, messages: list[dict]) -> str:
        try:
            response = http.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                json={"model": model, "messages": messages,
                      "max_tokens": 300, "temperature": 0},
                timeout=TIMEOUT_S,
            )
        except requests.RequestException as exc:
            raise LLMUnavailable(f"transport: {type(exc).__name__}") from exc

        if response.status_code == 429:
            # The gateway says every provider behind it is exhausted. Distinct
            # from a bad request, and worth naming so a run that identifies
            # nothing is not mistaken for a run that found nothing to identify.
            raise LLMUnavailable("429 quota exhausted across all providers")
        if response.status_code != 200:
            raise LLMUnavailable(f"HTTP {response.status_code}: {response.text[:120]}")

        try:
            payload = response.json()
            return payload["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError) as exc:
            raise LLMUnavailable(f"unexpected payload shape: {exc}") from exc

    return chat


def available(base_url: str, key: str) -> tuple[bool, str]:
    """Cheap check that the endpoint is up and has quota to spare."""
    if not base_url or not key:
        return False, "no base_url or key configured"
    try:
        r = requests.get(f"{base_url}/models",
                         headers={"Authorization": f"Bearer {key}"}, timeout=20)
    except requests.RequestException as exc:
        return False, f"{type(exc).__name__}"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    return True, f"{len(r.json().get('data', []))} models"


# --- Anthropic ---------------------------------------------------------------


def to_anthropic(messages: list[dict]) -> list[dict]:
    """Translate the OpenAI-shaped messages identify.py builds.

    identify.py stays provider-neutral on purpose, so the translation lives
    here. The only real difference is images: OpenAI nests a url inside an
    `image_url` object, Anthropic takes a `source` block. Anthropic fetches
    remote image urls itself, so the photographs never pass through this
    machine -- no downloading, no re-encoding, no base64 bloat in the request.
    """
    out = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            out.append({"role": message["role"], "content": content})
            continue
        blocks = []
        for part in content:
            if part.get("type") == "image_url":
                blocks.append({
                    "type": "image",
                    "source": {"type": "url",
                               "url": part["image_url"]["url"]},
                })
            else:
                blocks.append({"type": "text", "text": part.get("text", "")})
        out.append({"role": message["role"], "content": blocks})
    return out


def make_anthropic_chat(key: str, session=None, max_tokens: int = 300):
    """Build a `chat(model, messages) -> str` backed by Anthropic directly.

    Same callable shape as make_chat, so VisionIdentifier does not know or care
    which provider it is talking to.
    """
    http = session or requests.Session()

    def chat(model: str, messages: list[dict]) -> str:
        try:
            response = http.post(
                ANTHROPIC_URL,
                headers={"x-api-key": key,
                         "anthropic-version": ANTHROPIC_VERSION,
                         "content-type": "application/json"},
                json={"model": model, "max_tokens": max_tokens,
                      "temperature": 0, "messages": to_anthropic(messages)},
                timeout=TIMEOUT_S,
            )
        except requests.RequestException as exc:
            raise LLMUnavailable(f"transport: {type(exc).__name__}") from exc

        if response.status_code in (401, 403):
            raise LLMUnavailable(f"HTTP {response.status_code}: key rejected")
        if response.status_code == 429:
            raise LLMUnavailable("429 rate limited by Anthropic")
        if response.status_code != 200:
            raise LLMUnavailable(
                f"HTTP {response.status_code}: {response.text[:160]}")

        try:
            payload = response.json()
            # content is a list of blocks; concatenate the text ones so a
            # response split across blocks is not silently truncated to the
            # first.
            return "".join(b.get("text", "") for b in payload["content"]
                           if b.get("type") == "text")
        except (ValueError, KeyError, TypeError) as exc:
            raise LLMUnavailable(f"unexpected payload shape: {exc}") from exc

    return chat


def resolve(conf: dict, session=None):
    """Pick a backend from configuration. Anthropic wins when a key is present.

    Returns (chat, model, provider). Raises LLMUnavailable when nothing is
    configured, rather than returning a callable that fails later at a point
    where the failure reads as "identified nothing".
    """
    if conf.get("anthropic_key"):
        return (make_anthropic_chat(conf["anthropic_key"], session),
                conf.get("anthropic_model") or ANTHROPIC_MODEL,
                "anthropic")
    if conf.get("base_url") and conf.get("key"):
        return (make_chat(conf["base_url"], conf["key"], session),
                conf.get("model") or DEFAULT_MODEL,
                "gateway")
    raise LLMUnavailable(
        "no vision backend configured - set PHONELOOP_ANTHROPIC_KEY "
        "(preferred) or PHONELOOP_LLM_BASE_URL and PHONELOOP_LLM_KEY "
        "in ~/.phoneloop/.env"
    )
