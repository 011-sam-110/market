"""Write the knob-engine golden fixture the TypeScript side is pinned to.

    python scripts/export_knob_golden.py --data-dir ~/.phoneloop/prod

Regenerate this whenever `fees.py` or `metrics.check` changes on purpose, and
commit the result. If you change either and DO NOT regenerate,
`tests/test_knob_golden.py` goes red -- which is the entire point: the browser
holds a second copy of this arithmetic and a silent divergence between the two
would put a confident PASS on screen for a row the pipeline calls a FAIL.

Output is deterministic (sorted keys, no clock, no randomness), so regenerating
without changing anything produces an identical file and the drift alarm never
cries wolf.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phoneloop import config  # noqa: E402
from phoneloop.dashexport import golden as golden_mod  # noqa: E402
from phoneloop.dashexport.jsonio import dumps, write_json  # noqa: E402
from phoneloop.knobs import Knobs  # noqa: E402
from phoneloop.population import build  # noqa: E402

DEFAULT_OUT = "dashboard/test/fixtures/knob-golden.json"


def generate(data_dir: Path, knobs: Knobs | None = None) -> dict:
    """The fixture, from the real store plus the constructed edge cases."""
    knobs = knobs or Knobs()
    b = build(data_dir, knobs)
    cases = golden_mod.cases_from_store(b, knobs) + golden_mod.constructed_cases(knobs)
    return golden_mod.build_golden(cases)


def main(argv: list[str] | None = None) -> int:
    repo = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Write the knob golden fixture")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--out", default=str(repo / DEFAULT_OUT))
    parser.add_argument("--check", action="store_true",
                        help="regenerate and compare against the committed file "
                             "without writing; exit 1 on drift")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir) if args.data_dir else config.load().DATA_DIR
    payload = generate(data_dir)
    out = Path(args.out)

    if payload["inert_settings"]:
        print(f"WARNING: these knob settings changed no verdict on any case, so "
              f"they pin nothing: {payload['inert_settings']}", file=sys.stderr)

    if args.check:
        if not out.exists():
            print(f"{out} does not exist", file=sys.stderr)
            return 1
        drifted = out.read_text(encoding="utf-8") != dumps(payload, indent=2) + "\n"
        print("DRIFT" if drifted else "up to date")
        return 1 if drifted else 0

    size = write_json(out, payload, indent=2)
    print(f"{out}")
    print(f"  {payload['n_cases']} cases x {len(payload['knob_settings'])} knob "
          f"settings = {payload['n_expectations']} expectations, {size/1000:.1f} KB")
    n_store = sum(1 for c in payload["cases"] if c["source"] == "store")
    print(f"  {n_store} drawn from the real store, "
          f"{payload['n_cases'] - n_store} constructed edge cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
