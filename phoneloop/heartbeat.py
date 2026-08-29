"""What happened last night, and whether to believe it.

The heartbeat file EXPLAINS a night. `store.snapshot_dates()` is the
EVIDENCE for it. Health is therefore `f(heartbeat, snapshot_dates,
checkpoint, now)` and never the heartbeat alone -- because a heartbeat can
say "ok" while the store gained nothing, and that combination is this
project's defining failure: a wrong `PHONELOOP_DATA_DIR`, a store write that
died after the sweep, or an eBay block that checkpointed every shard as
"blocked" all exit 0, all write a plausible record, and all leave the board
printing yesterday's numbers forever. `assess()`'s
"state=='ok' but no snapshot for run_date" rule is the one that catches it.
Anyone later "simplifying" this module to read only the heartbeat
reintroduces that blind spot silently.

Three ways silence is designed against:

1. **The record is written TWICE** -- `state="running"` before anything
   launches, `state="ok"`/`"failed"` at the end. A hard kill (Task Scheduler
   enforcing its execution-time-limit, a battery unplug, a power cut) skips
   the `finally` that would write the terminal record, so the stuck
   `running` record IS the alarm. `assess()` must read a `running` older
   than `max_runtime_hours` as ALARM, never as "in progress" -- get that
   wrong and the stuck state becomes the quietest failure of all.
2. **`last_attempt.json` sits beside `last_run.json`** so "the trigger fired
   and the run skipped itself" is distinguishable from "the trigger never
   fired". A skipped night writes nothing to `last_run.json` at all.
3. **Assessment is computed at READ time from `now`.** The most likely
   failure on a laptop -- the machine being off at 03:00 -- writes nothing
   anywhere, so only staleness measured against the current clock can catch
   it. Baking a verdict in at write time cannot.

`run_date` is a **UTC** date, matching `MarketStore.snapshot_dates()` (which
casts `seen_at AT TIME ZONE 'UTC'`). A local date silently misfires across
BST midnight: 00:30 BST on the 29th is 23:30 UTC on the 28th.
"""

from __future__ import annotations

import json
import os
import socket
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

SCHEMA = 1

_HEALTH_DIR = "health"
_RUN_FILE = "last_run.json"
_ATTEMPT_FILE = "last_attempt.json"

# Levels, most to least severe.
ALARM = "ALARM"
WARN = "WARN"
UNKNOWN = "UNKNOWN"
OK = "OK"

# A run is expected to have STARTED by run_hour_utc + this many hours before
# its absence counts against it. Without the grace, a check at 03:05 for an
# 03:00 trigger reads a run that is legitimately still starting as missing.
DEFAULT_GRACE_HOURS = 1

# Longer than the task's 3h ExecutionTimeLimit: past this, a `running`
# record is definitionally abandoned rather than possibly still working.
DEFAULT_MAX_RUNTIME_HOURS = 3

# 26h, not 24: a run that slips an hour (StartWhenAvailable catch-up, a DST
# shift) must not read as a missed night the following morning.
DEFAULT_STALE_HOURS = 26

# A night that lands under half of the previous date's population is not
# "fewer listings today", it is a sweep something interfered with. Reported
# as both raw counts, never as a bare percentage.
COLLAPSE_FRACTION = 0.5


class CorruptRecord(ValueError):
    """The heartbeat file exists but is not a record we can read.

    Raised rather than swallowed, so a caller that needs the truth (the
    nightly runner) sees it. `health_for()` catches it and reports UNKNOWN,
    because a health check that can take the board down is worse than none.
    """


@dataclass(frozen=True)
class Health:
    level: str            # ALARM | WARN | UNKNOWN | OK
    headline: str         # one line, safe to print above a board
    detail: str           # names the path, the step, the numbers
    evidence: dict = field(default_factory=dict)

    @property
    def is_alarm(self) -> bool:
        return self.level == ALARM


# --- paths -------------------------------------------------------------------

def path_for(data_dir: Path | str) -> Path:
    return Path(data_dir) / _HEALTH_DIR / _RUN_FILE


def attempt_path_for(data_dir: Path | str) -> Path:
    return Path(data_dir) / _HEALTH_DIR / _ATTEMPT_FILE


# --- io ----------------------------------------------------------------------

