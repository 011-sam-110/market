"""Client tests.

The important one is the schema guard. If Vinted changes its payload shape and
we return an empty list, the loop reads that as "no listings today" and goes
quiet — the worst possible failure, because it looks exactly like a slow market.
"""

import pytest

from phoneloop.vinted_client import RateLimited, SchemaChanged, VintedClient


class FakeResponse:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class FakeSession:
    """Records calls and replays a scripted sequence of responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}
        self.cookies = {}

    def get(self, url, **kwargs):
        self.calls.append(url)
        if not self.responses:
            return FakeResponse(200, {"items": []})
        r = self.responses.pop(0)
        return r() if callable(r) else r


def client(responses):
    return VintedClient(session=FakeSession(responses))


def test_handshake_hits_the_homepage():
    c = client([FakeResponse(200, {}, "<html></html>")])
    c.handshake()
    assert c.session.calls[0].endswith("/")


def test_search_returns_items():
    c = client([
        FakeResponse(200, {}, "<html></html>"),                     # handshake
        FakeResponse(200, {"items": [{"id": 1}, {"id": 2}]}),       # search
    ])
    items = c.search(catalog_ids=3565)
    assert [i["id"] for i in items] == [1, 2]


def test_429_raises_rate_limited():
    c = client([FakeResponse(200, {}, "<html>"), FakeResponse(429, {})])
    with pytest.raises(RateLimited):
        c.search(catalog_ids=3565)


def test_403_triggers_exactly_one_rehandshake_then_retries():
    """A session expiring mid-run is normal. Re-handshaking in a loop is not —
    that turns one expiry into a hammering of Vinted's homepage."""
    c = client([
        FakeResponse(200, {}, "<html>"),                # initial handshake
        FakeResponse(403, {}),                          # search: session died
        FakeResponse(200, {}, "<html>"),                # re-handshake
        FakeResponse(200, {"items": [{"id": 7}]}),      # search retried, works
    ])
    assert [i["id"] for i in c.search(catalog_ids=3565)] == [7]
    assert c.session.calls.count("https://www.vinted.co.uk/") == 2


def test_persistent_403_gives_up_rather_than_looping():
    c = client([
        FakeResponse(200, {}, "<html>"),
        FakeResponse(403, {}),
        FakeResponse(200, {}, "<html>"),
        FakeResponse(403, {}),
    ])
    with pytest.raises(PermissionError):
        c.search(catalog_ids=3565)


def test_missing_items_key_raises_rather_than_returning_empty():
    """Silent empty looks identical to a quiet market and would hide an API
    change for as long as it took someone to notice no alerts."""
    c = client([FakeResponse(200, {}, "<html>"), FakeResponse(200, {"unexpected": []})])
    with pytest.raises(SchemaChanged):
        c.search(catalog_ids=3565)


def test_empty_items_list_is_legitimate():
    """An actually-empty result set is fine — it is a *missing key* that is not."""
    c = client([FakeResponse(200, {}, "<html>"), FakeResponse(200, {"items": []})])
    assert c.search(catalog_ids=3565) == []


def test_search_passes_filters_through():
    c = client([FakeResponse(200, {}, "<html>"), FakeResponse(200, {"items": []})])
    c.search(catalog_ids=3565, price_from=80, per_page=96)
    url = c.session.calls[-1]
    assert "catalog_ids=3565" in url and "price_from=80" in url and "per_page=96" in url


def test_fetch_public_page_returns_html():
    # Two responses: the lazy handshake consumes the first.
    c = client([
        FakeResponse(200, {}, "<html>"),
        FakeResponse(200, {}, "<html>hello</html>"),
    ])
    assert "hello" in c.fetch_public_page("/items/1-iphone")


def test_handshake_is_lazy_and_happens_once():
    c = client([
        FakeResponse(200, {}, "<html>"),
        FakeResponse(200, {"items": []}),
        FakeResponse(200, {"items": []}),
    ])
    c.search(catalog_ids=3565)
    c.search(catalog_ids=3565)
    assert c.session.calls.count("https://www.vinted.co.uk/") == 1


# --- session self-healing ---------------------------------------------------
#
# Found live: the collector ran 23 clean ticks, then took a 403 and never
# recovered - every subsequent tick failed the same way while a second process
# on the same machine kept working fine. Two causes, both here.

class FakeCookieJar(dict):
    def __init__(self):
        super().__init__()
        self.cleared = 0

    def clear(self):
        self.cleared += 1
        super().clear()


def test_rehandshake_starts_a_fresh_identity():
    """Re-fetching the homepage on the same cookie jar is not a new session.

    If Vinted has flagged the cookies we are holding, handing them back with
    the request is the one thing guaranteed not to help.
    """
    session = FakeSession([FakeResponse(200, text="<html/>")])
    session.cookies = FakeCookieJar()
    session.cookies["flagged"] = "1"

    VintedClient(session=session).handshake()

    assert session.cookies.cleared == 1
    assert "flagged" not in session.cookies


def test_a_dead_session_is_not_reused_forever():
    """After a failed re-handshake the next call must start over, not give up.

    _handshaked stayed True through the failure, so every later tick reused the
    session already known to be rejected and could not self-heal.
    """
    session = FakeSession([
        FakeResponse(200, text="<html/>"),    # lazy first handshake
        FakeResponse(403),                    # search
        FakeResponse(200, text="<html/>"),    # re-handshake
        FakeResponse(403),                    # search again
    ])
    session.cookies = FakeCookieJar()
    c = VintedClient(session=session)

    with pytest.raises(PermissionError):
        c.search(catalog_ids=3565)

    assert c._handshaked is False, "a rejected session must not be marked good"

    # The next call gets a genuinely fresh start rather than reusing the corpse.
    session.responses = [
        FakeResponse(200, text="<html/>"),
        FakeResponse(200, {"items": [{"id": 1}]}),
    ]
    assert c.search(catalog_ids=3565) == [{"id": 1}]


class TestExitRotation:
    """A 403 that survives a re-handshake is an address block, not a session one.

    handshake() clears cookies, but requests keeps the pooled TCP connection,
    so the retry leaves from the same exit IP that was just refused. Behind the
    rotating residential gateway a new connection means a new address --
    measured: a 403 that survived a re-handshake cleared on the first attempt
    from a fresh process, three times out of three.
    """

    def test_rotate_replaces_the_session(self):
        client = VintedClient()
        before = client.session
        assert client.rotate() is True
        assert client.session is not before
        assert client._handshaked is False

    def test_rotate_refuses_to_touch_an_injected_session(self):
        """Rebuilding the caller's session, or a test's fake, is not this
        class's decision to make."""
        fake = FakeSession([])
        client = VintedClient(session=fake)
        assert client.rotate() is False
        assert client.session is fake

    def test_a_403_that_survives_the_rehandshake_still_raises(self):
        """Rotation is gated off for an injected session, so this is the
        no-rotation path: it must still fail loudly rather than return [].
        """
        session = FakeSession([
            FakeResponse(200, text="homepage"),      # initial handshake
            FakeResponse(403),                       # search
            FakeResponse(200, text="homepage"),      # re-handshake
            FakeResponse(403),                       # search again
        ])
        client = VintedClient(session=session)
        with pytest.raises(PermissionError) as exc:
            client.search(catalog_ids=2999)
        assert "rotation" in str(exc.value)
