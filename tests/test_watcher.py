"""Watcher tests — the integration point.

The load-bearing test is `test_tick_never_raises`. A loop that dies on a bad
listing stops finding deals entirely, so every stage is wrapped and every
failure lands in `errors` rather than propagating.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from phoneloop import config
from phoneloop.alerter import Alerter
from phoneloop.pricebook import Pricebook
from phoneloop.store import Store
from phoneloop.vinted_client import RateLimited, SchemaChanged
from phoneloop.watcher import Watcher

CFG = config.load(env={"PHONELOOP_TELEGRAM_TOKEN": "t", "PHONELOOP_TELEGRAM_CHAT_ID": "c"})


def listing(item_id, title, total, condition="Very good"):
    return {
        "id": item_id,
        "title": title,
        "price": {"amount": str(total), "currency_code": "GBP"},
        "total_item_price": {"amount": str(total), "currency_code": "GBP"},
        "service_fee": {"amount": "0.00", "currency_code": "GBP"},
        "status": condition,
        "brand_title": "Apple",
        "favourite_count": 2,
        "view_count": 10,
        "photos": [{"url": "u1"}, {"url": "u2"}],
        "user": {"id": 5},
        "path": f"/items/{item_id}-x",
        "url": f"https://www.vinted.co.uk/items/{item_id}-x",
    }


class StubClient:
    def __init__(self, items=None, raises=None, page="{}"):
        self.items, self.raises, self.page = items or [], raises, page
        self.searches = 0

    def search(self, **kw):
        self.searches += 1
        if self.raises:
            raise self.raises
        return self.items

    def fetch_public_page(self, path):
        return self.page


def build(tmp_path, client, sender=None):
    store = Store(tmp_path)
    book = Pricebook(store, CFG)
    sent = []
    alerter = Alerter(CFG, sender=sender or (lambda t: (sent.append(t), True)[1]))
    return Watcher(client, store, book, alerter, CFG), sent, store, book


def seed_market(store, book, n=12, price=200):
    """Populate a bucket so the pricebook has a reference to compare against."""
    store.record([{
        "item_id": 90000 + i, "seen_at": datetime.now(timezone.utc),
        "title": "iPhone 13 128GB", "price": Decimal(str(price)),
        "total_item_price": Decimal(str(price)), "service_fee": Decimal("0"),
        "currency": "GBP", "condition": "Very good", "brand_title": "Apple",
        "favourite_count": 0, "view_count": 0, "photo_count": 3,
        "seller_id": 1, "url": "u",
    } for i in range(n)])
    book.refresh()


def test_tick_records_observations(tmp_path):
    w, _, store, _ = build(tmp_path, StubClient([listing(1, "iPhone 13 128GB", 200)]))
    result = w.tick()
    assert result.seen == 1
    assert result.written == 1


def test_tick_alerts_on_underpriced_listing(tmp_path):
    w, sent, store, book = build(tmp_path, StubClient([listing(1, "iPhone 13 128GB", 150)]))
    seed_market(store, book)
    result = w.tick()
    assert result.alerted == 1, f"expected an alert, got errors={result.errors}"
    assert "25% under market" in sent[0]


def test_tick_does_not_alert_on_bait(tmp_path):
    """End-to-end proof the fraud floor survives integration, not just unit tests."""
    w, sent, store, book = build(tmp_path, StubClient([listing(1, "iPhone 13 128GB", 50)]))
    seed_market(store, book)
    result = w.tick()
    assert result.alerted == 0
    assert sent == []


def test_tick_does_not_alert_without_a_reference(tmp_path):
    """A cold start has no pricebook. It must stay silent, not treat the absence
    of a reference as a 100% discount."""
    w, sent, _, _ = build(tmp_path, StubClient([listing(1, "iPhone 13 128GB", 10)]))
    assert w.tick().alerted == 0
    assert sent == []


def test_tick_skips_accessories_without_error(tmp_path):
    w, sent, store, book = build(tmp_path, StubClient([listing(1, "iPhone 13 case", 5)]))
    seed_market(store, book)
    result = w.tick()
    assert result.alerted == 0
    assert result.errors == []


def test_verification_failure_blocks_the_alert(tmp_path):
    """An unverified candidate is not a candidate."""
    client = StubClient([listing(1, "iPhone 13 128GB", 150)],
                        page='{"description":"sadly iCloud locked"}')
    w, sent, store, book = build(tmp_path, client)
    seed_market(store, book)
    assert w.tick().alerted == 0
    assert sent == []


def test_rate_limited_halves_the_poll_interval(tmp_path):
    w, _, _, _ = build(tmp_path, StubClient(raises=RateLimited("429")))
    before = w.poll_interval_s
    result = w.tick()
    assert w.poll_interval_s == before * 2, "backoff must slow the loop down"
    assert any("rate limit" in e.lower() for e in result.errors)


def test_poll_interval_recovers_after_success(tmp_path):
    w, _, _, _ = build(tmp_path, StubClient(raises=RateLimited("429")))
    w.tick()
    backed_off = w.poll_interval_s
    w.client = StubClient([listing(1, "iPhone 13 128GB", 200)])
    w.tick()
    assert w.poll_interval_s < backed_off, "must ease back off after a clean tick"


def test_schema_change_is_reported_not_swallowed(tmp_path):
    w, _, _, _ = build(tmp_path, StubClient(raises=SchemaChanged("no items key")))
    result = w.tick()
    assert any("schema" in e.lower() for e in result.errors)
    assert result.seen == 0


def test_tick_never_raises_even_when_everything_fails(tmp_path):
    """Every stage sabotaged. tick() must still return a result."""
    class Exploding:
        def search(self, **kw):
            raise RuntimeError("network on fire")
        def fetch_public_page(self, p):
            raise RuntimeError("also on fire")

    w, sent, _, _ = build(tmp_path, Exploding())
    result = w.tick()          # must not raise
    assert result.errors
    assert result.alerted == 0
    assert sent == []


def test_one_bad_listing_does_not_stop_the_others(tmp_path):
    """A single malformed payload must not cost us the rest of the batch."""
    bad = {"id": 2}  # no price, no title
    client = StubClient([listing(1, "iPhone 13 128GB", 150), bad])
    w, sent, store, book = build(tmp_path, client)
    seed_market(store, book)
    result = w.tick()
    assert result.alerted == 1, f"good listing lost; errors={result.errors}"
    assert result.errors, "the bad listing should still be reported"


def test_repeated_tick_does_not_realert_the_same_listing(tmp_path):
    """The same bargain sitting in the feed must alert once, not every minute."""
    client = StubClient([listing(1, "iPhone 13 128GB", 150)])
    w, sent, store, book = build(tmp_path, client)
    seed_market(store, book)
    w.tick()
    second = w.tick()
    assert second.alerted == 0, "an unchanged listing must not alert twice"
