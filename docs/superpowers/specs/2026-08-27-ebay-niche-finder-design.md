# eBay Niche Finder — stage one design (revised)

**Date:** 2026-08-27 · **Status:** design, awaiting Sam's review · **Supersedes:** the
first draft of the same day, after an independent review overturned four of its claims.

## 1. What this stage delivers

A system that ranks eBay UK **niches** and **candidate products** by demand and
competition, market-first, and hands the supplier stage a shortlist where every row
carries the **maximum landed cost** at which it still clears the target margin.

The six control knobs from the source design, and how each one is met here:

| # | Knob | Status | How it is computed |
|---|------|--------|--------------------|
| 1 | Min net profit margin | **Inverted** | Output `max_cost` per product (§7). Filtering on a margin needs a supplier cost eBay cannot give. |
| 2 | Sell-through threshold | **Computed, labelled** | `str_lifetime` day one; `str_window` (30-day) once snapshots accumulate. Every figure carries its denominator coverage (§6). |
| 3 | Retail price range | **Computed** | Landed price = item + postage. Business sellers only in price aggregates (§6). |
| 4 | Max delivery + handling | **Partial** | Median delivery ETA competitors already promise. Own supplier window is unknowable until a supplier exists. |
| 5 | Min supplier quality | **Reframed as competitor profile** | Feedback %, feedback count, account type, store age, stock. They describe rivals, and are kept for that. |
| 6 | Market saturation cap | **Computed at the best identity tier** | Distinct sellers per product (catalogued → inferred → cluster, §5), plus share making sales, plus new listings per week. |

Out of scope: supplier scraping (AliExpress, CJ, 1688), image hashing, sale-date
history (login-gated), Marketplace Insights and Feed APIs (both limited release).

## 2. Three legs, not one crawler

**A. eBay Browse API — primary transport.** Structured JSON, 5,000 calls/day default,
200 rows per search call, no browser, no proxy, no captcha. Returns two fields the
HTML search page never shows — `itemCreationDate` and `sellerAccountType` — plus
`epid`, seller feedback, postage and ETA, and item location. `getItem` adds
`estimatedSoldQuantity`, stock, `gtin`/`mpn`, and eBay's own `inferredEpid`.

**B. HTML crawler — fallback transport.** Headed Chrome, direct-first, residential
proxy only on a block. Same fields, but listing age and account type are item-page
only. Kept because eBay's Buy-API requirements page says production is "intended for
eBay partners only" while the Browse overview carries no limited-release banner: the
gate is a test, not a reading of the docs.

**C. Terapeak Product Research — validation, by hand.** Free inside Seller Hub for a
business seller. Official 90-day sell-through, sold price range, and seller count,
filterable by EPID/EAN. The system ranks 1,000 → 100; Sam validates 100 → 10 there,
and the gap between derived and official sell-through is recorded per identity tier
so the derived number earns trust or gets replaced.

### The gate: a thirty-minute API test

Runs the moment the developer account is approved. Production keyset, client
credentials grant, scope `https://api.ebay.com/oauth/api_scope`, header
`X-EBAY-C-MARKETPLACE-ID: EBAY_GB`:

```
GET /buy/browse/v1/item_summary/search
    ?category_ids=<one leaf>
    &filter=price:[15..55],priceCurrency:GBP,conditions:{NEW},buyingOptions:{FIXED_PRICE}
    &limit=200
```

| Result | Decision |
|--------|----------|
| 200 with `itemSummaries` | Transport A is primary. Build the API client first. |
| 403 / insufficient permissions / partner-only | Transport B is primary. Build the HTML crawler first. |
| 200 but `itemCreationDate` or `sellerAccountType` absent | Transport A, with those two fields taken from `getItem` at Tier 2 instead. |

Whichever wins, the other is built second as the fallback — both write the same rows.

## 3. Prerequisites (Sam)

- [ ] eBay Developer Program approval — **pending**, at least one business day.
- [ ] Business seller registration — required by eBay policy for anyone buying to
      resell, and the only account type permitted to use APIs and third-party tools.
      Unlocks Terapeak.
- [ ] VAT decision: registered or not. Sets the `vat_on_fees` multiplier (§7).
- [ ] Item-location assumption for the shortlist (UK stock vs shipped from abroad).
      Sets the `import_vat_term` (§7).

## 4. Pipeline

```
Tier 1  SWEEP      leaf × price band → every listing row       daily, cheap
Tier 2  DEEPEN     item detail for survivors only               daily, 1 row/call
Tier 3  DIFFERENCE today − previous snapshot → windowed demand  daily, no requests
        BOARD      niches ranked, products ranked within them   daily
        VALIDATE   Sam in Terapeak → validation table           by hand
```

### Tier 1 — sweep