def write(path: Path | str, record: Mapping) -> Path:
    """Write a record atomically: a temp file in the same directory, then
    `os.replace`. A half-written heartbeat read by the board would be worse
    than no heartbeat -- it would read as UNKNOWN when the run may well have
    been fine, and on Windows a torn write is a real possibility when the
    task is killed at its execution-time-limit mid-`write_text`.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(dict(record), indent=2, sort_keys=True),
                   encoding="utf-8")
    os.replace(tmp, path)
    return path


def read(path: Path | str) -> dict | None:
    """The record, or None when the file does not exist.

    A file that exists but does not parse raises `CorruptRecord` rather than
    returning None -- "never ran" and "wrote something we cannot read" are
    different claims and only one of them is an alarm.
    """
    path = Path(path)
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CorruptRecord(f"{path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise CorruptRecord(f"{path}: expected an object, got {type(loaded).__name__}")
    return loaded


def append_attempt(path: Path | str, entry: Mapping, *, keep: int = 20,
                   now: datetime | None = None) -> Path:
    """Append one attempt to the rolling attempt log.

    This is what distinguishes "the trigger fired and the run declined to do
    anything" (already done today, or another run holds the lock) from "the
    trigger never fired at all" -- the latter writes nothing anywhere, and
    is the most likely failure on a laptop that sleeps.
    """
    path = Path(path)
    now = now or datetime.now(UTC)
    attempts: list = []
    try:
        existing = read(path)
    except CorruptRecord:
        existing = None
    if existing and isinstance(existing.get("attempts"), list):
        attempts = existing["attempts"]
    attempts.append({"at": now.isoformat(), **dict(entry)})
    return write(path, {"schema": SCHEMA, "attempts": attempts[-keep:]})


def new_record(*, state: str, run_date: date, started_at: datetime,
               data_dir: Path | str, log_path: Path | str | None,
               harness_mode: str | None, **over) -> dict:
    """A record with every documented field present, so a consumer never has
    to distinguish "absent" from "unknown" for a field the writer simply
    forgot."""
    record = {
        "schema": SCHEMA,
        "state": state,
        "run_date": run_date.isoformat(),
        "started_at": started_at.isoformat(),
        "finished_at": None,
        "exit_code": None,
        "failed_step": None,
        "steps": [],
        "data_dir": str(data_dir),
        "log_path": str(log_path) if log_path is not None else None,
        "harness_mode": harness_mode,
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "listings_today": None,
        "listings_prev_date": None,
        "blocked_shards": None,
        "done_shards": None,
    }
    record.update(over)
    return record


# --- time --------------------------------------------------------------------

def _parse_dt(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _parse_date(value: object) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def expected_run_date(now: datetime, *, run_hour_utc: int = 2,
                      grace_hours: int = DEFAULT_GRACE_HOURS) -> date:
    """Which UTC date the most recent run should be stamped with.

    Stated explicitly because the off-by-one here decides whether the check
    is a nag (alarming at 02:30 about a run scheduled for 03:00) or a blind
    spot (never noticing a missed night). `now` must be UTC-aware; a naive
    value is read as UTC.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    now = now.astimezone(UTC)
    if now.hour >= run_hour_utc + grace_hours:
        return now.date()
    return now.date() - timedelta(days=1)


def _fmt_hours(delta: timedelta) -> str:
    return f"{delta.total_seconds() / 3600:.0f}"


# --- assessment ---------------------------------------------------------------

