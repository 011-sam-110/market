# Vinted Phone Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A loop that watches Vinted's phone category and pushes a Telegram alert when a phone is listed materially below the going rate for its exact model, storage and condition.

**Architecture:** Six stages — watch, parse, price, score, verify, alert. Stages 2–4 are pure functions with no I/O, so the decision logic is exhaustively unit-testable against fixtures. I/O lives at the edges (`vinted_client`, `store`, `alerter`). Observations append to Parquet and are queried with DuckDB.

**Tech Stack:** Python 3.11+, `requests`, `duckdb`, `pyarrow`, `xxhash`, `pytest`.

## Global Constraints

- **Every failure degrades toward silence, never toward a false alert.** A missed deal costs nothing; a bad alert costs money and trust.
- **Missing storage is never guessed.** `storage_gb` stays `None` and prices against the lowest tier for that model.
- **Scoring is banded, not ranked.** Below `BAND_BAIT` a low price means fraud; those listings are routed away from buy alerts.
- **No hardcoded tunables at call sites.** Everything in `config.py`.
- **Rules-first parsing.** LLM only for the residue rules cannot resolve; never inside the hot path by default.
- **No automated buying.** Alerts only.
- Category id `3565` (mobile phones and communication). Results cap at 960 entries.
- Landed cost is always `total_item_price` (includes `service_fee`), never `price`.

---

### Task 1: Scaffold and config

**Files:**
- Create: `requirements.txt`, `phoneloop/__init__.py`, `phoneloop/config.py`, `tests/__init__.py`, `.env.example`

**Interfaces:**
- Produces: `config.Config` dataclass with `POLL_INTERVAL_S:int`, `PRICE_WINDOW_DAYS:int`, `MIN_BUCKET_SAMPLES:int`, `BAND_BUY_HIGH:float`, `BAND_BUY_LOW:float`, `BAND_BAIT:float`, `ALERTS_PER_BUCKET_HOUR:int`, `CATALOG_ID:int`, `DATA_DIR:Path`, `TELEGRAM_TOKEN:str`, `TELEGRAM_CHAT_ID:str`; `config.load() -> Config` reading env with the spec's defaults.

- [ ] **Step 1:** Write `tests/test_config.py::test_defaults_match_spec` asserting `load()` returns `POLL_INTERVAL_S == 60`, `MIN_BUCKET_SAMPLES == 8`, `BAND_BAIT == -0.60`.
- [ ] **Step 2:** Run `pytest tests/test_config.py -v` — expect FAIL (no module).
- [ ] **Step 3:** Implement `phoneloop/config.py` with a frozen dataclass and `load()` reading `os.environ` with spec defaults.
- [ ] **Step 4:** Run `pytest tests/test_config.py -v` — expect PASS.
- [ ] **Step 5:** Commit `feat: project scaffold and config defaults`.

---

### Task 2: Parser (pure) — the highest-risk module

**Files:**
- Create: `phoneloop/parser.py`, `tests/test_parser.py`

**Interfaces:**
- Produces: `PhoneSpec` frozen dataclass — `make:str|None`, `model:str|None`, `storage_gb:int|None`, `network_lock:str|None`, `disqualifiers:tuple[str,...]`, `parse_method:str`, `confidence:float`; and `parse(title:str, description:str="") -> PhoneSpec`.
- `model` is normalised slug form: `iphone-13`, `iphone-13-pro`, `iphone-13-pro-max`, `iphone-13-mini`.

**Test cases (all from the live feed):**

```python
CASES = [
  ("iPhone 13 128gb Midnight  Excellent Condition", "iphone-13", 128, ()),
  ("Apple iPhone 13 128GB Black Network Unlocked",  "iphone-13", 128, ()),
  ("iPhone 13 White",                                "iphone-13", None, ()),
  ("Apple I-Phone 13 Green Unlocked",                "iphone-13", None, ()),
  ("iphone 13pro",                                   "iphone-13-pro", None, ()),
  ("Apple iPhone 13 RED - 128GB - Spares & Repair", "iphone-13", 128, ("spares_repair",)),
  ("iPhone 13 Pro Max 256 GB",                       "iphone-13-pro-max", 256, ()),
  ("iphone 13 mini 64gb",                            "iphone-13-mini", 64, ()),
]
```

