# eBay niche finder — independent verification

**Integration status from the overseer: NOT ACCEPTED.** This document proves,
with verbatim commands and output, exactly what works and what does not. I did
not write any of this code and trust nothing claimed about it — everything
below was run.

Repo: `C:/Users/sampo/OneDrive/Desktop/Python projects/PhoneLoop`
Branch: `feat/ebay-niche-finder`
HEAD: `a0e2d4585ab9584b3352ce033315df8966277fa6`

Environment for every run in this document: `PHONELOOP_FIXTURES=C:/Users/sampo/.phoneloop/fixtures`,
`PHONELOOP_HARNESS` unset (replay, no network), `PHONELOOP_DATA_DIR=C:/Users/sampo/.phoneloop/proof-e2e-20260828-042649`
(a fresh temp directory created for this verification, not committed).

**Headline result: the two-part offline end-to-end command that the build
contract names as the acceptance test does not run to completion.**
`scripts/bulk_ebay.py --leaf-sweep --max-pages 3 --fresh` raises
`phoneloop.harness.MissingRecording` on the first shard, and the fallback
keyword-mode sweep does too — both for a reason the code's own docstring
already documents (§3). `scripts/niche_board.py` therefore never sees a
per-niche real sweep and prints `(no rows)` (§4). The full unit/contract test
suite is green (660/660, §2), and the row-parsing/store/fee/metrics layers
underneath the broken CLI wiring do work correctly against real recorded
bytes when exercised directly (§3, §5, §6, §7) — but the thing the contract
actually promises as "offline end-to-end on real recordings" is not there.
Additionally, the entire s3 API-transport stream (`phoneloop/ebay_api.py`) is
absent from this branch (§8) — not merged, not partially wired, simply not
present.

---

## 1. Baseline

```
$ git log --oneline master..HEAD
```

```
a0e2d45 docs(run_deepen): flag the same-day assumption behind seen_at cleared to None
9b01dd6 fix(integration round 2): ship the Tier-2 deepen driver, fix the extra-JSON clobber, align sweep/record URL+key, correct a guard test's own docstring
a52d963 fix(integration): guard the top_level_of fee wiring, fix lifetime sell-through denominator, correct run_daily.cmd's overstated claim
e222b37 Integrate eBay niche finder: merge 5 accepted streams, wire cross-stream joins
fdfa71c Merge branch 'build/s5_snapshots_board' into feat/ebay-niche-finder
21bf598 Merge branch 'build/s6_category_scope' into feat/ebay-niche-finder
16c13bc Merge branch 'build/s2_html_transport' into feat/ebay-niche-finder
ef1174b Merge branch 'build/s4_identity_metrics' into feat/ebay-niche-finder
893e6c8 Merge branch 'build/s1_store_fees' into feat/ebay-niche-finder
00a3c88 fix(s2 rework round 1): correct origin_country map + false seller_type comment, plus cheap nits
4cfd049 feat(bulk_ebay): direct-first egress, drop LH_LocatedIn, leaf-sweep mode, harness-honouring
c37e573 feat(s5): Tier 3 differencing, niche board, Terapeak validation table, daily runner
1e626c1 feat(ebay_item): new parser for item pages -- sold/available, seller, store, specifics
405ac5f feat(s6): eBay category scope - in-scope departments, leaf enumeration, live tree
8552099 feat(ebay): extend search-row parser -- sold count, seller, promoted, ETA, decoy, stock hints, to_row
69c12ed Merge branches 'build/s1_store_fees' and 'build/s4_identity_metrics' into build/s5_snapshots_board
4726f48 s1 rework round 2: fix available-defaults-False bug, false comments, reindex rows_on
90dec90 feat(harness): PHONELOOP_FIXTURES root override; record_ebay.py records real eBay pages
94ac5ef s1 rework: fix UTC timezone bug in snapshot_dates/rows_on, correct false docstring, address nits
4a61919 test(s4): tighten the str_threshold_bp caveat guard
a35707c fix(s4): min_margin note/denominator match the basis actually used
20e03a6 s1: store columns, snapshot semantics, fee model, per-source cap
5c1bc35 feat(s4): identity tiers, denominator-carrying metrics, and control knobs
```

```
$ git diff master --stat
```

