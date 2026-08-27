"""Verifier and alerter tests.

Both sit on the boundary between the pure decision logic and the outside world,
so the tests here are mostly about what happens when the outside world fails.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from phoneloop import config
from phoneloop.alerter import Alerter
from phoneloop.parser import parse
from phoneloop.pricebook import Reference
from phoneloop.scorer import Band, Verdict, score
from phoneloop.verifier import count_photos, extract_description, verify

CFG = config.load(env={"PHONELOOP_TELEGRAM_TOKEN": "t", "PHONELOOP_TELEGRAM_CHAT_ID": "c"})

# Shape taken from a real Vinted page, 2026-08-27.
REAL_PAGE = (
    '<html><script>{"item":{"id":9791770344,'
    '"description":"Iphone 14 midnight black\\nDm for questions\\nOffers accepted",'
    '"photos":[{"url":"https://images1.vinted.net/a.jpg"},'
    '{"url":"https://images2.vinted.net/b.jpg"}]}}</script></html>'
)


class StubClient:
    def __init__(self, html=None, exc=None):
        self.html, self.exc = html, exc

    def fetch_public_page(self, path):
        if self.exc:
            raise self.exc
        return self.html


# --- verifier ---------------------------------------------------------------

def test_extracts_description_from_embedded_json():
    assert "midnight black" in extract_description(REAL_PAGE)


def test_description_with_escapes_is_not_truncated():
    """A newline or quote in the seller's text must not cut the capture short."""
    html = '{"description":"line one\\nline \\"two\\" here\\nend"}'
    assert extract_description(html) == 'line one\nline "two" here\nend'


def test_missing_description_returns_empty_not_crash():
    assert extract_description("<html>nothing here</html>") == ""


def test_counts_photos():
    assert count_photos(REAL_PAGE) == 2


def test_fetch_failure_returns_not_ok_and_never_raises():
    result = verify(StubClient(exc=ConnectionError("boom")), {"path": "/items/1"}, parse("iPhone 13"))
    assert result.ok is False
    assert "fetch failed" in result.reason


def test_missing_path_returns_not_ok():
    assert verify(StubClient(html="x"), {}, parse("iPhone 13")).ok is False


def test_icloud_lock_in_description_only_flips_ok_false():
    """The disqualifier that costs the most, and it is almost never in the title."""
    html = '{"description":"Great condition but sadly iCloud locked"}'
    result = verify(StubClient(html=html), {"path": "/items/1", "title": "iPhone 13 128GB"}, parse("iPhone 13 128GB"))
    assert result.ok is False
    assert "icloud_locked" in result.extra_disqualifiers


def test_clean_listing_verifies_ok():
    result = verify(StubClient(html=REAL_PAGE), {"path": "/items/1", "title": "iPhone 14"}, parse("iPhone 14"))
    assert result.ok is True


# --- alerter ----------------------------------------------------------------

def item(total="150.00", favourites=4):
    return {
        "total_item_price": {"amount": total, "currency_code": "GBP"},
        "price": {"amount": "142.50", "currency_code": "GBP"},
        "favourite_count": favourites,
        "status": "Very good",
        "path": "/items/1-iphone-13",
    }


SPEC = parse("iPhone 13 128GB")
REF = Reference(median=Decimal("200"), mad=Decimal("10"), samples=14)
VERDICT = score(SPEC, Decimal("150"), Decimal("200"), CFG)


def alerter(now=None, **kw):
    sent = []
    a = Alerter(CFG, sender=lambda text: (sent.append(text), True)[1], now_fn=now, **kw)
    return a, sent


def test_message_shows_landed_price_not_asking_price():
    """142.50 is the asking price; 150.00 is what actually leaves your account."""
    a, sent = alerter()
    assert a.send(item(), SPEC, VERDICT, REF) is True
    assert "150.00" in sent[0]
    assert "142.50" not in sent[0]


def test_message_carries_enough_to_decide():
    a, sent = alerter()
    a.send(item(), SPEC, VERDICT, REF)
    text = sent[0]
    for expected in ["Iphone 13", "128GB", "25% under market", "200", "14 comps", "Very good", "4 watching"]:
        assert expected in text, f"missing {expected!r} from alert"


def test_inferred_storage_is_disclosed():
    a, sent = alerter()
    inferred = Reference(median=Decimal("200"), mad=Decimal("10"), samples=14, inferred_from_lowest_tier=True)
    a.send(item(), parse("iPhone 13 White"), VERDICT, inferred)
    assert "storage inferred" in sent[0]


def test_non_alertable_verdict_is_never_sent():
    a, sent = alerter()
    bait = Verdict(band=Band.BAIT, delta=-0.7, alertable=False, reason="bait")
    assert a.send(item(), SPEC, bait, REF) is False
    assert sent == []


def test_rate_limit_blocks_the_fourth_alert_in_an_hour():
    frozen = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
    a, sent = alerter(now=lambda: frozen)
    results = [a.send(item(), SPEC, VERDICT, REF) for _ in range(4)]
    assert results == [True, True, True, False]
    assert len(sent) == 3


def test_rate_limit_resets_next_hour():
    clock = {"t": datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)}
    a, sent = alerter(now=lambda: clock["t"])
    for _ in range(3):
        a.send(item(), SPEC, VERDICT, REF)
    clock["t"] += timedelta(hours=1)
    assert a.send(item(), SPEC, VERDICT, REF) is True


def test_rate_limit_is_per_bucket():
    """A flood of iPhone 13s must not silence a genuine Galaxy S24 deal."""
    frozen = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
    a, sent = alerter(now=lambda: frozen)
    for _ in range(3):
        a.send(item(), SPEC, VERDICT, REF)
    other = parse("Samsung Galaxy S24 256GB")
    assert a.send(item(), other, VERDICT, REF) is True


def test_send_failure_returns_false_and_does_not_raise():
    """Telegram being down must never stop the watcher. The listing is already
    recorded by then; losing the notification is by far the cheaper failure."""
    def boom(_):
        raise ConnectionError("telegram down")
    a = Alerter(CFG, sender=boom)
    assert a.send(item(), SPEC, VERDICT, REF) is False


def test_failed_send_does_not_consume_rate_limit_budget():
    """A run of failures must not silently eat the hour's allowance and leave
    the next genuine deal suppressed."""
    calls = {"n": 0}
    def flaky(_):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise ConnectionError("down")
        return True
    a = Alerter(CFG, sender=flaky)
    for _ in range(3):
        assert a.send(item(), SPEC, VERDICT, REF) is False
    assert a.send(item(), SPEC, VERDICT, REF) is True


def test_alerts_disabled_sends_nothing():
    """collect mode: log everything, alert nothing."""
    cfg = config.load(env={"PHONELOOP_ALERTS_ENABLED": "0"})
    sent = []
    a = Alerter(cfg, sender=lambda t: (sent.append(t), True)[1])
    assert a.send(item(), SPEC, VERDICT, REF) is False
    assert sent == []
