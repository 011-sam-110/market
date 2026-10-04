"""Reddit text leaves the ledger and the export after 7 days. News stays."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from forecast import __main__ as cli
from forecast import agent, gamma, model, retention
from forecast.export import build_payload
from forecast.ledger import Ledger, iso
from phoneloop.dashexport.jsonio import dumps
from tests.forecast_fixtures import NOW, raw_market

# As long as a real Prospector quote can be (600 characters). A short quote
# is overwritten in place and would not show whether the old bytes stay.
QUOTE = "QUOTE-MARKER-only-reddit-said-this " + "x" * 560
TITLE = "TITLE-MARKER-a-reddit-thread"


def _reddit(rid="R1", *, created: datetime, title=TITLE, quote=QUOTE, used=True):
    return {"kind": "reddit", "id": rid, "url": f"https://www.reddit.com/r/x/comments/{rid}/",
            "subreddit": "x", "title": title, "quote": quote, "score": 0.81,
            "created_utc": int(created.timestamp()), "used": used}


def _news():
    return {"kind": "news", "id": "N1", "url": "https://news.example/1", "outlet": "bbc",
            "title": "A headline that stays", "published": "2026-09-20T10:00:00Z",
            "matched": 2, "used": False}


def _row(mid, stored_at: datetime, evidence: list) -> dict:
    return {"market_id": mid, "question": f"q{mid}", "outcome_a": "Yes", "outcome_b": "No",
            "market_price": 0.5, "price_read_at": iso(stored_at), "probability": 0.6,
            "reddit_status": "reddit: 1 hits", "news_status": "news: 1 headlines",
            "side": "none", "kelly_full": 0.0, "stake_fraction": 0.0, "stake": 0.0,
            "free_capital": 1000.0, "model": "m", "prompt_version": "v",
            "created_at": iso(stored_at), "evidence_json": evidence}


def _evidence(ledger, mid):
    row = ledger.conn.execute(
        "SELECT evidence_json FROM forecasts WHERE market_id = ?", (mid,)).fetchone()
    return json.loads(row[0])


KEPT = {"kind", "id", "url", "subreddit", "score", "created_utc", "used"}


def test_prune_strips_reddit_text_older_than_seven_days_and_keeps_news(tmp_path):
    stored = NOW - timedelta(days=8)
    with Ledger(tmp_path / "l.sqlite3") as ledger:
        ledger.record_forecast(_row("1", stored, [_reddit(created=stored), _news()]))
        counts = retention.prune_ledger(ledger, NOW)
        reddit, news = _evidence(ledger, "1")
    assert counts["rows changed"] == 1 and counts["reddit entries stripped"] == 1
    assert set(reddit) == KEPT
    assert reddit["url"].startswith("https://www.reddit.com/") and reddit["used"] is True
    assert news == _news()


def test_a_recent_row_keeps_fresh_text_but_not_an_old_post(tmp_path):
    stored = NOW - timedelta(days=2)
    fresh = _reddit("R1", created=NOW - timedelta(days=1))
    old_post = _reddit("R2", created=NOW - timedelta(days=10))
    with Ledger(tmp_path / "l.sqlite3") as ledger:
        ledger.record_forecast(_row("1", stored, [fresh, old_post, _news()]))
        retention.prune_ledger(ledger, NOW)
        kept, stripped, news = _evidence(ledger, "1")
    assert kept["quote"] == QUOTE and kept["title"] == TITLE
    assert "quote" not in stripped and "title" not in stripped
    assert news == _news()


def test_an_unreadable_date_counts_as_stale():
    item = _reddit(created=NOW)
    assert retention.reddit_text_expired(item, "not a date", NOW) is True
    assert retention.reddit_text_expired({**item, "created_utc": "junk"},
                                         iso(NOW - timedelta(days=1)), NOW) is False
    assert retention.reddit_text_expired(_news(), "not a date", NOW) is False


def _fake_gamma(url):
    if "/markets/" in url:
        return raw_market(url.rsplit("/", 1)[1])
    raise RuntimeError("the market list is not needed in this test")


def test_a_daily_run_prunes_first_even_when_it_fails_later(tmp_path, monkeypatch, capsys):
    stored = datetime.now(timezone.utc) - timedelta(days=8)
    path = tmp_path / "l.sqlite3"
    with Ledger(path) as ledger:
        ledger.record_forecast(_row("1", stored, [_reddit(created=stored), _news()]))

    def broken(url):
        raise RuntimeError("Gamma fell over")

    monkeypatch.setattr(gamma, "urllib_get_json", broken)
    monkeypatch.setattr(model, "load_key", lambda **kw: "")
    with pytest.raises(RuntimeError, match="Gamma fell over"):
        cli.main(["daily", "--db", str(path)])
    assert "1 rows checked, 1 changed, 1 entries stripped" in capsys.readouterr().out
    with Ledger(path) as ledger:
        reddit, news = _evidence(ledger, "1")
    assert set(reddit) == KEPT
    assert news == _news()


def test_a_daily_run_that_completes_also_prunes(tmp_path, monkeypatch, capsys):
    stored = datetime.now(timezone.utc) - timedelta(days=9)
    path = tmp_path / "l.sqlite3"
    with Ledger(path) as ledger:
        ledger.record_forecast(_row("1", stored, [_reddit(created=stored)]))
    monkeypatch.setattr(gamma, "urllib_get_json", _fake_gamma)
    monkeypatch.setattr(model, "load_key", lambda **kw: "")
    assert cli.main(["daily", "--db", str(path)]) == 2  # no key: no forecast
    with Ledger(path) as ledger:
        assert set(_evidence(ledger, "1")[0]) == KEPT


def test_prune_evidence_command(tmp_path, capsys):
    stored = datetime.now(timezone.utc) - timedelta(days=8)
    path = tmp_path / "l.sqlite3"
    with Ledger(path) as ledger:
        ledger.record_forecast(_row("1", stored, [_reddit(created=stored)]))
    assert cli.main(["prune-evidence", "--db", str(path)]) == 0
    assert "1 entries stripped" in capsys.readouterr().out
    with Ledger(path) as ledger:
        assert "quote" not in _evidence(ledger, "1")[0]


def test_removed_text_leaves_the_file(tmp_path):
    path = tmp_path / "l.sqlite3"
    stored = NOW - timedelta(days=8)
    with Ledger(path) as ledger:
        ledger.record_forecast(_row("1", stored, [_reddit(created=stored), _news()]))
    assert QUOTE.encode() in path.read_bytes()
    with Ledger(path) as ledger:
        retention.prune_ledger(ledger, NOW)
    raw = path.read_bytes()
    # The markers, not the whole strings: a later write can overwrite the
    # tail of an old record and leave its head readable.
    assert b"QUOTE-MARKER" not in raw
    assert b"TITLE-MARKER" not in raw
    assert b"A headline that stays" in raw


def test_the_export_never_carries_a_quote_or_a_stale_title(tmp_path):
    """The export checks the rule itself: a copied ledger may never have been pruned."""
    with Ledger(tmp_path / "l.sqlite3") as ledger:
        fresh = NOW - timedelta(days=1)
        stale = NOW - timedelta(days=8)
        ledger.record_forecast(_row("fresh", fresh, [_reddit("R1", created=fresh), _news()]))
        ledger.record_forecast(_row("stale", stale, [
            _reddit("R2", created=stale, title="STALE-TITLE-MARKER"), _news()]))
        payload = build_payload(ledger, NOW)
    text = dumps(payload)
    assert QUOTE not in text
    assert "STALE-TITLE-MARKER" not in text
    rows = {r["market_id"]: r for r in payload["recent"]}
    fresh_reddit, fresh_news = rows["fresh"]["evidence"]
    stale_reddit, stale_news = rows["stale"]["evidence"]
    assert fresh_reddit["title"] == TITLE
    assert stale_reddit["title"] is None
    assert stale_reddit["url"].startswith("https://www.reddit.com/")
    assert stale_news["title"] == "A headline that stays"
    assert all("quote" not in e for r in payload["recent"] for e in r["evidence"])


def test_the_agent_stores_no_text_for_a_post_already_too_old(tmp_path):
    binary = tmp_path / "prospector"
    binary.write_text("", encoding="utf-8")
    db = tmp_path / "p.db"
    db.write_text("", encoding="utf-8")

    def fake_run(cmd, **kwargs):
        hits = [{"permalink": "/r/x/comments/new/", "subreddit": "x", "title": "fresh title",
                 "quote": "fresh quote", "score": 0.9,
                 "created_utc": int((NOW - timedelta(days=1)).timestamp())},
                {"permalink": "/r/x/comments/old/", "subreddit": "x", "title": TITLE,
                 "quote": QUOTE, "score": 0.8,
                 "created_utc": int((NOW - timedelta(days=12)).timestamp())}]
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(hits), stderr="")

    class Client:
        model = "deepseek-flash"

        def complete(self, system, user):
            return model.Completion('{"probability": 0.5, "evidence_used": ["R2"]}',
                                    "deepseek-flash", 1, 1)

    market = gamma.parse_market(raw_market("m1"))[0]
    sources = agent.Sources(prospector_bin=binary, prospector_db=db, news_db=None)
    with Ledger(tmp_path / "l.sqlite3") as ledger:
        agent.forecast_markets(ledger, Client(), [market], sources, NOW, run=fake_run,
                               log=lambda s: None)
        fresh, old = _evidence(ledger, "m1")
    assert fresh["quote"] == "fresh quote"
    assert set(old) == KEPT and old["used"] is True
