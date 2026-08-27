"""Enumerate Vinted at scale: every tracked category, sharded past the 960 cap.

The shape of the problem: Vinted answers any query with at most 960 items, and
reports `total_entries: 960` whether the true count is 960 or 96,000. So the
only way to know a category has been seen whole is to keep splitting the query
until the shards stop coming back saturated -- see `phoneloop.catalog`.

Run it:

    python scripts/bulk_vinted.py                       # every category
    python scripts/bulk_vinted.py --category phones
    python scripts/bulk_vinted.py --category phones --target 11000
    python scripts/bulk_vinted.py --max-requests 400    # bounded run

It is resumable. A run writes its completed shards to a checkpoint, so an
interrupted sweep picks up where it stopped rather than re-walking ground it
has already paid for.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phoneloop import catalog, config  # noqa: E402
from phoneloop.market_store import MarketStore, to_pence  # noqa: E402
from phoneloop.parser import parse  # noqa: E402
from phoneloop.vinted_client import (  # noqa: E402
    RateLimited,
    SchemaChanged,
    VintedClient,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PER_PAGE = 96
MAX_PAGES = 10                 # page 11 is a 400
PAUSE_S = 0.8                  # between pages; the gateway rotates per connection
BACKOFF_S = 30.0               # after a 429
MAX_CONSECUTIVE_FAILS = 12


def row_from_item(item: dict, category: str, shard: str) -> dict | None:
    """One Vinted catalogue item -> one store row.

    Returns None for anything without an id or a price, rather than writing a
    row that cannot be joined or valued.
    """
    item_id = item.get("id")
    price = (item.get("price") or {}).get("amount")
    if item_id is None or price is None:
        return None

    total = (item.get("total_item_price") or {}).get("amount")
    title = item.get("title") or ""
    spec = parse(title)
    url = item.get("url") or ""
    if not url and item.get("path"):
        url = f"https://www.vinted.co.uk{item['path']}"

    return {
        "source": "vinted",
        "source_id": item_id,
        "category": category,
        "shard": shard,
        "title": title,
        "price_pence": to_pence(price),
        # Vinted states the buyer-protection-inclusive total on every item, so
        # unlike eBay there is no "delivery not specified" case here.
        "total_pence": to_pence(total),
        "currency": (item.get("price") or {}).get("currency_code") or "GBP",
        "condition": item.get("status") or "",
        "brand": item.get("brand_title") or "",
        "model": spec.model,
        "storage_gb": spec.storage_gb,
        "grade": "",
        "kind": "ask",
        "available": True,
        "region": "GB",
        "seller_id": str((item.get("user") or {}).get("id") or ""),
        "url": url,
        "extra": {
            "favourite_count": item.get("favourite_count"),
            "view_count": item.get("view_count"),
            "size_title": item.get("size_title"),
            "service_fee": (item.get("service_fee") or {}).get("amount"),
            "disqualifiers": list(spec.disqualifiers),
        },
    }


class Sweeper:
    """Walks one category's leaves, splitting price bands until nothing saturates."""

    def __init__(self, client, store, cat, checkpoint: dict, args):
        self.client = client
        self.store = store
        self.cat = cat
        self.checkpoint = checkpoint
        self.args = args
        self.requests = 0
        self.written = 0
        self.seen_ids: set[str] = set()
        self.fails = 0
        self.saturated_floor: list[str] = []

    def _fetch(self, leaf_id: int, lo: int, hi: int, page: int) -> list[dict]:
        """One page. Retries transport failures; lets a hard block escape."""
        for attempt in range(4):
            try:
                self.requests += 1
                return self.client.search(
                    catalog_ids=leaf_id,
                    per_page=PER_PAGE,
                    page=page,
                    order="newest_first",
                    price_from=lo / 100,
                    price_to=hi / 100,
                    currency="GBP",
                )
            except RateLimited:
                wait = BACKOFF_S * (attempt + 1)
                print(f"      429, backing off {wait:.0f}s")
                time.sleep(wait)
            except SchemaChanged as exc:
                # A 400 here is the cap talking, not a broken payload.
                if "400" in str(exc):
                    return []
                raise
            except (OSError, ValueError) as exc:
                # The residential gateway drops connections; that is its nature,
                # not an error worth aborting a six-hour sweep for.
                print(f"      transport {type(exc).__name__}, retry {attempt + 1}/4")
                self.client.rotate()
                time.sleep(2.0 * (attempt + 1))
            except PermissionError:
                print("      403 after rotation, resting 60s")
                time.sleep(60)
                self.client.rotate()
        self.fails += 1
        return []

    def shard(self, leaf_id: int, leaf_name: str, lo: int, hi: int) -> int:
        """Enumerate one (leaf, price band). Returns unique ids seen in it."""
        key = f"{self.cat.slug}/{leaf_id}/{lo}-{hi}"
        if key in self.checkpoint.get("done", {}):
            return self.checkpoint["done"][key]

        unique: set[str] = set()
        batch: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            items = self._fetch(leaf_id, lo, hi, page)
            if not items:
                break
            for item in items:
                row = row_from_item(item, self.cat.slug, key)
                if row is None:
                    continue
                unique.add(row["source_id"])
                batch.append(row)
            if len(items) < PER_PAGE * 0.5:
                break                      # short page: the shard is exhausted
            time.sleep(PAUSE_S + random.random() * 0.4)
            if self.args.max_requests and self.requests >= self.args.max_requests:
                break

        if batch:
            self.written += self.store.record(batch)
        self.seen_ids |= unique
        self.checkpoint.setdefault("done", {})[key] = len(unique)
        return len(unique)

    def walk(self, leaf_id: int, leaf_name: str) -> None:
        """Depth-first over price bands, splitting whatever saturates."""
        queue = deque([(self.cat.price_from * 100, self.cat.price_to * 100)])
        while queue:
            lo, hi = queue.popleft()
            if self.args.max_requests and self.requests >= self.args.max_requests:
                return
            if self.fails >= MAX_CONSECUTIVE_FAILS:
                print("   ! too many consecutive failures, stopping this leaf")
                return
            n = self.shard(leaf_id, leaf_name, lo, hi)
            flag = ""
            if catalog.is_saturated(n):
                halves = catalog.split_band(lo, hi)
                if halves:
                    queue.extendleft(reversed(halves))
                    flag = " SATURATED -> split"
                else:
                    # 960+ items inside a one-pound band. Price cannot separate
                    # them, so this is a coverage limit worth naming rather than
                    # a loop to keep grinding.
                    self.saturated_floor.append(f"{leaf_id} £{lo/100:.0f}-{hi/100:.0f}")
                    flag = " SATURATED AT FLOOR - unreachable by price"
            print(f"   {leaf_name[:28]:<28} £{lo/100:>6.0f}-{hi/100:<6.0f} "
                  f"{n:>4} uniq{flag}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--category", action="append",
                    help="slug; repeatable. Default: all fifteen.")
    ap.add_argument("--max-requests", type=int, default=0,
                    help="stop after this many API calls (0 = unbounded)")
    ap.add_argument("--target", type=int, default=0,
                    help="stop a category once it has this many unique listings")
    ap.add_argument("--leaf-limit", type=int, default=0,
                    help="only walk the first N leaves of each category")
    ap.add_argument("--checkpoint", type=Path, default=None)
    ap.add_argument("--fresh", action="store_true",
                    help="ignore the checkpoint and re-walk every shard")
    args = ap.parse_args()

    cfg = config.load()
    store = MarketStore(cfg.DATA_DIR, max_bytes=cfg.MAX_STORE_BYTES)
    tree = catalog.load_tree()

    cp_path = args.checkpoint or (cfg.DATA_DIR / "bulk_vinted_checkpoint.json")
    checkpoint: dict = {}
    if cp_path.exists() and not args.fresh:
        checkpoint = json.loads(cp_path.read_text(encoding="utf-8"))
        print(f"resuming: {len(checkpoint.get('done', {}))} shards already done")

    slugs = args.category or [c.slug for c in catalog.CATEGORIES]
    client = VintedClient()
    client.handshake()

    started = time.time()
    grand_total = 0
    for slug in slugs:
        cat = catalog.BY_SLUG.get(slug)
        if cat is None:
            print(f"unknown category {slug!r}; known: {sorted(catalog.BY_SLUG)}")
            continue
        leaves = catalog.leaves_of(tree, cat.vinted_root)
        if cat.vinted_leaves:
            wanted = set(cat.vinted_leaves)
            leaves = [x for x in leaves if x[0] in wanted]
        if args.leaf_limit:
            leaves = leaves[:args.leaf_limit]

        print(f"\n=== {slug} — {len(leaves)} leaves, "
              f"£{cat.price_from}-{cat.price_to} ===")
        sweeper = Sweeper(client, store, cat, checkpoint, args)
        for leaf_id, leaf_name in leaves:
            sweeper.walk(leaf_id, leaf_name)
            cp_path.parent.mkdir(parents=True, exist_ok=True)
            cp_path.write_text(json.dumps(checkpoint), encoding="utf-8")
            if args.target and len(sweeper.seen_ids) >= args.target:
                print(f"   target {args.target} reached")
                break
            if args.max_requests and sweeper.requests >= args.max_requests:
                print("   request budget spent")
                break

        grand_total += len(sweeper.seen_ids)
        print(f"--- {slug}: {len(sweeper.seen_ids):,} unique, "
              f"{sweeper.written:,} rows written, {sweeper.requests} requests")
        if sweeper.saturated_floor:
            print(f"    unreachable by price sharding: "
                  f"{len(sweeper.saturated_floor)} bands")

    cp_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    mins = (time.time() - started) / 60
    print(f"\nswept {grand_total:,} unique listings in {mins:.1f} min")
    print(f"store now holds {store.total_listings('vinted'):,} distinct "
          f"vinted listings")
    for row in store.counts():
        print("   ", row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
