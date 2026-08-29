"""The population review page: the whole scraped eBay population, and how
much of it can actually be read.

`scripts/niche_board.py` answers "which niche is best". This answers the
question that has to be settled first -- is there enough data under that
ranking to act on it? It renders one self-contained HTML page: what was
swept, which columns actually arrived, which board rows are green only
because a column is absent, what the market looks like, every listing
individually, and what the next sweep would unlock.

    python scripts/population_report.py                 # terminal summary
    python scripts/population_report.py --html          # + the report file
    python scripts/population_report.py --html --summary  # ~60 KB, no explorer

Panels that cannot be computed from the dates on file are RENDERED, not
hidden: each one names what it needs and shows no number at all. That is the
point -- an absent panel silently rewards a thin store, and a placeheld one
lies about it.

Reads DATA_DIR from `phoneloop.config` exactly like the board does, so
pointing this at a different store is `PHONELOOP_DATA_DIR`, not an argument
this script invents:

    PHONELOOP_DATA_DIR=... python scripts/population_report.py --html

Exit codes: 0 normally; 1 when the store holds no eBay rows -- but the named
empty-state page is still WRITTEN first, so that failure is visible on disk
rather than only in a shell nobody read.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phoneloop import config, population, population_html  # noqa: E402
from phoneloop.knobs import Knobs  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--html", action="store_true",
                    help="write a self-contained HTML report under DATA_DIR/reports/")
    ap.add_argument("--summary", action="store_true",
                    help="omit the population explorer and its payload "
                         "(~60 KB page instead of ~5 MB)")
    ap.add_argument("--out", type=Path, default=None,
                    help="explicit destination for the HTML page")
    ap.add_argument("--limit-explorer", type=int, default=None,
                    help="cap the number of listings in the explorer payload "
                         "(default: no cap)")
    args = ap.parse_args()

    cfg = config.load()
    knobs = Knobs.from_env()

    started = time.time()
    b = population.build(cfg.DATA_DIR, knobs)

    if b.day is None:
        print(f"no eBay snapshots in DATA_DIR={cfg.DATA_DIR}")
        print("this store holds no rows for source=ebay -- run the sweep "
              "first (scripts/run_daily.cmd), or point PHONELOOP_DATA_DIR at "
              "the store that holds them")
        # Written even on the failure path: an empty-state page on disk is
        # how the next reader finds out the store was empty, and a shell exit
        # code nobody saw is not.
        path = _write(b, cfg, args)
        if path:
            print(f"wrote {path}")
        return 1

    print(f"store          {cfg.DATA_DIR}")
    print(f"snapshot dates {len(b.dates)} ({', '.join(d.isoformat() for d in b.dates)})")
    print(f"listings       {len(b.rows):,} on {b.day}")
    print(f"niches         {len(b.niches)} in scope (of {len(b.in_scope)} "
          f"catalogued leaves)")

    ledger, _ = population.panel_trust_ledger(b)
    print()
    print("=== TRUST LEDGER ===")
    print(f"{ledger['verdicts']} of {ledger['cells']} knob cells rendered a "
          f"verdict. {ledger['buckets']['1']} of those rest on a denominator "
          f"of 1.")
    for name in population.DENOM_BUCKETS:
        print(f"  denominator {name:>4}: {ledger['buckets'][name]:>4} verdicts")

    broken, _ = population.panel_green_because_broken(b)
    print()
    print(f"{len(broken['flagged'])} of the {broken['n_scoring_above_zero']} "
          f"in-scope niches scoring above zero pass only because the data is "
          f"absent")
    for f in broken["flagged"]:
        print(f"  leaf {f['leaf']:<8} seller coverage {f['seller_cov']:.2%} "
              f"({f['seller_known']}/{f['n']}), score {f['score']}, "
              f"rank {f['rank']}, passing {', '.join(f['passing'])}")

    if len(b.dates) < 2:
        print()
        print(population.unlock(need=2, have=len(b.dates), latest=b.day))

    if args.html:
        path = _write(b, cfg, args)
        print()
        print(f"wrote {path} ({path.stat().st_size / 1024:.0f} KB) "
              f"in {time.time() - started:.1f}s")

    return 0


def _write(b, cfg, args) -> Path | None:
    if not args.html and args.out is None:
        return None
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            population_html.render(b, include_explorer=not args.summary,
                                   explorer_limit=args.limit_explorer),
            encoding="utf-8")
        return args.out
    return population_html.write_report(
        b, cfg.DATA_DIR, include_explorer=not args.summary,
        explorer_limit=args.limit_explorer)


if __name__ == "__main__":
    raise SystemExit(main())
