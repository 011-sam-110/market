"""Store tests.

Two properties matter here: money never drifts, and an unchanged listing costs
nothing to re-observe. The second is what keeps a change-log ~100x smaller than
a snapshot history.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from phoneloop.store import Store, content_hash


def obs(item_id=1, price="150.00", total="157.50", condition="Very good",
        title="iPhone 13 128GB", seen_at=None):
    return {
        "item_id": item_id,
        "seen_at": seen_at or datetime.now(timezone.utc),
        "title": title,
        "price": Decimal(price),
        "total_item_price": Decimal(total),
        "service_fee": Decimal("7.50"),
        "currency": "GBP",
        "condition": condition,
        "brand_title": "Apple",
        "favourite_count": 3,
        "view_count": 40,
        "photo_count": 4,
        "seller_id": 999,
        "url": "https://www.vinted.co.uk/items/1-iphone-13",
    }


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)


def test_first_observation_is_written(store):
    assert store.record([obs()]) == 1


def test_unchanged_listing_writes_no_row(store):
    store.record([obs()])
    assert store.record([obs()]) == 0, "re-observing an unchanged listing must be free"


def test_price_change_writes_a_row(store):
    store.record([obs(price="150.00")])
    assert store.record([obs(price="140.00")]) == 1


def test_condition_change_writes_a_row(store):
    store.record([obs(condition="Very good")])
    assert store.record([obs(condition="Good")]) == 1


def test_money_survives_a_roundtrip_exactly(store):
    """Prices are held as integer pence. A float would drift and quietly
    corrupt every median in the pricebook."""
    store.record([obs(price="199.99", total="209.98")])
    rows = store.observations_since(days=1)
    assert rows[0]["price"] == Decimal("199.99")
    assert rows[0]["total_item_price"] == Decimal("209.98")


def test_awkward_pence_values_roundtrip(store):
    for amount in ["0.01", "3.43", "1234.56", "0.10"]:
        s = Store(store.data_dir / amount.replace(".", "_"))
        s.record([obs(price=amount, total=amount)])
        assert s.observations_since(days=1)[0]["price"] == Decimal(amount)


def test_hashes_survive_restart(store):
    """A restarted loop must not rewrite every listing it has already seen."""
    store.record([obs()])
    reopened = Store(store.data_dir)
    assert reopened.record([obs()]) == 0


def test_content_hash_ignores_volatile_fields(store):
    """Favourites and views tick constantly. If they fed the hash, every poll
    would rewrite the entire catalogue."""
    a = obs(); a["favourite_count"] = 3; a["view_count"] = 10
    b = obs(); b["favourite_count"] = 99; b["view_count"] = 900
    assert content_hash(a) == content_hash(b)


def test_vanished_listings_recorded(store):
    store.record([obs(item_id=1), obs(item_id=2)])
    at = datetime.now(timezone.utc)
    store.mark_vanished([1], at)
    vanished = store.vanished_ids()
    assert 1 in vanished and 2 not in vanished


def test_observations_since_respects_window(store):
    old = datetime.now(timezone.utc) - timedelta(days=10)
    store.record([obs(item_id=1, seen_at=old)])
    store.record([obs(item_id=2)])
    recent = store.observations_since(days=2)
    assert {r["item_id"] for r in recent} == {2}


def test_byte_budget_refuses_to_grow(store):
    """Hard ceiling so a runaway loop can never consume the shared Drive quota
    the EV client's backups depend on.

    Semantics are check-before-write: the write that crosses the ceiling is
    allowed, and every write after it is refused. Checking after the fact would
    mean discovering the overrun only once the disk was already full.
    """
    assert store.record([obs()]) == 1     # under budget, writes fine
    store.max_bytes = 1                   # now over
    with pytest.raises(RuntimeError, match="byte budget"):
        store.record([obs(price="99.00")])


def test_byte_budget_of_none_is_unlimited(store):
    store.max_bytes = None
    assert store.record([obs()]) == 1