Adaptive price-band sharding from `catalog.py`, with the cap made **per source**:
eBay's is 10,000 rows per query (page 45 at 240/page returned zero on a
"21,000+ results" query), not Vinted's 960. A shard is saturated when it reaches the
cap; the saturation test is "the page came back empty", never the rounded result
count in the header.

Filters at sweep time: `conditions:{NEW}`, `buyingOptions:{FIXED_PRICE}`, the price
band. **Not** filtered: seller account type, item location. Both are recorded as
columns. `LH_LocatedIn=1` in the current crawler excludes China-shipped listings —
exactly the competitors a China-sourced dropshipper prices against — and is dropped.

Fields per row: item id, title, price, postage, delivery ETA, condition, seller id,
seller feedback % and count, seller account type, listing created date, `epid`, leaf
category id, item location country, promoted flag, sold count (HTML only — the API
summary does not carry it).

Quota budget: 4,000 of the 5,000 daily calls. At 200 rows per call that is up to
800,000 rows a day; the realistic sweep is 300–400 in-scope leaves × 2–3 bands ×
a few pages. When the budget is spent the sweep checkpoints and resumes next day —
the bulk scripts already do this.

### Tier 2 — deepen

`getItem` (or the HTML item page) for listings that survived the cheap filters:
`estimatedSoldQuantity`, `estimatedAvailableQuantity`, `gtin`, `mpn`, `inferredEpid`,
store joined date and lifetime items sold (HTML item page; API availability
unverified). Budget: the remaining ~1,000 calls. `getItems` batches twenty ids per
call if it is available — to verify.

Two more API calls with specific jobs:

- `search?epid=<epid>` — every live listing for a catalogued product: the exact
  per-product competitor count knob 6 is defined on.
- `filter=itemStartDate:[<7 days ago>..]` per leaf — new listings entering the niche
  per week. A saturation *trend*, free.

### Tier 3 — difference

`sold_qty(today) − sold_qty(previous snapshot)` per listing, summed per product and
per niche. This is what turns a lifetime count into a windowed sell-through.

## 5. Identity — three labelled tiers

eBay carries an `epid` on ~5% of search rows (13/248 measured) and no MPN/EAN on the
search page. That is partial, not unusable. Every product row carries an
`identity_tier`, and the competitor cap is computed at the best tier available:

