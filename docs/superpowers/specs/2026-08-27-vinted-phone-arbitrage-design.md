# Vinted Phone Loop — Design

**Date:** 2026-08-27
**Status:** Approved for planning
**Milestone:** 1 of 2 (Shopify gap finder deferred to milestone 2)

---

## 1. Goal

Continuously watch Vinted's phone category and surface listings priced materially
below the going rate for that exact model, storage and condition — fast enough to
act on them.

Success looks like: a Telegram alert arriving within a minute of a genuinely
underpriced phone being listed, carrying enough information to decide without
opening a browser, and being right often enough to be worth reading.

### Non-goals for this milestone

- No automated buying. Every purchase is a human decision.
- No selling automation. Listing on eBay/Vinted stays manual.
- No Shopify, eBay, Amazon or AliExpress ingestion — that is milestone 2.
- No non-phone categories. Phones only, because they have a liquid reference
  price and a parseable identity; neither is true of clothing.
- No CEX/eBay reference pricing in the first build. The bootstrap is Vinted's own
  distribution (§6). External references are a later refinement.

---

## 2. Why phones, and why Vinted

Verified live on 2026-08-27 (see §12 for the full evidence ledger):

- Vinted's internal catalogue API answers **anonymously** — seven cookies from the
  homepage are the whole handshake. No login, no key.
- The list response already carries condition, both price fields, the buyer fee,
  favourites, views, photos and seller id. Nothing else needs fetching to score.
- `total_item_price` includes `service_fee`, so **landed cost is exact, not
  modelled**. The buyer carries that fee, which is also why selling on Vinted is
  free.

Phones specifically dissolve the hardest problem from the previous design.
Cross-store product identity needed perceptual hashing and clustering; an iPhone 13
128GB is simply an iPhone 13 128GB, reachable by parsing text. And unlike dropship
gap-finding — which infers demand from stockouts and hopes the inference holds —
phones have an observable market price. We compare rather than predict.

---

## 3. Architecture

A tight watch loop, not a crawl. Everything expensive happens only to listings
that already look interesting.

```
 01 WATCH    poll /api/v2/catalog/items?catalog_ids=3565&order=newest_first
             dedupe on item id                                   ~25 KB/poll
 02 PARSE    title (+description when available) -> PhoneSpec         <1 ms
 03 PRICE    look up rolling reference for that bucket               <1 ms
 04 SCORE    banded discount vs reference, with a fraud floor        <1 ms
 05 VERIFY   candidates only: fetch public HTML, read description   1.9 MB/item
 06 ALERT    Telegram push with price, spread, condition, link        ~1 s
```

Stages 02–04 are pure local computation. End-to-end latency is therefore
**poll interval + one page fetch**, which makes the poll interval the only
number worth optimising.

### Components

Each is separately testable, with I/O pushed to the edges so the decision logic
stays pure.

| Module | Purpose | Depends on |
|---|---|---|
| `vinted_client.py` | Session lifecycle and API calls. Returns raw dicts. | `requests` |
| `parser.py` | `(title, description) -> PhoneSpec`. **Pure function, no I/O.** | — |
| `pricebook.py` | Maintains rolling distributions per bucket; answers `reference(spec)`. | `store` |
| `scorer.py` | `(spec, price, reference) -> Verdict`. **Pure function.** | — |
| `verifier.py` | Fetches the public item page, extracts description and photo count. | `vinted_client` |
| `alerter.py` | Formats and pushes to Telegram. | telegram bridge |
| `store.py` | Appends observations to Parquet; exposes DuckDB views. | `duckdb`, `pyarrow` |
| `watcher.py` | The loop. Wires the above together. | all |

`parser.py` and `scorer.py` being pure is deliberate — they hold the logic most
likely to be wrong, and purity makes them exhaustively unit-testable against
fixtures without touching the network.

---

## 4. Data model

Two tables, both append-only.

**`observation`** — one row per listing per time we see it.

| Column | Type | Note |
|---|---|---|
| `item_id` | int64 | Vinted's id |
| `seen_at` | timestamp | when we observed it |
| `title` | string | raw, unparsed |
| `price` | decimal | asking price |
| `total_item_price` | decimal | **landed cost, includes buyer fee** |
| `service_fee` | decimal | |
| `currency` | string | |
| `condition` | string | Vinted's `status` |
| `brand_title` | string | |
| `favourite_count` | int | |
| `view_count` | int | |
| `photo_count` | int | |
| `seller_id` | int64 | |
| `url` | string | |
| `content_hash` | uint64 | xxhash of the mutable fields |

Only write a row when `content_hash` differs from the last observation of that
`item_id`; otherwise bump `last_seen`. A listing disappearing from the feed is
recorded as a `vanished` event with its last-seen timestamp — that is the raw
material for the survival-time question in §11.