```
 ebay_categories.json               | 236 ++++++++++++++
 phoneloop/board.py                 | 377 +++++++++++++++++++++
 phoneloop/catalog.py               |  35 +-
 phoneloop/catalog_ebay.py          | 217 +++++++++++++
 phoneloop/ebay.py                  | 351 +++++++++++++++++++-
 phoneloop/ebay_item.py             | 334 +++++++++++++++++++
 phoneloop/fees.py                  | 208 ++++++++++++
 phoneloop/harness.py               |  29 +-
 phoneloop/identity.py              | 197 +++++++++++
 phoneloop/knobs.py                 | 114 +++++++
 phoneloop/market_store.py          | 266 ++++++++++++---
 phoneloop/metrics.py               | 453 ++++++++++++++++++++++++++
 phoneloop/snapshots.py             | 151 +++++++++
 phoneloop/validation.py            | 167 ++++++++++
 scripts/bulk_ebay.py               | 651 +++++++++++++++++++++++++++----------
 scripts/niche_board.py             | 116 +++++++
 scripts/record_ebay.py             | 233 +++++++++++++
 scripts/refresh_ebay_categories.py | 454 ++++++++++++++++++++++++++
 scripts/run_daily.cmd              | 109 +++++++
 tests/test_board.py                | 238 ++++++++++++++
 tests/test_bulk_ebay_deepen.py     |  77 +++++
 tests/test_catalog_cap.py          |  60 ++++
 tests/test_catalog_ebay.py         | 239 ++++++++++++++
 tests/test_ebay.py                 | 347 ++++++++++++++++++++
 tests/test_ebay_item.py            | 328 +++++++++++++++++++
 tests/test_fees.py                 | 226 +++++++++++++
 tests/test_harness.py              |  15 +
 tests/test_identity.py             | 222 +++++++++++++
 tests/test_knobs.py                | 121 +++++++
 tests/test_metrics.py              | 522 +++++++++++++++++++++++++++++
 tests/test_snapshots.py            | 150 +++++++++
 tests/test_store.py                | 299 +++++++++++++++++
 tests/test_validation.py           | 119 +++++++
 33 files changed, 7437 insertions(+), 224 deletions(-)
```

Notably absent from that file list: `phoneloop/ebay_api.py`, the module the
build contract names in full (§2.7) for the Browse API transport. See §8.

---

## 2. Tests

```
$ cd "C:/Users/sampo/OneDrive/Desktop/Python projects/PhoneLoop" \
  && unset PHONELOOP_HARNESS \
  && export PHONELOOP_FIXTURES=C:/Users/sampo/.phoneloop/fixtures \
  && export PHONELOOP_DATA_DIR=C:/Users/sampo/.phoneloop/proof-e2e-20260828-042649 \
  && "$LOCALAPPDATA/Programs/Python/Python313/python.exe" -m pytest -q -p no:cacheprovider
```

```
........................................................................ [ 10%]
........................................................................ [ 21%]
........................................................................ [ 32%]
........................................................................ [ 43%]
........................................................................ [ 54%]
........................................................................ [ 65%]
........................................................................ [ 76%]
........................................................................ [ 87%]
........................................................................ [ 98%]
............                                                             [100%]
660 passed in 6.82s
```

**660 / 660 passed.**

---

## 3. Offline end-to-end on real recordings

### 3.1 The contract's exact command — fails

The command is §6.3 of `docs/superpowers/plans/2026-08-27-ebay-niche-finder-contracts.md`.
Pytest already ran green above; running the remaining steps of that same chain:

```
$ cd "C:/Users/sampo/OneDrive/Desktop/Python projects/PhoneLoop" \
  && unset PHONELOOP_HARNESS \
  && export PHONELOOP_FIXTURES=C:/Users/sampo/.phoneloop/fixtures \
  && export PHONELOOP_DATA_DIR=C:/Users/sampo/.phoneloop/proof-e2e-20260828-042649 \
  && "$LOCALAPPDATA/Programs/Python/Python313/python.exe" scripts/refresh_ebay_categories.py --offline
```

```
fixtures: C:\Users\sampo\.phoneloop\fixtures
harness: replay, 57 ebay_categories recordings
in-scope departments: 9, up to 5 leaves each

    ! Appliances                     no numeric category id found on its own page (none of the known shapes matched)
  Home, Furniture & DIY             29 subcats seen,  4 leaves resolved to a real numeric id
  Garden & Patio                    23 subcats seen,  5 leaves resolved to a real numeric id
  Pet Supplies                      13 subcats seen,  5 leaves resolved to a real numeric id
  Sporting Goods                    53 subcats seen,  5 leaves resolved to a real numeric id
    ! Car parts & accessories        no numeric category id found on its own page (none of the known shapes matched)
  Vehicle Parts & Accessories       27 subcats seen,  4 leaves resolved to a real numeric id
  Mobile Phones & Communication     13 subcats seen,  5 leaves resolved to a real numeric id
  Crafts                            22 subcats seen,  5 leaves resolved to a real numeric id
  Health & Beauty                   19 subcats seen,  5 leaves resolved to a real numeric id
  Toys & Games                      21 subcats seen,  5 leaves resolved to a real numeric id

written to C:\Users\sampo\OneDrive\Desktop\Python projects\PhoneLoop\ebay_categories.json

leaves per in-scope department:
  Home, Furniture & DIY             4 /  29 subcats seen
  Garden & Patio                    5 /  23 subcats seen
  Pet Supplies                      5 /  13 subcats seen
  Sporting Goods                    5 /  53 subcats seen
  Vehicle Parts & Accessories       4 /  27 subcats seen
  Mobile Phones & Communication     5 /  13 subcats seen
  Crafts                            5 /  22 subcats seen
  Health & Beauty                   5 /  19 subcats seen
  Toys & Games                      5 /  21 subcats seen

43 leaves total across 9 departments
```

