"""Record once, then develop and review offline.

Why this exists
---------------
Every parser in this project was written against a live endpoint, and it cost
more than it should have. Vinted 403s per exit IP, CeX needs a headed browser
behind Cloudflare, eBay rate-limits within about ten requests. A single board
run touched all three, so iterating on a regex meant waiting on three
marketplaces and occasionally earning a block that outlasted the edit.

Worse, it made results unreproducible. When the eBay iPhone 12 median moved
from 212 to 152 there was no way to tell how much was the variant fix and how
much was a different set of live listings, because the feed had moved
underneath the change.

The harness fixes both. Capture the real responses once, then run the whole
pipeline against them: instant, offline, and identical every time, so a diff
in the output is a diff caused by the code.

Modes
-----
    replay   (default) no network at all; a missing recording RAISES
    record   fetch live, save what comes back
    live     fetch live, save nothing

Set with PHONELOOP_HARNESS, or pass mode= explicitly.

A missing recording is an error, never an empty result. That rule is the whole
value of the thing: this codebase has already shipped a photo counter that
returned zero for every real listing, and an availability probe that scored an
unreadable page as a sale. Silence that looks like data is the failure mode
this project keeps hitting, so replay refuses to invent it.

Fixtures are REAL captured bytes, never hand-written. A fixture that claims to
be real and is not has caused two bugs here already.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit, urlunsplit

MODE_ENV = "PHONELOOP_HARNESS"
REPLAY, RECORD, LIVE = "replay", "record", "live"
_MODES = (REPLAY, RECORD, LIVE)

DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "fixtures"

# Where a stream's recordings live when they should not touch the repo's own
# fixtures/ directory -- the eBay niche finder build records into
# ~/.phoneloop/fixtures, well outside the repo and outside OneDrive, the same
# reason DATA_DIR moved (see phoneloop/config.py). Set with PHONELOOP_FIXTURES;
# unset keeps every existing caller pointed at DEFAULT_ROOT exactly as before.
FIXTURES_ENV = "PHONELOOP_FIXTURES"


def fixtures_root(env: dict[str, str] | None = None) -> Path:
    """Where recordings live: PHONELOOP_FIXTURES when set, else DEFAULT_ROOT.

    Only Cassette.__init__ (when its `root` argument is omitted) consults this.
    Nothing else in the codebase re-implements the lookup, so there is exactly
    one place that decides what "unset" means.
    """
    e = env if env is not None else os.environ
    value = e.get(FIXTURES_ENV)
    return Path(value) if value else DEFAULT_ROOT

# Anything matching these never reaches a fixture file. The proxy URL carries a
# password, and requests puts it verbatim into ProxyError text; Vinted's cookies
# are a live session. Neither is needed to replay a response, and a fixture is a
# file that gets committed.
_SECRET_HEADERS = frozenset({
    "cookie", "set-cookie", "authorization", "x-api-key",
    "proxy-authorization", "x-csrf-token",
})
_SECRET_IN_URL = re.compile(r"//[^/@]*@")

# Headers worth keeping: they are what the parsers or a future debugger need.
_KEEP_HEADERS = frozenset({"content-type", "content-encoding", "x-ratelimit-remaining"})

# Query parameters that change between runs without changing what comes back.
_VOLATILE_PARAMS = frozenset({"_", "ts", "timestamp", "nonce", "cb", "_ipg_ts"})


class MissingRecording(LookupError):
    """Replay was asked for something that was never captured.

    Raised rather than returning an empty response, because an empty response
    is indistinguishable from a quiet market and would hide the gap for as long
    as it took someone to notice the board had gone silent.
    """


class HarnessError(RuntimeError):
    """The harness was used in a way that cannot be honoured."""


def mode(env: dict[str, str] | None = None) -> str:
    """Current mode. Defaults to replay, so nothing reaches the network by
    accident -- going live has to be asked for."""
    value = (env if env is not None else os.environ).get(MODE_ENV, REPLAY)
    value = value.strip().lower()
    if value not in _MODES:
        raise HarnessError(
            f"{MODE_ENV}={value!r} is not one of {', '.join(_MODES)}"
        )
    return value


def redact_url(url: str) -> str:
    return _SECRET_IN_URL.sub("//***:***@", url or "")


def canonical_url(url: str) -> str:
    """A URL reduced to what actually selects a response.

    Query parameters are sorted so that two requests differing only in
    parameter order share a recording, and cache-busting parameters are dropped
    so a replay is not missed over a timestamp.
    """
    parts = urlsplit(url)
    pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in _VOLATILE_PARAMS]
    query = "&".join(f"{k}={v}" for k, v in sorted(pairs))
    # Drop any user:password@ before it reaches the key -- and therefore the
    # FILENAME. Credentials do not select a response, so they are not part of
    # the identity of one, and a fixtures directory is a thing that gets
    # committed. Caught by its own test on the first run.
    host = parts.netloc.rsplit("@", 1)[-1]
    # "https://host" and "https://host/" are the same resource. They were
    # producing different keys, so a homepage recorded by capture.py could not
    # be found by handshake(), which asks for the trailing-slash form.
    path = parts.path or "/"
    return urlunsplit((parts.scheme, host, path, query, ""))


def _slug(text: str, limit: int = 48) -> str:
    """A readable filename stem, so a fixtures directory can be browsed."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (cleaned[:limit].rstrip("-") or "x")


