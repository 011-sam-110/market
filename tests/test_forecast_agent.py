"""The agent: stake sizing, the call cap in a run, the prompt, and the evidence."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from forecast import HARD_DAILY_CALLS, STAKE_CAP, agent, gamma, model
from forecast import evidence as ev
from forecast.ledger import Ledger
from tests.forecast_fixtures import NOW, raw_market


def _market(**kwargs) -> gamma.Market:
    market, why = gamma.parse_market(raw_market("m1", **kwargs))
    assert market is not None, why
    return market


# --- stake sizing ------------------------------------------------------------

def test_kelly_full_for_a_binary_contract():
    assert agent.kelly_full(0.6, 0.5) == pytest.approx(0.2)
    assert agent.kelly_full(0.5, 0.5) == 0.0
    assert agent.kelly_full(0.4, 0.5) < 0


def test_stake_backs_outcome_a_at_the_ask():
    stake = agent.paper_stake(0.55, _market(prices=("0.40", "0.60"), best_ask=0.41),
                              1000.0)
    assert stake.side == "a"
    assert stake.fill_price == pytest.approx(0.41)
    assert stake.fill_basis == "book"
    full = (0.55 - 0.41) / (1 - 0.41)
    assert stake.kelly_full == pytest.approx(full)
    assert stake.fraction == pytest.approx(0.25 * full)
    assert stake.amount == pytest.approx(round(0.25 * full * 1000, 2))


def test_stake_backs_outcome_b_at_one_minus_the_bid():
    stake = agent.paper_stake(0.20, _market(prices=("0.40", "0.60"), best_bid=0.39),
                              1000.0)
    assert stake.side == "b"
    assert stake.fill_price == pytest.approx(0.61)
    assert stake.kelly_full == pytest.approx((0.80 - 0.61) / (1 - 0.61))


def test_an_edge_inside_the_spread_gets_no_stake():
    stake = agent.paper_stake(0.405, _market(prices=("0.40", "0.60"), best_ask=0.41),
                              1000.0)
    assert stake.side == "none"
    assert stake.amount == 0.0


def test_the_stake_is_capped_at_six_percent():
    stake = agent.paper_stake(0.99, _market(prices=("0.10", "0.90"), best_ask=0.11),
                              1000.0)
    assert stake.fraction == STAKE_CAP
    assert stake.amount == pytest.approx(60.0)


def test_mid_price_is_the_fallback_when_the_book_is_missing():
    stake = agent.paper_stake(0.6, _market(prices=("0.40", "0.60"), best_ask=None), 500.0)
    assert stake.fill_basis == "mid"
    assert stake.fill_price == pytest.approx(0.40)


def test_no_free_capital_means_no_stake():
    stake = agent.paper_stake(0.9, _market(), 0.0)
    assert stake.amount == 0.0


# --- the run -------------------------------------------------------------------

class FakeClient:
    """Stands in for DeepSeek. Records every prompt. Never touches a network."""

    def __init__(self, answers=None, fail_on=()):
        self.model = "deepseek-flash"
        self.prompts: list[str] = []
        self.answers = answers or {}
        self.fail_on = set(fail_on)

    def complete(self, system, user):
        self.prompts.append(user)
        n = len(self.prompts)
        if n in self.fail_on:
            raise model.ModelError("HTTP 503: busy")
        content = self.answers.get(n, json.dumps(
            {"probability": 0.62, "reasoning": "base rate", "evidence_used": ["N1"]}))
        return model.Completion(content=content, model="deepseek-flash",
                                prompt_tokens=100, completion_tokens=20)


def _picked(n):
    return [gamma.parse_market(raw_market(f"m{i}", prices=("0.415", "0.585")))[0]
            for i in range(n)]


NO_SOURCES = agent.Sources(prospector_bin=None, prospector_db=None, news_db=None)


def test_a_run_makes_at_most_twenty_calls(tmp_path):
    client = FakeClient()
    with Ledger(tmp_path / "l.sqlite3") as ledger:
        result = agent.forecast_markets(ledger, client, _picked(25), NO_SOURCES, NOW,
                                        cap=999, log=lambda s: None)
        assert len(client.prompts) == HARD_DAILY_CALLS
        assert result.written == HARD_DAILY_CALLS
        assert result.calls_after == HARD_DAILY_CALLS
        assert len(ledger.all_forecasts()) == HARD_DAILY_CALLS
        # A second run the same day makes no call at all.
        again = agent.forecast_markets(ledger, client, _picked(25)[20:], NO_SOURCES, NOW,
                                       log=lambda s: None)
        assert len(client.prompts) == HARD_DAILY_CALLS
        assert again.written == 0
        assert "20 of 20" in again.stopped


def test_a_failed_call_uses_its_slot(tmp_path):
    client = FakeClient(fail_on={2}, answers={3: "not json"})
    with Ledger(tmp_path / "l.sqlite3") as ledger:
        result = agent.forecast_markets(ledger, client, _picked(4), NO_SOURCES, NOW,
                                        cap=4, log=lambda s: None)
        assert result.calls_after == 4
        assert result.written == 2
        outcomes = [r[0] for r in ledger.conn.execute(
            "SELECT outcome FROM model_calls ORDER BY call_id")]
        assert outcomes[0] == "ok"
        assert outcomes[1].startswith("error: HTTP 503")
        assert outcomes[2].startswith("bad answer")


def test_the_row_records_the_market_price_and_the_stake(tmp_path):
    with Ledger(tmp_path / "l.sqlite3") as ledger:
        agent.forecast_markets(ledger, FakeClient(), _picked(1), NO_SOURCES, NOW,
                               log=lambda s: None)
        row = ledger.all_forecasts()[0]
        assert row["market_price"] == pytest.approx(0.415)
        assert row["probability"] == pytest.approx(0.62)
        assert row["side"] == "a"
        assert 0 < row["stake"] <= STAKE_CAP * 1000
        assert row["price_read_at"] == "2026-10-04T12:00:00Z"
        assert row["reddit_status"].startswith("no reddit evidence")
        assert row["news_status"].startswith("no news evidence")


def test_the_prompt_does_not_show_the_market_price(tmp_path):
    client = FakeClient()
    with Ledger(tmp_path / "l.sqlite3") as ledger:
        agent.forecast_markets(ledger, client, _picked(1), NO_SOURCES, NOW,
                               log=lambda s: None)
    prompt = client.prompts[0]
    assert "0.415" not in prompt and "0.585" not in prompt
    assert "41.5" not in prompt
    assert "OUTCOME A: Yes" in prompt


# --- answers --------------------------------------------------------------------

def test_parse_answer_accepts_json_and_rejects_bad_probabilities():
    p, why, used = model.parse_answer('{"probability": 0.3, "reasoning": "x", '
                                      '"evidence_used": ["R1"]}')
    assert (p, why, used) == (0.3, "x", ["R1"])
    with pytest.raises(model.AnswerError):
        model.parse_answer('{"probability": 1.3}')
    with pytest.raises(model.AnswerError):
        model.parse_answer('{"reasoning": "no number"}')
    with pytest.raises(model.AnswerError):
        model.parse_answer("maybe 40%")


def test_the_client_sends_one_request_and_hides_the_key():
    sent = []

    def transport(url, headers, body):
        sent.append((url, headers, json.loads(body)))
        return 200, json.dumps({"model": "deepseek-flash", "choices": [
            {"message": {"content": '{"probability": 0.5}'}}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 3}}).encode()

    client = model.DeepSeek("sk-test-secret", transport=transport)
    assert "sk-test-secret" not in repr(client)
    done = client.complete("sys", "user")
    assert len(sent) == 1
    assert sent[0][0] == model.CHAT_URL
    assert sent[0][2]["response_format"] == {"type": "json_object"}
    assert done.prompt_tokens == 9


def test_a_non_200_is_a_model_error_and_is_not_retried():
    calls = []

    def transport(url, headers, body):
        calls.append(1)
        return 503, b'{"error": {"message": "busy"}}'

    with pytest.raises(model.ModelError, match="503"):
        model.DeepSeek("k", transport=transport).complete("s", "u")
    assert len(calls) == 1


def test_load_key_reads_only_the_deepseek_line(tmp_path):
    env_file = tmp_path / "newsfeed.env"
    env_file.write_text("NEWSFEED_INGEST_SECRET=other\nDEEPSEEK_API_KEY=sk-abc\n",
                        encoding="utf-8")
    assert model.load_key(env={}, key_file=env_file) == "sk-abc"
    assert model.load_key(env={"DEEPSEEK_API_KEY": "sk-env"}, key_file=env_file) == "sk-env"
    assert model.load_key(env={}, key_file=tmp_path / "missing.env") == ""


# --- Reddit through the Prospector CLI contract ------------------------------------

def test_reddit_uses_the_exact_cli_contract(tmp_path):
    binary = tmp_path / "prospector"
    binary.write_text("", encoding="utf-8")
    db = tmp_path / "p.db"
    db.write_text("", encoding="utf-8")
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        out = [{"permalink": "/r/news/comments/abc/x/", "subreddit": "news",
                "title": "Thread", "quote": "a quote", "score": 0.82,
                "created_utc": 1759500000},
               {"subreddit": "nolink"}]
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(out), stderr="")

    got = ev.reddit_evidence("Will X happen?", binary=binary, db=db, limit=5, run=fake_run)
    assert seen["cmd"] == [str(binary), "semantic-search", "Will X happen?", "--db",
                           str(db), "--limit", "5", "--json"]
    assert got.items[0]["url"] == "https://www.reddit.com/r/news/comments/abc/x/"
    assert got.status == "reddit: 1 hits (1 without a permalink dropped)"


def test_reddit_missing_binary_is_named_not_empty(tmp_path):
    got = ev.reddit_evidence("q", binary=tmp_path / "nope", db=tmp_path / "p.db")
    assert got.items == []
    assert got.status.startswith("no reddit evidence: prospector binary not found")


def test_reddit_bad_output_and_failure_are_named(tmp_path):
    binary = tmp_path / "prospector"
    binary.write_text("", encoding="utf-8")
    db = tmp_path / "p.db"
    db.write_text("", encoding="utf-8")

    def not_json(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="hello", stderr="")

    def failed(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="no such table\n")

    assert ev.reddit_evidence("q", binary=binary, db=db, run=not_json).status == \
        "no reddit evidence: prospector output is not JSON"
    assert ev.reddit_evidence("q", binary=binary, db=db, run=failed).status == \
        "no reddit evidence: prospector exited 2 no such table"


# --- NewsScraper headlines ------------------------------------------------------------

def _news_db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE stories (story_id TEXT PRIMARY KEY, outlet TEXT, "
                 "headline TEXT, description TEXT, url TEXT, published TEXT, "
                 "first_seen_at TEXT)")
    rows = [
        ("1", "bbc", "Iran and US agree to extend ceasefire", "Talks in Oman",
         "https://bbc.example/1", "2026-10-03T10:00:00Z", "2026-10-03T10:05:00Z"),
        ("2", "nyt", "Ceasefire holds as Iran talks resume", "",
         "https://nyt.example/2", "2026-10-02T10:00:00Z", "2026-10-02T10:05:00Z"),
        ("3", "pbs", "Football results", "Iran mentioned once",
         "https://pbs.example/3", "2026-10-02T10:00:00Z", "2026-10-02T10:05:00Z"),
        ("4", "bbc", "Old Iran ceasefire story", "",
         "https://bbc.example/4", "2026-08-01T10:00:00Z", "2026-08-01T10:05:00Z"),
    ]
    conn.executemany("INSERT INTO stories VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return path


def test_news_matches_two_keywords_in_the_window(tmp_path):
    db = _news_db(tmp_path / "news.sqlite3")
    got = ev.news_evidence("US x Iran ceasefire continues through October 31?", db=db,
                           now=NOW)
    urls = [i["url"] for i in got.items]
    assert urls == ["https://bbc.example/1", "https://nyt.example/2"]
    assert got.status.startswith("news: 2 headlines from 3 stories in 14 days")


def test_news_store_is_opened_read_only(tmp_path):
    db = _news_db(tmp_path / "news.sqlite3")
    before = db.read_bytes()
    ev.news_evidence("Iran ceasefire", db=db, now=NOW)
    assert db.read_bytes() == before


def test_news_missing_store_is_named(tmp_path):
    got = ev.news_evidence("Iran ceasefire", db=tmp_path / "none.sqlite3", now=NOW)
    assert got.status.startswith("no news evidence")


def test_a_short_acronym_matches_only_a_whole_word():
    assert ev._match_score(["us", "iran"], "iranian talks: use of force") == 1
    assert ev._match_score(["us", "iran"], "us and iranian envoys meet") == 2


def test_keywords_drop_market_words_and_keep_acronyms():
    assert ev.keywords("Will NATO x Russia military clash by October 31, 2026?") == \
        ["nato", "russia", "military", "clash"]
    assert ev.keywords("Jets vs. Bears") == ["jets", "bears"]


def test_gather_numbers_the_evidence_for_the_prompt(tmp_path):
    db = _news_db(tmp_path / "news.sqlite3")
    market = gamma.parse_market(raw_market(
        "m", question="US x Iran ceasefire continues through October 31?",
        end_in=timedelta(days=20)))[0]
    items, reddit_status, news_status = agent.gather(
        market, agent.Sources(prospector_bin=None, prospector_db=None, news_db=db), NOW)
    assert [i["id"] for i in items] == ["N1", "N2"]
    assert reddit_status.startswith("no reddit evidence")
    system, user = model.build_prompt(market, items, NOW)
    assert "[N1] News (bbc, 2026-10-03): Iran and US agree to extend ceasefire" in user
