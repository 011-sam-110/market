"""The unattended nightly wrapper around scripts/run_daily.cmd.

This script owns everything that makes a scheduled run *legible*: the lock,
the day-guard, the log, the two-phase heartbeat and a readable exit code. It
deliberately does NOT re-express the three steps of the night in Python.
`scripts/run_daily.cmd` is the only artefact that has been exercised against
live eBay, and a second definition of "the night" would be unverified code
running unattended at 03:00 -- exactly the thing this deliverable exists to
avoid. So the night is launched as ONE child and its output is streamed to
disk verbatim.

    python scripts/nightly.py                 # the scheduled path
    python scripts/nightly.py --force         # ignore the day-guard
    python scripts/nightly.py --dry-run       # print what it would launch

**Exit codes**, chosen so Task Scheduler's `LastTaskResult` is readable on
its own without opening a log:

    0   ok (including "already done today", which is a correct outcome)
    1   a step of run_daily.cmd failed
    75  EX_TEMPFAIL -- another run holds the lock. Deliberately NOT 1: a
        backed-off run is not a failed one, and with two triggers a backoff
        is a normal, expected event.
    2   internal error in this script

**Why the heartbeat is written twice.** `state="running"` lands before the
child launches; the terminal record lands in a `finally`. A hard kill --
Task Scheduler enforcing its 3h execution-time-limit, a battery unplug, a
power cut -- skips the `finally` entirely, so the stuck `running` record IS
the alarm, and `phoneloop.heartbeat.assess()` reads it as one. There is no
arrangement in which a killed run leaves no trace.

**The interpreter.** This module never launches python at all; it launches
`run_daily.cmd`, which pins `%LOCALAPPDATA%\\Programs\\Python\\Python313\\
python.exe` itself. If it ever needs to, it must use `sys.executable` --
since `run_daily_scheduled.cmd` starts this script with the pinned
interpreter, `sys.executable` IS the installer python by construction, which
makes the MSIX silent-exit trap structurally impossible here rather than
merely avoided by convention.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from bulk_ebay import default_checkpoint_path  # noqa: E402

from phoneloop import config, heartbeat, runlock  # noqa: E402
from phoneloop.market_store import MarketStore  # noqa: E402

EXIT_OK = 0
EXIT_STEP_FAILED = 1
EXIT_INTERNAL = 2
EXIT_BUSY = 75          # EX_TEMPFAIL

_LOG_DIR_NAME = "logs"

# run_daily.cmd's own step banners, e.g.
#   [run_daily] Fri 29/08/2026  3:00:01.00 -- [1/3] SWEEP
#   [run_daily] SWEEP failed -- checkpointed, safe to re-run
_STEP_RE = re.compile(r"\[run_daily\].*?--\s*\[\d+/\d+\]\s*(?P<name>[A-Z]+)")
_FAIL_RE = re.compile(r"\[run_daily\]\s+(?P<name>[A-Z]+)\s+failed")


def default_command(repo_root: Path | str | None = None,
                    platform: str = os.name) -> list[str]:
    """The child to launch: run_daily.cmd via cmd.exe on Windows, and
    run_daily.sh via bash everywhere else (the provenance-1 host since
    2026-09-27). The two files run the same steps in the same order.

    No element of this is a Python interpreter name -- see the module
    docstring.
    """
    root = Path(repo_root) if repo_root else _REPO_ROOT
    if platform == "nt":
        return ["cmd.exe", "/c", str(root / "scripts" / "run_daily.cmd")]
    return ["bash", str(root / "scripts" / "run_daily.sh")]


def log_path_for(data_dir: Path | str, day: date) -> Path:
    return Path(data_dir) / _LOG_DIR_NAME / f"nightly-{day:%Y-%m-%d}.log"


def checkpoint_path_for(data_dir: Path | str, day: date) -> Path:
    """The sweep's dated checkpoint. Imported from `bulk_ebay` rather than
    re-derived, so the filename format has exactly one definition."""
    return default_checkpoint_path(Path(data_dir), day)


def parse_steps(lines: Sequence[str]) -> tuple[list[dict], str | None]:
    """(steps seen, the step that failed).

    Read off run_daily.cmd's own banners rather than inferred, so the
    heartbeat says WHICH of SWEEP/DEEPEN/BOARD died without this script
    having its own opinion about what the night contains.
    """
    steps: list[dict] = []
    failed: str | None = None
    for line in lines:
        m = _STEP_RE.search(line)
        if m:
            steps.append({"name": m.group("name"), "line": line.strip()})
            continue
        f = _FAIL_RE.search(line)
        if f:
            failed = f.group("name")
    return steps, failed


def _subprocess_runner(command, *, log, env=None, cwd=None) -> int:
    """The real runner. Streams the child's combined output line by line into
    the open log handle, so a run killed at the execution-time-limit still
    leaves everything it printed up to that moment on disk."""
    proc = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        env=env, cwd=str(cwd) if cwd else None,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        log.write(line)
        log.flush()
    return proc.wait()


def _store_counts(data_dir: Path, run_date: date) -> tuple[int | None, int | None]:
    """(listings on run_date, listings on the previous snapshot date).

    Both are `None` on any failure -- an unknown must never be reported as a
    number, and "the store would not open" is emphatically not "0 listings".
    """
    try:
        store = MarketStore(data_dir)
        dates = store.snapshot_dates("ebay")
        today_n = len(store.rows_on(run_date)) if run_date in dates else 0
        prior = [d for d in dates if d < run_date]
        prev_n = len(store.rows_on(prior[-1])) if prior else None
        return today_n, prev_n
    except Exception:                            # noqa: BLE001 - see docstring
        return None, None


def _checkpoint_counts(data_dir: Path, run_date: date) -> tuple[int | None, int | None]:
    """(done shards, blocked shards) from the sweep's dated checkpoint.

    `None` when there is no checkpoint to read. Reporting "0 blocked" for a
    night that never wrote one would be an unknown printed as a number --
    and "0 blocked" is precisely the reassuring reading.
    """
    path = checkpoint_path_for(data_dir, run_date)
    try:
        cp = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    done = cp.get("done")
    blocked = cp.get("blocked")
    return (len(done) if isinstance(done, (dict, list)) else None,
            len(blocked) if isinstance(blocked, (dict, list)) else None)


def run_nightly(*, data_dir: Path | str, now: datetime | None = None,
                runner: Callable = _subprocess_runner,
                command: Sequence[str] | None = None,
                force: bool = False,
                clock: Callable[[], datetime] | None = None,
                repo_root: Path | str | None = None) -> int:
    """One night. Returns the process exit code.

    `runner` is injected so every branch here is unit-testable with no eBay
    and no Chrome; `now` likewise, so the day-guard and the lock's staleness
    are testable without waiting a day.

    `clock` supplies `finished_at` -- the night takes about an hour, so the
    start time is NOT the finish time, and `assess()`'s staleness rule reads
    `finished_at`. When `now` is injected without a clock the clock freezes
    at `now`, so a test asserting on the record stays deterministic; the
    production path (`now=None`) reads the real clock at both ends.
    """
    if clock is None:
        clock = (lambda frozen=now: frozen) if now is not None else (
            lambda: datetime.now(UTC))
    now = (now or datetime.now(UTC)).astimezone(UTC)
    data_dir = Path(data_dir)
    run_date = now.date()                    # UTC, matching snapshot_dates()
    hb_path = heartbeat.path_for(data_dir)
    attempt_path = heartbeat.attempt_path_for(data_dir)
    command = list(command) if command else default_command(repo_root)

    # --- day-guard ----------------------------------------------------------
    # What makes two triggers and a manual re-run safe. Keyed on state=='ok':
    # a FAILED night must stay re-runnable the same day, which is the whole
    # point of the logon catch-up trigger.
    try:
        existing = heartbeat.read(hb_path)
    except heartbeat.CorruptRecord:
        existing = None                      # unreadable == no claim on today
    if (not force and existing and existing.get("state") == "ok"
            and existing.get("run_date") == run_date.isoformat()):
        heartbeat.append_attempt(attempt_path, {
            "outcome": "already_done_today",
            "run_date": run_date.isoformat(),
            "note": "last_run.json already reports ok for this UTC date; "
                    "pass --force to sweep again",
        }, now=now)
        print(f"[nightly] already swept {run_date} (state=ok) -- nothing to do")
        return EXIT_OK

    # --- lock ---------------------------------------------------------------
    def _broke(info: dict) -> None:
        print(f"[nightly] broke an abandoned lock: pid {info['pid']}, "
              f"{info['age_hours']:.1f}h old (stale after "
              f"{info['stale_after_hours']:.0f}h)")

    try:
        lock_cm = runlock.hold(data_dir, now=now, on_break=_broke)
        lock_cm.__enter__()
    except runlock.Busy as exc:
        heartbeat.append_attempt(attempt_path, {
            "outcome": "busy",
            "held_by_pid": exc.pid,
            "held_since": exc.started_at,
            "note": str(exc),
        }, now=now)
        print(f"[nightly] {exc}")
        return EXIT_BUSY

    log_file = log_path_for(data_dir, run_date)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    harness_mode = os.environ.get("PHONELOOP_HARNESS")

    record = heartbeat.new_record(
        state="running", run_date=run_date, started_at=now,
        data_dir=data_dir, log_path=log_file, harness_mode=harness_mode,
        command=command,
    )
    # BEFORE the child launches: a kill after this point still leaves a
    # record, and a stuck `running` reads as ALARM the next morning.
    heartbeat.write(hb_path, record)

    exit_code: int | None = None
    error: str | None = None
    steps: list[dict] = []
    failed_step: str | None = None
    try:
        with log_file.open("a", encoding="utf-8") as log:
            log.write(f"\n=== nightly {now.isoformat()} pid {os.getpid()} "
                      f"data_dir={data_dir} ===\n")
            log.flush()
            try:
                exit_code = runner(command, log=log, env=os.environ.copy(),
                                   cwd=repo_root or _REPO_ROOT)
            except Exception as exc:             # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
                log.write(f"[nightly] launcher error: {error}\n")
        try:
            body = log_file.read_text(encoding="utf-8", errors="replace")
            steps, failed_step = parse_steps(body.splitlines())
        except OSError:                          # pragma: no cover - defensive
            pass
    finally:
        today_n, prev_n = _store_counts(data_dir, run_date)
        done_n, blocked_n = _checkpoint_counts(data_dir, run_date)
        finished = clock().astimezone(UTC)
        if error is not None:
            state, result = "failed", EXIT_INTERNAL
        elif exit_code == 0:
            state, result = "ok", EXIT_OK
        else:
            state, result = "failed", EXIT_STEP_FAILED
        record.update({
            "state": state,
            "finished_at": finished.isoformat(),
            "exit_code": exit_code,
            "error": error,
            "failed_step": failed_step or (steps[-1]["name"] if steps and
                                           state == "failed" else None),
            "steps": steps,
            "listings_today": today_n,
            "listings_prev_date": prev_n,
            "done_shards": done_n,
            "blocked_shards": blocked_n,
        })
        heartbeat.write(hb_path, record)
        lock_cm.__exit__(None, None, None)

    heartbeat.append_attempt(attempt_path, {
        "outcome": state, "exit_code": exit_code, "run_date": run_date.isoformat(),
    }, now=now)
    print(f"[nightly] {state} -- exit {exit_code!r}, log {log_file}")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true",
                    help="sweep again even if today's run already reported ok")
    ap.add_argument("--data-dir", type=Path, default=None,
                    help="override PHONELOOP_DATA_DIR for this run")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the child command and the paths, launch nothing")
    ap.add_argument("--command", action="append", default=None,
                    help="SMOKE TEST ONLY: launch this instead of "
                         "run_daily.cmd, one --command per argv element. Exists "
                         "so the lock/heartbeat/log/exit-code path can be proved "
                         "end to end against a trivial child rather than an hour "
                         "of live eBay. The scheduled action never passes it -- "
                         "run_daily_scheduled.cmd passes no arguments at all.")
    args = ap.parse_args(argv)

    data_dir = args.data_dir or config.load().DATA_DIR
    if args.dry_run:
        today = datetime.now(UTC).date()
        print(f"data_dir   {data_dir}")
        print(f"command    {default_command()}")
        print(f"heartbeat  {heartbeat.path_for(data_dir)}")
        print(f"log        {log_path_for(data_dir, today)}")
        print(f"lock       {runlock.lock_path_for(data_dir)}")
        print(f"checkpoint {checkpoint_path_for(data_dir, today)}")
        return EXIT_OK

    if args.command:
        print(f"[nightly] SMOKE TEST: launching {args.command}, NOT the sweep")

    try:
        return run_nightly(data_dir=data_dir, force=args.force,
                           command=args.command)
    except Exception as exc:                     # noqa: BLE001
        print(f"[nightly] internal error: {type(exc).__name__}: {exc}")
        return EXIT_INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())
