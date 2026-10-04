"""One DeepSeek call per market, and the prompt that goes with it.

The transport sends ONE request and does not retry. Each request is a model
call, and the ledger counts each call before it is sent. A retry would be a
second call, so a failed call costs its slot and the market waits for
another day.

The prompt does NOT show the market price. The record compares the agent's
Brier score with the market price's Brier score. An agent that sees the price
anchors on it, and then the comparison measures the price twice.

The key comes from DEEPSEEK_API_KEY, or from the one line that names it in
a KEY=VALUE file (the box's ~/.config/newsfeed/newsfeed.env). No other line
of that file is read, and the key is never printed or stored.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from forecast.gamma import Market

CHAT_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_KEY_FILE = Path.home() / ".config" / "newsfeed" / "newsfeed.env"
MAX_TOKENS = 600
TIMEOUT_S = 120
PROMPT_VERSION = "forecast-v1"
DESCRIPTION_CHARS = 1500

Transport = Callable[[str, dict[str, str], bytes], tuple[int, bytes]]


class ModelError(RuntimeError):
    """The call did not give a usable answer. Its slot is still used."""


class AnswerError(ModelError):
    """The model answered, but not with the JSON the prompt asked for."""


def load_key(env: dict[str, str] | None = None,
             key_file: str | os.PathLike | None = None) -> str:
    """The DeepSeek key, or "" when there is none. Never logged."""
    env = os.environ if env is None else env
    key = (env.get("DEEPSEEK_API_KEY") or "").strip()
    if key:
        return key
    path = Path(key_file).expanduser() if key_file else DEFAULT_KEY_FILE
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("DEEPSEEK_API_KEY="):
            return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def urllib_transport(url: str, headers: dict[str, str], body: bytes
                     ) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@dataclass(frozen=True)
class Completion:
    content: str
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None


class DeepSeek:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL,
                 transport: Transport | None = None) -> None:
        if not api_key:
            raise ModelError("no DeepSeek key: set DEEPSEEK_API_KEY or --key-file")
        self._key = api_key
        self.model = model
        self.transport = transport or urllib_transport

    def __repr__(self) -> str:  # never show the key
        return f"DeepSeek(model={self.model!r})"

    def complete(self, system: str, user: str) -> Completion:
        payload = {
            "model": self.model,
            "thinking": {"type": "disabled"},
            "temperature": 0.0,
            "max_tokens": MAX_TOKENS,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }
        headers = {"Authorization": f"Bearer {self._key}",
                   "Content-Type": "application/json",
                   "Accept": "application/json"}
        try:
            status, raw = self.transport(
                CHAT_URL, headers, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        except (TimeoutError, OSError) as exc:
            raise ModelError(f"transport: {type(exc).__name__}") from exc
        if status != 200:
            raise ModelError(f"HTTP {status}: {_reason(raw)}")
        try:
            answer = json.loads(raw.decode("utf-8", "replace"))
            choice = answer["choices"][0]
            content = choice["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ModelError(f"unexpected payload shape ({type(exc).__name__})") from exc
        usage = answer.get("usage") if isinstance(answer.get("usage"), dict) else {}
        return Completion(content=content, model=str(answer.get("model") or self.model),
                          prompt_tokens=usage.get("prompt_tokens"),
                          completion_tokens=usage.get("completion_tokens"))


def _reason(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace").strip()
    try:
        payload = json.loads(text)
    except ValueError:
        return text[:160]
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or "")[:160]
    return text[:160]


# --- the prompt --------------------------------------------------------------

SYSTEM = """You are a careful forecaster. You estimate the probability of one outcome of a \
two-outcome question. You get the question, its resolution rules, the date today, and some \
evidence. The evidence can be old, off-topic or wrong. Use your own knowledge as well, and say \
when you have little to go on. A probability near 0.5 is the right answer when you know little.

Answer with one JSON object and nothing else:
{"probability": <number from 0 to 1, the probability that OUTCOME A happens>,
 "reasoning": "<at most 80 words>",
 "evidence_used": ["<ids of the evidence items you relied on, for example R1 or N2>"]}"""


def build_prompt(market: Market, evidence: list[dict], now: datetime) -> tuple[str, str]:
    """The system and user messages. The market price is NOT in either."""
    lines = [
        f"Today (UTC): {now.strftime('%Y-%m-%d')}",
        f"Question: {market.question}",
        f"OUTCOME A: {market.outcomes[0]}",
        f"OUTCOME B: {market.outcomes[1]}",
        f"The question closes: {market.end_date.strftime('%Y-%m-%d %H:%M UTC') if market.end_date else 'unknown'}",
    ]
    if market.event_title and market.event_title != market.question:
        lines.append(f"Event: {market.event_title}")
    description = (market.description or "").strip()
    if description:
        lines.append("Resolution rules:")
        lines.append(description[:DESCRIPTION_CHARS])
    lines.append("")
    if evidence:
        lines.append("Evidence:")
        for item in evidence:
            lines.append(f"[{item['id']}] {_evidence_line(item)}")
    else:
        lines.append("Evidence: none found.")
    lines.append("")
    lines.append("What is the probability that OUTCOME A happens?")
    return SYSTEM, "\n".join(lines)


def _evidence_line(item: dict) -> str:
    if item.get("kind") == "reddit":
        quote = (item.get("quote") or "").replace("\n", " ")
        return (f"Reddit r/{item.get('subreddit', '')}: {item.get('title', '')}"
                + (f" | \"{quote[:400]}\"" if quote else ""))
    return (f"News ({item.get('outlet', '')}, {str(item.get('published') or '')[:10]}): "
            f"{item.get('title', '')}")


def parse_answer(content: str) -> tuple[float, str, list[str]]:
    """Probability, reasoning and the evidence ids the model says it used."""
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):] if "{" in text else text
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise AnswerError("answer is not JSON") from exc
    if not isinstance(payload, dict):
        raise AnswerError("answer is not a JSON object")
    raw = payload.get("probability")
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise AnswerError("no probability")
    try:
        probability = float(raw)
    except ValueError as exc:
        raise AnswerError("probability is not a number") from exc
    if not (0.0 <= probability <= 1.0) or probability != probability:
        raise AnswerError(f"probability {probability} is outside 0..1")
    reasoning = str(payload.get("reasoning") or "")[:1200]
    used = payload.get("evidence_used")
    used_ids = [str(u) for u in used] if isinstance(used, list) else []
    return probability, reasoning, used_ids
