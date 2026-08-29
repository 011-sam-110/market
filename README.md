# market

Finds dropshippable product niches on eBay UK: sweeps category hubs nightly,
tracks what actually sells between snapshots, and scores each candidate against
six knobs — margin, sell-through, price band, delivery, seller quality and
saturation.

The repo is still named after its first life (`PhoneLoop`, a Vinted phone
arbitrage watcher). That collector is preserved under `phoneloop/` — `vinted.py`,
`cex.py` and `__main__.py` are its surface — but the work since has been the eBay
niche finder, which is what everything below describes.

## How it runs

Three tiers, once a night, in this order:

| Tier | What it does | Cost |
|---|---|---|
| 1 — sweep | 43 category hubs × price band × 3 pages | ~21 min, ~128 page loads |
| 2 — deepen | item pages, for the fields only they carry (`seller_type`, delivery ETA) | ~9 s per listing |
| 3 — difference | `sold_qty(today) − sold_qty(previous)`, per listing | no requests |

```bash
scripts\run_daily.cmd          # sweep -> deepen -> board -> publish
```

Tier 3 needs two snapshot dates, so it contributes nothing on night one and
starts working on night two.

## Egress — the part that constrains everything

Measured 2026-08-28: eBay serves a 403 to the bundled Playwright Chromium, to
every VPN/datacenter exit, **and** to the rotating residential proxy. What gets a
200 is a Chrome for Testing build on the home IP, split-tunnelled out of Mullvad
by path.

That is why the scrape cannot move to a server, and why Vercel hosts only the
generated reports (see *Publishing*). `PHONELOOP_CHROME_EXE` points at that
binary and lives in `~/.phoneloop/.env` — machine config, never the repo.

## The harness

Anything that touches a live service goes through `phoneloop/harness.py`.

| Mode | Behaviour |
|---|---|
| `replay` (default) | serves recorded pages; a missing recording **raises** |
| `record` | live requests, writes cassettes |
| `live` | live requests, writes nothing |

The default is `replay` so a test or a stray script can never quietly hit eBay.
Recordings are gitignored — one capture is ~9 MB of HTML that goes stale within
hours, and the suite builds its own fixtures in `tmp_path`.

## Honesty rules the code enforces

These exist because the failure this project keeps hitting is *a number that is
wrong rather than missing*.

- **Unknown is never zero.** A missing `sold_qty` is excluded from its
  denominator, not counted as no sales.
- **Every figure carries its denominator.** 3.2 over 4 rows is not the claim 3.2
  over 200 rows is.
- **A figure is never compared to a threshold it isn't commensurate with.**
  Sell-through withholds its verdict until the observed window approaches the 30
  days the threshold describes — see `metrics.STR_WINDOW_DAYS`.
- **eBay's "sold" counter is not a counter.** `10,000+ sold` is a lower bound and
  is recorded as one; multi-variant listings report a different figure per page
  view. Deltas across a bound, or that fall, are dropped as unknown rather than
  published as sales.

## Storage

Date-partitioned parquet under `PHONELOOP_DATA_DIR` (default
`~/.phoneloop/data`; the nightly job uses `~/.phoneloop/prod`), read with DuckDB.
One row per listing per day, so history accumulates rather than being overwritten.
Never committed.

## Publishing

`scripts/publish_web.py` copies the newest board and population reports into
`web/` and rebuilds its index from what is actually on disk. Vercel serves that
directory as a static site behind Vercel Authentication — it hosts the viewer
only; the sweep stays on the home machine for the egress reason above.

## Setup

```bash
pip install -r requirements.txt
python -m pytest -q                      # 861 tests, no network
python scripts/niche_board.py --products --html
```

## Layout

| Path | What |
|---|---|
| `phoneloop/ebay.py` | search-row parser |
| `phoneloop/ebay_item.py` | item-page parser (Tier 2) |
| `phoneloop/market_store.py` | parquet store |
| `phoneloop/snapshots.py` | Tier 3 differencing |
| `phoneloop/metrics.py` | every figure, with its denominator |
| `phoneloop/knobs.py` | the six thresholds |
| `phoneloop/board.py` | ranking |
| `phoneloop/population.py` | review-page panels |
| `scripts/run_daily.cmd` | the nightly pipeline |
| `docs/superpowers/` | design spec, contracts, build proof |