**`parse`** — one row per `item_id`, the parser's output.

| Column | Type | Note |
|---|---|---|
| `item_id` | int64 | |
| `make` | string | apple, samsung, google… |
| `model` | string | normalised, e.g. `iphone-13-pro` |
| `storage_gb` | int, nullable | **null is meaningful** — see §5 |
| `condition` | string | from Vinted, not the title |
| `network_lock` | string, nullable | unlocked / carrier name |
| `disqualifiers` | list[string] | spares, icloud_locked, cracked… |
| `parse_method` | string | `rules` or `llm` |
| `confidence` | float | |

---

## 5. Parsing

Real titles from the live feed, each a distinct failure mode:

```
£221.20  Very good     iPhone 13 128gb Midnight  Excellent Condition
£184.45  Very good     Apple iPhone 13 128GB Black Network Unlocked
£179.20  Very good     iPhone 13 White                      <- storage missing
£315.70  Very good     Apple I-Phone 13 Green Unlocked      <- storage missing
£368.20  Very good     iphone 13pro                         <- no space
£126.70  Good          Apple iPhone 13 RED - Spares & Repair   <- TRAP
```

**Rules first, model second.** A deterministic regex layer resolves the great
majority instantly and explainably. An LLM handles only the residue it cannot
parse. The reverse ordering would put a network call inside a loop whose entire
value is speed, and would spend money parsing "iPhone 13 128GB".

Every parse records `parse_method` so rules-vs-LLM accuracy can be compared later.

### Missing storage

Common, and consequential — 128GB and 256GB of the same model are materially
different prices. An unstated capacity is **not** guessed. `storage_gb` stays
null and the listing is priced against the **lowest** capacity tier for that
model, which makes its discount look smaller than it may truly be.

This deliberately biases toward missing a good deal rather than buying a
disappointment. Asymmetric, and correct: a missed deal costs nothing.

### Disqualifiers

Checked before anything else. Any hit removes the listing from buy alerts.

| Signal | Usually hides in | Cost of missing it |
|---|---|---|
| spares / repair / for parts / faulty | title | Broken phone at a working price |
| iCloud locked, Find My on | description, or nowhere | A brick — unresellable |
| cracked, screen burn, battery health | description, photos | Below its stated grade |
| network locked to a carrier | title or description | Materially lower resale |
| stock photos, zero-history seller | photos, seller profile | The listing is not real |

The last row needs the most care: fraud deliberately imitates the profile the
scoring hunts for.

---

## 6. Pricing

The reference price **bootstraps from Vinted's own distribution** — the rolling
median asking price for that exact `(make, model, storage_gb, condition)` bucket
over a trailing window. No external source is needed to begin, and it
self-corrects for Vinted's own price level.

**Median and median-absolute-deviation, never mean and standard deviation.** The
distribution is polluted at both ends — spares-or-repair handsets and outright
scams below, wishful pricing above — and a mean would chase every one of them.

A bucket needs a **minimum sample count** before it can price anything. Below
that threshold `reference()` returns `None` and the listing is logged but never
alerted on. This is why §11 opens with a week of collection.

### Starting parameters

Every one of these is a calibration target, not a finding. They exist so the
first build has concrete numbers rather than a blocked decision, and §11 replaces
them with measured values.

| Parameter | Start at | Replaced by |
|---|---|---|
| `POLL_INTERVAL_S` | 60 | Measured survival time and the poll ceiling |
| `PRICE_WINDOW_DAYS` | 30 | Whatever window makes buckets both fresh and populated |
| `MIN_BUCKET_SAMPLES` | 8 | Sensitivity analysis once distributions exist |
| `BAND_BUY_HIGH` | −15% | Real outcomes on real purchases |
| `BAND_BUY_LOW` | −45% | Real outcomes on real purchases |
| `BAND_BAIT` | −60% | Measured fraud rate in the tail |
| `ALERTS_PER_BUCKET_HOUR` | 3 | Observed alert volume |

All live in one config module. None are hardcoded at a call site.

eBay sold prices and CEX guaranteed buy prices are later refinements. CEX matters
most: a firm buy price converts margin from a forecast into arithmetic. It
returned `403` from this machine on 2026-08-27 and is unproven through
residential proxies.

---

## 7. Scoring

```
discount = (reference_median - total_item_price) / reference_median
```

**The score is a band, not a ranking.** Past a point, a low price stops meaning
bargain and starts meaning fraud, brick, or stolen. An iPhone 14 at £40 is bait,
not the find of the day.

| Band | Discount | Action |
|---|---|---|
| Bait | below −60% | Logged, routed **away** from buy alerts, reviewed separately |
| Buy signal | −45% to −15% | Verify, then alert |
| Fair | around median | Logged only |
| Overpriced | above median | Logged only |

