# PhoneLoop

Watches Vinted's phone category and flags listings priced materially below the
going rate for that exact model, storage and condition.

Vinted's catalogue API answers anonymously — seven cookies from the homepage are
the whole handshake — and the list response carries condition, favourites, views
and the buyer fee. `total_item_price` already includes `service_fee`, so landed
cost is exact rather than modelled.

## Setup

```bash
pip install -r requirements.txt
python -m phoneloop collect --once
```

No credentials are needed to collect. Telegram alerts need two variables:

```bash
PHONELOOP_TELEGRAM_TOKEN=...
PHONELOOP_TELEGRAM_CHAT_ID=...
```

## Commands

| Command | What it does |
|---|---|
| `python -m phoneloop collect --once` | One pass, logged, no alerts |
| `python -m phoneloop collect` | Continuous collection, no alerts |
| `python -m phoneloop watch` | The live loop, alerts on |
| `python -m phoneloop stats` | Bucket coverage and medians |

## Week one: collect, don't buy

`stats` will say `0 buckets` until there are enough observations, and that is
correct rather than broken — a reference built on four listings is worse than no
reference, so the pricebook returns `None` below `MIN_BUCKET_SAMPLES` and nothing
is ever scored against it.

Running `collect` for a week answers three questions at once:

1. **How long does an underpriced phone survive?** Sets the required poll rate.
2. **What does the price distribution actually look like?** Calibrates the bands.
3. **How often is storage missing?** Measured at ~55% on the first live sample,
   which is more than the design assumed.

## How it works

```
01 WATCH    poll the category, newest first, dedupe on item id     ~25 KB
02 PARSE    title -> make, model, storage, disqualifiers            <1 ms
03 PRICE    rolling median for that bucket                          <1 ms
04 SCORE    banded discount with a fraud floor                      <1 ms
05 VERIFY   candidates only: public page, read description         1.9 MB
06 ALERT    Telegram with landed price, spread, condition, link      ~1 s
```

Stages 2-4 are pure functions, so latency is the poll interval plus one page
fetch. That makes the poll interval the only number worth tuning.

## Three rules worth knowing before changing anything

**Cheapness is not monotonic.** Past `BAND_BAIT` a low price stops meaning
bargain and starts meaning fraud, brick, or stolen. Scoring returns a *band*,
not a rank, and the cheapest listings are routed away from alerts. A
rank-by-discount feed surfaces scams first, every time.

**Storage is never guessed.** Around 55% of live listings omit it. Those price
against the *lowest* capacity tier, so their discount reads smaller than it may
be. That bias is deliberate — a missed deal costs nothing, a wrong one costs
money.

**Every failure degrades toward silence.** A fetch that fails, a bucket too thin,
a listing that won't parse, Telegram being down: all of them end in "no alert",
never in a guess. `tick()` cannot raise.

## Measured, not assumed

Verified against the live API on 2026-08-27:

- Catalogue API answers anonymously; item detail is auth-gated (403) but the
  public HTML page is not and carries the description.
- Category `3565` removes accessory noise — free-text "iphone 13" returns cases.
- `pagination.total_entries` caps at **960** even with filters, so wide
  enumeration needs sharding by model and price band.
- Rules parsing covers **78%** of the live feed (60% Apple-only, +18% from
  Samsung). `scripts/parser_accuracy.py` re-measures it on demand.
- The remaining 22% is mostly Google, Sony, Oppo and Xiaomi — the LLM tail, or
  the next manufacturers to add.

Still unproven: how fast an underpriced phone sells, what poll rate Vinted
tolerates, and what share of very cheap listings are fraud.

## Layout

```
phoneloop/
  config.py         every tunable; refuses to start with mis-ordered bands
  parser.py         pure: title -> PhoneSpec
  scorer.py         pure: price + reference -> banded Verdict
  pricebook.py      rolling median + MAD per bucket
  store.py          Parquet change-log, money as integer pence
  vinted_client.py  session, search, public page
  verifier.py       candidate second-look
  alerter.py        Telegram, rate limited per bucket
  watcher.py        the loop
```

Docs: `docs/superpowers/specs/` for the design, `docs/superpowers/plans/` for the
implementation plan.