def key_for(method: str, url: str) -> str:
    """Stable, readable, collision-resistant.

    The hash is what guarantees correctness; the slug in front of it is purely
    so a human can find the right file without opening every one.
    """
    canonical = f"{method.upper()} {canonical_url(url)}"
    digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]
    parts = urlsplit(canonical_url(url))
    stem = _slug(f"{parts.netloc}{parts.path}")
    return f"{stem}-{digest}"


def scrub_headers(headers) -> dict[str, str]:
    if not headers:
        return {}
    return {
        k.lower(): v for k, v in dict(headers).items()
        if k.lower() in _KEEP_HEADERS and k.lower() not in _SECRET_HEADERS
    }


@dataclass
class Recording:
    """One captured response, or one captured browser extraction."""

    key: str
    kind: str
    status: int = 200
    url: str = ""
    body: str = ""
    headers: dict = field(default_factory=dict)
    data: object = None            # for browser extractions: rows, text, etc.
    recorded_at: str = ""

    def to_json(self) -> dict:
        return {
            "key": self.key, "kind": self.kind, "status": self.status,
            "url": redact_url(self.url), "headers": self.headers,
            "body": self.body, "data": self.data,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_json(cls, payload: dict) -> Recording:
        return cls(
            key=payload["key"], kind=payload.get("kind", "http"),
            status=payload.get("status", 200), url=payload.get("url", ""),
            body=payload.get("body", ""), headers=payload.get("headers", {}),
            data=payload.get("data"), recorded_at=payload.get("recorded_at", ""),
        )


class Cassette:
    """A directory of recordings, one gzipped JSON file each.

    Gzipped because a single Vinted listing page is roughly 1.9MB of HTML and
    the whole point is to keep the real bytes rather than a trimmed stand-in --
    trimming is how a fixture stops being evidence.
    """

    def __init__(self, root: Path | str | None = None):
        """root=None consults PHONELOOP_FIXTURES, then falls back to DEFAULT_ROOT.

        An explicit root (Path or str) is used verbatim and never looks at the
        environment -- so a caller pinning a specific cassette directory keeps
        working exactly as before, env var or not.
        """
        self.root = Path(root) if root is not None else fixtures_root()

    def path_for(self, kind: str, key: str) -> Path:
        return self.root / kind / f"{key}.json.gz"

    def read(self, kind: str, key: str) -> Recording | None:
        path = self.path_for(kind, key)
        if not path.exists():
            return None
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return Recording.from_json(json.load(fh))

    def write(self, rec: Recording) -> Path:
        path = self.path_for(rec.kind, rec.key)
        path.parent.mkdir(parents=True, exist_ok=True)
        rec.recorded_at = rec.recorded_at or datetime.now(UTC).isoformat()
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            json.dump(rec.to_json(), fh, indent=1)
        return path

    def keys(self, kind: str) -> list[str]:
        folder = self.root / kind
        if not folder.exists():
            return []
        return sorted(p.name[: -len(".json.gz")] for p in folder.glob("*.json.gz"))

    def __len__(self) -> int:
        return sum(len(self.keys(d.name)) for d in self.root.glob("*")
                   if d.is_dir())


class ReplayResponse:
    """The parts of a requests.Response this project actually reads."""

    def __init__(self, rec: Recording):
        self._rec = rec
        self.status_code = rec.status
        self.text = rec.body
        self.url = rec.url
        self.headers = dict(rec.headers)

    @property
    def content(self) -> bytes:
        return self._rec.body.encode("utf-8", "replace")

    def json(self):
        return json.loads(self._rec.body)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise HarnessError(f"recorded status {self.status_code}")


class HarnessSession:
    """Stands in for a requests.Session across record, replay and live.

    Only .get is wrapped, because that is all this project issues. A .post
    would need the body in the key and there is no honest way to fake that
    silently, so it refuses instead.
    """

    def __init__(self, cassette: Cassette, inner=None, harness_mode: str | None = None,
                 kind: str = "http"):
        self.cassette = cassette
        self.inner = inner
        self.kind = kind
        self.mode = harness_mode or mode()
        self.hits: list[str] = []
        self.misses: list[str] = []
        # Callers treat this like a requests.Session and set headers on it --
        # VintedClient.handshake() does exactly that. On replay they select
        # nothing, since the response is already chosen by URL, but the
        # attribute has to exist or the client cannot be driven offline at all.
        self.headers: dict[str, str] = {}
        if self.mode in (RECORD, LIVE) and inner is None:
            raise HarnessError(f"mode {self.mode} needs a real session to fetch with")

    # requests.Session compatibility ---------------------------------------
    @property
    def cookies(self):
        return self.inner.cookies if self.inner is not None else _NullCookies()

    def close(self):
        if self.inner is not None:
            self.inner.close()

    def get(self, url, **kwargs):
        key = key_for("GET", url)
        if self.mode == REPLAY:
            rec = self.cassette.read(self.kind, key)
            if rec is None:
                self.misses.append(url)
                raise MissingRecording(
                    f"no recording for GET {redact_url(url)}\n"
                    f"  key:  {key}\n"
                    f"  kind: {self.kind}\n"
                    f"  capture it with: PHONELOOP_HARNESS=record "
                    "python scripts/capture.py"
                )
            self.hits.append(url)
            return ReplayResponse(rec)

        # Merge session headers under any per-request ones, the way requests
        # does, so a recorded request is the request the client would make.
        merged = dict(self.headers)
        merged.update(kwargs.pop("headers", None) or {})
        response = self.inner.get(url, headers=merged or None, **kwargs)
        if self.mode == RECORD:
            self.cassette.write(Recording(
                key=key, kind=self.kind, status=response.status_code,
                url=redact_url(url), body=response.text,
                headers=scrub_headers(response.headers),
            ))
        return response


class _NullCookies:
    """Replay has no session to keep, and clearing nothing must not explode."""

    def clear(self):
        return None

    def __len__(self):
        return 0

    def __iter__(self):
        return iter(())


def recorded(cassette: Cassette, kind: str, name: str, produce,
             harness_mode: str | None = None):
    """Replay a non-HTTP capture, or run `produce` and record what it returns.

    This is the browser half. CeX and eBay are reached with a headed Chrome
    session, so there is no response object to store -- what matters is the
    extraction: the page text, or the list of result rows. Recording that makes
    every parser downstream of it developable offline.

    `produce` is only ever called in record or live mode, so replay genuinely
    touches nothing.
    """
    current = harness_mode or mode()
    key = f"{_slug(name)}-{hashlib.sha1(name.encode()).hexdigest()[:8]}"

    if current == REPLAY:
        rec = cassette.read(kind, key)
        if rec is None:
            raise MissingRecording(
                f"no recording for {kind}/{name}\n"
                f"  key: {key}\n"
                f"  capture it with: PHONELOOP_HARNESS=record "
                "python scripts/capture.py"
            )
        return rec.data

    data = produce()
    if current == RECORD:
        cassette.write(Recording(key=key, kind=kind, url=name, data=data))
    return data