This step works and is real: 43 leaf category ids resolved from 57 real
`ebay_categories/*.json.gz` recordings (recorded 2026-08-28, per their file
mtimes). It rewrites the committed `ebay_categories.json`'s `_generated_at`
timestamp (a 1-line diff, confirmed with `git diff ebay_categories.json` and
reverted with `git checkout -- ebay_categories.json` before §7's clean-tree
requirement — no functional content changed).

```
$ "$LOCALAPPDATA/Programs/Python/Python313/python.exe" scripts/bulk_ebay.py --leaf-sweep --max-pages 3 --fresh
```

```
harness: replay, 7 ebay recordings under C:\Users\sampo\.phoneloop\fixtures
43 shards to sweep (leaf mode)
Traceback (most recent call last):
  File "C:\Users\sampo\OneDrive\Desktop\Python projects\PhoneLoop\scripts\bulk_ebay.py", line 565, in <module>
    raise SystemExit(main())
                     ~~~~^^
  File "C:\Users\sampo\OneDrive\Desktop\Python projects\PhoneLoop\scripts\bulk_ebay.py", line 469, in main
    raw = recorded(cassette, "ebay", key,
                   lambda: fetch_page(page, url))
  File "C:\Users\sampo\OneDrive\Desktop\Python projects\PhoneLoop\phoneloop\harness.py", line 365, in recorded
    raise MissingRecording(
    ...<4 lines>...
    )
phoneloop.harness.MissingRecording: no recording for ebay/sacat:26677:15-55:p1
  key: sacat-26677-15-55-p1-c208bee2
  capture it with: PHONELOOP_HARNESS=record python scripts/capture.py
```

