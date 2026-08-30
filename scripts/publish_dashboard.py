"""Build the dashboard and put it on Vercel, with a guard against stale data.

    python scripts/publish_dashboard.py                    # build + check only
    python scripts/publish_dashboard.py --deploy           # -> a preview URL
    python scripts/publish_dashboard.py --deploy --prod    # -> production

The guard is the point. `export_dashboard.py` writes into
`dashboard/public/data`, and `npm run build` copies whatever is sitting there
into `dist`. So a failed export does not produce a failed deploy -- it
produces a perfectly healthy deploy of YESTERDAY's figures, with today's date
nowhere on it. The board would look right, the operator would read it as
today, and nothing anywhere would have gone red.

So before anything is built, the manifest's `latest_date` is compared against
the newest snapshot date actually in the store. If they disagree the run
stops. `--allow-stale` exists for the deliberate case (re-publishing a fix
against an unchanged store) and says so in the output rather than being
silent about it.

No git command runs here, ever. This repository is nested inside the user's
home repository, and an automated commit is exactly how `.claude/` and the
memory directory would reach a public remote. `vercel deploy --prebuilt`
uploads a build; it does not need a commit, a branch, or a remote.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
DASHBOARD = REPO / "dashboard"
DATA = DASHBOARD / "public" / "data"


def _run(cmd: list[str], *, cwd: Path) -> int:
    print(f"  $ {' '.join(cmd)}")
    return subprocess.call(cmd, cwd=str(cwd), shell=(sys.platform == "win32"))


def store_latest_date(data_dir: Path) -> str | None:
    """The newest snapshot date in the store, read without opening DuckDB.

    The store is Hive-partitioned as `date=YYYY-MM-DD/`, so the directory
    names are the authority and reading them costs nothing. Deliberately does
    NOT go through `population.build`: this check has to work even when the
    thing that is broken is the code that builds.
    """
    dates = sorted(
        p.name.split("=", 1)[1]
        for p in data_dir.rglob("date=*")
        if p.is_dir() and "=" in p.name
    )
    return dates[-1] if dates else None


def check_freshness(data_dir: Path) -> tuple[bool, str]:
    manifest_path = DATA / "manifest.json"
    if not manifest_path.exists():
        return False, (
            f"{manifest_path} does not exist. Run "
            "`python scripts/export_dashboard.py` first -- there is nothing "
            "to publish."
        )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    exported = manifest.get("latest_date")
    newest = store_latest_date(data_dir)

    if newest is None:
        return False, (
            f"no `date=` partitions under {data_dir}, so the store's own "
            "newest date cannot be established and the guard cannot run."
        )
    if exported != newest:
        return False, (
            f"the exported bundle says {exported} but the newest snapshot in "
            f"the store is {newest}. The export did not finish, or it ran "
            "against a different store. Publishing now would republish "
            f"{exported} as though it were today."
        )
    return True, f"bundle date {exported} matches the newest snapshot in the store"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build and publish the dashboard")
    parser.add_argument("--data-dir", default=None,
                        help="the store to check the bundle against")
    parser.add_argument("--deploy", action="store_true",
                        help="upload the build; without it this only builds")
    parser.add_argument("--prod", action="store_true",
                        help="deploy to production instead of a preview URL")
    parser.add_argument("--allow-stale", action="store_true",
                        help="publish even when the bundle is older than the store")
    args = parser.parse_args(argv)

    if not DASHBOARD.exists():
        print(f"{DASHBOARD} does not exist", file=sys.stderr)
        return 1

    from phoneloop import config
    data_dir = Path(args.data_dir) if args.data_dir else config.load().DATA_DIR

    print("[publish] freshness guard")
    fresh, why = check_freshness(data_dir)
    print(f"  {why}")
    if not fresh:
        if not args.allow_stale:
            print("[publish] STOPPED. Re-run the export, or pass --allow-stale "
                  "if you meant to publish an unchanged bundle.", file=sys.stderr)
            return 1
        print("  --allow-stale given: publishing anyway, deliberately.")

    print("[publish] build")
    if _run(["npm", "run", "build"], cwd=DASHBOARD) != 0:
        print("[publish] the build failed; nothing was uploaded", file=sys.stderr)
        return 1

    if not args.deploy:
        print("[publish] built only (no --deploy). Preview it with:")
        print("  cd dashboard && npm run preview")
        return 0

    if shutil.which("vercel") is None:
        print("[publish] the vercel CLI is not on PATH. `npm i -g vercel`, then "
              "`vercel login` -- its auth is SEPARATE from the MCP integration's.",
              file=sys.stderr)
        return 1

    # `vercel build` produces .vercel/output the way the platform expects,
    # including the api/ function. Hand-assembling that directory works for
    # static files and quietly drops the function.
    target = ["--prod"] if args.prod else []
    print(f"[publish] vercel build{' --prod' if args.prod else ''}")
    if _run(["vercel", "build", *target], cwd=DASHBOARD) != 0:
        print("[publish] vercel build failed; nothing was uploaded", file=sys.stderr)
        return 1

    print(f"[publish] vercel deploy --prebuilt{' --prod' if args.prod else ''}")
    if _run(["vercel", "deploy", "--prebuilt", *target], cwd=DASHBOARD) != 0:
        print("[publish] the upload failed", file=sys.stderr)
        return 1

    print("[publish] done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
