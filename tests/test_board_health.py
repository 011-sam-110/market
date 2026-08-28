"""An alarm has to land where Sam already looks.

A heartbeat nobody reads is worth as much as no heartbeat, so the health
assessment is rendered on the board itself -- the artefact the pipeline
produces every night anyway -- rather than only in a file somebody would
have to remember to open.

Two constraints pull against each other here and both are tested:

* `write_report` must ALWAYS carry a health, so the real report cannot be
  published without one. That is why it assesses from its own `data_dir`
  rather than taking health as an argument the caller could forget.
* A health check must never be able to take the board down. A corrupt
  heartbeat renders an UNKNOWN banner; it does not raise.

The self-contained guard is repeated here deliberately. tests/test_board.py
:216-222 is the only place in the repo it exists, and the banner is new
markup carrying free text (paths, step names, store directories) straight
into the HTML -- exactly the kind of thing that leaks a URL. The store's
`url` column is 100% populated with `https://www.ebay.co.uk/itm/...`, so
this is one careless row-dump away from breaking.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime, timedelta

import pytest

from phoneloop import board, heartbeat
from phoneloop.knobs import Knobs
from phoneloop.market_store import MarketStore, empty_row

SEEN = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)
NOW = datetime(2026, 8, 29, 8, 0, tzinfo=UTC)
LEAF = "9355"


def _row(sid: str, seller: str) -> dict:
    row = empty_row("ebay", sid)
    row.update({
        "seen_at": SEEN, "category": "garden", "shard": f"leaf{LEAF}",
        "title": f"Widget {sid}", "price_pence": 3600, "total_pence": 4000,
        "condition": "New", "seller_id": seller, "seller_type": "business",
        "seller_feedback_bp": 9900, "seller_feedback_n": 500, "eta_days": 3,
        "leaf_category_id": LEAF, "sold_qty": 12,
    })
    return row


@pytest.fixture
def knobs():
    return Knobs()


@pytest.fixture
def store(tmp_path):
    s = MarketStore(tmp_path)
    s.record([_row("a", "sellerA"), _row("b", "sellerB")])
    return s


@pytest.fixture
def rows(store, knobs):
    return board.rank_niches(store, knobs)


ALARM = heartbeat.Health(
    heartbeat.ALARM,
    "last night's run FAILED at SWEEP",
    "step SWEEP exited 1 on 2026-08-29. Log: C:/store/logs/nightly.log",
)
OK = heartbeat.Health(heartbeat.OK, "nightly sweep ok for 2026-08-28",
                      "29,602 listings")


class TestBanner:
    def test_an_alarm_banner_appears_in_the_html(self, rows):
        out = board.render_html(rows, title="t", generated_at=NOW, health=ALARM)
        assert "health-alarm" in out
        # Escaped, not raw -- "night's" carries an apostrophe.
        assert html.escape(ALARM.headline) in out
        assert "step SWEEP exited 1" in out

    def test_the_banner_sits_above_the_table(self, rows):
        out = board.render_html(rows, title="t", generated_at=NOW, health=ALARM)
        assert out.index("health-alarm") < out.index("<table>")

    def test_health_none_renders_no_banner_at_all(self, rows):
        """Keeps the three existing TestRenderHtml/TestWriteReport tests
        green unchanged."""
        out = board.render_html(rows, title="t", generated_at=NOW)
        assert "class=\"health" not in out

    def test_each_level_gets_its_own_class(self, rows):
        for level, cls in [(heartbeat.OK, "health-ok"),
                           (heartbeat.WARN, "health-warn"),
                           (heartbeat.ALARM, "health-alarm"),
                           (heartbeat.UNKNOWN, "health-unknown")]:
            h = heartbeat.Health(level, f"{level} headline", "detail")
            out = board.render_html(rows, title="t", generated_at=NOW, health=h)
            assert cls in out

    def test_the_banner_is_escaped(self, rows):
        h = heartbeat.Health(heartbeat.ALARM, "<script>alert(1)</script>", "&")
        out = board.render_html(rows, title="t", generated_at=NOW, health=h)
        assert "<script>alert(1)</script>" not in out
        assert "&lt;script&gt;" in out

    def test_the_style_block_carries_the_banner_classes(self):
        for cls in ("health-ok", "health-warn", "health-alarm", "health-unknown"):
            assert f".{cls}" in board._STYLE


class TestStillSelfContained:
    """The same three assertions as tests/test_board.py:216-222, repeated
    with a banner present. Never emit a row's `url`; print bare source_id."""

    def test_no_external_resources_with_an_alarm_banner(self, rows):
        out = board.render_html(rows, title="t", generated_at=NOW, health=ALARM)
        assert "http://" not in out
        assert "https://" not in out
        assert "<style>" in out

    def test_a_health_detail_holding_a_url_would_be_caught(self, rows):
        """Proves the guard above can actually fire -- otherwise it is
        decoration. A detail carrying a link must be visible to the test."""
        h = heartbeat.Health(heartbeat.ALARM, "x", "see https://example.com")
        out = board.render_html(rows, title="t", generated_at=NOW, health=h)
        assert "https://" in out          # i.e. the guard is not vacuous


