"""Did last night's sweep actually happen? One command, one answer.

    python scripts/sweep_status.py              # the assessment, plus evidence
    python scripts/sweep_status.py --quiet      # the headline only

Exits 0 on OK/WARN/UNKNOWN and 1 on ALARM, so it is usable from a hook, a
scheduled check or a shell `if` without parsing anything.

The answer is `f(heartbeat, snapshot_dates, now)` -- never the heartbeat
alone. The heartbeat EXPLAINS a night; the store is the EVIDENCE for it, and
the combination "the record says ok and the store gained nothing" is the one
this project most needs to hear about. See phoneloop/heartbeat.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phoneloop import config, heartbeat  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, default=None,
                    help="override PHONELOOP_DATA_DIR")
    ap.add_argument("--now", default=None,
                    help="ISO timestamp to assess AS OF, for testing the "
                         "staleness rules without waiting")
    ap.add_argument("--quiet", action="store_true",
                    help="print the headline only")
    ap.add_argument("--json", action="store_true",
                    help="print the assessment as JSON")
    args = ap.parse_args(argv)

    data_dir = args.data_dir or config.load().DATA_DIR
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)

    health = heartbeat.health_for(data_dir, now=now)

    if args.json:
        print(json.dumps({"level": health.level, "headline": health.headline,
                          "detail": health.detail, "evidence": health.evidence},
                         indent=2, default=str))
    elif args.quiet:
        print(f"[{health.level}] {health.headline}")
    else:
        print(f"[{health.level}] {health.headline}")
        print(f"  {health.detail}")
        print()
        print(f"  data_dir  {data_dir}")
        print(f"  heartbeat {heartbeat.path_for(data_dir)}")
        for key in ("expected_run_date", "run_date", "snapshot_dates",
                    "listings_today", "listings_prev_date", "blocked_shards",
                    "done_shards", "log_path"):
            if key in health.evidence:
                print(f"  {key:<17} {health.evidence[key]}")

    # ALARM is the only level that is a problem right now. WARN wants a look;
    # UNKNOWN means the check could not run, which is not itself a failed
    # night and must not page anyone as though it were.
    return 1 if health.level == heartbeat.ALARM else 0


if __name__ == "__main__":
    raise SystemExit(main())
