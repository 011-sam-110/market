"""Does the product key collapse listings correctly? Measure, don't assume.

A product key has two failure modes and they pull in opposite directions:

    too specific  every product ends up with one seller, and the competition
                  signal -- the whole point -- becomes noise
    too loose     unrelated products merge, and the margin computed from the
                  merged pool describes nothing that exists

Neither is visible from the code. Both are visible from the data: the collapse
ratio, the largest buckets, and the price spread *inside* a bucket. A bucket
holding one real product prices tightly; one holding several does not.

    python scripts/product_audit.py
    python scripts/product_audit.py --category phones --show 30
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phoneloop import config  # noqa: E402
from phoneloop.market_store import MarketStore  # noqa: E402
from phoneloop.product import key_for  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--category", default=None)
    ap.add_argument("--source", default=None)
    ap.add_argument("--show", type=int, default=20)
    args = ap.parse_args()

    store = MarketStore(config.load().DATA_DIR)
    where = []
    if args.category:
        where.append(f"category = '{args.category}'")
    if args.source:
        where.append(f"source = '{args.source}'")
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    rows = store.sql(f"""
        SELECT source, source_id, category, title, brand, model, storage_gb,
               price_pence, seller_id
        FROM (SELECT *, row_number() OVER (PARTITION BY source, source_id
                                           ORDER BY seen_at DESC) rn
              FROM market {clause}) WHERE rn = 1
    """).fetchall()
    if not rows:
        print("no rows; run a bulk collector first")
        return 1

    buckets: dict[str, list] = defaultdict(list)
    conflicts = 0
    unusable = 0
    conf_sum = 0.0
    for src, sid, cat, title, brand, model, storage, price, seller in rows:
        pk = key_for(title, cat, brand=brand, model_slug=model,
                     storage_gb=storage)
        conflicts += pk.conflict
        conf_sum += pk.confidence
        if not pk.usable:
            unusable += 1
            continue
        buckets[pk.key].append((title, price, seller, src))

    listings = len(rows)
    products = len(buckets)
    print(f"listings              {listings:,}")
    print(f"products              {products:,}")
    print(f"collapse ratio        {listings / max(products, 1):.1f} listings/product")
    print(f"brand/model conflicts {conflicts:,} "
          f"({conflicts / listings:.1%}) — slug dropped, would have mispriced")
    print(f"unusable keys         {unusable:,} ({unusable / listings:.1%}) "
          "— no brand and no family; excluded, not guessed")
    print(f"mean confidence       {conf_sum / listings:.2f}")

    # A bucket that holds one product prices tightly. Wide dispersion is the
    # tell that the key merged things it should not have.
    print(f"\ntop {args.show} buckets — dispersion is the over-merge detector")
    print(f"{'product':<44} {'n':>4} {'sellers':>7} {'median':>8} {'MAD/med':>8}")
    ranked = sorted(buckets.items(), key=lambda kv: -len(kv[1]))
    for key, items in ranked[:args.show]:
        prices = sorted(p for _, p, _, _ in items if p)
        if not prices:
            continue
        med = statistics.median(prices)
        mad = statistics.median([abs(p - med) for p in prices])
        sellers = len({s for _, _, s, _ in items if s})
        flag = "  <-- WIDE, likely merged" if med and mad / med > 0.45 else ""
        print(f"{key[:44]:<44} {len(items):>4} {sellers:>7} "
              f"£{med/100:>7.0f} {(mad/med if med else 0):>8.2f}{flag}")

    # Singletons are the opposite failure: a key so specific it groups nothing.
    singles = sum(1 for v in buckets.values() if len(v) == 1)
    print(f"\nsingleton products    {singles:,} ({singles / max(products,1):.1%}) "
          "— too specific to carry a competition signal")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
