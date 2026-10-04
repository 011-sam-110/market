# Scrapling bake-off, 2026-10-04

Question: does Scrapling ([D4Vinci/Scrapling](https://github.com/D4Vinci/Scrapling) 0.4.15,
BSD-3-Clause) get past the bot checks that block our scrapers on eBay UK search, CeX and Reuters?

Short answer: only eBay was measured, on provenance-1. From the home line, Scrapling's plain
HTTP `Fetcher` got real eBay search pages, 2 of 2. Its stealth browser got page 1 and was refused
on page 2. CeX and Reuters were not measured. The laptop's egress was a Mullvad exit for the
whole session, and the plan does not allow a change to Mullvad.

Script: `scripts/scrapling_bakeoff.py`. Raw results: `2026-10-04-scrapling-bakeoff.json`.

## Conditions

| | provenance-1 | laptop |
|---|---|---|
| OS | Fedora, kernel 7.2.7-200.fc44 | Windows 11 (10.0.26200) |
| Python | 3.14.7 | 3.13.3 |
| Egress | Virgin Media, Brighton (home line), `mullvad_exit_ip: false` | Mullvad relay `be-bru-wg-103`, M247 Brussels, `mullvad_exit_ip: true` |
| Time | 2026-10-04 00:17:23 to 00:17:50 UTC (01:17 BST) | 00:25:13 UTC, stopped before any target request |
| Packages | scrapling 0.4.15, patchright 1.63.0, curl_cffi 0.16.3 | the same |
| Browser | Chromium 153.0.8010.12 (Playwright build 1243) | the same |

- The egress check uses `https://am.i.mullvad.net/json`. The script stops before any target
  request when the exit is Mullvad or the check fails.
- provenance-1 ran eBay only, with `Fetcher` and headless `StealthyFetcher`, as planned. There
  is no headed result from the box. The nightly service was inactive and the run
  ended 1 h 42 min before the 03:00 timer.
- No retries. At least 6 s between two requests to one host. No homepage warm-up (the nightly
  loads the eBay homepage first; this test went straight to the search URL).

## Results: eBay UK search, provenance-1

URL: `https://www.ebay.co.uk/sch/i.html?_sacat=20742&_fsrp=1&_ipg=240&_pgn={1,2}&_udlo=15&_udhi=55&_dmd=1`,
the nightly's URL shape (`phoneloop.ebay.search_url`). A "real page" means: no
`bulk_ebay.is_blocked` text, no `/b/` redirect, and 10 or more result rows (an `<li>` with a
£ price and an `/itm/` link, the 123456 template row excluded).

| page | variant | HTTP | real page | seconds | detail |
|---|---|---|---|---|---|
| 1 | fetcher | 200 | yes | 2.4 | 246 result rows, title "Dog Supplies \| eBay" |
| 2 | fetcher | 200 | yes | 2.8 | 254 result rows |
| 1 | stealth_headless | 200 | yes | 10.8 | 247 result rows |
| 2 | stealth_headless | 403 | no | 3.7 | "Error Page \| eBay", "Something went wrong" |

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
- **CeX.** Not measured. The baseline stands: only headed real Chrome gets 200.
- **Reuters.** Not measured. The baseline stands: installed Chrome on a real GPU display only.
  Scrapling's own README sends DataDome users to a paid third party.

## Smoke test: the MCP server on a Cloudflare page

URL: `https://help.webflow.com/hc/en-us/articles/33961277976467-Structure-and-style-Collection-pages`.
Run on provenance-1, because the laptop was on Mullvad.

| client | result |
|---|---|
| curl (control) | 403, "Just a moment..." Cloudflare challenge page |
| (a) `StealthyFetcher.fetch(headless=True, solve_cloudflare=True, timeout=90000)` | 200 in 5.0 s, `<h1>` "Structure and style Collection pages" |
| (b) `scrapling-mcp` over stdio: `initialize`, `tools/list` (13 tools), `tools/call stealthy_fetch` | `isError: false`, status 200 in 3.1 s, article title in the result |

Scrapling logged "No Cloudflare challenge found" both times. Cloudflare served the stealth
browser the page at once. So this shows the stealth browser is not challenged where curl is.
It does not exercise the Turnstile solver.

## Recommendation

- **Do not change `phoneloop/browser.py` or the nightly on this evidence.** Two pages cannot
  show whether the nightly's blocked shards (about 30 of 128 on 23 and 26 Sep) are fingerprint
  blocks or rate limits.
- **Follow-up worth doing: a `Fetcher`-only eBay soak on provenance-1**, outside the nightly
  window, at the nightly's pacing and page count, then compare its block rate with the
  nightly's. If curl_cffi holds, search pages do not need a browser (2 to 3 s a page against
  about 11 s here). Item pages (deepen) need their own test.
- **Do not use headless `StealthyFetcher` for eBay.** It was refused on page 2.
- **CeX and Reuters:** rerun `python scripts/scrapling_bakeoff.py --targets cex,reuters` on the
  laptop when its egress is the home line.

Rerun the eBay half on the box:

    python scripts/scrapling_bakeoff.py --targets ebay --variants fetcher,stealth_headless --out results.json
