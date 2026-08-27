"""The record/replay harness.

The load-bearing property is that a missing recording RAISES. This project has
twice shipped code where an unreadable input scored as a result -- a photo
counter that returned zero for every real listing, and an availability probe
that read an anti-bot interstitial as a sale. A replay that quietly returned an
empty page would be the third.
"""

from __future__ import annotations

import gzip
import json

import pytest

from phoneloop import harness
from phoneloop.harness import (
    Cassette,
    HarnessError,
    HarnessSession,
    MissingRecording,
    Recording,
    canonical_url,
    key_for,
    mode,
    recorded,
    redact_url,
    scrub_headers,
)

PROXY = "http://user:sekrit@thehub.proxy-cheap.com:8080"


class FakeResponse:
    def __init__(self, status_code=200, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class FakeSession:
    def __init__(self, response=None):
        self.response = response or FakeResponse(200, "live body")
        self.requested = []
        self.closed = False

    def get(self, url, **kwargs):
        self.requested.append(url)
        return self.response

    def close(self):
        self.closed = True


class TestMode:
    def test_replay_is_the_default(self):
        """Nothing should reach the network unless it was asked for."""
        assert mode({}) == harness.REPLAY

    def test_an_unknown_mode_is_refused(self):
        with pytest.raises(HarnessError):
            mode({harness.MODE_ENV: "yolo"})

    @pytest.mark.parametrize("value", ["record", "RECORD", " live "])
    def test_it_is_forgiving_about_case_and_space(self, value):
        assert mode({harness.MODE_ENV: value}) in (harness.RECORD, harness.LIVE)


class TestKeys:
    def test_parameter_order_does_not_change_the_key(self):
        a = "https://x.test/api?b=2&a=1"
        b = "https://x.test/api?a=1&b=2"
        assert key_for("GET", a) == key_for("GET", b)

    def test_a_cache_buster_does_not_change_the_key(self):
        """Otherwise every replay misses over a timestamp."""
        plain = "https://x.test/api?catalog_ids=3565"
        busted = "https://x.test/api?catalog_ids=3565&_=1724740000"
        assert key_for("GET", plain) == key_for("GET", busted)

    def test_different_endpoints_do_not_collide(self):
        assert key_for("GET", "https://x.test/a") != key_for("GET", "https://x.test/b")

    def test_the_key_is_readable_enough_to_find_by_eye(self):
        key = key_for("GET", "https://www.vinted.co.uk/api/v2/catalog/items?x=1")
        assert key.startswith("www-vinted-co-uk-api-v2-catalog-items")

    def test_the_fragment_is_not_part_of_the_response(self):
        assert canonical_url("https://x.test/a#frag") == "https://x.test/a"


class TestRedaction:
    def test_the_proxy_password_never_survives(self):
        assert "sekrit" not in redact_url(PROXY)
        assert redact_url(PROXY) == "http://***:***@thehub.proxy-cheap.com:8080"

    def test_cookies_and_keys_are_dropped_from_headers(self):
        kept = scrub_headers({
            "Set-Cookie": "session=abc123",
            "Cookie": "session=abc123",
            "Authorization": "Bearer sk-live",
            "X-Api-Key": "sk-live",
            "Content-Type": "application/json",
        })
        assert kept == {"content-type": "application/json"}

    def test_a_recording_redacts_its_url_on_the_way_out(self):
        rec = Recording(key="k", kind="http", url=PROXY)
        assert "sekrit" not in json.dumps(rec.to_json())


class TestCassette:
    def test_round_trip(self, tmp_path):
        cassette = Cassette(tmp_path)
        cassette.write(Recording(key="k1", kind="http", status=200, body="hello"))
        back = cassette.read("http", "k1")
        assert back.body == "hello"
        assert back.status == 200

    def test_it_is_stored_gzipped(self, tmp_path):
        """A real Vinted listing page is about 1.9MB, and the fixtures are
        kept whole rather than trimmed -- trimming is how a fixture stops
        being evidence."""
        cassette = Cassette(tmp_path)
        path = cassette.write(Recording(key="k1", kind="http", body="x" * 5000))
        assert path.suffix == ".gz"
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            assert json.load(fh)["body"] == "x" * 5000

    def test_a_missing_recording_reads_as_none(self, tmp_path):
        assert Cassette(tmp_path).read("http", "nope") is None

    def test_it_stamps_when_it_was_captured(self, tmp_path):
        cassette = Cassette(tmp_path)
        cassette.write(Recording(key="k", kind="http", body="b"))
        assert cassette.read("http", "k").recorded_at

    def test_keys_are_listable_per_kind(self, tmp_path):
        cassette = Cassette(tmp_path)
        cassette.write(Recording(key="b", kind="http"))
        cassette.write(Recording(key="a", kind="http"))
        cassette.write(Recording(key="c", kind="cex"))
        assert cassette.keys("http") == ["a", "b"]
        assert cassette.keys("cex") == ["c"]
        assert len(cassette) == 3


class TestReplay:
    def test_it_returns_the_recorded_response(self, tmp_path):
        cassette = Cassette(tmp_path)
        url = "https://x.test/api?a=1"
        cassette.write(Recording(key=key_for("GET", url), kind="http",
                                 status=200, body='{"items": [1, 2]}'))
        session = HarnessSession(cassette, harness_mode=harness.REPLAY)
        response = session.get(url)
        assert response.status_code == 200
        assert response.json() == {"items": [1, 2]}

    def test_replay_never_touches_the_network(self, tmp_path):
        cassette = Cassette(tmp_path)
        url = "https://x.test/api"
        cassette.write(Recording(key=key_for("GET", url), kind="http", body="x"))
        inner = FakeSession()
        session = HarnessSession(cassette, inner=inner, harness_mode=harness.REPLAY)
        session.get(url)
        assert inner.requested == [], "replay must not fetch"

    def test_a_missing_recording_raises_rather_than_returning_empty(self, tmp_path):
        """The whole point. An empty page is indistinguishable from a quiet
        market, and this codebase has been bitten by that exact confusion."""
        session = HarnessSession(Cassette(tmp_path), harness_mode=harness.REPLAY)
        with pytest.raises(MissingRecording) as exc:
            session.get("https://x.test/never-captured")
        assert "scripts/capture.py" in str(exc.value), "tell the reader the fix"

    def test_a_recorded_403_replays_as_a_403(self, tmp_path):
        """Error responses are evidence too -- the client's 403 self-heal path
        cannot be developed offline without them."""
        cassette = Cassette(tmp_path)
        url = "https://x.test/api"
        cassette.write(Recording(key=key_for("GET", url), kind="http", status=403))
        session = HarnessSession(cassette, harness_mode=harness.REPLAY)
        assert session.get(url).status_code == 403

    def test_clearing_cookies_on_a_replay_session_is_harmless(self, tmp_path):
        """VintedClient.handshake() clears the jar unconditionally."""
        session = HarnessSession(Cassette(tmp_path), harness_mode=harness.REPLAY)
        session.cookies.clear()
        assert len(session.cookies) == 0


class TestRecord:
    def test_it_fetches_live_and_saves(self, tmp_path):
        cassette = Cassette(tmp_path)
        inner = FakeSession(FakeResponse(200, "live body"))
        session = HarnessSession(cassette, inner=inner, harness_mode=harness.RECORD)
        url = "https://x.test/api"
        assert session.get(url).text == "live body"
        assert cassette.read("http", key_for("GET", url)).body == "live body"

    def test_it_does_not_write_secrets_into_the_fixture(self, tmp_path):
        cassette = Cassette(tmp_path)
        inner = FakeSession(FakeResponse(
            200, "body", {"Set-Cookie": "session=abc", "Content-Type": "text/html"}))
        session = HarnessSession(cassette, inner=inner, harness_mode=harness.RECORD)
        session.get(PROXY + "/x")
        raw = gzip.open(next(tmp_path.rglob("*.json.gz")), "rt",
                        encoding="utf-8").read()
        assert "sekrit" not in raw
        assert "abc" not in raw

    def test_live_mode_fetches_and_saves_nothing(self, tmp_path):
        cassette = Cassette(tmp_path)
        inner = FakeSession()
        session = HarnessSession(cassette, inner=inner, harness_mode=harness.LIVE)
        session.get("https://x.test/api")
        assert inner.requested
        assert len(cassette) == 0

    def test_recording_without_a_real_session_is_refused(self, tmp_path):
        """Better than silently producing an empty cassette."""
        with pytest.raises(HarnessError):
            HarnessSession(Cassette(tmp_path), harness_mode=harness.RECORD)


class TestBrowserCaptures:
    """CeX and eBay are reached with a headed browser, so what gets recorded is
    the extraction -- page text, or the list of result rows."""

    def test_replay_returns_the_recorded_rows_without_calling_produce(self, tmp_path):
        cassette = Cassette(tmp_path)
        rows = [{"text": "iPhone 13", "href": "https://ebay.co.uk/itm/1"}]
        cassette.write(Recording(
            key=recorded_key("iphone 13 128gb"), kind="ebay", data=rows))

        def produce():
            raise AssertionError("replay must not run the browser")

        assert recorded(cassette, "ebay", "iphone 13 128gb", produce,
                        harness_mode=harness.REPLAY) == rows

    def test_record_runs_produce_and_stores_the_result(self, tmp_path):
        cassette = Cassette(tmp_path)
        rows = [{"text": "iPhone 17"}]
        out = recorded(cassette, "cex", "iphone 17", lambda: rows,
                       harness_mode=harness.RECORD)
        assert out == rows
        assert recorded(cassette, "cex", "iphone 17", lambda: [],
                        harness_mode=harness.REPLAY) == rows

    def test_a_missing_capture_raises(self, tmp_path):
        with pytest.raises(MissingRecording):
            recorded(Cassette(tmp_path), "cex", "never fetched", lambda: [],
                     harness_mode=harness.REPLAY)

    def test_two_queries_do_not_share_a_recording(self, tmp_path):
        cassette = Cassette(tmp_path)
        recorded(cassette, "cex", "iphone 17", lambda: ["a"],
                 harness_mode=harness.RECORD)
        recorded(cassette, "cex", "iphone 17 pro", lambda: ["b"],
                 harness_mode=harness.RECORD)
        assert recorded(cassette, "cex", "iphone 17", lambda: [],
                        harness_mode=harness.REPLAY) == ["a"]


def recorded_key(name: str) -> str:
    """Mirror of the key `recorded()` builds, for arranging fixtures in tests."""
    import hashlib

    from phoneloop.harness import _slug

    return f"{_slug(name)}-{hashlib.sha1(name.encode()).hexdigest()[:8]}"


class TestCredentialsNeverReachDisk:
    """A fixtures directory gets committed. Credentials must not be in it --
    not in the body, not in the headers, and not in the FILENAME.

    The filename was the real leak: key_for built its slug from the URL's
    netloc, which carries any user:password@ prefix, so recording a request
    through the proxy wrote the password into a file name. Its own test caught
    it on the first run.
    """

    def test_credentials_are_not_part_of_a_key(self):
        assert key_for("GET", "https://u:p@x.test/a") == key_for("GET", "https://x.test/a")

    def test_a_password_never_appears_in_a_key(self):
        assert "sekrit" not in key_for("GET", PROXY + "/x")

    def test_a_password_never_appears_in_a_path_on_disk(self, tmp_path):
        cassette = Cassette(tmp_path)
        inner = FakeSession(FakeResponse(200, "body"))
        session = HarnessSession(cassette, inner=inner, harness_mode=harness.RECORD)
        session.get(PROXY + "/x")
        for path in tmp_path.rglob("*"):
            assert "sekrit" not in str(path), f"password leaked into {path}"


class TestSessionCompatibility:
    """Callers drive this like a requests.Session. VintedClient.handshake()
    sets headers on it and clears its cookie jar, and neither may explode or
    the client cannot be run offline at all."""

    def test_headers_can_be_set_like_a_real_session(self, tmp_path):
        session = HarnessSession(Cassette(tmp_path), harness_mode=harness.REPLAY)
        session.headers.update({"User-Agent": "x"})
        assert session.headers["User-Agent"] == "x"

    def test_recording_sends_the_session_headers(self, tmp_path):
        """A recorded request must be the request the client would really make
        -- a capture that omitted Accept and Referer produced a 403 that looked
        exactly like a blocked IP."""
        captured = {}

        class Spy:
            def get(self, url, headers=None, **kwargs):
                captured["headers"] = headers
                return FakeResponse(200, "ok")

        session = HarnessSession(Cassette(tmp_path), inner=Spy(),
                                 harness_mode=harness.RECORD)
        session.headers.update({"User-Agent": "ua", "Accept": "*/*"})
        session.get("https://x.test/a")
        assert captured["headers"]["User-Agent"] == "ua"
        assert captured["headers"]["Accept"] == "*/*"

    def test_a_per_request_header_wins_over_the_session_one(self, tmp_path):
        captured = {}

        class Spy:
            def get(self, url, headers=None, **kwargs):
                captured["headers"] = headers
                return FakeResponse(200, "ok")

        session = HarnessSession(Cassette(tmp_path), inner=Spy(),
                                 harness_mode=harness.LIVE)
        session.headers.update({"User-Agent": "session"})
        session.get("https://x.test/a", headers={"User-Agent": "request"})
        assert captured["headers"]["User-Agent"] == "request"


class TestUrlNormalisation:
    def test_a_bare_host_and_a_trailing_slash_are_one_resource(self):
        """capture.py fetches BASE_URL; handshake() fetches BASE_URL + "/".
        Treating those as different recordings made every replay of the
        handshake miss."""
        assert key_for("GET", "https://www.vinted.co.uk") == \
            key_for("GET", "https://www.vinted.co.uk/")

    def test_a_real_path_is_untouched(self):
        assert canonical_url("https://x.test/items/1") == "https://x.test/items/1"
