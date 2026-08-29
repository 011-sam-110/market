"""The scheduled night must resolve ONE store, not two.

`run_daily.cmd` defaults `PHONELOOP_DATA_DIR` to `%USERPROFILE%\\.phoneloop\\
prod`, so the sweep lands there. But the process that WATCHES the night --
`scripts/nightly.py`, which owns the lock, the day-guard, the log and the
heartbeat -- resolved its own paths from `config.load().DATA_DIR`, whose
default is `~/.phoneloop/data`. A Task Scheduler action carries no
environment, so nothing reconciled them.

Measured with `python scripts/nightly.py --dry-run` on 2026-08-29, which is
exactly the environment the task runs in:

    data_dir   C:\\Users\\sampo\\.phoneloop\\data      <- watcher
    heartbeat  ...\\.phoneloop\\data\\health\\last_run.json
    checkpoint ...\\.phoneloop\\data\\bulk_ebay_checkpoint_2026-08-29.json

while the sweep it launches writes to `...\\.phoneloop\\prod`, and
`register_daily_task.ps1 -Status` looks for the heartbeat under
`...\\.phoneloop\\prod\\health\\last_run.json`.

Three consequences, none of which go red on their own:

  * **A guaranteed false ALARM every successful night.** The status check
    reads `prod`; the heartbeat is written to `data`. A perfect run reports
    as a run that never happened -- and an alarm that cries wolf nightly is
    an alarm nobody reads on the night it is right.
  * **The day-guard stops working across stores.** It keys on the heartbeat,
    so the two triggers can no longer see each other's completed run.
  * **`_checkpoint_counts` reads a file that is never written**, because
    bulk_ebay writes its checkpoint under the sweep's store.

This is the same split-brain that was found before the scheduler branch
merged, half-fixed: `run_daily.cmd` got the default, and `nightly.py` was
left resolving its own. Half a fix is worse than none here, because the
sweep now works and only the watching is broken -- which is the state that
looks fine right up until the night it isn't.

The invariant pinned below is deliberately about AGREEMENT, not about the
literal path: the two files may name whatever store they like, as long as it
is the same one.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"

_ASSIGN = re.compile(
    r'set\s+"?PHONELOOP_DATA_DIR=([^"\r\n]+)"?', re.IGNORECASE)


def _declared_store(path: Path) -> str | None:
    """The PHONELOOP_DATA_DIR a .cmd assigns, ignoring REM lines."""
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.upper().startswith("REM") or line.startswith("::"):
            continue
        m = _ASSIGN.search(line)
        if m:
            return m.group(1).strip().rstrip("\\")
    return None


class TestTheWrapperPinsTheStore:
    def test_run_daily_scheduled_sets_the_data_dir(self):
        """The wrapper is the only process in the scheduled chain that runs
        BEFORE nightly.py, so it is the only place that can hand the watcher
        and the sweep the same answer."""
        assert _declared_store(SCRIPTS / "run_daily_scheduled.cmd") is not None, (
            "run_daily_scheduled.cmd sets no PHONELOOP_DATA_DIR, so nightly.py "
            "falls back to config's default while the sweep it launches uses "
            "run_daily.cmd's -- the heartbeat and the data land in different "
            "stores")

    def test_both_cmd_files_name_the_same_store(self):
        wrapper = _declared_store(SCRIPTS / "run_daily_scheduled.cmd")
        daily = _declared_store(SCRIPTS / "run_daily.cmd")
        assert wrapper == daily, (
            f"scheduled wrapper points at {wrapper!r} but run_daily.cmd "
            f"defaults to {daily!r}; the night would write its data to one "
            "store and its heartbeat to the other")

    def test_run_daily_still_yields_to_an_inherited_value(self):
        """The wrapper's value must WIN. run_daily.cmd's `if not defined`
        guard is what lets it; without that the wrapper's export would be
        overwritten by the default and the split would return."""
        text = (SCRIPTS / "run_daily.cmd").read_text(encoding="utf-8")
        assert re.search(r"if\s+not\s+defined\s+PHONELOOP_DATA_DIR", text,
                         re.IGNORECASE)


class TestTheWatcherAgreesWithTheSweep:
    def test_nightly_dry_run_reports_the_wrapper_store(self, tmp_path):
        """End to end, in the environment the task actually provides.

        `--dry-run` prints the paths nightly.py would use. Run with
        PHONELOOP_DATA_DIR set the way the wrapper sets it, every path it
        reports must sit under that store -- not under config's default.
        """
        store = tmp_path / "prod"
        # Inherit the real environment and override only the store. A
        # hand-built env omitting USERPROFILE makes Path.home() raise, which
        # fails this test for a reason that has nothing to do with the split
        # it is meant to catch.
        env = dict(os.environ)
        env["PHONELOOP_DATA_DIR"] = str(store)
        out = subprocess.run(
            [sys.executable, str(SCRIPTS / "nightly.py"), "--dry-run"],
            capture_output=True, text=True, env=env, cwd=str(REPO), timeout=120)
        assert out.returncode == 0, out.stderr

        paths = dict(
            line.split(None, 1) for line in out.stdout.splitlines()
            if line.split(None, 1)[0] in
            {"data_dir", "heartbeat", "log", "lock", "checkpoint"})
        assert paths, f"no paths printed: {out.stdout!r}"
        for name, value in paths.items():
            assert str(store) in value, (
                f"{name} resolved to {value.strip()!r}, outside the store the "
                f"wrapper pins ({store})")
