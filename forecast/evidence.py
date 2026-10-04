"""Evidence for one market: Reddit hits from Prospector, and NewsScraper headlines.

Both sources are optional. When one is missing, the forecast still goes
ahead, and the row records why the source gave nothing ("no reddit
evidence: ..."). A missing source must never look like a source that found
nothing, so the two states have different words.

Reddit comes through one CLI contract, owned by Prospector:

    prospector semantic-search "QUERY" --db PATH --limit N --json

It prints a JSON array of {permalink, subreddit, title, quote, score,
created_utc}. This module runs that command and reads nothing else of
Prospector's.

News comes from NewsScraper's SQLite store on the same box. The connection
is read-only (`mode=ro`), so this module cannot change that store.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

REDDIT_LIMIT = 5
NEWS_LIMIT = 8
NEWS_DAYS = 14
PROSPECTOR_TIMEOUT_S = 120

DEFAULT_PROSPECTOR_BIN = Path.home() / "prospector" / ".venv" / "bin" / "prospector"
# Prospector's own default is "prospector.db" in its working directory. The
# box layout is Prospector's decision, so these are the places it can be,
# in order. PROSPECTOR_DB (Prospector's own variable) wins over all of them.
PROSPECTOR_DB_CANDIDATES = (
    Path.home() / "prospector" / "prospector.db",
    Path.home() / "prospector-data" / "prospector.db",
    Path.home() / ".local" / "share" / "prospector" / "prospector.db",
    Path.home() / ".prospector" / "prospector.db",
)
DEFAULT_NEWS_DB = Path.home() / ".local" / "share" / "newsscraper" / "news.sqlite3"

Runner = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class Evidence:
    items: list[dict]
    status: str


# --- the query --------------------------------------------------------------

STOPWORDS = frozenset("""
a about above after again against all am an and any are as at be been before
being below between both but by can could did do does doing down during each
end ends ending few for from further had has have having he her here hers him
his how i if in into is it its itself just me more most my no nor not now of
off on once only or other our out over own same she should so some such than
that the their them then there these they this those through to too under
until up very was we were what when where which while who whom why will with
would you your yes
win wins winner won lose loses beat beats vs versus game match total totals
spread handicap price reach reached hit dip happen happens occur announce
announced announces say says said market odds over under o/u
january february march april may june july august september october november
december jan feb mar apr jun jul aug sep sept oct nov dec
monday tuesday wednesday thursday friday saturday sunday
today tomorrow week weekend month year day days
""".split())

_TOKEN = re.compile(r"[A-Za-z][A-Za-z'\-]+")


def keywords(question: str, extra: Sequence[str] = ()) -> list[str]:
    """Words worth matching in a headline, lower case, in first-seen order.

    An all-capitals acronym (NATO, US) is kept at two letters. Any other word
    needs four letters, because "ran" in a headline matches nothing useful.
    """
    out: list[str] = []
    for text in (question, *extra):
        for raw in _TOKEN.findall(text or ""):
            word = raw.strip("'-")
            low = word.lower()
            if low.endswith("'s"):
                low = low[:-2]
            if low in STOPWORDS:
                continue
            keep = (len(word) >= 2 and word.isupper()) or len(low) >= 4
            if keep and low not in out:
                out.append(low)
    return out


# --- Reddit, through Prospector ---------------------------------------------

def find_prospector_db(explicit: str | os.PathLike | None = None) -> Path | None:
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("PROSPECTOR_DB")
    if env:
        return Path(env).expanduser()
    for candidate in PROSPECTOR_DB_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def _absolute_permalink(permalink: str) -> str:
    if permalink.startswith("/"):
        return f"https://www.reddit.com{permalink}"
    return permalink


def reddit_evidence(query: str, *, binary: Path | None, db: Path | None,
                    limit: int = REDDIT_LIMIT, run: Runner = subprocess.run
                    ) -> Evidence:
    if binary is None or not Path(binary).exists():
        return Evidence([], f"no reddit evidence: prospector binary not found at {binary}")
    if db is None:
        return Evidence([], "no reddit evidence: no prospector store found")
    if not Path(db).exists():
        return Evidence([], f"no reddit evidence: prospector store not found at {db}")
    cmd = [str(binary), "semantic-search", query, "--db", str(db),
           "--limit", str(limit), "--json"]
    try:
        done = run(cmd, capture_output=True, text=True, timeout=PROSPECTOR_TIMEOUT_S,
                   check=False)
    except subprocess.TimeoutExpired:
        return Evidence([], f"no reddit evidence: prospector timed out after "
                            f"{PROSPECTOR_TIMEOUT_S}s")
    except OSError as exc:
        return Evidence([], f"no reddit evidence: prospector did not start "
                            f"({type(exc).__name__})")
    if done.returncode != 0:
        tail = (done.stderr or "").strip().splitlines()[-1:] or [""]
        return Evidence([], f"no reddit evidence: prospector exited {done.returncode} "
                            f"{tail[0][:120]}".rstrip())
    try:
        payload = json.loads(done.stdout or "")
    except ValueError:
        return Evidence([], "no reddit evidence: prospector output is not JSON")
    if not isinstance(payload, list):
        return Evidence([], "no reddit evidence: prospector output is not a JSON array")
    items: list[dict] = []
    dropped = 0
    for hit in payload:
        if not isinstance(hit, dict) or not isinstance(hit.get("permalink"), str) \
                or not hit["permalink"]:
            dropped += 1
            continue
        items.append({
            "kind": "reddit",
            "url": _absolute_permalink(hit["permalink"]),
            "subreddit": str(hit.get("subreddit") or ""),
            "title": str(hit.get("title") or "")[:300],
            "quote": str(hit.get("quote") or "")[:600],
            "score": hit.get("score"),
            "created_utc": hit.get("created_utc"),
        })
        if len(items) >= limit:
            break
    status = f"reddit: {len(items)} hits"
    if dropped:
        status += f" ({dropped} without a permalink dropped)"
    return Evidence(items, status)


# --- NewsScraper headlines ---------------------------------------------------

def _match_score(words: list[str], text: str) -> int:
    hits = 0
    for word in words:
        if re.search(rf"\b{re.escape(word)}", text):
            hits += 1
    return hits


def news_evidence(question: str, *, db: Path | None, now: datetime,
                  extra: Sequence[str] = (), days: int = NEWS_DAYS,
                  limit: int = NEWS_LIMIT) -> Evidence:
    if db is None or not Path(db).is_file():
        return Evidence([], f"no news evidence: NewsScraper store not found at {db}")
    words = keywords(question, extra)
    if not words:
        return Evidence([], "news: no keywords in the question")
    cutoff = (now - timedelta(days=days)).astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    uri = f"{Path(db).resolve().as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=10)
        try:
            rows = conn.execute(
                "SELECT outlet, headline, description, url, published, first_seen_at "
                "FROM stories WHERE first_seen_at >= ? AND headline IS NOT NULL",
                (cutoff,)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return Evidence([], f"no news evidence: NewsScraper store unreadable "
                            f"({type(exc).__name__})")
    need = 1 if len(words) == 1 else 2
    scored = []
    for outlet, headline, description, url, published, first_seen in rows:
        text = f"{headline or ''} {description or ''}".lower()
        score = _match_score(words, text)
        if score >= need:
            scored.append((score, published or first_seen or "", outlet, headline,
                           url))
    # Highest score first, newest first within a score.
    scored.sort(key=lambda r: (r[0], r[1]), reverse=True)
    items = [{
        "kind": "news",
        "url": url or "",
        "outlet": outlet,
        "title": (headline or "")[:300],
        "published": published,
        "matched": score,
    } for score, published, outlet, headline, url in scored[:limit]]
    return Evidence(items, f"news: {len(items)} headlines from {len(rows)} stories "
                           f"in {days} days (keywords: {', '.join(words[:8])})")
