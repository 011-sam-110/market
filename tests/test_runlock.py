"""One nightly run at a time, with time-based staleness only.

The lock does NOT protect parquet integrity -- `MarketStore.record` is
append-only with per-call unique part filenames and `rows_on()` dedupes
newest-per-source_id, so an overlap cannot corrupt the store. It protects
DUPLICATED eBay EGRESS (two headed Chromes from one IP is a block risk) and
the shared dated checkpoint JSON.

The load-bearing negative test is `test_never_uses_os_kill_for_liveness`. On
Windows, CPython's `os.kill` calls OpenProcess + TerminateProcess with the
signal number as the exit code for any non-CTRL signal, so the idiomatic
POSIX probe `os.kill(pid, 0)` KILLS the process it is asking about, and does
it with exit code 0 -- a silent success of exactly the kind this repo keeps
hitting. Staleness here is time-based, with `now` injected so it is testable.
"""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime, timedelta

import pytest

from phoneloop import runlock

NOW = datetime(2026, 8, 29, 3, 0, tzinfo=UTC)


class TestLockPath:
    def test_lock_lives_under_data_dir_health(self, tmp_path):
        assert (runlock.lock_path_for(tmp_path)
                == tmp_path / "health" / "nightly.lock")


class TestHold:
    def test_creates_the_lock_and_removes_it_on_exit(self, tmp_path):
        path = runlock.lock_path_for(tmp_path)
        with runlock.hold(tmp_path, now=NOW):
            assert path.exists()
            held = json.loads(path.read_text(encoding="utf-8"))
            assert held["pid"] > 0
            assert held["started_at"] == NOW.isoformat()
        assert not path.exists()

    def test_releases_on_exception(self, tmp_path):
        path = runlock.lock_path_for(tmp_path)
        with pytest.raises(ZeroDivisionError):
            with runlock.hold(tmp_path, now=NOW):
                raise ZeroDivisionError
        assert not path.exists()

    def test_second_holder_raises_busy_with_the_first_pid(self, tmp_path):
        with runlock.hold(tmp_path, now=NOW):
            with pytest.raises(runlock.Busy) as exc:
                with runlock.hold(tmp_path, now=NOW + timedelta(minutes=1)):
                    pass
        assert exc.value.pid is not None

    def test_a_lock_older_than_stale_after_is_broken_not_obeyed(self, tmp_path):
        path = runlock.lock_path_for(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "pid": 999999,
            "started_at": (NOW - timedelta(hours=5)).isoformat(),
            "host": "testbox",
        }), encoding="utf-8")

        broken: list[dict] = []
        with runlock.hold(tmp_path, now=NOW, on_break=broken.append):
            assert path.exists()
        assert len(broken) == 1
        # It must say WHOSE lock it broke and how old, never break silently.
        assert broken[0]["pid"] == 999999
        assert broken[0]["age_hours"] >= 4

    def test_stale_after_is_above_the_task_execution_time_limit(self):
        """4h > the 3h ExecutionTimeLimit, so 'older than the limit' means
        'definitionally abandoned' rather than 'possibly still working'."""
        assert runlock.LOCK_STALE_AFTER > timedelta(hours=3)

    def test_a_fresh_lock_is_obeyed_not_broken(self, tmp_path):
        path = runlock.lock_path_for(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "pid": 999999,
            "started_at": (NOW - timedelta(minutes=30)).isoformat(),
            "host": "testbox",
        }), encoding="utf-8")
        with pytest.raises(runlock.Busy):
            with runlock.hold(tmp_path, now=NOW):
                pass

    def test_a_corrupt_lock_falls_back_to_mtime_and_is_breakable(self, tmp_path):
        """An unparseable lock must not wedge the schedule forever."""
        path = runlock.lock_path_for(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        import os
        old = (NOW - timedelta(hours=9)).timestamp()
        os.utime(path, (old, old))
        with runlock.hold(tmp_path, now=NOW):
            assert path.exists()

    def test_does_not_delete_a_lock_it_no_longer_owns(self, tmp_path):
        """If something else broke and re-took our lock while we ran, the
        release must leave the new owner's lock alone."""
        path = runlock.lock_path_for(tmp_path)
        with runlock.hold(tmp_path, now=NOW):
            path.write_text(json.dumps({
                "pid": 123456, "started_at": NOW.isoformat(), "host": "other",
            }), encoding="utf-8")
        assert path.exists()
        assert json.loads(path.read_text(encoding="utf-8"))["pid"] == 123456


class TestNoOsKill:
    def test_never_uses_os_kill_for_liveness(self):
        """os.kill(pid, 0) on Windows TERMINATES the target with exit code 0.
        This is the exact 'hardening' a future reader would reach for.

        The docstring is deliberately excluded from the scan -- it is
        REQUIRED to name os.kill (see the test below), and a guard that
        forbade the warning as well as the mistake would delete its own
        explanation."""
        src = inspect.getsource(runlock)
        code = src.replace(runlock.__doc__ or "", "", 1)
        assert "os.kill" not in code
        assert "signal.SIGTERM" not in code
        assert "import signal" not in code

    def test_module_docstring_says_why_the_lock_exists(self):
        """So nobody later 'simplifies' it away for the wrong reason."""
        doc = runlock.__doc__ or ""
        assert "egress" in doc.lower()
        assert "os.kill" in doc