**This is exactly what the contract says must happen when a recording is
missing (§6.3: "It fails if any step produces an empty result where a
recording is missing... `MissingRecording` is [the correct answer]").** The
harness rail is doing its job. The problem is that no matching leaf-sweep
recording exists at all under `fixtures/ebay/` — only 7 files, all from a
*keyword* search on "iphone 13 128gb" / "iphone 13 mini 128gb" / "samsung
galaxy s21 128gb" / "wireless charger", none in the `sacat:`-keyed leaf-sweep
shape the 43-shard `--leaf-sweep` needs.

I also tried the keyword-mode fallback the script's own docstring says is
"fully exercisable today":

```
$ "$LOCALAPPDATA/Programs/Python/Python313/python.exe" scripts/bulk_ebay.py --category phones --max-pages 3 --fresh
```

```
harness: replay, 7 ebay recordings under C:\Users\sampo\.phoneloop\fixtures
6 shards to sweep (keyword mode)
Traceback (most recent call last):
  File "C:\Users\sampo\OneDrive\Desktop\Python projects\PhoneLoop\scripts\bulk_ebay.py", line 565, in <module>
    raise SystemExit(main())
                     ~~~~^^
  File "C:\Users\sampo\OneDrive\Desktop\Python projects\PhoneLoop\scripts\bulk_ebay.py", line 469, in main
    raw = recorded(cassette, "ebay", key,
                   lambda: fetch_page(page, url))
  File "C:\Users\sampo\OneDrive\Desktop\Python projects\PhoneLoop\phoneloop\harness.py", line 365, in recorded
    raise MissingRecording(
    ...<4 lines>...
    )
phoneloop.harness.MissingRecording: no recording for ebay/nkw:iphone:15-2000:p1
  key: nkw-iphone-15-2000-p1-5059e315
  capture it with: PHONELOOP_HARNESS=record python scripts/capture.py
```

Also raises immediately, for the same root cause. `scripts/bulk_ebay.py`'s
own module docstring already names this exact gap in full:

> "a fresh offline sweep against fixtures/ebay/ still raises
> `MissingRecording` on the first shard: those recordings were captured
> before this fix, under the old shape. A live recording session with the
> aligned scripts is what closes that gap; see this build's unverified[]."

I confirmed the mechanism precisely: the 7 real recordings under
`fixtures/ebay/` were captured under an OLD key shape
(`search:<term>:pN` → `search-iphone-13-128gb-p1-<hash>`, verified by
recomputing `harness.recorded`'s own `slug(name) + "-" + sha1(name)[:8]` rule
against the on-disk filename in §3.2 below), while `phoneloop.ebay.search_key`
today builds `sacat:`/`nkw:`-shaped keys. **The offline end-to-end command the
contract names as the acceptance test cannot run to completion against the
recordings that currently exist. This is the concrete, reproducible reason
the overseer marked the integration NOT accepted.**

### 3.2 What does work: the row pipeline, run directly against the same real recordings

`scripts/bulk_ebay.py`'s CLI wiring is broken (§3.1), but the modules
underneath it — `phoneloop.harness`, `phoneloop.ebay.parse_rows`,
`phoneloop.ebay.to_row`, `phoneloop.market_store.MarketStore` — are not. To
prove that, I called them directly against the three real, on-disk
`fixtures/ebay/search-iphone-13-128gb-p{1,2,3}-*.json.gz` recordings, using
their real capture-time key shape. No network is touched anywhere in this
(mode stays `replay`); this is a direct exercise of the accepted-stream code,
not a substitute for the contract's own command, and I'm calling it out as
such rather than passing it off as the acceptance test.

First, confirmed the key-shape match (this is `phoneloop.harness.recorded`'s
own key-derivation rule, run against the three real recording names):

```
$ "$LOCALAPPDATA/Programs/Python/Python313/python.exe" -c "
import hashlib, re
def slug(s):
    s = s.strip().lower()
    s = re.sub(r'[^a-z0-9]+', '-', s)
    return s.strip('-')
names = ['search:iphone 13 128gb:p1', 'search:iphone 13 128gb:p2', 'search:iphone 13 128gb:p3']
for n in names:
    print(n, '->', slug(n), hashlib.sha1(n.encode()).hexdigest()[:8])
"
```

```
search:iphone 13 128gb:p1 -> search-iphone-13-128gb-p1 1d920422
search:iphone 13 128gb:p2 -> search-iphone-13-128gb-p2 19ed97bf
search:iphone 13 128gb:p3 -> search-iphone-13-128gb-p3 7e5ce07a
```

Matches the on-disk filenames exactly
(`search-iphone-13-128gb-p1-1d920422.json.gz`, `...-p2-19ed97bf...`,
`...-p3-7e5ce07a...`) — confirming these are genuinely the pages recorded
under those names, not a coincidence.

Script run (`direct_pipeline.py`, full source below the output):

```
$ cd "C:/Users/sampo/OneDrive/Desktop/Python projects/PhoneLoop" \
  && unset PHONELOOP_HARNESS \
  && export PHONELOOP_FIXTURES=C:/Users/sampo/.phoneloop/fixtures \
  && export PHONELOOP_DATA_DIR=C:/Users/sampo/.phoneloop/proof-e2e-20260828-042649 \
  && "$LOCALAPPDATA/Programs/Python/Python313/python.exe" direct_pipeline.py
```

```
'search:iphone 13 128gb:p1': 87 raw rows, 87 parsed listings, 87 rows written
'search:iphone 13 128gb:p2': 72 raw rows, 72 parsed listings, 72 rows written
'search:iphone 13 128gb:p3': 60 raw rows, 60 parsed listings, 60 rows written
TOTAL rows written: 219
```

**219 rows written**, all from `MarketStore.record()` on the real project
path (`$PHONELOOP_DATA_DIR/market/date=2026-08-28/source=ebay/*.parquet`,
three files — `ebay` is in `ALWAYS_SNAPSHOT_SOURCES`, so every observation
lands, hash or not).

`direct_pipeline.py` (written to a scratch path, run from the repo so its
`sys.path.insert` resolves `phoneloop`):

```python
"""Exercise the real row pipeline (harness replay -> ebay.parse_rows -> ebay.to_row
-> MarketStore.record) directly against the seven real search recordings that
already exist under fixtures/ebay/, bypassing scripts/bulk_ebay.py's CLI wiring.

Why this script exists, and why it is not the contract's own command: the
contract's `bulk_ebay.py --leaf-sweep` / `--category phones` sweep derives its
harness key from `phoneloop.ebay.search_key`, which was realigned during
integration (see bulk_ebay.py's own module docstring) to a `sacat:`/`nkw:`
shape. The seven recordings under fixtures/ebay/ predate that realignment and
were captured under the OLD `search:<term>:pN` key shape (verified below by
reconstructing the exact on-disk filename from harness.recorded()'s own slug+
sha1 rule) -- so the CLI raises MissingRecording on shard 1, which is the real,
current, NOT ACCEPTED state of the integration, not a bug in this proof.

This script proves the row-shape / parser / store layer underneath that CLI
gap still functions correctly on real captured bytes: no network is touched
(mode is replay throughout, PHONELOOP_HARNESS stays unset), every byte parsed
comes from a real recorded eBay response, and the rows land in the same
MarketStore/parquet layout the accepted contract describes.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, r"C:/Users/sampo/OneDrive/Desktop/Python projects/PhoneLoop")

from phoneloop import harness
from phoneloop.ebay import parse_rows, to_row
from phoneloop.market_store import MarketStore

FIXTURES = os.environ["PHONELOOP_FIXTURES"]
DATA_DIR = os.environ["PHONELOOP_DATA_DIR"]

assert harness.mode() == harness.REPLAY, f"expected replay, got {harness.mode()!r}"

cassette = harness.Cassette(FIXTURES)

# The three real, on-disk search-result recordings for one term (pages 1-3),
# named exactly as scripts/record_ebay.py captured them on 2026-08-27.
NAMES = [
    "search:iphone 13 128gb:p1",
    "search:iphone 13 128gb:p2",
    "search:iphone 13 128gb:p3",
]

store = MarketStore(DATA_DIR)
total_written = 0

for name in NAMES:
    def _no_network():
        raise RuntimeError("replay must never call produce()")
    raw_rows = harness.recorded(cassette, "ebay", name, _no_network)
    listings = parse_rows(raw_rows, today=None)
    rows = [to_row(l, category="phones", shard=name) for l in listings]
    n = store.record(rows)
    total_written += n
    print(f"{name!r}: {len(raw_rows)} raw rows, {len(listings)} parsed listings, {n} rows written")

print(f"TOTAL rows written: {total_written}")
```

### 3.3 10 real rows, queried from the temp store with pyarrow

```
$ cd "C:/Users/sampo/OneDrive/Desktop/Python projects/PhoneLoop" \
  && export PHONELOOP_DATA_DIR=C:/Users/sampo/.phoneloop/proof-e2e-20260828-042649 \
  && "$LOCALAPPDATA/Programs/Python/Python313/python.exe" query_rows.py
```

```python
"""Print 10 real rows from the temp MarketStore written by direct_pipeline.py,
via pyarrow.dataset over the parquet files under $PHONELOOP_DATA_DIR/market.
"""
import os
import pyarrow.dataset as ds

DATA_DIR = os.environ["PHONELOOP_DATA_DIR"]
market_dir = os.path.join(DATA_DIR, "market")

dataset = ds.dataset(market_dir, format="parquet", partitioning="hive")
table = dataset.to_table(
    columns=["source_id", "title", "price_pence", "sold_qty", "seller_id",
             "seller_type", "epid"]
)
print(f"total rows in store: {table.num_rows}")
sub = table.slice(0, 10).to_pylist()
for i, row in enumerate(sub):
    title = (row["title"] or "")[:60]
    print(f"{i}: id={row['source_id']} price_pence={row['price_pence']} "
          f"sold_qty={row['sold_qty']} seller_id={row['seller_id']!r} "
          f"seller_type={row['seller_type']!r} epid={row['epid']!r} "
          f"title={title!r}")
```

Output:

```
total rows in store: 219
0: id=225236871939 price_pence=20999 sold_qty=552 seller_id='the_ioutlet_extra' seller_type=None epid='12049274306' title='Apple iPhone 13 128GB/256/512 - ALL COLOURS - UNLOCKED - GOO'
1: id=406124792115 price_pence=18999 sold_qty=76 seller_id='sands-phones' seller_type=None epid='8050688139' title='Apple iPhone 13 / 128GB /Midnight Color/Unlocked Excellent C'
2: id=407173705482 price_pence=15999 sold_qty=None seller_id='sands-phones' seller_type=None epid='5053757278' title='NEW LISTINGApple iPhone 13 / 128GB /Alpine Green Color/ Exce'
3: id=406208020069 price_pence=17999 sold_qty=32 seller_id='sands-phones' seller_type=None epid=None title='Apple iPhone 13/128GB /STARLIGHT Color/Unlocked Excellent Co'
4: id=128047213718 price_pence=16000 sold_qty=None seller_id='stay_free77' seller_type=None epid='16052764082' title='NEW LISTINGApple iPhone 13 - 128GB - Pink (Unlocked) - 76% B'
5: id=318787883180 price_pence=12855 sold_qty=None seller_id='bl_850779' seller_type=None epid=None title='NEW LISTINGiPhone 13'
6: id=225236807512 price_pence=22999 sold_qty=129 seller_id='the_ioutlet_extra' seller_type=None epid=None title='Apple iPhone 13 128GB/256/512 - ALL COLOURS - UNLOCKED - VER'
7: id=225236871939 price_pence=20999 sold_qty=None seller_id=None seller_type=None epid='12049274306' title='Apple iPhone 13 128GB/256/512 - ALL COLOURS - UNLOCKED - GOO'
8: id=225236807512 price_pence=22999 sold_qty=None seller_id=None seller_type=None epid=None title='Apple iPhone 13 128GB/256/512 - ALL COLOURS - UNLOCKED - VER'
9: id=116756767948 price_pence=25999 sold_qty=None seller_id=None seller_type=None epid='15049284953' title='Apple iPhone 13 - All Sizes - All Colours - Unlocked - VERY '
```

`seller_type` is `None` on every row because these are search-result rows
(Tier 1 sweep only); `seller_type` is a Tier-2 field the item-page parser
(`ebay_item.py`) fills, which `--deepen` was not run in this proof (§8). Rows
0/7 and 6/8 are the same `source_id` seen with different completeness across
pages 1–2 — both real observations, both written, because `ebay` is an
always-snapshot source (§1.3 of the contract).

---

## 4. The board

Run against the temp store populated in §3 (219 real rows, all from a
keyword-mode phone search, none carrying a `leaf_category_id`):

```
$ cd "C:/Users/sampo/OneDrive/Desktop/Python projects/PhoneLoop" \
  && unset PHONELOOP_HARNESS \
  && export PHONELOOP_FIXTURES=C:/Users/sampo/.phoneloop/fixtures \
  && export PHONELOOP_DATA_DIR=C:/Users/sampo/.phoneloop/proof-e2e-20260828-042649 \
  && "$LOCALAPPDATA/Programs/Python/Python313/python.exe" scripts/niche_board.py --products --html
```

```
only one snapshot date (2026-08-28) -- every sell-through figure below is 'lifetime', not 'window'

=== NICHES ===
(no rows)

=== PRODUCTS (within the ranked niches above) ===
(no rows)

wrote C:\Users\sampo\.phoneloop\proof-e2e-20260828-042649\reports\niche-board-20260828T033020Z.html
```

**`(no rows)`, both tables.** `board.rank_niches` groups the store's rows by
`leaf_category_id` (contract §2.13), then `scripts/niche_board.py` filters to
`catalog_ebay.top_level_of()`'s known real leaves. The 219 real rows in the
store are all keyword-mode search rows with `leaf_category_id = None`
(§1.1, column 34: "unknown" is the correct value for a row a keyword sweep
produced), so nothing survives the filter. This is the direct, structural
consequence of §3's finding: no real leaf-sweep recording exists to populate
even one real niche row, so the board — code that itself works, per the tests
below — has nothing real to rank.

**Confirming "every STR prints its coverage" and "max_cost values are
integers" by inspection**, since no real populated board row exists to paste:

`phoneloop/board.py`, `render_table` (lines 281–287) iterates every
`KnobResult` in `r.knobs` — including the sell-through knob — through one
shared format string that always prints `denom=` and `cov=`:

```python
        for k in r.knobs:
            lines.append(
                f"          {k.knob:<14} value={_fmt(k.value):<16} "
                f"threshold={_fmt(k.threshold):<18} "
                f"[{_fmt_pass(k.passed)}] denom={k.denominator} cov={k.coverage:.0%}"
                + (f" -- {k.note}" if k.note else "")
            )
```

There is no per-knob branch that skips this for `sell_through` — every
`KnobResult`, sell-through included, carries `denominator`/`coverage` fields
(contract §2.10) and every one gets printed through this one path. This is
also exercised (with test data, not real listings — `tests/test_board.py`,
part of the 660 passing in §2) end to end.

`max_cost` is typed `-> int` throughout and built only from integer floor
division (`phoneloop/fees.py::bp`, `(x * n + 5000) // 10000`); confirmed live:

```
$ "$LOCALAPPDATA/Programs/Python/Python313/python.exe" -c "
import sys; sys.path.insert(0, '.')
from phoneloop import fees
from phoneloop.knobs import Knobs
k = Knobs()
mc = fees.max_cost(3000, None, k)
print('max_cost(3000, None, default knobs) =', mc, type(mc))
mc2 = fees.max_cost(50, None, k)
print('max_cost(50, None, default knobs) =', mc2, type(mc2))
"
```

```
max_cost(3000, None, default knobs) = 1724 <class 'int'>
max_cost(50, None, default knobs) = -594 <class 'int'>
```

Both a positive and a negative result, both `<class 'int'>` — negative is the
documented, intentional "no supplier price clears the margin" answer
(fees.py docstring), never clamped to 0, never a float.

---

## 5. Rail 1 — missing recording raises

```
$ cd "C:/Users/sampo/OneDrive/Desktop/Python projects/PhoneLoop" \
  && unset PHONELOOP_HARNESS \
  && export PHONELOOP_FIXTURES=C:/Users/sampo/.phoneloop/fixtures \
  && "$LOCALAPPDATA/Programs/Python/Python313/python.exe" -c "
import sys; sys.path.insert(0, '.')
from phoneloop import harness
cassette = harness.Cassette(r'C:/Users/sampo/.phoneloop/fixtures')
harness.recorded(cassette, 'ebay', 'search:definitely not a real recording xyz123:p99', lambda: (_ for _ in ()).throw(RuntimeError('must not be called in replay')))
"
```

```
Traceback (most recent call last):
  File "<string>", line 5, in <module>
    harness.recorded(cassette, 'ebay', 'search:definitely not a real recording xyz123:p99', lambda: (_ for _ in ()).throw(RuntimeError('must not be called in replay')))
    ~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\sampo\OneDrive\Desktop\Python projects\PhoneLoop\phoneloop\harness.py", line 365, in recorded
    raise MissingRecording(
    ...<4 lines>...
    )
phoneloop.harness.MissingRecording: no recording for ebay/search:definitely not a real recording xyz123:p99
  key: search-definitely-not-a-real-recording-xyz123-p9-65f17b69
  capture it with: PHONELOOP_HARNESS=record python scripts/capture.py
```

`phoneloop.harness.MissingRecording` raised, not an empty result — and the
`produce` lambda (which would raise a different, network-shaped error if
ever called) was never invoked, confirming replay genuinely touches nothing.
This is also what happened for real, unprompted, in §3.1 — the rail fired on
the actual e2e run, not just in this isolated one-liner.

---

## 6. Rail 2 — unknown is not zero

```
$ cd "C:/Users/sampo/OneDrive/Desktop/Python projects/PhoneLoop" \
  && "$LOCALAPPDATA/Programs/Python/Python313/python.exe" -c "
import sys; sys.path.insert(0, '.')
from phoneloop.market_store import empty_row
from phoneloop import metrics

r1 = empty_row('ebay', 'item-with-sold')
r1['sold_qty'] = 5
r2 = empty_row('ebay', 'item-unknown-sold')
r2['sold_qty'] = None  # unknown -- must NOT be treated as 0

m = metrics.compute([r1, r2])
print('n_listings =', m.n_listings)
print('sold_known =', m.sold_known)
print('coverage =', m.coverage)
print('str_lifetime =', m.str_lifetime, 'denominator =', m.str_lifetime_denominator)
"
```

```
n_listings = 2
sold_known = 1
coverage = 0.5
str_lifetime = 5.0 denominator = 1
```

Two rows in, one with `sold_qty=None`. `sold_known = 1` (the unknown row is
excluded, not counted as a zero-sale row), `coverage = 0.5` (1 known of 2
listings), and `str_lifetime = 5.0` — the sum (5) divided by the *known*
count (1), not by `n_listings` (2). If the unknown row were wrongly treated
as a 0, `str_lifetime` would read `2.5`, not `5.0`.

---

## 7. Rail 3 — guards go red

`git status` clean of tracked changes first (the only tracked diff at this
point was §3.1's `ebay_categories.json` timestamp, already reverted with
`git checkout -- ebay_categories.json`):

```
$ git status --short
```

```
?? .claude/
?? body.html
?? graphify-out/
?? shard.xml.gz
?? vitems.xml
```

(Untracked files pre-existing in this checkout, outside this task's
ownership list — not created, touched, or inspected by this proof. See §8.)
No tracked file carries a diff. HEAD is `a0e2d45` throughout — the working
state was already committed before any regression was injected.

**Regression injected**, `phoneloop/metrics.py`, the exact line the "missing
sold is unknown, not zero" rule (contract §2.10, rule 1) depends on:

```diff
-    known_sold = [(r, r["sold_qty"]) for r in rows if r.get("sold_qty") is not None]
+    # DELIBERATE PROOF REGRESSION (verifier subagent, 2026-08-28): treats a
+    # missing sold_qty as a real 0-sale observation instead of excluding it.
+    known_sold = [(r, r.get("sold_qty") or 0) for r in rows]
```

Full suite, same environment as §2:

```
$ "$LOCALAPPDATA/Programs/Python/Python313/python.exe" -m pytest -q -p no:cacheprovider
```

```
FAILED tests/test_metrics.py::TestCoverageGuard::test_halving_known_denominator_changes_str_lifetime_and_coverage
FAILED tests/test_metrics.py::TestMissingSoldExcluded::test_a_row_with_sold_qty_none_is_excluded_not_counted_as_zero
FAILED tests/test_metrics.py::TestVelocity::test_velocity_day1_sums_each_known_listings_own_rate
FAILED tests/test_metrics.py::TestDispersion::test_dispersion_is_sellers_with_sales_over_sellers_with_known_sold_qty
4 failed, 656 passed in 8.07s
```

Failure detail for the two guard tests written specifically for this rule:

```
>       assert x.sold_known == 2 and y.sold_known == 1
E       assert (4 == 2)
tests\test_metrics.py:78: AssertionError
```

```
>       assert m.sold_known == 2
E       assert 3 == 2
tests\test_metrics.py:102: AssertionError
```

**4 tests genuinely went red**, watched directly:
`TestCoverageGuard::test_halving_known_denominator_changes_str_lifetime_and_coverage`,
`TestMissingSoldExcluded::test_a_row_with_sold_qty_none_is_excluded_not_counted_as_zero`,
`TestVelocity::test_velocity_day1_sums_each_known_listings_own_rate`,
`TestDispersion::test_dispersion_is_sellers_with_sales_over_sellers_with_known_sold_qty`.

Revert:

```
$ git checkout -- phoneloop/metrics.py && git status --short
```

```
?? .claude/
?? body.html
?? graphify-out/
?? shard.xml.gz
?? vitems.xml
```

Re-run, full suite:

```
$ "$LOCALAPPDATA/Programs/Python/Python313/python.exe" -m pytest -q -p no:cacheprovider
```

```
........................................................................ [ 10%]
........................................................................ [ 21%]
........................................................................ [ 32%]
........................................................................ [ 43%]
........................................................................ [ 54%]
........................................................................ [ 65%]
........................................................................ [ 76%]
........................................................................ [ 87%]
........................................................................ [ 98%]
............                                                             [100%]
660 passed in 9.03s
```

```
$ git diff --stat
```

```
(no output — empty)
```

`injection_went_red = true` (4 real failures observed, with assertion
detail). `injection_reverted = true` (660/660 green again, `git diff` empty,
`git status` shows only the same pre-existing untracked files that were
there before any of this proof's commands ran).

---

## 8. What remains unverified

- **The entire s3 API-transport stream does not exist in this branch.**
  `phoneloop/ebay_api.py` — the module the build contract specifies in full
  (§2.7: `Credentials`, `Budget`, `BrowseClient`, `token()`/`search()`/
  `search_all()`/`get_item()`/`search_by_epid()`, `REPLAY_TOKEN`, etc.) — is
  absent from `git diff master --stat` (§1) and `import phoneloop.ebay_api`
  fails with `ImportError: cannot import name 'ebay_api' from 'phoneloop'`
  (confirmed live). This is not "unverified due to a missing keyset" — the
  code itself was never merged, so nothing about OAuth, the daily call
  budget, or Browse API row-parity with the HTML transport could be
  exercised, checked, or even attempted. `PHONELOOP_EBAY_CLIENT_ID` /
  `PHONELOOP_EBAY_CLIENT_SECRET` are also unset in this environment
  (confirmed with `printenv | grep PHONELOOP_EBAY`, no output) — so even if
  the module existed, live calls would still be blocked, per the task brief's
  own framing ("developer account pending").

- **The contract's own offline end-to-end acceptance command does not
  complete** (§3.1): `scripts/bulk_ebay.py --leaf-sweep --max-pages 3
  --fresh` and its `--category phones` fallback both raise
  `MissingRecording` on shard 1, because the 7 real recordings under
  `fixtures/ebay/` predate a key-shape realignment the CLI itself now
  requires (root-caused in §3.1/§3.2, and already named — but not fixed — in
  `scripts/bulk_ebay.py`'s own module docstring). Closing this needs a fresh
  `record_ebay.py` recording session (real, live, under the current
  `search_key`/`search_url` shape) plus real `--leaf-sweep` recordings for
  the 9 in-scope departments' 43 leaves, neither of which exists yet. I did
  not attempt a live recording session — out of this proof's scope (replay
  only, per the task brief) and it would need a live browser session subject
  to eBay's block behaviour.

- **`scripts/niche_board.py` was never exercised against a real, populated
  per-niche store** (§4), as a direct consequence of the point above: the
  only real rows available (keyword-mode phone search) carry no
  `leaf_category_id`, so both the niches and products tables print
  `(no rows)`. The claims about STR-coverage printing and integer `max_cost`
  are verified instead by source inspection plus isolated runtime checks
  (§4) and by the passing `tests/test_board.py` suite (test data, not real
  listings) — not by a real populated board table, which I could not produce
  honestly from what exists.

- **`--deepen` (Tier 2 item-page enrichment via `phoneloop.ebay_item`) was
  not exercised.** Real item-page recordings do exist
  (`fixtures/ebay_item/*.json.gz`, 6 files), but running `--deepen` against
  them was outside this proof's required scope and was not attempted.

- **Pre-existing untracked files in the working tree** — `.claude/`,
  `body.html`, `graphify-out/`, `shard.xml.gz`, `vitems.xml` — were present
  before this session started (confirmed: they showed up in the very first
  `git status` I ran) and are outside this task's ownership list. I did not
  read, touch, stage, or otherwise act on them, per the standing rule against
  touching files outside scope; flagging their presence here rather than
  silently ignoring it.

- **`refresh_ebay_categories.py --offline`'s two `!` warnings**
  ("Appliances", "Car parts & accessories" — "no numeric category id found on
  its own page") are pre-existing behaviour of that script, observed but not
  diagnosed further; they did not block the 43-leaf result and are unrelated
  to the regression tested in §7.

- **Live/record-mode harness paths were not exercised anywhere in this
  proof** — every command above ran with `PHONELOOP_HARNESS` unset (replay),
  per the task brief's environment spec, so nothing about live egress,
  proxy fallback, or eBay's block behaviour was tested here.

---

## Commit

Proof doc committed alone, explicit path, on `feat/ebay-niche-finder`:
`git add docs/superpowers/proof/2026-08-27-ebay-niche-finder-proof.md`.
