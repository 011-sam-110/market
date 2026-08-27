# eBay Niche Finder — build contracts for six parallel streams

**Date:** 2026-08-27 · **Base:** `master` at `a87fc76` (405 tests, ~14s) ·
**Spec:** `docs/superpowers/specs/2026-08-27-ebay-niche-finder-design.md`

Six Sonnet workers build in six separate worktrees at the same time. They cannot
see each other's code. This document is the only thing that makes the pieces fit,
so everything in it is a promise: if your implementation disagrees with this
document, this document wins and you say so in `unverified[]` rather than
quietly diverging.

Read the spec first. Then read this. Then read the files you own.

> **Rule of thumb for every ambiguity:** the contract is a *name and a shape*.
> If this document names a function, that exact name and that exact parameter
> list must exist, even if you think a better one exists. Add keyword-only
> parameters if you need them; never rename, never reorder, never drop.

---

## 0. The rules that fail review

These are the repo owner's standing rules. Each one has cost this project real
money or real time already.

1. **Money is integer pence.** No float ever touches money. Percentages that get
   *stored* are integer basis points (`99.8%` → `9980`). Ratios computed for
   display (`coverage`, `success_rate`) may be floats — they are never written
   to parquet.
2. **Never fake results.** A fixture that is not a real captured page or a real
   captured response must be named `SYNTHETIC_*`, must carry a top-level
   `"_synthetic"` key saying where its shape came from, and every test that uses
   it must have `synthetic` in the test name.
3. **Harness, always.** Replay is the default (`PHONELOOP_HARNESS` unset). A
   missing recording raises `phoneloop.harness.MissingRecording`; it never
   returns an empty result. Anything that touches the network goes through the
   harness.
4. **A guard test nobody watched go red is decoration.** For every new guard:
   commit the working state, break the code, run the test, see it fail, restore,
   run it again, see it pass. Report which tests you watched go red.
5. **Git: explicit paths only.** Never `git add -A`, `git add .`, `git commit -a`.
   Never commit `fixtures/`, `*.parquet`, `.env`, or anything under
   `~/.phoneloop`. Never touch a file outside your ownership list — if you must,
   stop and put it in `unverified[]`.
6. **Foreground only.** No background processes. Nothing is "done" while pending.
7. **Python is the installer build:**
   `"$LOCALAPPDATA/Programs/Python/Python313/python.exe"`. The MSIX/WindowsApps
   python exits silently when detached.
8. **Never print proxy or API credentials.** They live in `~/.phoneloop/.env`;
   `phoneloop/proxies.py::load_env_file` shows how to read it.
9. **No `mcp__playwright__*` or browser MCP tools.** For live pages: the Python
   `playwright` package from a script, headed Chromium, direct first, residential
   proxy only after a block.
10. **Match the surrounding code.** Docstrings explain *why* and cite what was
    measured. Comments never claim something the code does not do. The existing
    `phoneloop/` modules are the style guide.
11. **Windows.** Forward slashes in Python paths. Git Bash is the shell. Every
    command starts with `cd` into your worktree — the session cwd is not the repo.

---

## 1. THE ROW

One row is *one listing seen on one marketplace at one moment*. Both transports
— the HTML crawler (s2) and the Browse API (s3) — produce this exact dict. The
store (s1) owns the schema and exports the field list.

`phoneloop/market_store.py` exports:

```python
ROW_FIELDS: tuple[str, ...] = tuple(SCHEMA.names)
```

in schema order. It is the single source of truth for "what a row is". A
transport that produces a dict with a key not in `ROW_FIELDS` is a bug; a
transport that omits a key gets `NULL` written for it, which is why every
transport must build its row from `empty_row()` (below) rather than from a
literal, so a forgotten column is impossible rather than silent.

### 1.1 The 37 columns

Types are pyarrow. "Nullable" is true for every column except where noted;
pyarrow makes everything nullable, so the column that matters is the *meaning*
of NULL, which is stated for each.

#### Existing columns (unchanged — do not alter types or order)

| # | Column | Type | Unit | NULL means |
|---|--------|------|------|-----------|
| 1 | `source` | `string` | — | never NULL. `vinted` \| `ebay` \| `cex` |
| 2 | `source_id` | `string` | — | never NULL. eBay item number as a string. Ids collide across sources, so identity is the pair `(source, source_id)` |
| 3 | `seen_at` | `timestamp("us", tz="UTC")` | UTC instant | never NULL. Filled by the store from `datetime.now(UTC)` if the transport omits it |
| 4 | `category` | `string` | — | our slug (`phones`, `garden`…). NULL = uncategorised |
| 5 | `shard` | `string` | — | the query/leaf/page that found it, for coverage audits. NULL = unrecorded provenance (treat as a bug) |
| 6 | `title` | `string` | — | seller's title. NULL = not read |
| 7 | `price_pence` | `int64` | **pence** | headline price before delivery. NULL = no price on the row (such a row is normally dropped) |
| 8 | `total_pence` | `int64` | **pence** | landed = price + delivery. **NULL = delivery was not stated.** Never 0 — "free delivery" is 0 delivery and a non-NULL total |
| 9 | `currency` | `string` | ISO 4217 | `GBP`. NULL = unknown |
| 10 | `condition` | `string` | — | eBay's condition string verbatim (`New`, `Pre-owned`…). NULL = not shown |
| 11 | `brand` | `string` | — | marketplace's own brand field. NULL = marketplace stated none |
| 12 | `model` | `string` | — | parser slug. NULL = unparsed |
| 13 | `storage_gb` | `int32` | GB | NULL = not stated / not applicable |
| 14 | `grade` | `string` | — | CeX A/B/C. NULL everywhere else |
| 15 | `kind` | `string` | — | `ask` \| `committed`. eBay rows are always `ask` |
| 16 | `available` | `bool_` | — | never NULL; `False` once seen sold/gone. Store coerces with `bool(row.get("available", True))` |
| 17 | `region` | `string` | — | `GB` when known to ship from the UK. NULL/empty = unknown. Superseded for eBay by `origin_country`, kept for Vinted |
| 18 | `seller_id` | `string` | — | eBay seller username. NULL = not shown |
| 19 | `url` | `string` | — | canonical listing URL, query string stripped. NULL = not captured |
| 20 | `extra` | `string` | JSON | source-specific leftovers, JSON-encoded. NULL = nothing extra. **Excluded from the content hash** |
| 21 | `content_hash` | `string` | — | never NULL. Computed by the store, never by a transport |

#### New columns (spec §8) — added by s1, produced by s2 and s3

| # | Column | Type | Unit | NULL means |
|---|--------|------|------|-----------|
| 22 | `sold_qty` | `int64` | count | **unknown, never zero.** eBay shows a sold count on some rows only (measured coverage on a full 240-row page: 13%). A row with no sold count is excluded from `sold_known`, not counted as zero. **Joins `_HASHED`** |
| 23 | `qty_available` | `int64` | count | unknown. `"More than 10 available"` records `10` and sets `extra.qty_available_is_floor = true`. **Joins `_HASHED`** |
| 24 | `seller_type` | `string` | — | unknown. Values: `business` \| `private`. From API `sellerAccountType` or the item page's "Registered as a business seller". Never inferred from feedback |
| 25 | `seller_feedback_bp` | `int32` | **basis points** | unknown. `99.8%` → `9980`. `0% positive` is a real observation → `0`, not NULL |
| 26 | `seller_feedback_n` | `int64` | count | unknown. `(74.6K)` → `74600`; eBay abbreviates above 1,000, so this is accurate to ±50 above that and the docstring must say so |
| 27 | `listed_at` | `timestamp("us", tz="UTC")` | UTC instant | unknown. API `itemCreationDate`, or the item page's embedded `SEMANTIC_DATA.startDate`. Survival across a relist is spec §13.2, **unverified** |
| 28 | `epid` | `string` | — | no catalogue match. Holds *either* a true `epid` *or* eBay's `inferredEpid`; which one is recorded in `identity_tier` |
| 29 | `gtin` | `string` | — | not stated. EAN/UPC/ISBN as printed, digits only |
| 30 | `mpn` | `string` | — | not stated. **Recorded, never trusted for identity** — seller-typed MPNs are junk (`"Wireless-charge-carphonr"` measured) |
| 31 | `identity_tier` | `string` | — | not yet assigned. `catalogued` \| `inferred` \| `cluster` \| `none`. Transports may write `catalogued`/`inferred`/NULL; `cluster`/`none` are assigned by `identity.py` at analysis time |
| 32 | `origin_country` | `string` | ISO 3166-1 alpha-2 | unknown. `GB`, `DE`, `CN`… Recorded, **never filtered at sweep time** — `LH_LocatedIn=1` is dropped because China-shipped listings are exactly the competitors a China-sourced dropshipper prices against |
| 33 | `promoted` | `bool_` | — | unknown (Vinted rows, and any eBay row whose promoted state could not be read). `True`/`False` are both real observations on eBay |
| 34 | `leaf_category_id` | `string` | — | unknown. eBay's numeric leaf id, as a string (`"139971"`). The niche key, and the fee lookup key |
| 35 | `eta_days` | `int32` | days | unknown. The **maximum** delivery days the listing promises. `"Free delivery in 3 days"` → `3`; a range `"3–5 days"` → `5` |
| 36 | `store_joined` | `date32` | ISO date | unknown. eBay shows month precision only (`"Joined Mar 2019"`), so the day is always `01` and the docstring must say so |
| 37 | `store_items_sold` | `int64` | count | unknown. Lifetime store total (`"12.4K items sold"` → `12400`), same ±50 abbreviation caveat as `seller_feedback_n` |

