"""The paper ledger: one SQLite file, outside the repo, and the model-call counter.

Three tables carry the record:

- `forecasts`: one row per market, ever. It holds the market price read at
  the moment of the forecast, the agent's probability, the evidence links
  and the paper stake. The scorer settles the row when the market resolves.
- `model_calls`: one row per model request, written BEFORE the request is
  sent. The daily cap counts these rows, so a crash or a failed request
  still uses up its slot. A trigger in the schema refuses a row past the
  hard cap, so code that skips `reserve_call()` still cannot pass it.
- `runs`: one row per daily run, with its printed summary.

The default path is ~/.forecast/ledger.sqlite3 (env FORECAST_DB). The ledger
refuses a path inside this repo's checkout: the record is data, and the repo
is code.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from forecast import HARD_DAILY_CALLS, PAPER_CAPITAL

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = Path.home() / ".forecast" / "ledger.sqlite3"
SCHEMA_VERSION = 1

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_calls (
    call_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    day               TEXT NOT NULL,
    reserved_at       TEXT NOT NULL,
    finished_at       TEXT,
    market_id         TEXT,
    model             TEXT,
    outcome           TEXT NOT NULL DEFAULT 'reserved',
    prompt_tokens     INTEGER,
    completion_tokens INTEGER
);
CREATE INDEX IF NOT EXISTS model_calls_day ON model_calls(day);

-- The hard cap, in the store itself. reserve_call() checks first and names
-- the reason. This trigger is the backstop for any code path that does not.
CREATE TRIGGER IF NOT EXISTS model_calls_cap
BEFORE INSERT ON model_calls
WHEN (SELECT count(*) FROM model_calls WHERE day = NEW.day) >= {HARD_DAILY_CALLS}
BEGIN
    SELECT RAISE(ABORT, 'daily model call cap reached');
END;

CREATE TABLE IF NOT EXISTS forecasts (
    forecast_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id       TEXT NOT NULL UNIQUE,
    event_id        TEXT,
    event_title     TEXT,
    question        TEXT NOT NULL,
    outcome_a       TEXT NOT NULL,
    outcome_b       TEXT NOT NULL,
    market_url      TEXT,
    end_date        TEXT,
    liquidity       REAL,
    volume          REAL,
    is_sports       INTEGER NOT NULL DEFAULT 0,
    market_price    REAL NOT NULL,
    best_bid        REAL,
    best_ask        REAL,
    price_read_at   TEXT NOT NULL,
    probability     REAL NOT NULL,
    reasoning       TEXT,
    evidence_json   TEXT NOT NULL DEFAULT '[]',
    reddit_status   TEXT NOT NULL,
    news_status     TEXT NOT NULL,
    side            TEXT NOT NULL,
    fill_price      REAL,
    fill_basis      TEXT,
    kelly_full      REAL NOT NULL,
    stake_fraction  REAL NOT NULL,
    stake           REAL NOT NULL,
    free_capital    REAL NOT NULL,
    model           TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    call_id         INTEGER REFERENCES model_calls(call_id),
    created_at      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',
    final_price_a   REAL,
    settled_at      TEXT,
    pnl             REAL
);
CREATE INDEX IF NOT EXISTS forecasts_status ON forecasts(status);

CREATE TABLE IF NOT EXISTS runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    summary     TEXT
);
"""


class LedgerPathError(ValueError):
    """The ledger path is inside the repo checkout."""


