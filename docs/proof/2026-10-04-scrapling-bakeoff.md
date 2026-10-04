# Scrapling bake-off, 2026-10-04

Question: does Scrapling ([D4Vinci/Scrapling](https://github.com/D4Vinci/Scrapling) 0.4.15,
BSD-3-Clause) get past the bot checks that block our scrapers on eBay UK search, CeX and Reuters?

Short answer, from provenance-1 on the home line, 2 pages per target:

- **eBay:** the plain HTTP `Fetcher` got real search pages, 2 of 2. The headless stealth
  browser got page 1 and was refused on page 2.
- **CeX:** the headless stealth browser got real CASH prices, 2 of 2. `Fetcher` got a 200 app
  shell with no prices, because CeX renders prices client-side.
- **Reuters:** DataDome refused both variants on both pages (401, `x-datadome: protected`).

The headed variant is still unmeasured. It needs the laptop off Mullvad, and Sam decides that.
The laptop's egress was a Mullvad exit for the whole session, and the script refused to run there.

Script: `scripts/scrapling_bakeoff.py`. Raw results: `2026-10-04-scrapling-bakeoff.json`.

## Conditions

| | provenance-1 | laptop |
|---|---|---|
| OS | Fedora, kernel 7.2.7-200.fc44 | Windows 11 (10.0.26200) |
| Python | 3.14.7 | 3.13.3 |
| Egress | Virgin Media, Brighton (home line), `mullvad_exit_ip: false` | Mullvad relay `be-bru-wg-103`, M247 Brussels, `mullvad_exit_ip: true` |
| Time | eBay 00:17:23 to 00:17:50 UTC; CeX and Reuters 00:30:40 to 00:32:17 UTC (2026-10-04) | 00:25:13 UTC, stopped before any target request |
| Packages | scrapling 0.4.15, patchright 1.63.0, curl_cffi 0.16.3 | the same |
| Browser | Chromium 153.0.8010.12 (Playwright build 1243) | the same |

- The egress check uses `https://am.i.mullvad.net/json`. The script stops before any target
  request when the exit is Mullvad or the check fails.
- provenance-1 ran `Fetcher` and headless `StealthyFetcher` only. There is no headed result.
  The nightly service was inactive, and the last run ended 1 h 28 min before the 03:00 timer.
- No retries. At least 6 s between two requests to one host. One browser session per target
  and variant, so page 2 runs in the same session as page 1. No homepage warm-up (the eBay
  nightly loads the homepage first; this test went straight to the search URL).

## Results, provenance-1

A "real page" is judged by a content signal, never by the status alone:

- eBay: no `bulk_ebay.is_blocked` text, no `/b/` redirect, and 10 or more result rows (an
  `<li>` with a £ price and an `/itm/` link, the 123456 template row excluded).
- CeX: a "CASH" line followed by a £ amount, as `phoneloop.cex.parse_sell_page` reads it.
- Reuters: `window.Fusion.globalContent` set within 30 s in the browser (NewsScraper's
  `wait_for_reuters_page`), or `Fusion.globalContent` in the `Fetcher` HTML.

| target | page | variant | HTTP | real page | seconds | detail |
|---|---|---|---|---|---|---|
| eBay | 1 | fetcher | 200 | yes | 2.4 | 246 result rows, title "Dog Supplies \| eBay" |
| eBay | 2 | fetcher | 200 | yes | 2.8 | 254 result rows |
| eBay | 1 | stealth_headless | 200 | yes | 10.8 | 247 result rows |
| eBay | 2 | stealth_headless | 403 | no | 3.7 | "Error Page \| eBay", "Something went wrong" |
| CeX | 1 | fetcher | 200 | no | 0.3 | app shell, title "CeX", no CASH price |
| CeX | 2 | fetcher | 200 | no | 0.1 | app shell, no CASH price |
| CeX | 1 | stealth_headless | 200 | yes | 6.0 | 17 CASH prices, first £110.00 |
| CeX | 2 | stealth_headless | 200 | yes | 2.9 | 17 CASH prices, first £123.00 |
| Reuters | 1 | fetcher | 401 | no | 0.2 | DataDome captcha page, `x-datadome: protected` |
| Reuters | 2 | fetcher | 401 | no | 0.1 | DataDome captcha page |
| Reuters | 1 | stealth_headless | 401 | no | 32.0 | Fusion not set after 30 s, DataDome captcha page |
| Reuters | 2 | stealth_headless | 401 | no | 31.2 | Fusion not set after 30 s, DataDome captcha page |

eBay: `/sch/i.html?_sacat=20742&_fsrp=1&_ipg=240&_pgn={1,2}&_udlo=15&_udhi=55&_dmd=1`, the
nightly's URL shape. CeX: `uk.webuy.com/sell/search?stext=iphone 12 128gb` and `iphone 13
128gb`, the page `scripts/refresh_cex.py` loads. Reuters: `/world/africa/` and `/world/europe/`.

Laptop: no rows. Mullvad split tunnelling exempts only the Chrome for Testing 152 binary, so
the Scrapling venv's `python.exe` and its Chromium would leave through Belgium. eBay refuses
VPN exits (measured 2026-08-28), so a result from that exit would say nothing about the home line.

## What the results mean against earlier measurements

- **eBay, `Fetcher`.** `phoneloop/ebay.py` records "live search, scripted client 403"
  (2026-08-27). That note does not record the client or the exit, so I cannot say what
  changed. Tonight a curl_cffi client with Chrome TLS impersonation, and no browser, got two
  full results pages from the home line.
- **eBay, headless `StealthyFetcher`.** `phoneloop/browser.py` records Playwright's bundled
  Chromium 148 at 403 on the homepage. Patchright Chromium 153 got further: page 1 real, page 2
  refused in the same session. This is the same "first page passes, later pages fail" shape as
  DataDome on Reuters under Xvfb. Two pages cannot separate a fingerprint verdict after the
  first page from a rate limit: page 2 was the fourth page request to `www.ebay.co.uk` in about
  25 s from this IP.
- **CeX, headless `StealthyFetcher`.** The earlier measurement had headless Chromium and
  headless real Chrome at 403, and only headed real Chrome at 200. Tonight headless patchright
  Chromium 153 got real prices on both pages, with no Cloudflare challenge shown. This is the
  first headless pass on CeX. `refresh_cex.py` opens a window today because a scheduled
  headless run returned zero boxes.
- **CeX, `Fetcher`.** The earlier measurement had curl_cffi at 403. Tonight it got a 200 app
  shell. The shell holds no prices: they come from a client-side search call, and a scripted
  call to CeX's Algolia search was 403 earlier. So `Fetcher` alone cannot read CeX prices.
- **Reuters.** No change from the baseline. curl_cffi and headless patchright Chromium both
  got 401 on both pages, and the DataDome check did not clear in 30 s. The only known pass is
  installed Chrome on a real GPU display. Scrapling's own README sends DataDome users to a paid
  third party.

## Smoke test: the MCP server on a Cloudflare page

URL: `https://help.webflow.com/hc/en-us/articles/33961277976467-Structure-and-style-Collection-pages`.
Run on provenance-1, because the laptop was on Mullvad.

| client | result |
|---|---|
| curl (control) | 403, "Just a moment..." Cloudflare challenge page |
| (a) `StealthyFetcher.fetch(headless=True, solve_cloudflare=True, timeout=90000)` | 200 in 5.0 s, `<h1>` "Structure and style Collection pages" |
| (b) `scrapling-mcp` over stdio: `initialize`, `tools/list` (13 tools), `tools/call stealthy_fetch` | `isError: false`, status 200 in 3.1 s, article title in the result |

Scrapling logged "No Cloudflare challenge found" on every stealth request, Webflow and CeX
included. Cloudflare served the stealth browser the page at once. So this shows the stealth
browser is not challenged where curl is. It does not exercise the Turnstile solver.

## Recommendation

- **Do not change `phoneloop/browser.py`, `refresh_cex.py` or the nightly on this evidence.**
  Two pages cannot show whether a pass holds, or whether the nightly's blocked shards (about 30
  of 128 on 23 and 26 Sep) are fingerprint blocks or rate limits.
- **Follow-up 1: a `Fetcher`-only eBay soak on provenance-1**, outside the nightly window, at
  the nightly's pacing and page count, then compare its block rate with the nightly's. If
  curl_cffi holds, search pages do not need a browser (2 to 3 s a page against about 11 s
  here). Item pages (deepen) need their own test.
- **Follow-up 2: a headless `StealthyFetcher` run of the full CeX refresh query list.** If it
  holds past 2 pages, the CeX refresh can run headless on a schedule instead of opening a window.
- **Do not use headless `StealthyFetcher` for eBay** (refused on page 2), **or Scrapling for
  Reuters** (refused on every request).
- **Headed variant:** still unmeasured. It needs the laptop off Mullvad, and Sam decides that.
  Then run `python scripts/scrapling_bakeoff.py --variants stealth_headed --out results.json`.

Rerun the box half:

    python scripts/scrapling_bakeoff.py --targets ebay,cex,reuters --variants fetcher,stealth_headless --out results.json