- [ ] **Step 1:** Write `tests/test_parser.py` parametrised over `CASES`, asserting model, storage and disqualifiers.
- [ ] **Step 2:** Add `test_pro_max_not_misread_as_pro` — `"iphone 13 pro max"` must not yield `iphone-13-pro`. **Order matters: match longest variant first.**
- [ ] **Step 3:** Add `test_accessory_rejected` — `"iphone 13 pro max case"` yields `model=None` (an accessory, not a phone).
- [ ] **Step 4:** Add `test_icloud_locked_from_description` — disqualifier found in description, not title.
- [ ] **Step 5:** Run `pytest tests/test_parser.py -v` — expect FAIL.
- [ ] **Step 6:** Implement `parser.py`. Order of operations: normalise (lowercase, strip punctuation, collapse `i-phone`/`iphone`), reject accessories, match variant longest-first, extract storage, scan disqualifiers over `title + " " + description`.
- [ ] **Step 7:** Run tests — expect PASS.
- [ ] **Step 8:** Commit `feat: rules-based title parser with accessory and disqualifier rejection`.

---

### Task 3: Scorer (pure) — the money logic

**Files:**
- Create: `phoneloop/scorer.py`, `tests/test_scorer.py`

**Interfaces:**
- Consumes: `config.Config`, `parser.PhoneSpec`.
- Produces: `Band` str-enum — `BAIT|BUY|FAIR|OVERPRICED|UNPRICEABLE`; `Verdict` frozen dataclass — `band:Band`, `discount:float|None`, `alertable:bool`, `reason:str`; `score(spec, total_price:Decimal, reference:Decimal|None, cfg) -> Verdict`.

**Critical behaviours to test:**

```python
# reference 200, bands -15/-45/-60
(200, 210, Band.OVERPRICED, False)   # above median
(200, 190, Band.FAIR,       False)   # -5%, inside the fair zone
(200, 150, Band.BUY,        True)    # -25%, the target
(200, 112, Band.BUY,        True)    # -44%, just inside
(200, 108, Band.BUY,        True)    # -46% -> still buy, above bait floor
(200,  70, Band.BAIT,       False)   # -65%, fraud floor: NOT alertable
(200, None_ref, Band.UNPRICEABLE, False)  # thin bucket
```

- [ ] **Step 1:** Write `tests/test_scorer.py` parametrised over the table above.
- [ ] **Step 2:** Add `test_bait_is_never_alertable` — the single most important assertion in the codebase.
- [ ] **Step 3:** Add `test_disqualified_spec_never_alertable` — any non-empty `disqualifiers` forces `alertable=False` regardless of discount.
- [ ] **Step 4:** Add `test_none_reference_is_unpriceable_not_zero` — guards against a thin bucket being read as a 100% discount.
- [ ] **Step 5:** Run tests — expect FAIL.
- [ ] **Step 6:** Implement `scorer.py`. Guard order: disqualifiers → `reference is None` → bait floor → buy band → fair/overpriced.
- [ ] **Step 7:** Run tests — expect PASS.
- [ ] **Step 8:** Commit `feat: banded scorer with fraud floor and disqualifier veto`.

---

### Task 4: Store — Parquet append with content-hash dedup

**Files:**
- Create: `phoneloop/store.py`, `tests/test_store.py`

**Interfaces:**
- Produces: `Store(data_dir:Path)` with `record(observations:list[dict]) -> int` (returns rows actually written), `last_hashes(item_ids) -> dict[int,int]`, `observations_df(since:datetime) -> duckdb relation`, `mark_vanished(item_ids, at)`.
- `content_hash` = xxhash64 over `(price, total_item_price, condition, title, is_visible)`.

- [ ] **Step 1:** Write `test_unchanged_listing_writes_no_row` — record the same observation twice, assert second call returns `0`.
- [ ] **Step 2:** Write `test_price_change_writes_row` — same id, different price, assert returns `1`.
- [ ] **Step 3:** Write `test_roundtrip_preserves_decimal_price` — guard against float drift on money.
- [ ] **Step 4:** Run tests — expect FAIL.
- [ ] **Step 5:** Implement `store.py` writing date-partitioned Parquet under `data_dir/observations/date=YYYY-MM-DD/`, reading back with DuckDB.
- [ ] **Step 6:** Run tests — expect PASS.
- [ ] **Step 7:** Commit `feat: parquet store with content-hash change detection`.