class CallBudgetExhausted(RuntimeError):
    """The day's model calls are used up. The run stops making calls."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_path(path: str | os.PathLike | None = None) -> Path:
    raw = path if path is not None else os.environ.get("FORECAST_DB")
    target = Path(raw).expanduser() if raw else DEFAULT_PATH
    resolved = target.resolve()
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        raise LedgerPathError(
            f"The ledger path {resolved} is inside the repo checkout {REPO_ROOT}. "
            "The record is data, not code. Put it outside the checkout, "
            "for example ~/.forecast/ledger.sqlite3.")
    return resolved


@dataclass(frozen=True)
class CallSlot:
    call_id: int
    day: str
    used_after: int  # calls on this day, including this one
    cap: int


class Ledger:
    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self.path = resolve_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: this class opens its own transactions, so the
        # call counter can take a write lock BEFORE it reads the count.
        self.conn = sqlite3.connect(str(self.path), isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        # Removed text must leave the file, not only the row. Without this,
        # SQLite leaves the old bytes of a rewritten row in free space, and
        # Reddit text that retention.py removed could still be read from disk.
        self.conn.execute("PRAGMA secure_delete = ON")
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),))
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('paper_capital', ?)",
            (repr(PAPER_CAPITAL),))

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    # --- the call counter ------------------------------------------------

    @staticmethod
    def day_of(when: datetime) -> str:
        return when.astimezone(timezone.utc).strftime("%Y-%m-%d")

    def calls_on(self, day: str) -> int:
        row = self.conn.execute(
            "SELECT count(*) FROM model_calls WHERE day = ?", (day,)).fetchone()
        return int(row[0])

    def reserve_call(self, now: datetime, market_id: str, model: str,
                     cap: int = HARD_DAILY_CALLS) -> CallSlot:
        """Write the call row first, or raise CallBudgetExhausted.

        The cap is clamped to HARD_DAILY_CALLS. A caller can ask for fewer
        calls. A caller cannot ask for more.
        """
        cap = max(0, min(int(cap), HARD_DAILY_CALLS))
        day = self.day_of(now)
        with self._write() as conn:
            used = int(conn.execute(
                "SELECT count(*) FROM model_calls WHERE day = ?", (day,)).fetchone()[0])
            if used >= cap:
                raise CallBudgetExhausted(
                    f"{used} of {cap} model calls used on {day} (UTC)")
            cur = conn.execute(
                "INSERT INTO model_calls(day, reserved_at, market_id, model) "
                "VALUES (?, ?, ?, ?)", (day, iso(now), market_id, model))
            call_id = int(cur.lastrowid)
        return CallSlot(call_id=call_id, day=day, used_after=used + 1, cap=cap)

    def finish_call(self, call_id: int, outcome: str, *, model: str | None = None,
                    prompt_tokens: int | None = None,
                    completion_tokens: int | None = None) -> None:
        with self._write() as conn:
            conn.execute(
                "UPDATE model_calls SET finished_at = ?, outcome = ?, "
                "model = COALESCE(?, model), prompt_tokens = ?, completion_tokens = ? "
                "WHERE call_id = ?",
                (iso(utc_now()), outcome[:300], model, prompt_tokens,
                 completion_tokens, call_id))

    # --- forecasts -------------------------------------------------------

    def known_markets(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT market_id FROM forecasts")}

    def known_events(self) -> set[str]:
        return {r[0] for r in self.conn.execute(
            "SELECT DISTINCT event_id FROM forecasts WHERE event_id IS NOT NULL")}

    def paper_capital(self) -> float:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = 'paper_capital'").fetchone()
        return float(row[0])

    def free_capital(self) -> float:
        """Paper capital, plus settled P&L, minus the stakes still open."""
        row = self.conn.execute(
            "SELECT "
            "  COALESCE(SUM(CASE WHEN status != 'open' THEN pnl END), 0), "
            "  COALESCE(SUM(CASE WHEN status = 'open' THEN stake END), 0) "
            "FROM forecasts").fetchone()
        return self.paper_capital() + float(row[0]) - float(row[1])

    def record_forecast(self, row: dict) -> int:
        fields = dict(row)
        fields["evidence_json"] = json.dumps(fields.get("evidence_json") or [],
                                             ensure_ascii=False, sort_keys=True)
        cols = sorted(fields)
        sql = (f"INSERT INTO forecasts({', '.join(cols)}) "
               f"VALUES ({', '.join('?' for _ in cols)})")
        with self._write() as conn:
            cur = conn.execute(sql, [fields[c] for c in cols])
            return int(cur.lastrowid)

    def open_forecasts(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM forecasts WHERE status = 'open' ORDER BY forecast_id"))

    def all_forecasts(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM forecasts ORDER BY forecast_id"))

    def replace_evidence(self, updates: list[tuple[str, int]]) -> None:
        """Write new evidence_json values, as (json_text, forecast_id) pairs."""
        with self._write() as conn:
            conn.executemany(
                "UPDATE forecasts SET evidence_json = ? WHERE forecast_id = ?", updates)

    def settle(self, forecast_id: int, *, status: str, final_price_a: float,
               pnl: float, settled_at: datetime) -> None:
        if status not in ("resolved", "void"):
            raise ValueError(f"unknown settlement status {status!r}")
        with self._write() as conn:
            conn.execute(
                "UPDATE forecasts SET status = ?, final_price_a = ?, pnl = ?, "
                "settled_at = ? WHERE forecast_id = ? AND status = 'open'",
                (status, final_price_a, pnl, iso(settled_at), forecast_id))

    # --- runs ------------------------------------------------------------

    def start_run(self, kind: str, now: datetime) -> int:
        with self._write() as conn:
            cur = conn.execute("INSERT INTO runs(kind, started_at) VALUES (?, ?)",
                               (kind, iso(now)))
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, summary: dict) -> None:
        with self._write() as conn:
            conn.execute("UPDATE runs SET finished_at = ?, summary = ? WHERE run_id = ?",
                         (iso(utc_now()), json.dumps(summary, sort_keys=True), run_id))
