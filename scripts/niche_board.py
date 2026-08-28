"""The niche board: rank eBay niches, then products within the top ones, at
whatever identity tier each supports (design spec section 4 BOARD stage).

Reads the market store (the sweep + deepen output of Tiers 1-2), computes
Tier 3's difference when a second snapshot date exists, and ranks. Prints a
terminal table always; `--html` also writes a self-contained report under
`DATA_DIR/reports/`.

    python scripts/niche_board.py                    # niches only, terminal
    python scripts/niche_board.py --products          # + products within the top niches
    python scripts/niche_board.py --products --html   # + a report file

Every knob's value, pass/fail/unknown, and denominator coverage is printed
per row; a row whose sell-through is `lifetime` (no second snapshot yet)
says so and is never silently compared to the knob-2 threshold -- see
phoneloop/metrics.py and phoneloop/board.py.

Niches are still discovered from what the store actually observed
(`board.rank_niches` groups by whatever `leaf_category_id` values are on the
latest snapshot's rows) -- `rank_niches`'s contract signature (section 2.13)
takes no scope argument, so this script cannot hand it a curated list without
inventing an unspecified parameter. Instead (contracts section 6.2, point 3,
wired at integration since `phoneloop.catalog_ebay`/s6 was not in this
stream's worktree), the niche rows `rank_niches` returns are filtered here to
`catalog_ebay`'s recorded leaves before display/HTML, over-fetching from
`rank_niches` first so the filter cannot silently shrink below `--limit`. A
row surviving this filter is provably one of the leaves
`scripts/refresh_ebay_categories.py` actually recorded under an IN_SCOPE
department -- a stray `leaf_category_id` from a parsing bug or a future
non-scoped sweep is dropped here rather than boarded as if it were a real
niche.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phoneloop import board, catalog_ebay, config, heartbeat  # noqa: E402
from phoneloop.knobs import Knobs  # noqa: E402
from phoneloop.market_store import MarketStore  # noqa: E402
from phoneloop.snapshots import difference  # noqa: E402

# Over-fetch factor applied before the IN_SCOPE filter below, so filtering
# out a few stray leaves cannot silently shrink the printed table below
# --limit. Rank_niches is cheap (it groups rows already in memory), so this
# costs nothing measurable.
_OVERFETCH = 10

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=20,
                    help="rows to keep per table (default 20)")
    ap.add_argument("--products", action="store_true",
                    help="also rank products within the top-ranked niches")
    ap.add_argument("--html", action="store_true",
                    help="write a self-contained HTML report under DATA_DIR/reports/")
    args = ap.parse_args()

    cfg = config.load()
    knobs = Knobs.from_env()
    store = MarketStore(cfg.DATA_DIR, max_bytes=cfg.MAX_STORE_BYTES)

    # FIRST, above everything -- including the "no snapshots at all" exit
    # below. A board that opens with rankings and buries "last night failed"
    # underneath them is a board that gets read as though the numbers were
    # fresh. `health_for` never raises.
    health = heartbeat.health_for(cfg.DATA_DIR)
    banner = board.render_health_text(health)
    if banner:
        print(banner)
        print()

    dates = store.snapshot_dates("ebay")
    if not dates:
        print("no eBay snapshots in the store yet -- run the sweep first "
             f"(DATA_DIR={cfg.DATA_DIR})")
        return 1

    diff = None
    if len(dates) >= 2:
        diff = difference(store, dates[-2], dates[-1])
        print(f"Tier 3 difference: {diff.date_a} -> {diff.date_b}, "
             f"{diff.n_known}/{diff.n_listings} listings with a known delta "
             f"({diff.coverage:.0%} coverage)")
    else:
        print(f"only one snapshot date ({dates[-1]}) -- every sell-through "
             "figure below is 'lifetime', not 'window'")

    known_leaves = set(catalog_ebay.top_level_of().keys())
    niche_rows = board.rank_niches(
        store, knobs, difference=diff, limit=args.limit * _OVERFETCH)
    # Filter on the row's leaf, never on its key: a niche key carries the
    # price band ("9355@15-55"), so matching it against raw leaf ids drops
    # every niche and prints "(no rows)" as though the market were empty.
    niche_rows = [r for r in niche_rows
                  if r.leaf_category_id in known_leaves][:args.limit]
    print()
    print("=== NICHES ===")
    print(board.render_table(niche_rows))

    all_rows = list(niche_rows)

    if args.products:
        # rank_products matches these against row["leaf_category_id"], so it
        # wants leaves, not niche keys. Deduplicated because two price bands
        # of one leaf are two niches but one leaf to scan for products.
        top_niche_keys = list(dict.fromkeys(
            r.leaf_category_id for r in niche_rows if r.leaf_category_id))
        product_rows = board.rank_products(
            store, knobs, top_niche_keys, difference=diff, limit=args.limit)
        print()
        print("=== PRODUCTS (within the ranked niches above) ===")
        print(board.render_table(product_rows))
        all_rows += product_rows

    if args.html:
        path = board.write_report(all_rows, cfg.DATA_DIR, health=health)
        print()
        print(f"wrote {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
