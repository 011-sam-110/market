# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

Python 3.13 package (`phoneloop/`) with a DuckDB/parquet store, publishing a
static bundle. The dashboard surface is Vite + React + TypeScript, chosen by the
user, built locally and uploaded prebuilt to Vercel. Vercel runs no build and
serves static files only.

## Users

One operator (the project's author) with a second collaborator who will also
operate it and act on what it says. The operator knows the pipeline; the
collaborator does not. Terminology that this project invented — knob, leaf,
niche, denominator, coverage, deepen, sell-through window — has to teach itself
on screen rather than assume the reader has read the specs.

Usage scene: a short look most days, often on a phone, to see whether the
overnight run changed anything, with an occasional long desk session when it
did. The phone layout is a first-class target, not a courtesy.

## Product Purpose

Find UK eBay dropshipping niches by sweeping the marketplace nightly and ranking
what it finds against six tunable knobs: minimum net margin, sell-through,
price band, delivery time, competitor quality, and market saturation.

The dashboard exists because the pipeline currently has no readable surface. It
publishes three static pages with no charts, no filters and no interactivity,
and the one interactive panel that exists in the codebase is stripped before
publication.

**Success, right now, is not a purchase decision.** The operator is still
calibrating: deciding whether the knobs and the data underneath them are
trustworthy enough to act on at all. So the job of a ranked row is to expose its
own evidence, not to recommend a product. A row that cannot say how much of
itself is real has failed even if its ranking is correct.

## Positioning

Every competing product-research tool is a shopping list: it shows you winners
and hides its working. This one shows its working, because its working is
currently the interesting part — a third of the store's sold counts are
missing, less than 1% of listings have been deepened, and the top-ranked niche
scores well precisely because almost nothing about it is known.

The mechanism a neighbouring product could not truthfully copy: every figure
carries the denominator it was computed over, and "unknown" is a distinct state
from "zero" and from "fails" all the way from the parquet store to the pixel.

## Constraints

- **The sweep can never run on the host.** eBay refuses datacenter egress; only
  a split-tunnelled Chrome on the home IP gets a 200. The sweep takes ~21
  minutes against a 300s function limit, and the store is gitignored parquet.
  Vercel hosts the viewer and nothing else.
- **The store is read-only to this surface**, and the bundle is a static export:
  21.4 MB raw, 3.3 MB gzipped across nine files, from one read of the store.
- **Two snapshot dates exist** (2026-08-28, 2026-08-29). Day-over-day reads are
  live; anything needing 7 or 14 dates is locked and must render as locked with
  a date count, never as an empty chart.
- **Sell-through is honestly unknown until about 24 September.** The measured
  window is 1.18 days against a threshold describing 30, so the knob withholds
  its verdict by design. It must read as withheld, not as a failure.
- The operator reviews at roughly 80% browser zoom, so type renders about 20%
  smaller than designed.

## Evidence and product facts future work must preserve

Measured against the live store, and load-bearing for the design:

- 29,863 listings on the latest snapshot; 40,035 distinct across both dates.
- **218 listings deepened — 0.73%.** `min_margin`, `price_band` and `delivery`
  are gated on that cohort and read unknown almost everywhere.
- **`toys_games` seller coverage is 0.95%** against 97.66–99.09% for every
  sibling department. A single global figure (86.93%) hides it entirely.
- **25,406 of 27,206 products are singletons (93.4%).** Only 1,800 have two or
  more listings and 67 have five or more. A singleton carries no
  repeated-supply signal.
- `promoted` is `True` on 100% of rows and carries no information.
- eBay's sold counter is not monotonic: 401 negative day-over-day deltas, worst
  −7,468 on a listing whose title and price were identical both days.
- `score = passed − failed`, so an unknown knob scores exactly like a satisfied
  one. This is why a row must show its evidence beside its score.

## Terminology

- **Knob** — one of six tunable thresholds the board ranks against.
- **Leaf** — an eBay category id the sweep visits. The 43 swept "leaves" are
  really second-level hubs.
- **Niche** — a leaf crossed with a price band.
- **Product** — listings grouped by catalogue id or title cluster within one leaf.
- **Deepen** — fetching a listing's own item page for the fields a search row
  cannot show (seller type, delivery estimate, listing date).
- **Denominator / coverage** — how many rows a figure was actually computed
  over, and what share of the population that is.

## Accessibility

Dark and light both required; the page is read on a phone in the morning and a
laptop at a desk. Status must never be carried by colour alone — pass, fail and
unknown are three states and the third is the one that matters most.

## Open decisions

- `vat_on_fees_bp` (1.2× not VAT-registered vs 1.0× registered) is a live
  business decision, not a measurement.
- `import_vat_term_bp` is unverified: whether eBay's buyer-side import VAT is
  additive or already inclusive has never been established.
- `str_threshold_bp` (50%) is a chosen convention. The spec names sell-through
  as a knob and gives no number.
- The `toys_games` coverage outage is surfaced but undiagnosed.