A naive rank-by-discount feed would surface scams first, every single time.

Band edges are **opening guesses, not findings**. They get calibrated against real
outcomes once there is a week of listings and some actual purchases to score
against.

---

## 8. Alerting

Telegram, via the existing `@FacePalming_bot` bridge. Each alert carries model,
storage, condition, landed price, reference median, discount, favourite count and
a direct link — enough to decide without opening a browser.

Rate-limited per bucket so one mispriced reference cannot produce a flood.

---

## 9. Storage

Local Parquet, queried with DuckDB. Nightly batched upload to Google Drive using
the existing `gdrive.py` module from EV_Scraper — few, large, immutable files,
because Drive rate limits count requests rather than bytes.

**Volume is trivial for this milestone**: phone listings run to gigabytes a year,
not hundreds. The Drive quota pressure that dominated the previous design does not
apply here. The writer still enforces a **hard byte budget** so it can never
consume the EV client's backup headroom on the shared account.

---

## 10. Error handling

| Failure | Response |
|---|---|
| Session expires | Re-handshake from the homepage, resume. Log the interval — it is unknown. |
| Rate limited (`429`) | Exponential backoff, halve the poll rate, alert if sustained. |
| API shape changes | Schema check on every poll; halt and alert rather than write malformed rows. |
| Parser cannot resolve | Fall through to LLM; if that fails, log with `confidence=0` and never alert. |
| `reference()` returns None | Log the listing, do not alert. Never score against a thin bucket. |
| Verify fetch fails | Do not alert. An unverified candidate is not a candidate. |
| Drive upload fails | Keep local Parquet, retry next cycle. Local is the source of truth. |

The consistent rule: **every failure degrades toward silence, never toward a false
alert.** A missed deal costs nothing; a bad alert costs money and trust.

---

## 11. Week one — calibration

**Log everything, buy nothing.** One week of timestamped observations answers
three open questions simultaneously:

1. **How long does an underpriced phone survive?** Sets the required poll rate.
   Everything downstream depends on this number.
2. **What does the real price distribution look like?** Populates the pricebook
   and calibrates the band edges in §7.
3. **How often is storage missing?** Determines how much of the market the
   parser can actually price.

Alongside that:

- **Find the poll ceiling deliberately.** Push the rate until Vinted objects, on
  purpose, early, while nothing depends on it.
- **Hand-grade 100 parses** against real titles. Measured accuracy before a
  single pound is committed.
- **Attempt CEX through residential proxies.**

---

## 12. Evidence ledger

Separating what was measured from what is assumed matters more than the design
does — the estimates are where this is most likely to be wrong.

### Measured live, 2026-08-27

- Catalogue API answers anonymously: `200` with seven homepage cookies.
- List response carries condition, `total_item_price`, `service_fee`,
  `favourite_count`, `view_count`, photos, `user.id`.
- `/api/v2/items/{id}` → `404`; `/api/v2/items/{id}/details` → `403` (exists,
  auth-gated); public HTML `/items/{id}-slug` → `200` with description in
  embedded JSON.
- Category `3565` (mobile phones and communication) removes accessory noise;
  free-text "iphone 13" returns phone cases first.
- `pagination.total_entries` caps at **960** even with category and price filters
  applied — wide enumeration requires sharding by model and price band.

### Estimated

- Spreads of roughly £40 on nominally identical specs. **Suggestive only** — the
  cheaper listing did not state its storage, which is precisely the parsing
  problem this design has to solve.

### Unproven

- How long an underpriced phone survives before selling.
- What poll rate Vinted tolerates.
- What share of very cheap listings are fraud rather than opportunity.
- Whether CEX is reachable through residential proxies.

---

## 13. Risks

| Risk | Mitigation |
|---|---|
| Fraud imitates the target profile | Bait floor, disqualifier checks, seller history, photo checks |
| Parser misreads and we buy a broken phone | Hand-graded accuracy gate before any purchase; disqualifiers checked first |
| Vinted blocks the loop | Residential proxies available; back off on `429`; poll ceiling found deliberately |
| API changes shape without notice | Schema check per poll; halt and alert rather than write bad data |
| Thin buckets produce nonsense references | Minimum sample count; `None` rather than a guess |
| Crawler consumes the EV client's Drive quota | Hard byte budget in the writer |

---

## 14. Open questions

1. Does the arbitrage archive get its own Drive account, or stay on the shared one
   behind the byte budget? (Low stakes at this milestone's volume.)
2. Which phone makes beyond Apple are worth parsing first — Samsung is the obvious
   second, but its model naming is far messier.
3. Does the loop watch the whole `3565` category, or shard per model from the
   start? Sharding is required at scale because of the 960 cap, but may be
   unnecessary for newest-first watching.
