"""The nightly orchestrator: day-guard, lock, two-phase heartbeat, exit code.

The subprocess runner is INJECTED, so every branch below is exercised with
no eBay, no Chrome and no hour of runtime. What is NOT re-expressed here is
the night itself: `scripts/run_daily.cmd` stays the only definition of
SWEEP -> DEEPEN -> BOARD, because it is the only artefact that has been run
against live eBay. A second definition in Python would be unverified.

Exit codes are chosen so Task Scheduler's `LastTaskResult` is readable on
its own: 0 ok, 1 a step failed, 75 busy (EX_TEMPFAIL -- deliberately NOT 1,
so a backed-off run does not read as a failure), 2 internal.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import nightly  # noqa: E402

from phoneloop import heartbeat, runlock  # noqa: E402

NOW = datetime(2026, 8, 29, 3, 0, tzinfo=UTC)
TODAY = NOW.date()


class FakeRunner:
    """Stands in for the run_daily.cmd child. Records what it was asked to
    launch and replays a canned exit code and output."""

    def __init__(self, exit_code: int = 0, output: str = ""):
        self.exit_code = exit_code
        self.output = output
        self.calls: list[list[str]] = []

    def __call__(self, command, *, log, env=None, cwd=None):
        self.calls.append(list(command))
        for line in self.output.splitlines():
            log.write(line + "\n")
        return self.exit_code


GREEN_OUTPUT = """[run_daily] Fri 29/08/2026  3:00:01.00 -- [1/3] SWEEP
swept 29,410 unique eBay listings in 58.2 min
[run_daily] Fri 29/08/2026  3:58:20.00 -- [2/3] DEEPEN (Tier 2, item-page enrichment)
[run_daily] Fri 29/08/2026  4:05:00.00 -- [3/3] BOARD (difference runs inside niche_board.py once 2+ snapshot dates exist)
[run_daily] Fri 29/08/2026  4:05:40.00 -- done
"""

RED_OUTPUT = """[run_daily] Fri 29/08/2026  3:00:01.00 -- [1/3] SWEEP
[run_daily] SWEEP failed -- checkpointed, safe to re-run
"""


def _read_run(data_dir) -> dict:
    return heartbeat.read(heartbeat.path_for(data_dir))


def _attempts(data_dir) -> list[dict]:
    rec = heartbeat.read(heartbeat.attempt_path_for(data_dir))
    return rec["attempts"] if rec else []


class TestHappyPath:
    def test_exits_zero_and_writes_an_ok_heartbeat(self, tmp_path):
        runner = FakeRunner(0, GREEN_OUTPUT)
        code = nightly.run_nightly(data_dir=tmp_path, now=NOW, runner=runner)
        assert code == 0
        rec = _read_run(tmp_path)
        assert rec["state"] == "ok"
        assert rec["exit_code"] == 0
        assert rec["run_date"] == TODAY.isoformat()

    def test_run_date_is_utc(self, tmp_path):
        """`snapshot_dates()` is UTC-cast, so a LOCAL run_date would compare
        against the wrong day across BST midnight."""
        late = datetime(2026, 8, 28, 23, 30, tzinfo=UTC)
        nightly.run_nightly(data_dir=tmp_path, now=late, runner=FakeRunner(0))
        assert _read_run(tmp_path)["run_date"] == "2026-08-28"

    def test_launches_run_daily_cmd_as_one_child(self, tmp_path):
        runner = FakeRunner(0, GREEN_OUTPUT)
        nightly.run_nightly(data_dir=tmp_path, now=NOW, runner=runner)
        assert len(runner.calls) == 1
        assert runner.calls[0][-1].lower().endswith("run_daily.cmd")

    def test_the_heartbeat_says_running_before_the_child_starts(self, tmp_path):
        """A hard kill skips every `finally`. The record written BEFORE the
        child launches is the only thing that survives one."""
        seen: list[str] = []

        def runner(command, *, log, env=None, cwd=None):
            seen.append(_read_run(tmp_path)["state"])
            return 0

        nightly.run_nightly(data_dir=tmp_path, now=NOW, runner=runner)
        assert seen == ["running"]

    def test_finished_at_is_the_completion_time_not_the_start_time(self, tmp_path):
        """A finished_at copied from started_at makes every run look
        instantaneous and, worse, makes `assess()`'s staleness rule measure
        the wrong end of the night."""
        ticks = iter([NOW + timedelta(hours=1, minutes=6)])
        nightly.run_nightly(data_dir=tmp_path, now=NOW, runner=FakeRunner(0),
                            clock=lambda: next(ticks))
        rec = _read_run(tmp_path)
        assert rec["started_at"] == NOW.isoformat()
        assert rec["finished_at"] == (NOW + timedelta(hours=1, minutes=6)).isoformat()

    def test_streams_the_child_output_into_a_dated_log(self, tmp_path):
        nightly.run_nightly(data_dir=tmp_path, now=NOW,
                            runner=FakeRunner(0, GREEN_OUTPUT))
        log = Path(_read_run(tmp_path)["log_path"])
        assert log.exists()
        assert "[1/3] SWEEP" in log.read_text(encoding="utf-8")
        assert log.name == "nightly-2026-08-29.log"


class TestFailure:
    def test_a_failed_child_exits_one_and_names_the_step(self, tmp_path):
        code = nightly.run_nightly(data_dir=tmp_path, now=NOW,
                                   runner=FakeRunner(1, RED_OUTPUT))
        assert code == 1
        rec = _read_run(tmp_path)
        assert rec["state"] == "failed"
        assert rec["exit_code"] == 1
        assert rec["failed_step"] == "SWEEP"

    def test_an_exception_still_leaves_a_failed_record(self, tmp_path):
        def runner(command, *, log, env=None, cwd=None):
            raise OSError("chrome is gone")

        code = nightly.run_nightly(data_dir=tmp_path, now=NOW, runner=runner)
        assert code == 2
        rec = _read_run(tmp_path)
        assert rec["state"] == "failed"
        assert "chrome is gone" in json.dumps(rec)

    def test_the_lock_is_released_after_a_failure(self, tmp_path):
        nightly.run_nightly(data_dir=tmp_path, now=NOW, runner=FakeRunner(1))
        assert not runlock.lock_path_for(tmp_path).exists()


class TestDayGuard:
    def test_a_second_run_the_same_utc_day_does_nothing_and_exits_zero(self, tmp_path):
        first = FakeRunner(0, GREEN_OUTPUT)
        nightly.run_nightly(data_dir=tmp_path, now=NOW, runner=first)
        before = _read_run(tmp_path)

        second = FakeRunner(0, GREEN_OUTPUT)
        code = nightly.run_nightly(data_dir=tmp_path, now=NOW + timedelta(hours=6),
                                   runner=second)
        assert code == 0
        assert second.calls == []                      # nothing launched
        assert _read_run(tmp_path) == before           # last_run.json untouched
        assert _attempts(tmp_path)[-1]["outcome"] == "already_done_today"

    def test_force_overrides_the_day_guard(self, tmp_path):
        nightly.run_nightly(data_dir=tmp_path, now=NOW, runner=FakeRunner(0))
        second = FakeRunner(0, GREEN_OUTPUT)
        nightly.run_nightly(data_dir=tmp_path, now=NOW + timedelta(hours=6),
                            runner=second, force=True)
        assert len(second.calls) == 1

    def test_a_failed_night_is_retried_not_guarded_out(self, tmp_path):
        """The guard keys on state=='ok'. A failed run must be re-runnable
        the same day -- that is the whole point of the logon trigger."""
        nightly.run_nightly(data_dir=tmp_path, now=NOW, runner=FakeRunner(1))
        second = FakeRunner(0, GREEN_OUTPUT)
        nightly.run_nightly(data_dir=tmp_path, now=NOW + timedelta(hours=2),
                            runner=second)
        assert len(second.calls) == 1

    def test_a_new_utc_day_is_not_guarded_out(self, tmp_path):
        nightly.run_nightly(data_dir=tmp_path, now=NOW, runner=FakeRunner(0))
        second = FakeRunner(0, GREEN_OUTPUT)
        nightly.run_nightly(data_dir=tmp_path, now=NOW + timedelta(days=1),
                            runner=second)
        assert len(second.calls) == 1


class TestBusy:
    def test_a_held_lock_exits_75_and_leaves_the_record_alone(self, tmp_path):
        heartbeat.write(heartbeat.path_for(tmp_path),
                        heartbeat.new_record(state="running", run_date=TODAY,
                                             started_at=NOW, data_dir=tmp_path,
                                             log_path=None, harness_mode="live"))
        before = _read_run(tmp_path)
        with runlock.hold(tmp_path, now=NOW):
            runner = FakeRunner(0)
            code = nightly.run_nightly(data_dir=tmp_path,
                                       now=NOW + timedelta(minutes=5),
                                       runner=runner)
        assert code == 75
        assert runner.calls == []
        assert _read_run(tmp_path) == before
        assert _attempts(tmp_path)[-1]["outcome"] == "busy"

    def test_75_is_not_1_so_a_backoff_does_not_read_as_a_failure(self):
        assert nightly.EXIT_BUSY == 75
        assert nightly.EXIT_BUSY != nightly.EXIT_STEP_FAILED


class TestStepParsing:
    def test_names_the_step_that_failed(self):
        steps, failed = nightly.parse_steps(RED_OUTPUT.splitlines())
        assert failed == "SWEEP"
        assert [s["name"] for s in steps] == ["SWEEP"]

    def test_a_green_night_names_all_three_and_no_failure(self):
        steps, failed = nightly.parse_steps(GREEN_OUTPUT.splitlines())
        assert [s["name"] for s in steps] == ["SWEEP", "DEEPEN", "BOARD"]
        assert failed is None

    def test_unrecognisable_output_yields_no_false_step(self):
        steps, failed = nightly.parse_steps(["something else entirely"])
        assert steps == []
        assert failed is None


class TestEvidenceGathering:
    def test_records_listing_counts_from_the_store(self, tmp_path):
        from phoneloop.market_store import MarketStore, empty_row

        store = MarketStore(tmp_path)
        prev = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)

        def _row(sid, seen):
            r = empty_row("ebay", sid)
            r.update({"seen_at": seen, "leaf_category_id": "9355",
                      "total_pence": 4000, "sold_qty": 3})
            return r

        store.record([_row("a", prev), _row("b", prev), _row("c", prev)])
        store.record([_row("a", NOW)])

        nightly.run_nightly(data_dir=tmp_path, now=NOW, runner=FakeRunner(0))
        rec = _read_run(tmp_path)
        assert rec["listings_prev_date"] == 3
        assert rec["listings_today"] == 1

    def test_records_blocked_and_done_shards_from_the_checkpoint(self, tmp_path):
        cp = tmp_path / f"bulk_ebay_checkpoint_{TODAY:%Y-%m-%d}.json"
        cp.write_text(json.dumps({
            "done": {"a": 1, "b": 2, "c": 3},
            "blocked": {"d": "403"},
        }), encoding="utf-8")
        nightly.run_nightly(data_dir=tmp_path, now=NOW, runner=FakeRunner(0))
        rec = _read_run(tmp_path)
        assert rec["done_shards"] == 3
        assert rec["blocked_shards"] == 1

    def test_a_missing_checkpoint_is_none_not_zero(self, tmp_path):
        """Reporting "0 blocked" for a night whose checkpoint was never
        written would be an unknown printed as a number."""
        nightly.run_nightly(data_dir=tmp_path, now=NOW, runner=FakeRunner(0))
        rec = _read_run(tmp_path)
        assert rec["blocked_shards"] is None
        assert rec["done_shards"] is None

    def test_checkpoint_path_comes_from_bulk_ebay_not_a_copy(self):
        """One definition of the filename format. A private copy here would
        drift the day the sweep's changes."""
        import bulk_ebay
        assert (nightly.checkpoint_path_for(Path("C:/store"), date(2026, 8, 29))
                == bulk_ebay.default_checkpoint_path(Path("C:/store"),
                                                     date(2026, 8, 29)))


