"""Does the list-view fix survive the whole sweep path, not just the URL?

`scripts/probe_toys_view.py` proved the cause at the page level: the same
leaf, band and page returns a seller line on 97.3% of rows in list view and
3.1% in gallery. This proves the fix reaches the store.

It runs the real `scripts/bulk_ebay.py --leaf-sweep` against a scratch data
directory, one page of every leaf, then reads the rows back out of the store
and reports seller coverage per leaf. Nothing in between is stubbed, so a
break anywhere in url -> fetch -> parse_rows -> to_row -> MarketStore shows
up here. The toys_games leaf and one healthy control leaf are checked against
a floor; every other leaf is reported.

The production store is never touched. PHONELOOP_DATA_DIR is set to a
scratch path below and the store the sweep writes is thrown away by the
caller.

    PHONELOOP_HARNESS=live python scripts/verify_view_fix.py --data-dir <scratch>
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phoneloop import ebay  # noqa: E402
from phoneloop.market_store import MarketStore  # noqa: E402

# The leaf that was dark, and the leaf that never was. A run that reports a
# healthy toys_games and nothing to compare it against proves less than half
# of what this is for -- eBay has good days.
TOYS_LEAF = "246"
CONTROL_LEAF = "20742"

# The floor a department has to clear to count as fixed. The eight healthy
# departments measured 96.6-97.9% across three recorded pages each, and
# toys_games measured 1.6-2.1%. Anything between those two is a partial fix
# and must not read as a pass.
PASS_FLOOR = 0.90


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", type=Path, required=True,
                    help="a SCRATCH store; never the production one")
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    prod = Path.home() / ".phoneloop" / "prod"
    if args.data_dir.resolve() == prod.resolve():
        print("refusing to sweep into the production store")
        return 2

    args.data_dir.mkdir(parents=True, exist_ok=True)
    print(f"view pinned by phoneloop.ebay: {ebay.VIEW}")
    print(f"url: {ebay.search_url('sacat', TOYS_LEAF, 15, 55, 1)}")
    print(f"harness key: {ebay.search_key('sacat', TOYS_LEAF, 15, 55, 1)}\n")

    # One page of EVERY leaf, not two. The sweep shards off the recorded
    # tree and has no per-leaf switch, and one page each is cheap enough
    # (43 loads, about seven minutes at the measured 10 s) that narrowing it
    # would cost more in new code than it saves in time. It also reports
    # every department rather than two, which is the stronger result.
    print(f"sweeping one page of every leaf into {args.data_dir}\n")

    env = dict(os.environ)
    env["PHONELOOP_DATA_DIR"] = str(args.data_dir)
    cmd = [sys.executable, "scripts/bulk_ebay.py", "--leaf-sweep",
           "--pages-per-shard", "1", "--fresh"]
    if args.headless:
        cmd.append("--headless")
    result = subprocess.run(cmd, cwd=str(Path(__file__).resolve().parent.parent),
                            env=env)
    if result.returncode != 0:
        print("\nthe sweep failed; nothing to read back")
        return result.returncode

    store = MarketStore(args.data_dir)
    dates = store.snapshot_dates(source="ebay")
    if not dates:
        print("\nthe sweep wrote no snapshot")
        return 1
    rows = store.rows_on(dates[-1], source="ebay")

    per_leaf: dict[str, list[int]] = {}
    for r in rows:
        leaf = str(r.get("leaf_category_id") or "")
        if not leaf:
            continue
        bucket = per_leaf.setdefault(leaf, [0, 0])
        bucket[0] += 1
        if r.get("seller_id"):
            bucket[1] += 1

    print(f"\n{'leaf':<10} {'rows':>6} {'seller':>7} {'coverage':>9}")
    failures = []
    for leaf in (TOYS_LEAF, CONTROL_LEAF):
        total, hit = per_leaf.get(leaf, [0, 0])
        if total == 0:
            print(f"{leaf:<10} {'0':>6} {'-':>7} {'no rows':>9}")
            failures.append(f"leaf {leaf} returned no rows")
            continue
        share = hit / total
        print(f"{leaf:<10} {total:>6} {hit:>7} {share * 100:>8.1f}%")
        if share < PASS_FLOOR:
            failures.append(
                f"leaf {leaf} carried a seller on {share * 100:.1f}% of rows, "
                f"below the {PASS_FLOOR * 100:.0f}% floor the eight healthy "
                "departments already clear")

    other = {k: v for k, v in per_leaf.items() if k not in (TOYS_LEAF, CONTROL_LEAF)}
    if other:
        print(f"\n{len(other)} other leaves also swept:")
        for leaf, (total, hit) in sorted(other.items()):
            share = f"{hit / total * 100:.1f}%" if total else "-"
            print(f"  {leaf:<10} {total:>6} {hit:>7} {share:>9}")

    if failures:
        print("\nNOT FIXED:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\nBoth leaves carry a seller on over "
          f"{PASS_FLOOR * 100:.0f}% of rows. The fix reaches the store.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
