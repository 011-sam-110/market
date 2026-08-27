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


class LLMUnavailable(RuntimeError):
    """The endpoint could not answer. Callers treat this as "unknown"."""


def settings(env: dict[str, str] | None = None) -> dict[str, str]:
    env = proxies.load_env_file() if env is None else env
    return {
        "base_url": env.get("PHONELOOP_LLM_BASE_URL", "").rstrip("/"),
        "key": env.get("PHONELOOP_LLM_KEY", ""),
        "model": env.get("PHONELOOP_VISION_MODEL", DEFAULT_MODEL),
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