class TestTheRealRunner:
    """`_subprocess_runner` is the ONE part of nightly.py the injected
    FakeRunner never exercises, and it is the part that runs unattended at
    03:00. These launch a real child -- a trivial one, not the sweep."""

    def test_captures_a_real_child_exit_code_and_output(self, tmp_path):
        log = tmp_path / "out.log"
        with log.open("w", encoding="utf-8") as fh:
            rc = nightly._subprocess_runner(
                [sys.executable, "-c", "print('hello from the child')"], log=fh)
        assert rc == 0
        assert "hello from the child" in log.read_text(encoding="utf-8")

    def test_a_nonzero_child_propagates(self, tmp_path):
        log = tmp_path / "out.log"
        with log.open("w", encoding="utf-8") as fh:
            rc = nightly._subprocess_runner(
                [sys.executable, "-c", "raise SystemExit(7)"], log=fh)
        assert rc == 7

    def test_stderr_lands_in_the_log_too(self, tmp_path):
        """A traceback that only reached stderr would be invisible at 03:00."""
        log = tmp_path / "out.log"
        with log.open("w", encoding="utf-8") as fh:
            nightly._subprocess_runner(
                [sys.executable, "-c",
                 "import sys; print('to stderr', file=sys.stderr)"], log=fh)
        assert "to stderr" in log.read_text(encoding="utf-8")

    def test_end_to_end_through_main_with_a_stand_in_command(self, tmp_path):
        """The whole path -- argparse, config, lock, heartbeat, real Popen --
        against a child that is unmistakably NOT the sweep."""
        rc = nightly.main([
            "--data-dir", str(tmp_path),
            f"--command={sys.executable}", "--command=-c",
            "--command=print('[run_daily] x -- [1/3] SWEEP')",
        ])
        assert rc == 0
        rec = _read_run(tmp_path)
        assert rec["state"] == "ok"
        assert rec["steps"][0]["name"] == "SWEEP"