### 1.2 The hash

```python
_HASHED = ("source", "source_id", "title", "price_pence", "total_pence",
           "condition", "available", "sold_qty", "qty_available")
```

`sold_qty` and `qty_available` join because they are the two fields Tier 3
differences. `extra` stays out; view/watcher counts stay out — they tick on every
poll and hashing them would rewrite the catalogue twice a day.

### 1.3 Snapshot semantics

```python
# Sources whose every observation is written, hash or not.
ALWAYS_SNAPSHOT_SOURCES: frozenset[str] = frozenset({"ebay"})
```

For `source == "ebay"`, `MarketStore.record` writes **every** observation, even
when the content hash is unchanged. Differencing needs "the value on day D", not
"the value when it last changed": a listing whose sold count did not move is
exactly the evidence that it did not move, and a change-log cannot express that.
~100k rows/day is a few tens of MB of zstd parquet.

Vinted and CeX behaviour is **unchanged**: unchanged listing → zero rows written.
`self._hashes` is still updated for eBay rows so the index stays honest.

### 1.4 Partitioning

`record()` today derives the partition from `changed[0]`, so a mixed-source or
midnight-straddling batch lands in one directory under the wrong `source=`.
With eBay always writing, that gets hit. s1 must group `changed` by
`(seen_at.date(), source)` and write one parquet file per group, and must test it.

---

## 2. PUBLIC SIGNATURES

Stubs, with the docstring each one must carry (one line here; expand it in the
code to explain *why*, in the house style). Type hints are part of the contract.

### 2.1 `phoneloop/market_store.py` — s1

```python
ROW_FIELDS: tuple[str, ...]
"""Every column of the shared row, in schema order. Both transports produce all of them."""

ALWAYS_SNAPSHOT_SOURCES: frozenset[str]
"""Sources whose every observation is written, because differencing needs the value on day D."""


def empty_row(source: str, source_id: str) -> dict:
    """A row dict with every ROW_FIELDS key present and NULL, so a forgotten column cannot be silent."""


def content_hash(row: dict) -> str:
    """Stable hash over _HASHED. Unchanged signature; _HASHED now includes sold_qty and qty_available."""


class MarketStore:
    def record(self, rows: list[dict]) -> int:
        """Write rows; every eBay observation, only changed rows elsewhere. Returns rows written."""

    def snapshot_dates(self, source: str = "ebay") -> list[date]:
        """Dates that have at least one row for this source, ascending — the days Tier 3 can difference."""

    def rows_on(self, day: date | str, *, source: str = "ebay") -> list[dict]:
        """Every row observed on one UTC date, newest observation per source_id wins."""
```

`snapshot_dates` and `rows_on` exist because s5 needs them and must not
re-implement parquet reading. s1 delivers them.

### 2.2 `phoneloop/fees.py` — s1 (new)

```python
REGULATORY_BP: int = 35
"""eBay's regulatory operating fee, 0.35%, quoted ex-VAT, charged on price + postage."""

PER_ORDER_PENCE_LOW: int = 30    # order total <= £10
PER_ORDER_PENCE_HIGH: int = 40   # order total  > £10
PER_ORDER_THRESHOLD_PENCE: int = 1000

EVERYTHING_ELSE_BP: int = 1290
"""The fallback final value fee, 12.9%. An unknown category is charged the worst rate, not the best."""

FVF_BP_BY_TOP_LEVEL_NAME: dict[str, int]
"""Business final value fee in basis points, keyed by eBay UK top-level department title."""

TOP_LEVEL_ID_TO_NAME: dict[str, str]
"""eBay UK top-level category id -> department title. Only ids read off a real page appear here."""


class FeeKnobs(Protocol):
    """The knob attributes fees.py reads. phoneloop.knobs.Knobs satisfies it structurally."""
    vat_on_fees_bp: int
    ad_rate_bp: int
    import_vat_term_bp: int
    min_margin_pct_bp: int
    min_margin_fixed_pence: int


def bp(amount_pence: int, basis_points: int) -> int:
    """amount x bp/10000, rounded half up, in integer pence. The only arithmetic money is allowed."""


def fvf_bp(leaf_category_id: str | None,
           top_level_of: Mapping[str, str] | None = None) -> int:
    """Final value fee for a leaf, via its top-level ancestor. Unknown falls to EVERYTHING_ELSE_BP."""


def per_order_pence(price_pence: int) -> int:
    """30p at or below £10, 40p above. Quoted ex-VAT, like every other eBay fee."""


def fees(price_pence: int, leaf_category_id: str | None, knobs: FeeKnobs, *,
         top_level_of: Mapping[str, str] | None = None) -> int:
    """Total eBay fees on a sale at this landed price, integer pence."""


def max_cost(price_pence: int, leaf_category_id: str | None, knobs: FeeKnobs, *,
             top_level_of: Mapping[str, str] | None = None) -> int:
    """Highest landed supplier cost that still clears the margin knob. Negative means no cost clears it."""
```

**`fees.py` must not import `phoneloop.knobs`.** s1 and s4 build in parallel; the
`Protocol` is how they meet. s1's tests use a small local frozen dataclass that
satisfies `FeeKnobs`.

**The arithmetic, exactly** (spec §7, all integer pence, `bp()` rounds half up):

```
variable = bp(bp(price_pence, fvf_bp + REGULATORY_BP), knobs.vat_on_fees_bp)
order    = bp(per_order_pence(price_pence),            knobs.vat_on_fees_bp)
ads      = bp(price_pence, knobs.ad_rate_bp)
importv  = bp(price_pence, knobs.import_vat_term_bp)
fees     = variable + order + ads + importv

target   = max(bp(price_pence, knobs.min_margin_pct_bp), knobs.min_margin_fixed_pence)
max_cost = price_pence - fees - target
```

Rounding is **sequential** — round the VFF+regulatory product to pence, then
apply VAT to that. One rounding rule, applied the same way everywhere, is
testable; a single combined division is not reproducible by hand.

`bp(x, n) == (x * n + 5000) // 10000` for non-negative `x`. Note it does not
clamp: `max_cost` may be negative, and a negative number is the honest answer
("there is no supplier price that works at this sale price"). Do not clamp it
to zero — a zero would read as "free stock works", which is the direction that
loses money.

