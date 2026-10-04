"""Print the trial log: each sample order with its gate, and each test
listing's predicted figures against its realised ones, each with its n
(docs/dropship-process.md, stage 6).

    python scripts/trial_report.py

Reads ~/.phoneloop/trials/trials.jsonl (override: PHONELOOP_TRIALS_DIR).
A figure with no observation prints "?" with n=0. It is never read as zero.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phoneloop import trials  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter).parse_args(argv)
    try:
        log = trials.TrialLog(trials.trials_dir())
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(trials.render_report(log))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
