"""Export the paper forecast record as the dashboard's forecast.json.

    python scripts/export_forecast.py --db ~/.forecast/ledger.sqlite3

The ledger lives on provenance-1 (~/.forecast/ledger.sqlite3). To view it on
this machine, copy it first:

    scp provenance-1:.forecast/ledger.sqlite3 ~/.forecast/ledger.sqlite3

Writes dashboard/public/data/forecast.json by default, with the same writer
as scripts/export_dashboard.py. Read only: it opens no network connection
and changes nothing in the ledger.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forecast.export import build_payload  # noqa: E402
from forecast.ledger import Ledger  # noqa: E402
from phoneloop.dashexport.jsonio import write_json  # noqa: E402


def _git_sha(repo: Path) -> str:
    try:
        out = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=10, check=False)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    repo = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Export the forecast record for the dashboard")
    parser.add_argument("--db", default=None, help="ledger (default ~/.forecast/ledger.sqlite3)")
    parser.add_argument("--out", default=str(repo / "dashboard" / "public" / "data"))
    parser.add_argument("--indent", type=int, default=None)
    args = parser.parse_args(argv)

    with Ledger(args.db) as ledger:
        payload = build_payload(ledger, datetime.now(timezone.utc), git_sha=_git_sha(repo))
        size = write_json(Path(args.out) / "forecast.json", payload, indent=args.indent)
    counts = payload["report"]["counts"]
    print(f"ledger  {payload['report']['ledger_path']}")
    print(f"wrote   {Path(args.out) / 'forecast.json'} ({size} bytes)")
    print(f"        forecasts {counts['forecasts']}, resolved {counts['resolved']}, "
          f"recent rows {len(payload['recent'])}, dropped {payload['recent_dropped']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