**Worked examples s1 must ship as tests** (recompute them; if your rounding
differs, this document's rounding rule wins and you fix the code):

| price | department (FVF) | variable | order | fees | target | `max_cost` |
|-------|------------------|---------:|------:|-----:|-------:|-----------:|
| 3000p (£30) | Mobile Phones & Communication (990) | 370 | 48 | 418 | 750 | **1832** |
| 1500p (£15) | everything else (1290) | 239 | 48 | 287 | 600 | **613** |
| 1000p (£10) | Home, Furniture & DIY (1190) | 148 | 36 | 184 | 600 | **216** |

All three at the defaults: `vat_on_fees_bp=12000`, `ad_rate_bp=0`,
`import_vat_term_bp=0`, `min_margin_pct_bp=2500`, `min_margin_fixed_pence=600`.
Note the £10 row uses the 30p band and the other two use 40p.

The spec's own sanity check on the same arithmetic: Mobile Phones
`9.9% + 0.35% = 10.25%`, `× 1.2 = 12.3%`, and `£0.40 × 1.2 = £0.48` — exactly the
"12.3% + £0.48" the spec's table quotes. Assert that identity **on the rates**:

```python
assert (990 + REGULATORY_BP) * 12000 // 10000 == 1230   # 12.30%
assert bp(PER_ORDER_PENCE_HIGH, 12000) == 48            # £0.48
```

Assert it on the rates and **not** on the pence, because sequential rounding can
differ from the single-step rate by a penny: at £30, sequential gives `370p`
(`307.5 → 308`, then `×1.2 → 369.6 → 370`) while the combined 12.3% gives `369p`.
Both are defensible; sequential is the rule this project uses, so `370` is the
right answer and a test asserting `369` is testing the wrong thing. Say this in
the module docstring — someone will otherwise "fix" it back.

**The fee table.** The spec gives five rate groups (§7). The authoritative source
is eBay help `id=4809` (updated 4 Aug 2026). Fetch it live through the harness
if you can and take all 28 rows; if it blocks you, ship the five groups from the
spec plus `EVERYTHING_ELSE_BP` and say in `unverified[]` that the table is
partial and which rows are missing. **Do not invent a category id.** Every id in
`TOP_LEVEL_ID_TO_NAME` must have been read off a real page; ids you cannot source
are left out and the leaf falls to `EVERYTHING_ELSE_BP`, which is the safe
direction. s6 delivers `catalog_ebay.top_level_of()` to fill the mapping at
integration.

### 2.3 `phoneloop/catalog.py` — s1

```python
RESULT_CAPS: dict[str, int] = {"vinted": 960, "ebay": 10_000}
"""Rows a single query can return, per source. Vinted's 960 is ten pages of 96; eBay's 10,000 is measured (page 45 at 240/page returned zero on a '21,000+ results' query)."""

SATURATED_AT_BY_SOURCE: dict[str, float] = {"vinted": 0.94, "ebay": 1.0}
"""Fraction of the cap that counts as saturated. Vinted pages return 94-95 of 96; eBay's cap is exact."""

RESULT_CAP: int = 960
"""Vinted's cap. Kept as a module constant so existing callers and config.RESULT_CAP still agree."""


def result_cap(source: str) -> int:
    """The result cap for one source. Unknown source raises KeyError rather than guessing Vinted's."""


def is_saturated(unique_seen: int, source: str = "vinted") -> bool:
    """Did this shard hit its source's cap, i.e. is it hiding items we never saw?"""
```

The `source` default keeps `scripts/bulk_vinted.py:194` (`catalog.is_saturated(n)`)
working untouched. For eBay the *operational* saturation test in the sweep is
"the page came back with zero rows", never the rounded header count;
`is_saturated(..., "ebay")` is the cap guard that stops a sweep at 10,000.

### 2.4 `phoneloop/ebay.py` — s2

```python
DECOY_ITEM_IDS: frozenset[str] = frozenset({"123456"})
"""eBay's first search row is a hidden template pointing at /itm/123456. It parses cleanly and is not a listing."""


def is_decoy(url: str) -> bool:
    """Is this href the template row rather than a real listing?"""


@dataclass(frozen=True)
class EbayListing:
    # existing — unchanged names, unchanged order
    title: str
    price_pence: int
    delivery_pence: int | None
    condition: str = ""
    aspects: tuple[str, ...] = field(default_factory=tuple)
    storage_gb: int | None = None
    watchers: int = 0
    ships_from: str = ""
    url: str = ""
    # added by s2 — every one defaults, so existing constructions keep working
    item_id: str = ""                       # from the /itm/ URL
    sold_qty: int | None = None             # "1,283 sold" -> 1283; absent -> None, NEVER 0
    seller_id: str = ""                     # "jaessentials"
    seller_feedback_bp: int | None = None   # "99.8% positive" -> 9980
    seller_feedback_n: int | None = None    # "(74.6K)" -> 74600
    promoted: bool = False                  # row text ends with the reversed word "derosnopS"
    eta_days: int | None = None             # max promised delivery days
    epid: str = ""                          # from an epid= href on the row
    stock_hint: str = ""                    # "Almost gone" | "Last one" | ""

    @property
    def landed_pence(self) -> int | None: ...
    @property
    def is_uk(self) -> bool: ...
    @property
    def catalogue_model(self) -> str | None: ...
    @property
    def is_used(self) -> bool: ...


def parse_row(text: str, url: str = "", *, today: date | None = None) -> EbayListing | None:
    """Parse one search-result row. None for a decoy row or a row with no usable price."""


def parse_rows(rows, *, today: date | None = None) -> list[EbayListing]:
    """Parse many rows, dropping decoys and anything that yields nothing usable."""


def to_row(listing: EbayListing, *, category: str, shard: str,
           leaf_category_id: str | None = None) -> dict:
    """One EbayListing -> the shared ROW_FIELDS dict, built from market_store.empty_row()."""
```

Parsing notes that are part of the contract:

- **Sold count.** `"1,283 sold"`. Missing → `None`. Never `0`. A row saying
  `"0 sold"` (if such a row exists) is a real zero.
- **Seller line.** `"jaessentials 99.8% positive (74.6K)"` →
  `("jaessentials", 9980, 74600)`. `"reshi-1723 0% positive (0)"` →
  `("reshi-1723", 0, 0)`. `K`/`M` expansion is approximate above 1,000 and the
  docstring says so.
- **Promoted.** eBay renders "Sponsored" as reversed text so it does not select;
  the row's visible text ends with `derosnopS`. Parse it into `promoted=True`.
  Do not drop sponsored rows — they are competitors and `promoted_pct` is a
  metric.
- **ETA.** `"Free delivery in 3 days"` → `3`. `"Estimated delivery 3–5 days"` →
  `5` (the maximum). `"Get it by Tue 1 Sep"` needs a reference date: with
  `today=None` it yields `None`, with `today` supplied it yields the day count.
  `parse_row` stays pure — it must not read the clock.
- **Decoy.** `href` ending `/itm/123456` is a template. Dropped by `parse_row`
  returning `None`, and covered by its own test.
- **Stock hints.** `"Almost gone"`, `"Last one"` recorded verbatim in
  `stock_hint`. They are not a quantity and must not become `qty_available`.

`retail_ceiling`, `saturation`, `uk_share`, `looks_geolocated_abroad`,
`MIN_SAMPLES`, `MAX_DISPERSION`, `MIN_UK_SHARE` keep their current signatures and
behaviour. The existing `tests/test_ebay.py` fixtures are verbatim live captures
from 2026-08-27 — they stay, and they must still pass.

### 2.5 `phoneloop/ebay_item.py` — s2 (new)

```python
@dataclass(frozen=True)
class EbayItem:
    item_id: str
    sold_qty: int | None = None
    qty_available: int | None = None
    qty_available_is_floor: bool = False   # True for "More than 10 available"
    seller_id: str = ""
    seller_feedback_bp: int | None = None
    seller_feedback_n: int | None = None
    seller_type: str = ""                  # "business" | "private" | ""
    store_joined: date | None = None       # "Joined Mar 2019" -> 2019-03-01, month precision only
    store_items_sold: int | None = None
    listed_at: datetime | None = None      # SEMANTIC_DATA "startDate", UTC
    brand: str = ""
    mpn: str = ""                          # recorded, never used for identity
    gtin: str = ""
    epid: str = ""
    leaf_category_id: str = ""


def parse_item(text: str, *, html: str = "", url: str = "") -> EbayItem | None:
    """Parse one item page. `text` is the rendered body text; `html` carries the embedded SEMANTIC_DATA."""


def to_row_updates(item: EbayItem) -> dict:
    """The subset of ROW_FIELDS an item page can fill, for merging onto a sweep row."""
```

- `"Registered as a business seller"` → `seller_type="business"`. Absence is
  **not** evidence of `private`; it stays `""`. Only an explicit private-seller
  marker sets `private`.
- `"More than 10 available"` → `qty_available=10, qty_available_is_floor=True`.
  The floor flag rides in `extra` on the stored row.
- `mpn` is recorded raw. `ebay_item.py` does **not** judge it — the junk rule
  lives in `identity.py` (s4) and there must be exactly one copy of it.

### 2.6 `phoneloop/harness.py` — s2 (root override only)

```python
FIXTURES_ENV = "PHONELOOP_FIXTURES"

def fixtures_root(env: Mapping[str, str] | None = None) -> Path:
    """Where recordings live. PHONELOOP_FIXTURES when set, else the repo's fixtures/ directory."""


class Cassette:
    def __init__(self, root: Path | str | None = None):
        """A directory of recordings. root=None consults PHONELOOP_FIXTURES, then DEFAULT_ROOT."""
```

`Cassette()` and `Cassette(some_path)` both keep working exactly as today — the
env var only participates when `root` is omitted. `DEFAULT_ROOT` stays. **This
is the only change s2 may make to `harness.py`**, and it gets one test in
`tests/test_harness.py` and nothing else in that file.

Streams that need a specific fixtures root before this lands (s6) pass the root
positionally: `Cassette(Path(os.environ["PHONELOOP_FIXTURES"]))`. That works on
`master` today, so s6 has no dependency on s2.

### 2.7 `phoneloop/ebay_api.py` — s3 (new)

```python
OAUTH_SCOPE = "https://api.ebay.com/oauth/api_scope"
MARKETPLACE_ID = "EBAY_GB"
API_BASE = "https://api.ebay.com"
TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_LIMIT = 200            # eBay's maximum page for item_summary/search
DAILY_CALL_BUDGET = 4000      # of the 5,000 default; the rest is Tier 2's


class EbayApiError(RuntimeError):
    """The API answered, and the answer was not usable."""


class QuotaExhausted(RuntimeError):
    """The daily call budget is spent. Checkpoint and resume tomorrow, do not retry."""


class MissingKeyset(RuntimeError):
    """No production keyset is configured. Raised instead of touching the network."""


@dataclass(frozen=True)
class Credentials:
    client_id: str
    client_secret: str


def credentials(env: Mapping[str, str] | None = None) -> Credentials | None:
    """Keyset from PHONELOOP_EBAY_CLIENT_ID/SECRET: environment first, then ~/.phoneloop/.env. None if absent."""


@dataclass
class Budget:
    """A day's call allowance, checkpointed to disk so a crash does not re-spend it."""
    limit: int = DAILY_CALL_BUDGET
    used: int = 0
    day: str = ""                    # ISO date; a new day resets `used`
    path: Path | None = None

    @classmethod
    def load(cls, path: Path, *, limit: int = DAILY_CALL_BUDGET,
             today: date | None = None) -> "Budget":
        """Read the checkpoint, resetting the count when the stored day is not today."""

    def spend(self, n: int = 1) -> None:
        """Charge n calls. Raises QuotaExhausted rather than letting the day overrun."""

    def remaining(self) -> int: ...
    def save(self) -> None: ...


class BrowseClient:
    """Buy Browse API over the harness. Every GET is recorded or replayed; the token never is."""

    def __init__(self, session, *, marketplace_id: str = MARKETPLACE_ID,
                 creds: Credentials | None = None, budget: Budget | None = None): ...

    def token(self) -> str:
        """Client-credentials access token. In replay mode returns REPLAY_TOKEN and touches nothing."""

    def search(self, *, category_ids: Sequence[str] | None = None,
               q: str | None = None,
               price_from_pence: int | None = None,
               price_to_pence: int | None = None,
               conditions: Sequence[str] = ("NEW",),
               buying_options: Sequence[str] = ("FIXED_PRICE",),
               epid: str | None = None,
               item_start_date: str | None = None,
               limit: int = SEARCH_LIMIT, offset: int = 0) -> dict:
        """One item_summary/search call. Returns the decoded JSON body."""

    def search_all(self, **kwargs) -> Iterator[dict]:
        """Page by offset to catalog.result_cap('ebay'), stopping on an empty itemSummaries."""

    def get_item(self, item_id: str) -> dict:
        """getItem for one listing: estimatedSoldQuantity, stock, gtin/mpn, inferredEpid."""

    def search_by_epid(self, epid: str, **kwargs) -> dict:
        """Every live listing for one catalogued product — the exact competitor count for knob 6."""


def item_start_date_filter(days: int, *, now: datetime | None = None) -> str:
    """The itemStartDate filter term for 'listed in the last N days' — the free saturation trend."""


def price_filter(from_pence: int | None, to_pence: int | None) -> str:
    """eBay's price band filter term. Pence in, GBP decimals out, because the API takes decimals."""


def parse_summary(summary: Mapping, *, category: str, shard: str,
                  leaf_category_id: str | None = None) -> dict:
    """One itemSummaries entry -> the shared ROW_FIELDS dict. Same row the HTML transport produces."""


def parse_item(item: Mapping, *, category: str, shard: str) -> dict:
    """One getItem response -> the ROW_FIELDS subset it can fill, for merging onto a sweep row."""
```

Two hard points:

- **The token is never recorded.** `HarnessSession` wraps `.get` only, and the
  token exchange is a `POST` — deliberately outside the harness. In replay mode
  `token()` returns the module constant `REPLAY_TOKEN = "REPLAY-NO-TOKEN"` and
  makes no request. In record/live mode it does a real POST and **writes nothing
  to any cassette**: an access token is a credential, and a cassette is a file
  that gets committed. Say this in the docstring.
- **`parse_summary` produces the same row as `ebay.to_row`.** Where the API
  cannot fill a column (it carries no sold count on search summaries), the value
  is `None`, not a substitute. `sold_qty` from a search summary is always `None`.

### 2.8 `phoneloop/knobs.py` — s4 (new)

```python
@dataclass(frozen=True)
class Knobs:
    """The six control knobs plus the fee terms, all integers, all overridable from the environment."""

    # 1 — min net profit margin (inverted into max_cost)
    min_margin_pct_bp: int = 2500          # 25%
    min_margin_fixed_pence: int = 600      # £6
    # 2 — sell-through threshold, basis points of listings sold in the window
    str_threshold_bp: int = 5000           # 50%: a convention, NOT a measurement
    # 3 — retail price range, landed
    price_from_pence: int = 1500           # £15
    price_to_pence: int = 5500             # £55
    # 4 — max delivery + handling, days
    max_eta_days: int = 7
    # 6 — market saturation cap, distinct sellers per product
    max_competitors: int = 15
    # 5 — competitor quality floors (a profile of rivals, not a supplier filter)
    min_seller_feedback_bp: int = 9500     # 95.0% positive
    min_seller_feedback_n: int = 50
    # fee terms (spec §7)
    vat_on_fees_bp: int = 12000            # 1.2 non-registered; 10000 if registered and reclaiming
    ad_rate_bp: int = 0                    # Promoted Listings, set when used
    import_vat_term_bp: int = 0            # unverified (spec §13.6); the term exists so the answer is config

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Knobs":
        """Defaults overridden by PHONELOOP_<FIELD_NAME_UPPER>. Every value is an int; a bad one raises."""
```

`Knobs` structurally satisfies `fees.FeeKnobs` — that is not an accident, and a
test in `tests/test_knobs.py` asserts every `FeeKnobs` attribute exists on
`Knobs`... except that s4 must not import `fees`. So the test asserts the five
attribute names from a literal tuple in the test, with a comment pointing at this
document. The integrator adds the cross-check.

`str_threshold_bp = 5000` is a **chosen default, not a measured one** — the spec
names the knob and gives no number. The docstring must say exactly that.

Env names are the field names uppercased with the `PHONELOOP_` prefix:
`PHONELOOP_MIN_MARGIN_PCT_BP`, `PHONELOOP_MIN_MARGIN_FIXED_PENCE`,
`PHONELOOP_STR_THRESHOLD_BP`, `PHONELOOP_PRICE_FROM_PENCE`,
`PHONELOOP_PRICE_TO_PENCE`, `PHONELOOP_MAX_ETA_DAYS`,
`PHONELOOP_MAX_COMPETITORS`, `PHONELOOP_MIN_SELLER_FEEDBACK_BP`,
`PHONELOOP_MIN_SELLER_FEEDBACK_N`, `PHONELOOP_VAT_ON_FEES_BP`,
`PHONELOOP_AD_RATE_BP`, `PHONELOOP_IMPORT_VAT_TERM_BP`.

### 2.9 `phoneloop/identity.py` — s4 (new)

```python
TIERS: tuple[str, ...] = ("catalogued", "inferred", "cluster", "none")
JACCARD_MIN: float = 0.6
SHINGLE_N: int = 3


def is_junk_mpn(mpn: str | None) -> bool:
    """Is this MPN seller-typed noise rather than a manufacturer part number?

    Measured junk: "Wireless-charge-carphonr". Junk MPNs are recorded and never
    used for identity — the sole authority on this rule lives here.
    """


def tier_for(row: Mapping) -> str:
    """Best identity tier this row supports: catalogued | inferred | cluster | none (spec §5)."""


def product_key(row: Mapping, tier: str, *, cluster_id: int | None = None) -> str | None:
    """The string listings group on at this tier. None for tier 'none' — a nameless product is niche-level only."""


def shingles(title: str, n: int = SHINGLE_N) -> frozenset[str]:
    """Overlapping n-word shingles of a normalised title, the unit clustering compares."""


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """|A n B| / |A u B|. Two empty sets score 0.0, not 1.0 — nothing in common is not identity."""


def cluster(rows: Sequence[Mapping], *, threshold: float = JACCARD_MIN) -> list[int]:
    """Cluster ids parallel to `rows`, by title shingle Jaccard, INSIDE one niche.

    Raises ValueError if `rows` span more than one leaf_category_id: clustering
    across niches is how "wireless charger" and "wireless doorbell" become one
    product.
    """
```

Tier rules, exactly:

| Tier | Condition |
|------|-----------|
| `catalogued` | `row["epid"]` is set **and** `row["identity_tier"] != "inferred"`, or `row["gtin"]` is set |
| `inferred` | `row["epid"]` is set and the transport marked it `inferred` (it came from `getItem`'s `inferredEpid`) |
| `cluster` | neither, but the row has a title and a `leaf_category_id` |
| `none` | no title or no leaf |

`product_key` returns:

- `catalogued` → `f"gtin:{gtin}"` if a GTIN exists, else `f"epid:{epid}"`
- `inferred` → `f"epid~:{epid}"` (the tilde marks it as eBay's own match, not a
  seller's declaration)
- `cluster` → `f"cluster:{leaf_category_id}:{cluster_id}"`; raises `ValueError`
  if `cluster_id is None`
- `none` → `None`

**`mpn` never appears in a product key**, at any tier.

Cluster ids are assigned in order of first appearance (`0`, `1`, `2`…), so the
same input list always yields the same ids.

### 2.10 `phoneloop/metrics.py` — s4 (new)

Every figure carries the denominator it was computed over. That is the whole
point of the module: a sell-through of 3.2 over 4 known rows out of 240 is not
the same claim as 3.2 over 200 out of 240, and the board must be able to show
the difference.

```python
@dataclass(frozen=True)
class Metrics:
    """Spec §6 over one niche or one product. Every value sits beside its denominator."""

    # population
    n_listings: int
    n_sellers: int
    n_business: int
    n_private: int

    # the denominator that governs every demand figure
    sold_known: int                       # rows where sold_qty IS NOT NULL
    coverage: float                       # sold_known / n_listings; 0.0 when n_listings == 0

    # demand, lifetime — averages over each listing's whole life, they hide decline
    str_lifetime: float | None
    str_lifetime_denominator: int         # == sold_known
    velocity_day1: float | None           # sold per day, from listed_at
    velocity_day1_denominator: int        # rows with sold_qty AND listed_at

    # demand, windowed — the only figure knob 2 may be compared against
    str_window_bp: int | None             # basis points; None when no second snapshot
    str_window_denominator: int           # listings with a known delta on both days
    velocity_win: float | None
    velocity_win_denominator: int

    # distribution of demand
    success_rate: float | None            # sold_qty > 0 over sold_known
    success_rate_denominator: int
    dispersion: float | None              # sellers with sales / sellers with a known sold count
    dispersion_denominator: int

    # saturation
    new_7d: int                           # listings with listed_at in the last 7 days
    new_7d_denominator: int               # rows with listed_at at all
    promoted_pct: float | None
    promoted_denominator: int             # rows where promoted IS NOT NULL

    # price, BUSINESS sellers only — private prices include the buyer-protection fee
    price_median_pence: int | None
    price_median_denominator: int
    price_floor_pence: int | None         # min landed of the five cheapest business listings
    price_floor_denominator: int          # how many of the five actually existed

    # delivery
    eta_median_days: int | None
    eta_median_denominator: int

    # competitor profile (knob 5, reframed)
    feedback_median_bp: int | None
    feedback_median_denominator: int
    feedback_median_n: int | None
    new_account_share: float | None       # accounts under a year old
    new_account_denominator: int


def compute(rows: Sequence[Mapping], *,
            now: datetime | None = None,
            deltas: Mapping[str, tuple[int, int]] | None = None) -> Metrics:
    """Spec §6 over ROW_FIELDS rows. `deltas` maps source_id -> (delta_sold, delta_days)."""


@dataclass(frozen=True)
class KnobResult:
    knob: str            # "min_margin" | "sell_through" | "price_band" | "delivery" | "seller_quality" | "saturation"
    value: object
    threshold: object
    passed: bool | None  # None = unknown, because the denominator was zero
    denominator: int
    coverage: float
    note: str            # e.g. "lifetime, not window - not comparable to the threshold"


def check(m: Metrics, knobs: Knobs, *,
          competitors: int | None = None,
          max_cost_at_floor_pence: int | None = None,
          max_cost_at_median_pence: int | None = None) -> list[KnobResult]:
    """Pass/fail per knob, in knob order. A zero denominator is `None`, never False."""
```

The three rules that make these honest, restated as testable behaviour:

1. **Missing sold is unknown, not zero.** A row with `sold_qty is None` is
   excluded from `sold_known` and from every numerator. `coverage` reports how
   much was excluded. Halving `sold_known` while holding the numerator changes
   `str_lifetime`; a test asserts exactly that.
2. **Lifetime is not a window.** `check()` compares `str_window_bp` against
   `knobs.str_threshold_bp`. When `str_window_bp is None`, the sell-through knob
   comes back `passed=None` with the note that only a lifetime figure exists.
   It never falls back to comparing `str_lifetime`. A test asserts it.
3. **The result-count header is never a denominator.** Numerator and denominator
   come from the same observed rows. Nothing in `metrics.py` may take a count as
   an argument.

Private sellers (`seller_type == "private"`) are excluded from
`price_median_pence`, `price_floor_pence` and `eta_median_days`, and counted in
`n_private`. Rows with `seller_type is None` are **not** assumed business: they
are excluded from the price aggregates too, and `price_median_denominator` says
how many survived.

Median of an even-sized set of integer pence rounds half up: `(a + b + 1) // 2`.
Document it; a test pins it.

`metrics.py` imports `knobs` (same stream) and **nothing else from phoneloop**.
It must not import `fees`, `market_store` or `identity` — the board composes them.

### 2.11 `phoneloop/snapshots.py` — s5 (new)

```python
@dataclass(frozen=True)
class ListingDelta:
    source_id: str
    sold_a: int | None
    sold_b: int | None
    delta_sold: int | None   # None when either side is NULL or the listing is missing on a side
    days: float              # elapsed between the two observations
    known: bool              # delta_sold is not None


@dataclass(frozen=True)
class Difference:
    date_a: date
    date_b: date
    listings: dict[str, ListingDelta]
    n_listings: int
    n_known: int
    coverage: float

    def by_product(self, key_of: Callable[[str], str | None]) -> dict[str, tuple[int, int]]:
        """product_key -> (delta_sold, delta_days), summing only known deltas."""

    def by_niche(self, niche_of: Callable[[str], str | None]) -> dict[str, tuple[int, int]]:
        """niche key -> (delta_sold, delta_days), summing only known deltas."""


def difference(store: MarketStore, date_a: date | str, date_b: date | str, *,
               source: str = "ebay") -> Difference:
    """Per-listing sold_qty delta between two snapshot dates. NULL on either side is unknown, never zero."""
```

A listing present on day A and absent on day B is `delta_sold=None`,
`known=False` — it may have sold out or been ended, and the two are not
distinguishable from the store. A negative delta (a relist reset the counter) is
recorded as observed and excluded from `known` sums by the caller if it chooses;
`difference` does not silently clamp it. Say which you did in the docstring.

The output shape `(delta_sold, delta_days)` is exactly what `metrics.compute`
takes as `deltas`.

### 2.12 `phoneloop/validation.py` — s5 (new)

```python
VALIDATION_SCHEMA = pa.schema([
    ("product_key", pa.string()),
    ("identity_tier", pa.string()),
    ("derived_str_lifetime", pa.float64()),
    ("derived_velocity", pa.float64()),
    ("terapeak_str_90d_bp", pa.int32()),              # basis points
    ("terapeak_sellers", pa.int32()),
    ("terapeak_avg_sold_price_pence", pa.int64()),    # pence
    ("checked_at", pa.timestamp("us", tz="UTC")),
])


@dataclass(frozen=True)
class ValidationRecord:
    product_key: str
    identity_tier: str
    derived_str_lifetime: float | None
    derived_velocity: float | None
    terapeak_str_90d_bp: int | None
    terapeak_sellers: int | None
    terapeak_avg_sold_price_pence: int | None
    checked_at: datetime


@dataclass(frozen=True)
class TierError:
    identity_tier: str
    n: int
    median_abs_error_bp: int | None   # |derived - official| as basis points of the official figure
    median_signed_error_bp: int | None


class ValidationTable:
    """Sam's Terapeak readings against the derived figures, so derived STR earns trust or gets replaced."""

    def __init__(self, data_dir: Path | str): ...
    def add(self, record: ValidationRecord | Mapping) -> int: ...
    def list(self) -> list[dict]: ...
    def error_by_tier(self) -> dict[str, TierError]:
        """Derived-vs-official error per identity tier. A tier with no rows is absent, not zero-error."""
```

Lives at `DATA_DIR/validation/*.parquet`. Terapeak's sell-through is a
percentage, so it is stored in basis points; its average sold price is money, so
it is stored in pence. `derived_str_lifetime` and `derived_velocity` are rates,
not percentages, so they stay floats.

### 2.13 `phoneloop/board.py` — s5 (new)

```python
@dataclass(frozen=True)
class BoardRow:
    key: str                              # niche key or product key
    label: str                            # human-readable
    level: str                            # "niche" | "product"
    identity_tier: str
    metrics: Metrics
    knobs: list[KnobResult]
    max_cost_at_floor_pence: int | None
    max_cost_at_median_pence: int | None
    str_source: str                       # "window" | "lifetime" — printed on the row
    score: float


def rank_niches(store: MarketStore, knobs: Knobs, *,
                difference: Difference | None = None,
                limit: int = 100) -> list[BoardRow]:
    """Rank niches (leaf x price band) by knobs passed, then demand, then size."""


def rank_products(store: MarketStore, knobs: Knobs,
                  niches: Sequence[str], *,
                  difference: Difference | None = None,
                  limit: int = 100) -> list[BoardRow]:
    """Rank products within the given niches, at the best identity tier each supports."""


def render_table(rows: Sequence[BoardRow]) -> str:
    """The terminal board: one line per row, every knob value with its pass/fail and coverage."""


def render_html(rows: Sequence[BoardRow], *, title: str,
                generated_at: datetime) -> str:
    """A self-contained HTML board — no CDN, no external font, no fetch."""


def write_report(rows: Sequence[BoardRow], data_dir: Path | str, *,
                 name: str | None = None) -> Path:
    """Write the HTML board under DATA_DIR/reports/. Returns the path."""
```

`score` is defined, not vibes: `passed - failed` across the knob results, ties
broken by `str_window_bp or 0`, then `n_listings`. Unknown knobs (`passed is
None`) count as neither. Write the tie-break into the docstring.

Every board row shows, for each knob: the value, the threshold, pass/fail/unknown,
and the denominator coverage behind it. `str_source` says `window` wherever two
snapshots exist and `lifetime` otherwise, and a `lifetime` row must visibly say
that its sell-through is not comparable to the knob-2 threshold.

`board.py` **reads the store through `market_store`, computes through
`metrics.py`, and prices through `fees.py`.** It re-implements none of them.

### 2.14 `phoneloop/catalog_ebay.py` — s6 (new)

```python
@dataclass(frozen=True)
class Department:
    slug: str
    title: str
    category_id: str          # eBay's top-level id, as a string; "" until read off a real page
    note: str = ""


IN_SCOPE: tuple[Department, ...]
"""Departments where dropship goods live and returns are cheap (spec §9)."""

EXCLUDED: tuple[Department, ...]
"""Departments excluded on purpose, each with the reason in `note` (spec §9)."""


@dataclass(frozen=True)
class Leaf:
    category_id: str
    title: str
    path: tuple[str, ...]     # department -> ... -> leaf, titles


TREE_PATH: Path
"""ebay_categories.json at the repo root, beside catalog_tree.json. Generated, committed, small."""


def load_tree(path: Path | None = None) -> dict:
    """The recorded eBay UK category tree. Missing file raises with the refresh command in the message."""


def leaves_for(department: str, *, tree: dict | None = None) -> tuple[Leaf, ...]:
    """Every childless category under one in-scope department. Unknown department raises KeyError."""


def top_level_of(tree: dict | None = None) -> dict[str, str]:
    """leaf category id -> top-level category id. This is what fees.max_cost needs to price a leaf."""


# --- Taxonomy API, UNVERIFIED: shaped from the docs, never called without a keyset ---

def get_default_category_tree_id(client, marketplace_id: str = "EBAY_GB") -> str:
    """UNVERIFIED: Commerce Taxonomy getDefaultCategoryTreeId. Never exercised — no keyset exists."""


def get_category_subtree(client, tree_id: str, category_id: str) -> dict:
    """UNVERIFIED: Commerce Taxonomy getCategorySubtree. Never exercised — no keyset exists."""
```

The in-scope departments (spec §9): Home, Furniture & DIY · Garden & Patio · Pet
Supplies · Sporting Goods · Vehicle accessories (**not** parts) · Mobile phone
accessories · Crafts · Health & Beauty (non-consumable devices and tools) ·
Toys & Games (non-branded).

Excluded, each carrying its reason: clothing and shoes (sizing returns) · branded
electronics (counterfeit exposure) · fragile goods · consumables and cosmetics
(regulation) · anything with safety compliance (car seats, mains electrical).

`category_id` starts as `""` for any department whose id you have not read off a
real page. Fill them from your own recording of `/n/all-categories`. Do not guess
an id. A department with `category_id == ""` after the live run goes in
`unverified[]` by name.

**Generated artefact:** `scripts/refresh_ebay_categories.py` writes
`ebay_categories.json` at the repo root. s6 owns that file too — nobody else
creates or reads it before integration.

---

## 3. ENVIRONMENT VARIABLES

| Variable | Value / default | Read by | Notes |
|----------|-----------------|---------|-------|
| `PHONELOOP_FIXTURES` | `C:/Users/sampo/.phoneloop/fixtures` | `phoneloop/harness.py` (`fixtures_root`, `Cassette.__init__` when `root` is omitted) | Unset falls back to the repo's `fixtures/`. **No other module reads it.** Scripts and tests set it; they never re-implement the lookup |
| `PHONELOOP_HARNESS` | unset ⇒ `replay`; `record`; `live` | `phoneloop/harness.py` (`mode`) | Unset means replay means no network. A missing recording raises `MissingRecording` |
| `PHONELOOP_EBAY_CLIENT_ID` | — | `phoneloop/ebay_api.py` (`credentials`) | `os.environ` first, then `proxies.load_env_file()` from `~/.phoneloop/.env`. Never logged, never recorded |
| `PHONELOOP_EBAY_CLIENT_SECRET` | — | `phoneloop/ebay_api.py` (`credentials`) | as above |
| `PHONELOOP_DATA_DIR` | `~/.phoneloop/data` | `phoneloop/config.py` (`load`) | Deliberately outside the repo and outside OneDrive — OneDrive takes file handles and kills a parquet writer mid-write. `validation.py`, `board.py` and `snapshots.py` take `data_dir` from `config.load()`, never from the env directly |
| `PHONELOOP_PROXY_URL` | — | `phoneloop/proxies.py` (`proxy_url`) | Residential gateway. Redact before it can reach any log line, exception or fixture |
| `PHONELOOP_<KNOB>` (12 names, §2.8) | per-knob defaults | `phoneloop/knobs.py` (`Knobs.from_env`) | All integers. A non-integer raises rather than silently defaulting |
| `PHONELOOP_MAX_STORE_BYTES` | 20 GB | `phoneloop/config.py` | Existing. Hard ceiling so a runaway loop cannot eat the shared Drive quota |

---

## 4. THE SIX STREAMS

Five run immediately and in parallel: **s1, s2, s3, s4, s6**. **s5 starts only
after s1 and s4 are accepted**, and its first act is to merge both their branches
into its own.

Ownership is absolute. Nobody may touch a file on another stream's list. If you
believe you must, **stop and put it in `unverified[]`** — do not edit it.

### s1_store_fees — store columns, snapshot semantics, fee model, per-source cap

**Owns:** `phoneloop/market_store.py` · `phoneloop/fees.py` (new) ·
`phoneloop/catalog.py` · `tests/test_store.py` · `tests/test_fees.py` (new) ·
`tests/test_catalog_cap.py` (new)

Contracts: §1 (the whole row), §2.1, §2.2, §2.3.

Note that `tests/test_store.py` on `master` tests the *old* `phoneloop/store.py`,
not `market_store.py`. Add the `MarketStore` tests to it; do not delete what is
there.

### s2_html_transport — extended row parser, item-page parser, reworked crawler, live recordings

**Owns:** `phoneloop/ebay.py` · `phoneloop/ebay_item.py` (new) ·
`phoneloop/harness.py` (**only** the `PHONELOOP_FIXTURES` root override) ·
`scripts/bulk_ebay.py` · `scripts/record_ebay.py` (new) · `tests/test_ebay.py` ·
`tests/test_ebay_item.py` (new) · `tests/test_harness.py` (**only** the
root-override test)

Contracts: §2.4, §2.5, §2.6, and §1 for the row `to_row` produces.

`scripts/bulk_ebay.py` changes:

- **Direct first**, residential proxy only after a 403 or a captcha. The current
  default of proxy-first is inverted.
- **Drop `LH_LocatedIn=1`.** Record `origin_country` instead. The docstring's
  long justification for that parameter is now the justification for *removing*
  it — rewrite it, do not leave a comment claiming something the code no longer
  does. The `uk_share` / `MIN_UK_SHARE_OK` guard becomes an observation printed
  on the run, not a reason to skip a page.
- **`_sacat=<leaf id>` category sweep** alongside the existing `_nkw` keyword
  sweep. `leaf_category_id` goes onto every row.
- **Cap via `catalog.result_cap("ebay")`**, with "the page returned zero rows" as
  the saturation test. Never the rounded header count.
- **Rows carry every `ROW_FIELDS` column**, built from `market_store.empty_row()`.
- **Checkpoint/resume** exactly as now.
- **A blocked fetch marks the shard unknown, never empty.** A captcha and a dead
  market must not look the same downstream.
- **Honours `harness.mode()`.** In replay it never launches a browser: page
  extractions go through `harness.recorded(cassette, "ebay", name, produce)`,
  the pattern already in the file's neighbourhood. This is what makes the
  integrator's offline end-to-end run possible.

`scripts/record_ebay.py` records **real** pages through the harness into
`C:/Users/sampo/.phoneloop/fixtures`:

- at least **3 search pages** — two different queries or leaves, plus page 2 of one
- **6 item pages**
- **one `/p/<epid>` product page**, found from an `epid=` href on a search page

Run it. If eBay blocks you after both a direct and a proxy attempt, say so in
`unverified[]` and **do not fabricate a fixture**. Tests run against those real
recordings with `PHONELOOP_FIXTURES=C:/Users/sampo/.phoneloop/fixtures`; tests
that need no page use `tmp_path` fixtures built from real captured text.

### s3_api_transport — Browse API: auth, search, getItem, epid search, quota budget, gate script

**Owns:** `phoneloop/ebay_api.py` (new) · `scripts/api_gate.py` (new) ·
`tests/test_ebay_api.py` (new) · `tests/fixtures_synthetic/` (new, committed,
`SYNTHETIC_*` files only)

Contracts: §2.7, and §1 for the row `parse_summary` produces.

`scripts/api_gate.py` is the thirty-minute test from spec §2. If the keyset env
vars are absent it prints exactly why — which variables, which file it looked in,
what to do — and **exits 2 without touching the network**. It never fabricates a
result.

**No live keyset exists yet.** Shape the parsers from the official OAS at
`https://developer.ebay.com/api-docs/master/buy/browse/openapi/3/buy_browse_v1_oas3.json`
and store any example payload under `tests/fixtures_synthetic/` as
`SYNTHETIC_oas_<name>.json` with a top-level `"_synthetic"` key naming the OAS
URL, the date fetched, and which schema the example came from. Every test that
touches one has `synthetic` in its name. A live test exists and is skipped with
exactly the reason `"needs production keyset"`.

### s4_identity_metrics — identity tiers, knobs, and every metric with its denominator

**Owns:** `phoneloop/identity.py` (new) · `phoneloop/metrics.py` (new) ·
`phoneloop/knobs.py` (new) · `tests/test_identity.py` (new) ·
`tests/test_metrics.py` (new) · `tests/test_knobs.py` (new)

Contracts: §2.8, §2.9, §2.10.

s4 depends on **no other stream**. It consumes `ROW_FIELDS`-shaped dicts, which
this document defines in §1 — build them as literals in your tests from the
column list here.

### s6_category_scope — in-scope departments, leaf enumeration, live recording of the category tree

**Owns:** `phoneloop/catalog_ebay.py` (new) ·
`scripts/refresh_ebay_categories.py` (new) · `tests/test_catalog_ebay.py` (new)
· plus the generated `ebay_categories.json` at the repo root

Contracts: §2.14.

`scripts/refresh_ebay_categories.py` records `https://www.ebay.co.uk/n/all-categories`
and the department pages it links for the **in-scope departments only**, through
the harness, into `C:/Users/sampo/.phoneloop/fixtures` with `PHONELOOP_FIXTURES`
set — passing the root positionally to `Cassette`, since s2's env override has
not merged yet. Then it parses category ids and titles into `ebay_categories.json`.

**Run it live.** Report how many leaves you got per in-scope department. Tests run
against the real recording.

### s5_snapshots_board — Tier 3 differencing, the niche board, the validation table, the daily runner

**Starts after s1 and s4 are accepted.** First action, inside its own worktree:

```
git merge --no-ff build/s1_store_fees
git merge --no-ff build/s4_identity_metrics
```

**Owns:** `phoneloop/snapshots.py` (new) · `phoneloop/validation.py` (new) ·
`phoneloop/board.py` (new) · `scripts/niche_board.py` (new) ·
`scripts/run_daily.cmd` (new) · `tests/test_snapshots.py` (new) ·
`tests/test_board.py` (new) · `tests/test_validation.py` (new)

Contracts: §2.11, §2.12, §2.13.

`scripts/run_daily.cmd` pins the installer python
(`%LOCALAPPDATA%\Programs\Python\Python313\python.exe` — the MSIX build exits
silently when detached), runs **sweep → deepen → difference → board**, checkpoints
each step, and documents the Task Scheduler line as a comment at the top of the
file. 03:00 daily, per spec §10.

---

## 5. ACCEPTANCE

Every stream, without exception:

```
cd <your worktree> && "$LOCALAPPDATA/Programs/Python/Python313/python.exe" -m pytest -q -p no:cacheprovider
```

**All 405 existing tests still pass, plus yours.** A stream that breaks an
existing test has broken the contract, not improved it.

For every guard listed below: commit the working state first, then break the
code, run the test, **see it fail**, restore, run it again, see it pass. Name
the tests you watched go red in your report. A guard nobody watched go red is
decoration.

### s1_store_fees

Tests that must exist and pass:

- `tests/test_store.py`: a `source="ebay"` listing observed twice with the **same**
  `sold_qty` writes **two** rows; the same listing on `source="vinted"` writes
  **one**. This is the snapshot-semantics guard.
- `tests/test_store.py`: an eBay listing observed twice with a **different**
  `sold_qty` writes two rows and the second `content_hash` differs — the
  `_HASHED` guard.
- `tests/test_store.py`: `ROW_FIELDS` has 37 entries, matches `SCHEMA.names`
  exactly and in order, and `empty_row()` produces every one of them.
- `tests/test_store.py`: a batch containing both an eBay and a Vinted row writes
  each into its own `source=` partition — the partitioning guard.
- `tests/test_store.py`: money round-trips exactly; a `None` `total_pence` stays
  `None` and never becomes `0`.
- `tests/test_fees.py`: the three worked examples from §2.2, in integer pence,
  with the arithmetic written out in each test's docstring.
- `tests/test_fees.py`: the spec's own identity — Mobile Phones at
  `vat_on_fees_bp=12000` costs `12.3% + 48p`.
- `tests/test_fees.py`: `vat_on_fees_bp=10000` (VAT-registered) removes exactly
  the VAT uplift and nothing else.
- `tests/test_fees.py`: an unknown `leaf_category_id` charges
  `EVERYTHING_ELSE_BP`, not the cheapest rate.
- `tests/test_fees.py`: `max_cost` returns a negative number rather than clamping,
  for a price below the fixed margin floor.
- `tests/test_fees.py`: `bp()` never returns a float and never uses one.
- `tests/test_catalog_cap.py`: `result_cap("vinted") == 960`,
  `result_cap("ebay") == 10_000`, `result_cap("nope")` raises.
- `tests/test_catalog_cap.py`: `is_saturated(900)` is `True` for vinted and
  `False` for ebay — the "cap is per source" guard from spec §11.

**Watch red (minimum):** the snapshot-semantics guard (make eBay hash-skip like
Vinted), and the per-source cap guard (make `result_cap` ignore its argument).

`unverified[]` may contain: any fee-table row you could not source from a real
page, named; the top-level category ids you could not read; the import-VAT
treatment (spec §13.6, expected to stay unverified).

### s2_html_transport

- `tests/test_harness.py`: one test, that `Cassette()` honours
  `PHONELOOP_FIXTURES` and that `Cassette(explicit_path)` ignores it. Nothing
  else in that file changes.
- `tests/test_ebay.py`: every existing test still passes unchanged.
- `tests/test_ebay.py`: sold count parsed from a real captured row; a row with no
  sold count yields `None` — asserted as `is None`, **not** as falsy.
- `tests/test_ebay.py`: seller id, feedback basis points and feedback count from
  a real captured row, including a `0% positive (0)` row.
- `tests/test_ebay.py`: the `derosnopS` row parses `promoted=True` and is **kept**.
- `tests/test_ebay.py`: an `/itm/123456` decoy row returns `None`.
- `tests/test_ebay.py`: ETA days from "in N days" and from a range; "Get it by
  <date>" with `today=None` yields `None`.
- `tests/test_ebay.py`: `to_row` output has exactly the `ROW_FIELDS` keys.
- `tests/test_ebay_item.py`: against the **real recorded item pages** — sold
  count, `"N available"`, `"More than N available"` (with the floor flag),
  feedback, `"Joined Mon YYYY"`, `"NNNK items sold"`, business-seller marker,
  `SEMANTIC_DATA.startDate`, Brand/MPN/EAN specifics.
- `tests/test_ebay_item.py`: a junk seller-typed MPN is **recorded** in `mpn`
  and the module makes no identity claim about it.

**Watch red (minimum):** the missing-sold-count guard (make it return `0`), and
the decoy guard (stop dropping `/itm/123456`).

`unverified[]` may contain: pages eBay blocked after both a direct and a proxy
attempt, named with what you tried; whether `startDate` survives a relist (spec
§13.2); any field you found on some item pages and not others, with the counts.
It may **not** contain a claim that a recording exists when it does not.

### s3_api_transport

- `tests/test_ebay_api.py`: `credentials()` returns `None` when neither the
  environment nor a `.env` supplies a keyset, and never raises on a missing file.
- `tests/test_ebay_api.py`: `Budget.spend` raises `QuotaExhausted` at the limit;
  a stored day that is not today resets `used` to 0; the checkpoint round-trips.
- `tests/test_ebay_api.py`: `price_filter` and `item_start_date_filter` produce
  the exact filter strings from spec §2 — pinned as literal expected strings.
- `tests/test_ebay_api.py`: **synthetic** — `parse_summary` on a
  `SYNTHETIC_oas_*.json` payload produces a dict whose keys are exactly
  `ROW_FIELDS`, with `sold_qty is None` (search summaries carry no sold count).
- `tests/test_ebay_api.py`: **synthetic** — `parse_item` maps
  `estimatedSoldQuantity`, `estimatedAvailableQuantity`, `gtin`, `mpn`,
  `inferredEpid` onto the right columns, and `inferredEpid` sets
  `identity_tier="inferred"` while a true `epid` sets `"catalogued"`.
- `tests/test_ebay_api.py`: in replay mode with no recording,
  `BrowseClient.search` raises `MissingRecording` — not an empty result set.
- `tests/test_ebay_api.py`: `token()` in replay mode makes no request and returns
  `REPLAY_TOKEN`.
- `tests/test_ebay_api.py`: a live test, skipped with exactly
  `"needs production keyset"`.
- Every synthetic test name contains `synthetic`. Every synthetic fixture carries
  a top-level `"_synthetic"` key naming the OAS URL and the date fetched.

**Watch red (minimum):** the `MissingRecording` guard (make replay return `{}`),
and the `ROW_FIELDS` key-set guard (drop a column from `parse_summary`).

`unverified[]` **must** contain, because no keyset exists: whether
`item_summary/search` works on a fresh production keyset (spec §13.1); whether
`itemCreationDate` and `sellerAccountType` are present (spec §2's gate table);
`inferredEpid` coverage (§13.3); whether `getItem` returns
`estimatedSoldQuantity: 0` (§13.4); store age via the API (§13.5); `getItems`
batching (§13.7). Every parser you wrote is shape-from-docs and must say so.

### s4_identity_metrics

- `tests/test_knobs.py`: every default matches §2.8; `from_env` overrides each
  one; a non-integer value raises; `str_threshold_bp`'s docstring says it is a
  chosen convention.
- `tests/test_knobs.py`: `Knobs` carries all five `FeeKnobs` attribute names
  (asserted from a literal tuple, with a comment pointing here).
- `tests/test_identity.py`: each of the four tiers from a row that supports it;
  `gtin` beats `epid` for the key; an `inferred` epid keys as `epid~:`.
- `tests/test_identity.py`: a junk MPN never reaches a product key at any tier.
- `tests/test_identity.py`: two titles at Jaccard ≥ 0.6 cluster together and two
  below it do not, with the shingle sets written out in the docstring.
- `tests/test_identity.py`: `cluster()` raises `ValueError` on rows spanning two
  `leaf_category_id`s.
- `tests/test_metrics.py`, every expectation **hand-computed with the arithmetic
  in the test docstring**:
  - halving `sold_known` while holding the numerator changes `str_lifetime` and
    changes `coverage` — the "coverage < 1 changes the denominator" guard;
  - a private-seller row increments `n_private` and leaves `price_median_pence`
    unchanged;
  - a row with `sold_qty is None` is excluded from every numerator and is not
    counted as zero;
  - `check()` returns `passed=None` for sell-through when only a lifetime figure
    exists, and **never** compares `str_lifetime` to `str_threshold_bp` — the
    "lifetime is never compared to the window threshold" guard;
  - `price_floor_pence` is the min landed of the five cheapest **business** rows,
    and `price_floor_denominator` reports fewer than five when fewer exist;
  - an even-count median of integer pence rounds half up.

**Watch red (minimum):** the lifetime-vs-window guard (make `check` fall back to
`str_lifetime`), and the missing-sold guard (make `None` count as `0`).

`unverified[]` may contain: the choice of `str_threshold_bp`; any spec §6 formula
whose intent you had to interpret, with the interpretation stated.

### s6_category_scope

- `tests/test_catalog_ebay.py`: `IN_SCOPE` and `EXCLUDED` match spec §9 exactly,
  every excluded department carries a reason.
- `tests/test_catalog_ebay.py`: `leaves_for` against the **real recording**
  returns leaves for each in-scope department, every id numeric-as-string, no
  duplicates, and every leaf reachable from its department in `path`.
- `tests/test_catalog_ebay.py`: `leaves_for("not-a-department")` raises `KeyError`.
- `tests/test_catalog_ebay.py`: `top_level_of()` maps every leaf to a top-level
  id present in `IN_SCOPE` or `EXCLUDED`.
- `tests/test_catalog_ebay.py`: `load_tree` on a missing file raises with the
  refresh command in the message — never returns `{}`.
- `tests/test_catalog_ebay.py`: the Taxonomy stubs are never called without a
  client; a test asserts they raise rather than fabricating a tree.

**Watch red (minimum):** the missing-tree guard (make `load_tree` return `{}`).

`unverified[]` may contain: departments whose `category_id` you could not read
off a real page, named; department pages eBay blocked; both Taxonomy functions,
which are shape-from-docs and were never exercised. Report the **leaf count per
in-scope department** as a table in your report.

### s5_snapshots_board

- `tests/test_snapshots.py`: build a store in `tmp_path` with two dated snapshots
  of real-shaped rows (rows shaped from §1, with real captured values where you
  have them); assert the per-listing deltas.
- `tests/test_snapshots.py`: a listing present on day A and **missing on day B**
  is `delta_sold=None`, `known=False` — never `0`, never "sold out".
- `tests/test_snapshots.py`: `sold_qty` NULL on either side is unknown.
- `tests/test_snapshots.py`: `by_product` and `by_niche` sum only known deltas
  and report their own coverage.
- `tests/test_board.py`: ranking order over a small store is exactly the
  documented `score` and tie-break.
- `tests/test_board.py`: a niche with two snapshots shows `str_source="window"`;
  one with a single snapshot shows `"lifetime"` and says so in the rendered row.
- `tests/test_board.py`: every board row carries `max_cost` at both
  `price_floor` and `price_median`, and `board.py` gets them from `fees.max_cost`
  rather than computing them.
- `tests/test_board.py`: `render_html` output contains no `http://` or `https://`
  resource reference — the self-contained guard.
- `tests/test_validation.py`: `add`/`list` round-trip; `error_by_tier` on a tier
  with no rows is **absent from the mapping**, not a zero error.

**Watch red (minimum):** the missing-on-day-B guard (make it `0`), and the
`str_source` guard (make `lifetime` print as `window`).

`unverified[]` may contain: anything about the board's ranking weights you chose
rather than derived; whether a negative delta is a relist or a correction.

---

## 6. INTEGRATION

### 6.1 Merge order

```
1. s1_store_fees        # ROW_FIELDS: everything downstream reads it
2. s4_identity_metrics  # no dependencies at all
3. s2_html_transport    # harness override + the HTML row
4. s6_category_scope    # leaves + top_level_of; no code dependency on s2
5. s3_api_transport     # the API row, checked against s1's ROW_FIELDS
6. s5_snapshots_board    # already merged s1 and s4; resolve against the rest
```

After each merge: full suite, foreground, installer python.

### 6.2 Integrator's wiring, after the merges

Three joins this document defers to integration, because no single stream can
make them:

1. `fees.TOP_LEVEL_ID_TO_NAME` filled from `catalog_ebay.top_level_of()` and the
   department ids s6 read off real pages.
2. `tests/test_knobs.py`'s literal `FeeKnobs` attribute tuple replaced with a
   real `isinstance`/attribute cross-check against `phoneloop.fees.FeeKnobs`.
3. `scripts/niche_board.py` pointed at `catalog_ebay.IN_SCOPE` for its niche list.

### 6.3 The end-to-end command, offline, on real recordings

Run from the main checkout, in the foreground. `PHONELOOP_HARNESS` **unset** —
that is what makes it replay, and a missing recording raises rather than
producing an empty board.

```bash
cd "C:/Users/sampo/OneDrive/Desktop/Python projects/PhoneLoop" \
&& unset PHONELOOP_HARNESS \
&& export PHONELOOP_FIXTURES=C:/Users/sampo/.phoneloop/fixtures \
&& export PHONELOOP_DATA_DIR=C:/Users/sampo/.phoneloop/e2e \
&& PY="$LOCALAPPDATA/Programs/Python/Python313/python.exe" \
&& "$PY" -m pytest -q -p no:cacheprovider \
&& "$PY" scripts/refresh_ebay_categories.py --offline \
&& "$PY" scripts/bulk_ebay.py --leaf-sweep --max-pages 3 --fresh \
&& "$PY" scripts/niche_board.py --html
```

It passes when:

- the full suite is green;
- `bulk_ebay.py` never launches a browser (replay mode) and writes rows into
  `$PHONELOOP_DATA_DIR/market/date=*/source=ebay/`;
- `niche_board.py` prints a terminal table and writes an HTML file under
  `$PHONELOOP_DATA_DIR/reports/`;
- every board row shows each knob's value, its pass/fail/unknown, its denominator
  coverage, `max_cost` at both `price_floor` and `price_median`, and its identity
  tier;
- a sell-through printed from a single snapshot is labelled `lifetime` and says
  it is not comparable to the knob-2 threshold.

It **fails** if any step produces an empty result where a recording is missing.
Empty is not an answer here; `MissingRecording` is.

`$PHONELOOP_DATA_DIR` is under `~/.phoneloop` and is **never committed**.

---

## 7. WORKTREE PROTOCOL — verbatim, for workers

Your session's cwd is **not** the repo. Every command starts with a `cd`.

```bash
cd "C:/Users/sampo/OneDrive/Desktop/Python projects/PhoneLoop" && git worktree add "C:/Users/sampo/.phoneloop/wt/<stream_key>" -b build/<stream_key> master
```

Replace `<stream_key>` with your stream's key exactly: `s1_store_fees`,
`s2_html_transport`, `s3_api_transport`, `s4_identity_metrics`,
`s5_snapshots_board`, `s6_category_scope`.

Then:

- **Work only in `C:/Users/sampo/.phoneloop/wt/<stream_key>`.** Never edit the
  main checkout.
- Run tests there:
  ```bash
  cd "C:/Users/sampo/.phoneloop/wt/<stream_key>" && "$LOCALAPPDATA/Programs/Python/Python313/python.exe" -m pytest -q -p no:cacheprovider
  ```
- **Stage explicit paths only.** One `git add <path>` per file you own. Never
  `git add -A`, never `git add .`, never `git commit -a`. This repo holds
  untracked secrets and a live key has reached a public repo this way before.
- Commit on your branch. Commit the working state **before** you break anything
  to watch a guard go red, and never commit the broken state.
- Report: your **branch name**, your **worktree path**, the tests you watched go
  red, and `unverified[]`.

s5 additionally, as its first action inside its worktree:

```bash
cd "C:/Users/sampo/.phoneloop/wt/s5_snapshots_board" && git merge --no-ff build/s1_store_fees && git merge --no-ff build/s4_identity_metrics
```

---

## 8. What "unverified" means

`unverified[]` is where a worker records what it could not establish. It is not a
disclaimer and it is not a place to hedge work that was actually done. It holds:

- a page or endpoint that blocked you, and what you tried;
- a value shaped from documentation and never exercised against a real response;
- a default you chose because the spec named a knob without giving a number;
- a file you needed to touch that you do not own.

It must **never** hold a claim that something works. If it works, you ran it and
you say what you ran. If you did not run it, it is unverified — and an unverified
parser that is presented as tested is the exact failure this project has already
paid for twice.