---

### Task 5: Pricebook — rolling median and MAD

**Files:**
- Create: `phoneloop/pricebook.py`, `tests/test_pricebook.py`

**Interfaces:**
- Consumes: `store.Store`, `config.Config`.
- Produces: `Pricebook(store, cfg)` with `reference(spec) -> Decimal|None` and `refresh()`.
- Bucket key: `(make, model, storage_gb, condition)`. `storage_gb=None` prices against the **lowest observed tier** for that model.

- [ ] **Step 1:** Write `test_thin_bucket_returns_none` — fewer than `MIN_BUCKET_SAMPLES` observations yields `None`.
- [ ] **Step 2:** Write `test_uses_median_not_mean` — feed `[100,100,100,100,100,100,100,100,1000]`; assert reference is `100`, not `200`. **This is the outlier-resistance guarantee.**
- [ ] **Step 3:** Write `test_null_storage_prices_against_lowest_tier` — buckets at 128GB=£200 and 256GB=£300; a null-storage spec references £200.
- [ ] **Step 4:** Run tests — expect FAIL.
- [ ] **Step 5:** Implement `pricebook.py` with a DuckDB `median()` aggregate over the trailing `PRICE_WINDOW_DAYS`.
- [ ] **Step 6:** Run tests — expect PASS.
- [ ] **Step 7:** Commit `feat: rolling median pricebook with thin-bucket guard`.

---

### Task 6: Vinted client — session and search

**Files:**
- Create: `phoneloop/vinted_client.py`, `tests/test_vinted_client.py`

**Interfaces:**
- Produces: `VintedClient(base_url, session_factory)` with `handshake()`, `search(catalog_ids, per_page, order, **filters) -> list[dict]`, `fetch_public_page(path) -> str`, and exception `RateLimited`.
- On `429`: raise `RateLimited`. On session expiry (`401`/`403` on a previously working call): re-`handshake()` once, then retry.

- [ ] **Step 1:** Write `test_handshake_stores_cookies` against a stubbed session.
- [ ] **Step 2:** Write `test_429_raises_rate_limited`.
- [ ] **Step 3:** Write `test_403_triggers_single_rehandshake_then_retries` — asserts it re-handshakes exactly once, not in a loop.
- [ ] **Step 4:** Write `test_schema_guard_rejects_unexpected_payload` — a response missing `items` must raise, never return `[]` silently (silent empty looks like "no listings" and would hide an API change).
- [ ] **Step 5:** Run tests — expect FAIL.
- [ ] **Step 6:** Implement `vinted_client.py`. Inject the session so tests never hit the network.
- [ ] **Step 7:** Run tests — expect PASS.
- [ ] **Step 8:** Commit `feat: vinted client with rehandshake and schema guard`.

---

### Task 7: Verifier — public page description

**Files:**
- Create: `phoneloop/verifier.py`, `tests/test_verifier.py`

**Interfaces:**
- Consumes: `vinted_client.VintedClient`, `parser`.
- Produces: `verify(client, item:dict, spec:PhoneSpec) -> VerifyResult` with `description:str`, `photo_count:int`, `extra_disqualifiers:tuple[str,...]`, `ok:bool`.
- Extracts `"description":"..."` from the page's embedded JSON.
- **A failed fetch returns `ok=False`.** An unverified candidate is not a candidate.

- [ ] **Step 1:** Write `test_extracts_description_from_embedded_json` using a saved HTML fixture.
- [ ] **Step 2:** Write `test_fetch_failure_returns_not_ok` — never raises into the loop.
- [ ] **Step 3:** Write `test_description_disqualifier_flips_ok_false` — `"iCloud locked"` in description only.
- [ ] **Step 4:** Run tests — expect FAIL.
- [ ] **Step 5:** Implement `verifier.py`.
- [ ] **Step 6:** Run tests — expect PASS.
- [ ] **Step 7:** Commit `feat: candidate verifier reading the public item page`.

---

### Task 8: Alerter — Telegram

