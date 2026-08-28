"""The heartbeat EXPLAINS a night; `store.snapshot_dates()` is the EVIDENCE
for it.

Health is `f(heartbeat, snapshot_dates, now)` and never the heartbeat alone,
because a heartbeat can say "ok" while the store gained nothing -- which is
exactly this project's defining failure mode (a run that exits 0 and
produces no data reads identical to a successful one). The single most
important test in this file is
`test_success_without_a_snapshot_for_today_is_alarm`; if that one ever goes
green for the wrong reason, the whole scheduling deliverable is decoration.

Assessment is computed at READ time from `now`, not baked in at write time,
because the most likely failure -- the machine being off at 03:00 -- writes
nothing at all, and only staleness can catch a night that left no record.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from phoneloop import heartbeat

NOW = datetime(2026, 8, 29, 8, 0, tzinfo=UTC)
TODAY = NOW.date()
YESTERDAY = TODAY - timedelta(days=1)


def _ok_record(**over) -> dict:
    rec = {
        "schema": heartbeat.SCHEMA,
        "state": "ok",
        "run_date": TODAY.isoformat(),
        "started_at": (NOW - timedelta(hours=6)).isoformat(),
        "finished_at": (NOW - timedelta(hours=5)).isoformat(),
        "exit_code": 0,
        "failed_step": None,
        "steps": [],
        "data_dir": "C:/store",
        "log_path": "C:/store/logs/nightly-2026-08-29.log",
        "harness_mode": "live",
        "host": "testbox",
        "pid": 4242,
        "listings_today": 29000,
        "listings_prev_date": 29602,
        "blocked_shards": 0,
        "done_shards": 43,
    }
    rec.update(over)
    return rec


class TestPaths:
    def test_path_for_is_under_data_dir_health(self, tmp_path):
        assert heartbeat.path_for(tmp_path) == tmp_path / "health" / "last_run.json"

    def test_attempt_path_is_beside_it(self, tmp_path):
        assert (heartbeat.attempt_path_for(tmp_path)
                == tmp_path / "health" / "last_attempt.json")


class TestWriteRead:
    def test_roundtrips(self, tmp_path):
        path = heartbeat.path_for(tmp_path)
        heartbeat.write(path, _ok_record())
        assert heartbeat.read(path) == _ok_record()

    def test_write_leaves_no_tmp_file_behind(self, tmp_path):
        path = heartbeat.path_for(tmp_path)
        heartbeat.write(path, _ok_record())
        leftovers = [p.name for p in path.parent.iterdir() if p.name != path.name]
        assert leftovers == []

    def test_read_missing_is_none(self, tmp_path):
        assert heartbeat.read(heartbeat.path_for(tmp_path)) is None

    def test_read_corrupt_raises_rather_than_returning_a_lie(self, tmp_path):
        path = heartbeat.path_for(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"state": "ok"', encoding="utf-8")   # truncated
        with pytest.raises(heartbeat.CorruptRecord):
            heartbeat.read(path)


class TestAssess:
    def test_no_heartbeat_and_no_snapshot_is_alarm_naming_the_path(self, tmp_path):
        h = heartbeat.assess(None, None, [], now=NOW, data_dir=tmp_path)
        assert h.level == heartbeat.ALARM
        assert str(heartbeat.path_for(tmp_path)) in h.detail

    def test_no_heartbeat_but_a_snapshot_exists_is_unknown_not_alarm(self, tmp_path):
        """Today's real state: e2e_clean holds 2026-08-28 and no run has ever
        written a heartbeat. That is not an alarm -- it is 'not wired up yet'."""
        h = heartbeat.assess(None, None, [TODAY], now=NOW, data_dir=tmp_path)
        assert h.level == heartbeat.UNKNOWN

    def test_stuck_running_older_than_max_runtime_is_alarm(self, tmp_path):
        rec = _ok_record(state="running",
                         started_at=(NOW - timedelta(hours=5)).isoformat(),
                         finished_at=None, exit_code=None)
        h = heartbeat.assess(rec, None, [TODAY], now=NOW, data_dir=tmp_path,
                             max_runtime_hours=3)
        assert h.level == heartbeat.ALARM
        assert "never reported finishing" in h.detail

    def test_running_within_the_limit_is_not_an_alarm(self, tmp_path):
        rec = _ok_record(state="running",
                         started_at=(NOW - timedelta(minutes=20)).isoformat(),
                         finished_at=None, exit_code=None)
        h = heartbeat.assess(rec, None, [TODAY], now=NOW, data_dir=tmp_path,
                             max_runtime_hours=3)
        assert h.level != heartbeat.ALARM
        assert "in progress" in h.headline

    def test_failed_state_is_alarm_naming_step_exit_code_and_log(self, tmp_path):
        rec = _ok_record(state="failed", exit_code=1, failed_step="SWEEP")
        h = heartbeat.assess(rec, None, [TODAY], now=NOW, data_dir=tmp_path)
        assert h.level == heartbeat.ALARM
        assert "SWEEP" in h.detail
        assert "nightly-2026-08-29.log" in h.detail

    def test_success_without_a_snapshot_for_today_is_alarm(self, tmp_path):
        """THE rule. A run that reports success and leaves the store empty is
        the failure this whole module exists to catch: wrong DATA_DIR, a
        store write that failed after the sweep, or a sweep eBay blocked
        entirely. Every one of those exits 0."""
        h = heartbeat.assess(_ok_record(), None, [], now=NOW, data_dir=tmp_path)
        assert h.level == heartbeat.ALARM
        assert "gained nothing" in h.detail
        assert TODAY.isoformat() in h.detail

    def test_success_with_a_snapshot_for_that_date_is_ok(self, tmp_path):
        h = heartbeat.assess(_ok_record(), None, [YESTERDAY, TODAY], now=NOW,
                             data_dir=tmp_path)
        assert h.level == heartbeat.OK

    def test_stale_finished_at_is_alarm_naming_the_age(self, tmp_path):
        old = NOW - timedelta(hours=30)
        rec = _ok_record(run_date=old.date().isoformat(),
                         started_at=(old - timedelta(hours=1)).isoformat(),
                         finished_at=old.isoformat())
        h = heartbeat.assess(rec, None, [old.date()], now=NOW, data_dir=tmp_path)
        assert h.level == heartbeat.ALARM
        assert "30" in h.detail          # hours ago, stated as a number
        assert "trigger" in h.detail.lower()

    def test_blocked_shards_warn_carries_the_raw_count(self, tmp_path):
        rec = _ok_record(blocked_shards=7, done_shards=36)
        h = heartbeat.assess(rec, None, [TODAY], now=NOW, data_dir=tmp_path)
        assert h.level == heartbeat.WARN
        assert "7" in h.detail and "36" in h.detail

    def test_listing_collapse_warns_with_both_raw_numbers(self, tmp_path):
        rec = _ok_record(listings_today=400, listings_prev_date=29602)
        h = heartbeat.assess(rec, None, [TODAY], now=NOW, data_dir=tmp_path)
        assert h.level == heartbeat.WARN
        assert "400" in h.detail and "29,602" in h.detail

    def test_a_mild_drop_is_not_a_warning(self, tmp_path):
        rec = _ok_record(listings_today=28000, listings_prev_date=29602)
        h = heartbeat.assess(rec, None, [TODAY], now=NOW, data_dir=tmp_path)
        assert h.level == heartbeat.OK

    def test_unknown_state_string_is_unknown_not_ok(self, tmp_path):
        rec = _ok_record(state="banana")
        h = heartbeat.assess(rec, None, [TODAY], now=NOW, data_dir=tmp_path)
        assert h.level == heartbeat.UNKNOWN


class TestExpectedDate:
    def test_before_the_run_hour_the_expected_date_is_yesterday(self):
        early = datetime(2026, 8, 29, 1, 0, tzinfo=UTC)
        assert heartbeat.expected_run_date(early, run_hour_utc=2) == date(2026, 8, 28)

    def test_after_the_run_hour_plus_grace_it_is_today(self):
        late = datetime(2026, 8, 29, 4, 0, tzinfo=UTC)
        assert heartbeat.expected_run_date(late, run_hour_utc=2) == date(2026, 8, 29)

    def test_uses_utc_not_local_midnight(self):
        """A LOCAL date silently misfires across BST midnight: 00:30 BST on
        the 29th is 23:30 UTC on the 28th, and snapshot_dates() is UTC."""
        just_before_utc_midnight = datetime(2026, 8, 28, 23, 30, tzinfo=UTC)
        assert heartbeat.expected_run_date(
            just_before_utc_midnight, run_hour_utc=2) == date(2026, 8, 28)


class TestHealthFor:
    def test_corrupt_heartbeat_yields_unknown_and_never_raises(self, tmp_path):
        """A health check that can take the board down is worse than none."""
        path = heartbeat.path_for(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json at all", encoding="utf-8")
        h = heartbeat.health_for(tmp_path, now=NOW)
        assert h.level == heartbeat.UNKNOWN
        assert "unreadable" in h.detail.lower() or "corrupt" in h.detail.lower()

    def test_reads_the_real_record_and_the_real_store(self, tmp_path):
        heartbeat.write(heartbeat.path_for(tmp_path), _ok_record())
        h = heartbeat.health_for(tmp_path, now=NOW)
        # Empty store, heartbeat says ok -> the hollow-success alarm.
        assert h.level == heartbeat.ALARM

    def test_attempt_record_is_appended_not_overwritten(self, tmp_path):
        p = heartbeat.attempt_path_for(tmp_path)
        heartbeat.append_attempt(p, {"outcome": "already_done_today"}, keep=3)
        heartbeat.append_attempt(p, {"outcome": "busy"}, keep=3)
        entries = json.loads(p.read_text(encoding="utf-8"))["attempts"]
        assert [e["outcome"] for e in entries] == ["already_done_today", "busy"]
        assert all("at" in e for e in entries)
