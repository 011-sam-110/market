"""Export the whole store as the dashboard's JSON bundle.

Reads the store ONCE through `population.build()` and hands that one Build to
every exporter, so no two files in a bundle can disagree about the population
they describe.

    python scripts/export_dashboard.py --data-dir ~/.phoneloop/prod

Writes into `dashboard/public/data` by default. Every file is written
atomically and the run prints a size table, because payload size is a real
constraint on a static host and a bundle that quietly tripled is worth
noticing the day it happens rather than the day the page stops loading.

This is a READ-ONLY consumer of the store. It opens no network connection and
writes nothing back into the data directory.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phoneloop import config, dashexport  # noqa: E402
from phoneloop.knobs import Knobs  # noqa: E402
from phoneloop.population import build  # noqa: E402


def _git_sha(repo: Path) -> str:
    """The commit this bundle was built from, or a marked unknown.

    Marked, not blank: a dashboard footer reading "built from " invites the
    reader to assume the field is broken rather than that the answer is
    genuinely unavailable.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        sha = out.stdout.strip()
        return sha or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _human(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f} MB"
    if n >= 1_000:
        return f"{n / 1_000:.1f} KB"
    return f"{n} B"


# What the board can actually load before it paints. Measured against the
# 2026-08-30 bundle, which is 21.17 MB raw across nine files for 29,824
# listings and takes a visible pause to appear.
#
# These are not a limit on the store. They are the point at which the browser
# stops being the right place to hold the whole market, and the export says
# so rather than letting a bigger sweep quietly produce an unusable board.
TOTAL_BUDGET_BYTES = 32 * 1024 * 1024
FILE_BUDGET_BYTES = 12 * 1024 * 1024


def main(argv: list[str] | None = None) -> int:
    repo = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="Export the niche-finder store as the dashboard JSON bundle")
    parser.add_argument("--data-dir", default=None,
                        help="store to read (default: phoneloop.config)")
    parser.add_argument("--out", default=str(repo / "dashboard" / "public" / "data"),
                        help="directory to write the bundle into")
    parser.add_argument("--skip-listings", action="store_true",
                        help="omit the full listing payload (the big one) for a "
                             "fast structural run")
    parser.add_argument("--indent", type=int, default=None,
                        help="pretty-print with this indent (bigger files; for "
                             "reading a bundle by hand, not for shipping)")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir) if args.data_dir else config.load().DATA_DIR
    out_dir = Path(args.out)
    knobs = Knobs.from_env()
    generated_at = datetime.now(timezone.utc)

    print(f"store   {data_dir}")
    print(f"out     {out_dir}")
    print("reading the store once ...", flush=True)
    b = build(data_dir, knobs)

    if b.day is None:
        print("  store holds no eBay snapshots -- writing an empty-state bundle")

    day = b.day.isoformat() if b.day else "empty"
    written: list[tuple[str, int]] = []

    def emit(name: str, payload: object) -> None:
        size = dashexport.write_json(out_dir / name, payload, indent=args.indent)
        written.append((name, size))
        flag = "  OVER BUDGET" if size > FILE_BUDGET_BYTES else ""
        print(f"  {name:<34} {_human(size):>10}{flag}", flush=True)

    emit("manifest.json", dashexport.build_manifest(
        b, git_sha=_git_sha(repo), generated_at=generated_at,
        schema_version=dashexport.SCHEMA_VERSION, store_path=str(data_dir)))
    emit("knobs.json", dashexport.export_knobs(knobs))
    emit("fees.json", dashexport.export_fees(b))
    emit(f"niches-{day}.json", dashexport.export_niches(b, knobs))
    products = dashexport.product_payloads(b, knobs)
    emit(f"products-{day}.json", dashexport.round_floats(products["board"]))
    emit(f"product-metrics-{day}.json", dashexport.round_floats(products["metrics"]))
    emit(f"coverage-{day}.json", dashexport.export_coverage(b))

    if b.diff is not None:
        pair = f"{b.diff.date_a.isoformat()}-{b.diff.date_b.isoformat()}"
        emit(f"deltas-{pair}.json",
             dashexport.round_floats(dashexport.export_deltas(b)))
        # The per-listing and per-product arrays, in their own file. Only the
        # Change view reads them, and the board loads the summary above
        # before it paints anything.
        emit(f"delta-detail-{pair}.json",
             dashexport.round_floats(dashexport.export_delta_detail(b)))
    else:
        emit("deltas.json", dashexport.export_deltas(b))

    if args.skip_listings:
        print("  listings                           SKIPPED (--skip-listings)")
    else:
        emit(f"listings-{day}.json",
             dashexport.round_floats(dashexport.encode_listings(b.rows, b.extra)))

    total = sum(size for _n, size in written)
    print(f"\n  {'TOTAL':<34} {_human(total):>10}   ({len(written)} files)")

    # Every payload here is O(listings) and nothing in it is sampled or
    # capped, which is the right default and the reason a scale change can
    # break the board silently. Raising the leaf set ten-fold does not fail
    # this export -- it writes a bundle no browser will load, and the only
    # symptom is a board that never finishes its first paint.
    #
    # So the budget is stated, checked, and printed. It does not stop the
    # export: a bundle that is too big is still the truth about a store that
    # got bigger, and refusing to write it would leave the board serving
    # yesterday with nothing to say about why. It fails LOUDLY instead, and
    # names the payloads to shard first.
    over = [(n, size) for n, size in written if size > FILE_BUDGET_BYTES]
    if total > TOTAL_BUDGET_BYTES or over:
        print(f"\n  OVER BUDGET. The bundle is {_human(total)} against a "
              f"{_human(TOTAL_BUDGET_BYTES)} budget"
              + (f", and {len(over)} file(s) exceed "
                 f"{_human(FILE_BUDGET_BYTES)} on their own" if over else "")
              + ".")
        for name, size in sorted(over, key=lambda x: -x[1]):
            print(f"    {name:<34} {_human(size):>10}")
        print("  The board loads the whole bundle before it paints anything, "
              "so this is a board that hangs, not a board that is slow.")
        print("  Shard the payloads above by leaf, or ship a ranked head and "
              "load the tail on demand. Do not sample them silently.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