**Files:**
- Create: `phoneloop/alerter.py`, `tests/test_alerter.py`

**Interfaces:**
- Produces: `Alerter(token, chat_id, cfg, sender=None)` with `send(item, spec, verdict, reference) -> bool` and internal per-bucket rate limiting.
- Message carries model, storage, condition, landed price, reference median, discount %, favourite count, direct link.

- [ ] **Step 1:** Write `test_message_contains_landed_price_not_asking_price`.
- [ ] **Step 2:** Write `test_rate_limit_blocks_fourth_alert_in_same_bucket_hour` (`ALERTS_PER_BUCKET_HOUR=3`).
- [ ] **Step 3:** Write `test_send_failure_returns_false_and_does_not_raise`.
- [ ] **Step 4:** Run tests — expect FAIL.
- [ ] **Step 5:** Implement `alerter.py` with an injectable `sender` so tests never hit Telegram.
- [ ] **Step 6:** Run tests — expect PASS.
- [ ] **Step 7:** Commit `feat: telegram alerter with per-bucket rate limiting`.

---

### Task 9: Watcher — the loop

**Files:**
- Create: `phoneloop/watcher.py`, `tests/test_watcher.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `Watcher(client, store, pricebook, alerter, cfg)` with `tick() -> TickResult` (`seen`, `new`, `parsed`, `candidates`, `alerted`, `errors`) and `run(stop_event)`.
- `tick()` is one full pass, synchronous and testable. `run()` is just `tick()` on an interval with backoff.

- [ ] **Step 1:** Write `test_tick_alerts_on_underpriced_listing` — full wiring with stubs, one bargain in, one alert out.
- [ ] **Step 2:** Write `test_tick_does_not_alert_on_bait` — end-to-end proof the fraud floor survives integration.
- [ ] **Step 3:** Write `test_rate_limited_halves_poll_interval`.
- [ ] **Step 4:** Write `test_tick_never_raises` — every stub throws; `tick()` returns with `errors` populated and alerts nothing.
- [ ] **Step 5:** Run tests — expect FAIL.
- [ ] **Step 6:** Implement `watcher.py`.
- [ ] **Step 7:** Run tests — expect PASS.
- [ ] **Step 8:** Commit `feat: watch loop wiring parse, price, score, verify, alert`.

---

### Task 10: CLI and live smoke test

**Files:**
- Create: `phoneloop/__main__.py`, `README.md`
- Modify: `requirements.txt`

**Interfaces:**
- Produces: `python -m phoneloop watch` (the loop), `python -m phoneloop collect --once` (log-only, no alerts — the week-one mode), `python -m phoneloop stats` (bucket counts and coverage).

- [ ] **Step 1:** Implement `__main__.py` with argparse over the three subcommands. `collect` sets `alerts_enabled=False`.
- [ ] **Step 2:** Run `python -m phoneloop collect --once` against live Vinted. Expected: non-zero rows written, zero alerts.
- [ ] **Step 3:** Run `python -m phoneloop stats`. Expected: bucket table with sample counts.
- [ ] **Step 4:** Run the full suite `pytest -q`. Expected: all pass.
- [ ] **Step 5:** Write `README.md` covering setup, the three commands, and the week-one protocol.
- [ ] **Step 6:** Commit `feat: CLI with collect/watch/stats and README`.

---

## Self-Review

**Spec coverage:** §3 loop → Tasks 6,9. §4 data model → Task 4. §5 parsing → Task 2. §6 pricing → Tasks 1,5. §7 scoring → Task 3. §8 alerting → Task 8. §9 storage → Task 4. §10 error handling → Tasks 6,7,8,9 (each has a failure test). §11 week-one → Task 10 `collect` mode. Drive upload is **deliberately deferred** — local Parquet is the source of truth and §9 notes volume is trivial; adding it before there is data to archive would be premature.

**Type consistency:** `PhoneSpec` (Task 2) consumed by Tasks 3,5,7,8,9. `Verdict`/`Band` (Task 3) consumed by 8,9. `Store.record` (Task 4) consumed by 5,9. `VintedClient.search` (Task 6) consumed by 7,9. Names checked identical across all references.

**Placeholder scan:** none — every step names a file, a command, or concrete test data.
