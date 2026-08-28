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

NOTE for integration (contracts section 6.2, point 3): this script currently
discovers its niche list from what the store actually observed
(`board.rank_niches` groups by whatever `leaf_category_id` values are on the
latest snapshot's rows) rather than from `catalog_ebay.IN_SCOPE`, because
`phoneloop.catalog_ebay` (s6) is not merged into this stream's worktree. The
integrator wiring note in the contracts is the place to point this at
catalog_ebay's department list once it exists, if a curated (rather than
observed) niche list turns out to matter.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phoneloop import board, config  # noqa: E402
from phoneloop.knobs import Knobs  # noqa: E402
from phoneloop.market_store import MarketStore  # noqa: E402
from phoneloop.snapshots import difference  # noqa: E402

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

    niche_rows = board.rank_niches(store, knobs, difference=diff, limit=args.limit)
    print()
    print("=== NICHES ===")
    print(board.render_table(niche_rows))

    all_rows = list(niche_rows)

    if args.products:
        top_niche_keys = [r.key for r in niche_rows]
        product_rows = board.rank_products(
            store, knobs, top_niche_keys, difference=diff, limit=args.limit)
        print()
        print("=== PRODUCTS (within the ranked niches above) ===")
        print(board.render_table(product_rows))
        all_rows += product_rows

    if args.html:
        path = board.write_report(all_rows, cfg.DATA_DIR)
        print()
        print(f"wrote {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