class TestWriteReportAlwaysAssesses:
    def test_the_report_carries_a_health_banner_without_being_asked(
            self, rows, tmp_path):
        """`write_report` already receives `data_dir`, so it has everything
        needed. Making it assess itself means the real report can never be
        published without one."""
        path = board.write_report(rows, tmp_path)
        out = path.read_text(encoding="utf-8")
        assert "class=\"health" in out

    def test_it_surfaces_a_failed_run_read_from_the_data_dir(self, rows, tmp_path):
        heartbeat.write(heartbeat.path_for(tmp_path), heartbeat.new_record(
            state="failed", run_date=NOW.date(), started_at=NOW,
            data_dir=tmp_path, log_path=tmp_path / "logs" / "n.log",
            harness_mode="live", exit_code=1, failed_step="DEEPEN"))
        out = board.write_report(rows, tmp_path).read_text(encoding="utf-8")
        assert "health-alarm" in out
        assert "DEEPEN" in out

    def test_a_corrupt_heartbeat_yields_unknown_and_the_board_still_renders(
            self, rows, tmp_path):
        """A health check that can take the board down is worse than none."""
        p = heartbeat.path_for(tmp_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"state": "ok"', encoding="utf-8")
        out = board.write_report(rows, tmp_path).read_text(encoding="utf-8")
        assert "health-unknown" in out
        assert "<table>" in out              # the board itself survived

    def test_an_explicit_health_still_wins(self, rows, tmp_path):
        out = board.write_report(rows, tmp_path, health=ALARM).read_text(
            encoding="utf-8")
        assert html.escape(ALARM.headline) in out


class TestTerminalBanner:
    def test_render_health_text_names_level_headline_and_detail(self):
        text = board.render_health_text(ALARM)
        assert "ALARM" in text
        assert ALARM.headline in text
        assert "step SWEEP exited 1" in text

    def test_an_ok_health_is_one_quiet_line_not_a_wall(self):
        text = board.render_health_text(OK)
        assert "OK" in text
        assert len(text.splitlines()) <= 2

    def test_none_renders_nothing(self):
        assert board.render_health_text(None) == ""


class TestSweepStatusCli:
    def test_it_exits_non_zero_on_alarm(self, tmp_path):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import sweep_status

        heartbeat.write(heartbeat.path_for(tmp_path), heartbeat.new_record(
            state="failed", run_date=NOW.date(), started_at=NOW,
            data_dir=tmp_path, log_path=None, harness_mode="live",
            exit_code=1, failed_step="SWEEP"))
        assert sweep_status.main(["--data-dir", str(tmp_path)]) != 0

    def test_it_exits_zero_when_the_night_landed(self, tmp_path, store):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import sweep_status

        # `store` wrote rows dated 2026-08-28 into tmp_path.
        heartbeat.write(heartbeat.path_for(tmp_path), heartbeat.new_record(
            state="ok", run_date=SEEN.date(), started_at=SEEN,
            finished_at=(SEEN + timedelta(hours=1)).isoformat(),
            data_dir=tmp_path, log_path=None, harness_mode="live", exit_code=0))
        assert sweep_status.main(
            ["--data-dir", str(tmp_path),
             "--now", (SEEN + timedelta(hours=2)).isoformat()]) == 0