def assess(run: Mapping | None, attempt: Mapping | None,
           snapshot_dates: Sequence[date], *, now: datetime,
           data_dir: Path | str | None = None,
           run_hour_utc: int = 2,
           grace_hours: int = DEFAULT_GRACE_HOURS,
           max_runtime_hours: float = DEFAULT_MAX_RUNTIME_HOURS,
           stale_hours: float = DEFAULT_STALE_HOURS) -> Health:
    """Read the night. The severity ladder, in order, is the module contract.

    `snapshot_dates` is the evidence half of the equation -- pass
    `MarketStore(data_dir).snapshot_dates("ebay")`. It is a plain argument
    rather than something this function fetches so the ladder stays testable
    without a store, and so a caller that already has the dates does not pay
    for a second duckdb open.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    now = now.astimezone(UTC)

    hb_path = path_for(data_dir) if data_dir is not None else None
    dates = set(snapshot_dates or ())
    expected = expected_run_date(now, run_hour_utc=run_hour_utc,
                                 grace_hours=grace_hours)
    evidence: dict = {
        "now": now.isoformat(),
        "expected_run_date": expected.isoformat(),
        "snapshot_dates": [d.isoformat() for d in sorted(dates)][-5:],
        "n_snapshot_dates": len(dates),
        "heartbeat_path": str(hb_path) if hb_path else None,
    }
    if attempt is not None:
        attempts = attempt.get("attempts") if isinstance(attempt, Mapping) else None
        if isinstance(attempts, list) and attempts:
            evidence["last_attempt"] = attempts[-1]

    # 1 -- nothing has ever reported.
    if not run:
        if expected not in dates:
            return Health(
                ALARM,
                "no nightly run has ever reported, and the store has no "
                f"snapshot for {expected}",
                (f"expected a heartbeat at {hb_path}. Neither it nor a "
                 f"{expected} eBay snapshot exists, so no sweep has run "
                 "under the scheduler. Register the task "
                 "(scripts/register_daily_task.ps1) or run "
                 "scripts/run_daily_scheduled.cmd by hand."),
                evidence,
            )
        return Health(
            UNKNOWN,
            "no nightly heartbeat yet (the store does have data)",
            (f"no heartbeat at {hb_path}, but the store holds a snapshot for "
             f"{expected} -- the sweep has been run by hand, not under "
             "nightly.py. Nothing is wrong; nothing is being watched either."),
            evidence,
        )

    state = run.get("state")
    run_date = _parse_date(run.get("run_date"))
    started_at = _parse_dt(run.get("started_at"))
    finished_at = _parse_dt(run.get("finished_at"))
    log_path = run.get("log_path")
    evidence.update({"state": state,
                     "run_date": run_date.isoformat() if run_date else None,
                     "started_at": run.get("started_at"),
                     "finished_at": run.get("finished_at"),
                     "exit_code": run.get("exit_code"),
                     "log_path": log_path})

    # 2 -- a run that started and never reported finishing.
    if state == "running":
        if started_at is None:
            return Health(
                UNKNOWN, "a run is marked running with no start time",
                f"heartbeat at {hb_path} has state=running but no readable "
                "started_at, so its age cannot be judged.", evidence)
        age = now - started_at
        if age > timedelta(hours=max_runtime_hours):
            return Health(
                ALARM,
                f"a run started {started_at:%Y-%m-%d %H:%M}Z and never "
                "reported finishing",
                (f"a run started {started_at:%Y-%m-%d %H:%M}Z "
                 f"({_fmt_hours(age)}h ago) and never reported finishing -- "
                 f"killed at the {max_runtime_hours}h execution-time-limit, "
                 "unplugged (the task stops on battery), or the machine "
                 f"slept. A hard kill skips the terminal write. Log: {log_path}"),
                evidence)
        return Health(
            OK, f"a run is in progress (started {started_at:%H:%M}Z)",
            (f"started {started_at:%Y-%m-%d %H:%M}Z, {_fmt_hours(age)}h ago, "
             f"inside the {max_runtime_hours}h limit. Log: {log_path}"),
            evidence)

    # 3 -- it said it failed.
    if state == "failed":
        step = run.get("failed_step") or "an unnamed step"
        return Health(
            ALARM, f"last night's run FAILED at {step}",
            (f"step {step} exited {run.get('exit_code')!r} on "
             f"{run.get('run_date')}. Log: {log_path}"),
            evidence)

    if state != "ok":
        return Health(
            UNKNOWN, f"unrecognised run state {state!r}",
            (f"the heartbeat at {hb_path} carries state={state!r}, which this "
             "version of phoneloop.heartbeat does not know how to read."),
            evidence)

    # 4 -- it said it succeeded. Believe the STORE, not the record.
    if run_date is None:
        return Health(
            UNKNOWN, "a successful run with no readable run_date",
            f"heartbeat at {hb_path} says ok but its run_date is unreadable, "
            "so it cannot be checked against the store.", evidence)
    if run_date not in dates:
        return Health(
            ALARM,
            f"last night reported SUCCESS and the store gained nothing for "
            f"{run_date}",
            (f"the run for {run_date} exited {run.get('exit_code')!r} and the "
             f"store gained nothing: {run.get('data_dir')} has no eBay "
             f"snapshot for {run_date} (it holds {len(dates)} date(s): "
             f"{', '.join(evidence['snapshot_dates']) or 'none'}). Either "
             "PHONELOOP_DATA_DIR pointed elsewhere, the store write failed "
             "after the sweep, or every shard was blocked. Exit 0 is not "
             f"evidence. Log: {log_path}"),
            evidence)

    # 5 -- it succeeded, but a long time ago. Only the clock can see this.
    if finished_at is not None:
        age = now - finished_at
        if age > timedelta(hours=stale_hours):
            return Health(
                ALARM,
                f"no run since {finished_at:%Y-%m-%d %H:%M}Z "
                f"({_fmt_hours(age)}h ago)",
                (f"the last run finished {finished_at:%Y-%m-%d %H:%M}Z, "
                 f"{_fmt_hours(age)}h ago, and no trigger has fired since -- "
                 "the machine was off or on battery at the scheduled time, or "
                 "the task is no longer registered. A skipped night writes "
                 "nothing at all, which is why this is measured against the "
                 "clock rather than a record."),
                evidence)

    # 6 -- it ran, it landed, but something was thin about it.
    blocked = run.get("blocked_shards")
    done = run.get("done_shards")
    today_n = run.get("listings_today")
    prev_n = run.get("listings_prev_date")
    evidence.update({"blocked_shards": blocked, "done_shards": done,
                     "listings_today": today_n, "listings_prev_date": prev_n})

    warnings: list[str] = []
    if isinstance(blocked, int) and blocked > 0:
        warnings.append(
            f"{blocked} shard(s) blocked and not swept ({done} done) -- eBay "
            "refused them; they are left out of 'done' so the next run "
            "retries. The sweep still exits 0 in this case.")
    if (isinstance(today_n, int) and isinstance(prev_n, int)
            and prev_n > 0 and today_n < COLLAPSE_FRACTION * prev_n):
        warnings.append(
            f"{today_n:,} listings swept on {run_date} against {prev_n:,} on "
            "the previous snapshot date -- a collapse that size usually means "
            "the egress was blocked, not that the market shrank.")
    if warnings:
        return Health(
            WARN, f"the run for {run_date} landed, but thinly",
            " ".join(warnings), evidence)

    return Health(
        OK, f"nightly sweep ok for {run_date}",
        (f"finished {run.get('finished_at')}, exit {run.get('exit_code')!r}, "
         f"{today_n if today_n is not None else '?'} listings on {run_date}"
         + (f" against {prev_n:,} previously" if isinstance(prev_n, int) else "")
         + f". Store: {run.get('data_dir')}"),
        evidence)


def health_for(data_dir: Path | str, *, now: datetime | None = None,
               snapshot_dates: Sequence[date] | None = None,
               source: str = "ebay", **kw) -> Health:
    """`assess()` over the files and the store at `data_dir`, and NEVER
    raises.

    This is what the board and the CLI call. A health check that can take
    the board down is worse than none at all -- a corrupt heartbeat, a
    locked parquet file or a duckdb that will not open must degrade to
    UNKNOWN with the reason on screen, not to a traceback where the board
    used to be.
    """
    now = now or datetime.now(UTC)
    data_dir = Path(data_dir)

    run = None
    corrupt: str | None = None
    try:
        run = read(path_for(data_dir))
    except CorruptRecord as exc:
        corrupt = str(exc)
    except OSError as exc:                       # pragma: no cover - defensive
        corrupt = f"{path_for(data_dir)}: {exc}"

    attempt = None
    try:
        attempt = read(attempt_path_for(data_dir))
    except (CorruptRecord, OSError):
        attempt = None

    if corrupt is not None:
        return Health(
            UNKNOWN, "the nightly heartbeat is unreadable",
            (f"corrupt or truncated heartbeat: {corrupt}. The run may have "
             "been fine -- this says only that its record cannot be read. "
             "Check the log, or delete the file and let tonight rewrite it."),
            {"heartbeat_path": str(path_for(data_dir))})

    if snapshot_dates is None:
        try:
            from phoneloop.market_store import MarketStore
            snapshot_dates = MarketStore(data_dir).snapshot_dates(source)
        except Exception as exc:                 # noqa: BLE001 - see docstring
            return Health(
                UNKNOWN, "the market store could not be read",
                (f"{data_dir}: {exc}. Without snapshot_dates the heartbeat "
                 "cannot be checked against the store, and the heartbeat "
                 "alone is not evidence."),
                {"data_dir": str(data_dir)})

    return assess(run, attempt, snapshot_dates, now=now, data_dir=data_dir, **kw)