class TestLauncherDiscipline:
    def test_default_command_is_the_pinned_cmd_not_a_bare_python(self):
        """nightly.py launches run_daily.cmd, which pins the interpreter
        itself. Nothing here may reach for `python` on PATH -- that resolves
        to the MSIX stub, which exits silently with no console."""
        cmd = nightly.default_command()
        assert [c for c in cmd if c.lower() in ("python", "py", "python.exe")] == []
        assert cmd[-1].lower().endswith("run_daily.cmd")

    def test_any_python_it_ever_launches_is_sys_executable(self):
        """Structural, not conventional: the .cmd launched nightly.py with
        the pinned interpreter, so sys.executable IS the installer python by
        construction. A literal 'python' in this module would break that."""
        src = Path(nightly.__file__).read_text(encoding="utf-8")
        code = src.replace(nightly.__doc__ or "", "", 1)
        assert '"python"' not in code
        assert "'python'" not in code


class TestHealthAfterwards:
    def test_a_green_run_over_an_empty_store_still_reads_as_alarm(self, tmp_path):
        """The whole point. The runner exited 0, the heartbeat says ok, and
        the store gained nothing -- that must NOT read as a good night."""
        nightly.run_nightly(data_dir=tmp_path, now=NOW,
                            runner=FakeRunner(0, GREEN_OUTPUT))
        h = heartbeat.health_for(tmp_path, now=NOW + timedelta(hours=1))
        assert h.level == heartbeat.ALARM
        assert "gained nothing" in h.detail