| Tier | Source | Exactness | Competitor count |
|------|--------|-----------|------------------|
| `catalogued` | `epid` or `gtin` from the API or a `/p/{epid}` page | exact | `search?epid=` live listing count |
| `inferred` | `inferredEpid` from `getItem` (eBay's own match on title + aspects) | near-exact | same, labelled inferred |
| `cluster` | title shingles (3-word) with Jaccard ≥ 0.6 inside one niche | approximate | distinct sellers in the cluster |
| `none` | nothing matched | — | niche-level only |

Seller-typed MPNs on item pages are junk (`"mpn":"Wireless-charge-carphonr"` measured)
and are never used for identity. **Niche** (leaf × price band) remains the primary
ranking level because it needs no identity and is the level a decision is made at.

## 6. Metrics — every figure carries its denominator

For a niche or a product, over the listings observed in that pass:

```
n_listings, n_sellers, n_business, n_private
sold_known    = listings with sold_qty NOT NULL
coverage      = sold_known / n_listings                      # printed beside every STR

str_lifetime  = Σ sold_qty / sold_known                      # label: lifetime
velocity_day1 = Σ (sold_qty / age_days)                      # from listing created date
str_window    = Σ Δsold(30d) / n_listings × 100              # the knob-2 definition, ≥2 snapshots
velocity_win  = Σ Δsold / Δdays

success_rate  = listings with sold_qty > 0 / sold_known
dispersion    = sellers with sales / sellers with sold_known
new_7d        = listings created in the last 7 days          # saturation trend
promoted_pct  = promoted / n_listings

price_median  = median landed price, BUSINESS sellers only   # private prices include the buyer-protection fee
price_floor   = min landed of the five cheapest business listings
eta_median    = median max-delivery days

competitor_profile = median feedback %, median feedback n, share of accounts < 1 year
```

Three rules that make these honest:

- **Missing sold is unknown, not zero.** It is excluded from `sold_known`, and
  `coverage` says how much was excluded. Measured coverage on a full 240-row page was
  13%, not the 32% of the top 60 — Best Match front-loads winners, so a shallow sample
  is a winners' sample.
- **Lifetime is not a window.** `str_lifetime` and `velocity_day1` are averages over
  the listing's whole life and hide decline. They are labelled as such. Only
  `str_window` may be compared against the knob-2 threshold as written.
- **The result-count header is never a denominator.** Numerator and denominator come
  from the same observed rows.

## 7. The inverted margin knob and the fee model

The account has to be a business seller, and business fees are per category, charged
on item price plus postage, and **quoted exclusive of VAT** (eBay help id=4809,
updated 4 Aug 2026):

| Category (examples) | FVF | Effective for a non-VAT-registered seller |
|---------------------|-----|-------------------------------------------|
| Mobile Phones & Communication | 9.9% | 12.3% + £0.48 |
| Vehicle Parts & Accessories | 9.5% | 11.8% + £0.48 |
| Health & Beauty, Toys, Sporting Goods, Garden | 10.9% | 13.5% + £0.48 |
| Home, Furniture & DIY | 11.9% | 14.7% + £0.48 |
| Pet Supplies, Crafts, everything else | 12.9% | 15.9% + £0.48 |

Plus a 0.35% regulatory operating fee and £0.30 per order (≤ £10) or £0.40 (> £10),
also ex-VAT. The full 28-row table lives in `phoneloop/fees.py`, keyed off the leaf
category's top-level ancestor.

```
fees(price)  = price × (fvf_cat + 0.0035) × vat_on_fees + per_order × vat_on_fees
             + price × ad_rate
             + price × import_vat_term
target(price)= max(min_margin_pct × price, min_margin_fixed)
max_cost     = price − fees(price) − target(price)          # integer pence throughout
```

Knobs and defaults: `vat_on_fees = 1.2` (non-registered; `1.0` if registered and
reclaiming), `ad_rate = 0` (Promoted Listings, set when used), `import_vat_term = 0`
(eBay collects VAT from the buyer on orders ≤ £135 shipped from outside the UK;
whether that is added on top of the listed price or treated as inclusive is
**unverified** — the term exists so the answer is a config change, not a rewrite),
`min_margin_pct = 0.25`, `min_margin_fixed = £6`.

`price` is `price_floor` for the conservative figure and `price_median` for the
expected one; the board shows both.

## 8. Store changes

`market_store.py` writes a row only when `content_hash` changes, and `_HASHED`
excludes `extra`. A sold count in `extra` would never trigger a write, so Tier 3
would difference a store that never recorded the second snapshot. Two changes:

1. **New nullable columns** on the shared schema (Vinted rows unaffected):
   `sold_qty`, `qty_available`, `seller_type`, `seller_feedback_bp` (basis points,
   integer), `seller_feedback_n`, `listed_at`, `epid`, `gtin`, `mpn`, `identity_tier`,
   `origin_country`, `promoted`, `leaf_category_id`, `eta_days`, `store_joined`,
   `store_items_sold`. `sold_qty` and `qty_available` join `_HASHED`.
2. **Snapshot semantics for `source = ebay`.** Every observation is written, hash or
   not. Differencing needs "the value on day D", not "the value when it changed".
   ~100k rows a day is a few tens of MB of parquet.

A `validation` table records Sam's Terapeak readings against the derived figures:
`product_key, identity_tier, derived_str_lifetime, derived_velocity, terapeak_str_90d,
terapeak_sellers, terapeak_avg_sold_price, checked_at`.

## 9. Category scope

Stage one sweeps departments where dropship goods live and returns are cheap:
Home, Furniture & DIY · Garden & Patio · Pet Supplies · Sporting Goods · Vehicle
accessories (not parts) · Mobile phone accessories · Crafts · Health & Beauty
(non-consumable devices and tools) · Toys & Games (non-branded).

Excluded on purpose: clothing and shoes (sizing returns), branded electronics
(counterfeit exposure), fragile goods, consumables and cosmetics (regulation),
anything with safety compliance (car seats, electrical mains devices).

Leaves come from the Commerce Taxonomy API (`getCategorySubtree` on the EBAY_GB
tree) or, on the HTML path, from `/n/all-categories` (1,046 department links
measured). The in-scope list is a config file, `phoneloop/catalog_ebay.py`.

## 10. Harness, schedule, failure

**Harness first.** Record real API responses and real pages once; the whole pipeline
then runs offline in seconds. A missing recording raises. Fixtures come from real
captures only — the existing `harness.py` pattern.

**Schedule.** Tier 1 at 03:00 daily via Task Scheduler on the installer Python
(the MSIX build exits silently when detached), then Tier 2, Tier 3, board. Each tier
checkpoints and is resumable.

**Failure degrades toward silence at the loop, and is counted on the board.**
API 429 or quota exhaustion: stop for the day, resume from checkpoint. HTML captcha:
switch to proxy for that page; proxy 403 (measured today on the search page): back
off and retry direct later. A blocked fetch marks a niche **unknown**, never empty.

## 11. Traps this design guards against

- The first HTML result row is a template decoy pointing at `/itm/123456`.
- Sponsored rows carry reversed text (`derosnopS`) — parsed as `promoted`, not ignored.
- Private-seller prices include the buyer-protection fee (7% ≤ £20, 4% £20–£300,
  +£0.10) and are excluded from price aggregates.
- A 403 that survives a re-handshake is the address, not the session (from the
  Vinted work): rotate, don't retry.
- `RESULT_CAP` is per source. 960 is Vinted's; 10,000 is eBay's.

## 12. Testing

- Parsers (API JSON → row, HTML row text → row) against recorded real fixtures.
- Metric functions against hand-computed expectations, including the denominator
  labels and the unknown-vs-zero rule.
- Fee model against the three worked rows in §7, in integer pence.
- Store: the same listing observed twice with a different `sold_qty` produces two
  rows for `source = ebay`, one for `source = vinted`.
- Identity tier assignment, including the junk-MPN case.
- A guard that the cap constant is looked up per source.

## 13. Open verifications (each becomes a task, none blocks the design)

1. `item_summary/search` works on a fresh production keyset without Partner Network approval.
2. `itemCreationDate` / item-page `startDate` survives a relist.
3. `inferredEpid` coverage on a real sweep.
4. `getItem` returns `estimatedSoldQuantity: 0` for single-quantity listings (would fix the success-rate denominator).
5. Store age via the API, or item page only.
6. Import-VAT treatment on the listed price.
7. `getItems` batch availability.

## 14. Milestones

- **M1 — transport + store.** API test; whichever transport wins; store columns and
  snapshot semantics; harness recordings; one leaf swept end to end.
- **M2 — metrics + board.** Niche and product metrics with denominators; identity
  tiers; fee model; `scripts/niche_board.py` with pass/fail per knob.
- **M3 — time.** Daily schedule; Tier 3 differencing after the second snapshot;
  `str_window` replaces `str_lifetime` on the board where it exists.
- **M4 — validation loop.** Terapeak validation table; derived-vs-official error per
  identity tier; decide whether derived STR stays.
- **M5 — fallback transport**, if not already built as primary.

## 15. Live corrections, 2026-08-28 (measured during the first recording session)

Four claims above turned out to be wrong or incomplete once the built sweep ran
live. Each is corrected here rather than silently edited into the sections
above, so the record shows what the design believed and what the market did.

1. **The browser build is the gate, not the address.** From the home IP
   (86.20.84.13) eBay served the 403 "Something went wrong" page to Playwright's
   bundled Chromium 148 — with and without `navigator.webdriver` and
   `--enable-automation` suppressed — and a 200 to Chrome for Testing 152 at the
   same address, minutes apart. §2 and §11 assumed "headed Chrome" was one thing.
   The binary is now configured by `PHONELOOP_CHROME_EXE` (`phoneloop/browser.py`),
   defaulting to the installed Chrome.
2. **The residential proxy is a second chance, not a fallback.** Its exit (a US
   residential address) was refused on the homepage the same night; on 2026-08-27
   it 403'd the search page 2/2. §10's "escalate to the proxy" stays, but nothing
   may depend on it. The machine-wide VPN (Mullvad) put direct egress on a
   datacenter exit eBay refuses outright; the fix is a per-executable split
   tunnel for the automation binary only, never the whole machine.
3. **A category-only search does not return a results page.** `sch/i.html?_sacat=…`
   with no keyword is redirected to the category's `/b/bn_<n>` browse node for
   every variant tried (plain, `rt=nc`, empty `_nkw`, `_sop=12`, `LH_BIN=1`, page 2,
   the `/sch/<id>/i.html` path form), and a hub's browse node lists nothing at all
   — the sweep would have recorded `done=0`, "genuinely nothing there", and the
   board would have stayed empty with every test green. `_fsrp=1` keeps the request
   on a 256-row results page (page 2 included, price band applied). It is now part
   of `phoneloop.ebay.search_url`, and a landing on `/b/` raises
   `RedirectedOffResults` instead of counting as an empty shard.
4. **§9's "43 leaves" are second-level hubs**, not eBay leaves — `Dog Supplies`,
   `Bathroom Fixtures, Accessories & Supplies`, `Mobile Phone Accessories`
   (1.59 M results). With `_fsrp=1` a hub sweeps fine, but the 10,000-result cap
   then bites per hub × price band, so Tier 1 sees the Best-Match head of each hub,
   not its whole population. Descending to true leaves is a Tier-1 follow-up, not
   a blocker for stage one.

Two operational fixes landed with them: the sweep checkpoint is dated per UTC day
(`bulk_ebay_checkpoint_YYYY-MM-DD.json`), so day two re-observes the population
Tier 3 differences instead of skipping every shard day one finished; and
`--pages-per-shard` bounds each leaf, where `--max-pages` budgeted the whole run
and let one hub consume it.
