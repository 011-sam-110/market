"""One nightly run at a time.

**What this lock actually protects, so nobody later removes it for the wrong
reason.** It does NOT protect parquet integrity. `MarketStore.record` is
append-only, gives every call its own part filename, and `rows_on()` dedupes
to the newest row per `source_id`, so two overlapping runs cannot corrupt
the store -- they would just write the same observations twice and the
reader would collapse them. What an overlap DOES cost is real:

* **Duplicated eBay egress.** Two headed Chrome instances sweeping from one
  residential IP is a block risk, and a block does not raise -- the sweep
  checkpoints the shard as "blocked" and exits 0.
* **The shared dated checkpoint JSON.** Both runs read, mutate and rewrite
  `bulk_ebay_checkpoint_<date>.json`; last writer wins and one run's
  completed shards vanish from `done`.

**Staleness is TIME-BASED ONLY. Never `os.kill(pid, 0)`.** On Windows,
CPython's `os.kill` calls `OpenProcess` + `TerminateProcess` with the signal
number as the exit code for any non-CTRL signal -- so the idiomatic POSIX
liveness probe `os.kill(pid, 0)` TERMINATES the process it is asking about,
and does it with exit code 0. A "hardening" that silently kills the running
sweep and makes it look like a clean exit is exactly the failure mode this
repo keeps hitting, so the guard is a test (`tests/test_runlock.py::
TestNoOsKill`) and not a comment.

`LOCK_STALE_AFTER` is deliberately ABOVE the scheduled task's 3h
`ExecutionTimeLimit`: past it, a lock is definitionally abandoned rather
than possibly still working. Breaking one is logged with the broken lock's
age and pid, never done silently.
"""

from __future__ import annotations

import json
import os
import socket
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

_HEALTH_DIR = "health"
_LOCK_FILE = "nightly.lock"

# Above the task's 3h ExecutionTimeLimit -- see the module docstring.
LOCK_STALE_AFTER = timedelta(hours=4)


class Busy(Exception):
    """Another run holds the lock and it is not yet stale."""

    def __init__(self, path: Path, pid: object, started_at: str | None,
                 age: timedelta | None):
        self.path = path
        self.pid = pid
        self.started_at = started_at
        self.age = age
        age_txt = f"{age.total_seconds() / 60:.0f} min" if age else "unknown age"
        super().__init__(
            f"nightly run already in progress: {path} held by pid {pid} "
            f"since {started_at} ({age_txt})")


def lock_path_for(data_dir: Path | str) -> Path:
    return Path(data_dir) / _HEALTH_DIR / _LOCK_FILE


def _read_lock(path: Path) -> dict:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            return loaded
    except (OSError, ValueError):
        pass
    return {}


def _held_since(path: Path, held: dict) -> datetime | None:
    """When the existing lock was taken.

    Falls back to the file's mtime when the JSON is unreadable -- an
    unparseable lock must not wedge the schedule forever, and its mtime is
    still an honest lower bound on its age.
    """
    raw = held.get("started_at")
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except OSError:                              # pragma: no cover - defensive
        return None


@contextmanager
def hold(data_dir: Path | str, *, now: datetime | None = None,
         stale_after: timedelta = LOCK_STALE_AFTER,
         on_break: Callable[[dict], None] | None = None):
    """Hold the nightly lock for the duration of the block.

    Raises `Busy` when a live lock is held. A lock older than `stale_after`
    is broken -- reported through `on_break` with its pid and age first, so
    a broken lock always leaves a trace.

    `now` is injected so staleness is testable without sleeping, which is
    the whole reason the expiry is time-based rather than a liveness probe.
    """
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    path = lock_path_for(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "pid": os.getpid(),
        "started_at": now.isoformat(),
        "host": socket.gethostname(),
    }
    raw = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")

    for attempt in (0, 1):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if attempt:                          # pragma: no cover - a racing
                break                            # third process took it back
            held = _read_lock(path)
            since = _held_since(path, held)
            age = (now - since) if since else None
            if age is None or age <= stale_after:
                raise Busy(path, held.get("pid"), held.get("started_at"), age) from None
            broken = {
                "path": str(path),
                "pid": held.get("pid"),
                "started_at": held.get("started_at"),
                "age_hours": age.total_seconds() / 3600.0,
                "stale_after_hours": stale_after.total_seconds() / 3600.0,
            }
            if on_break is not None:
                on_break(broken)
            try:
                path.unlink()
            except OSError:                      # pragma: no cover - defensive
                raise Busy(path, held.get("pid"), held.get("started_at"),
                           age) from None
            continue
        else:
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
            break
    else:                                        # pragma: no cover
        raise Busy(path, None, None, None)

    try:
        yield path
    finally:
        # Only release a lock that is still OURS. If something broke and
        # re-took it while we ran, deleting it would hand a third run a free
        # pass into an already-running sweep.
        try:
            if path.exists() and _read_lock(path).get("pid") == payload["pid"]:
                path.unlink()
        except OSError:                          # pragma: no cover - defensive
            pass
