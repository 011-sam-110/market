@echo off
REM eBay niche finder -- daily pipeline (design spec section 10; contracts
REM section 4, s5_snapshots_board's "run_daily.cmd" deliverable).
REM
REM Runs SWEEP -> DEEPEN -> BOARD, in that order, once a day. DIFFERENCE
REM (Tier 3) is NOT a separate step here: scripts\niche_board.py calls
REM phoneloop.snapshots.difference itself, over the two most recent
REM snapshot dates in the store, whenever at least two exist (one exists on
REM day one; the difference step naturally starts contributing from day
REM two onward, exactly as spec section 10's "Tier 3 ... daily, no
REM requests" describes).
REM
REM Pins the INSTALLER python, never the MSIX/WindowsApps one -- the MSIX
REM build exits silently (exit code 0, no output) when launched detached
REM with no console attached, which is exactly how Task Scheduler launches
REM it. That looks identical to a successful no-op run and is worse than a
REM visible crash, because nothing downstream notices the sweep never ran.
REM
REM Task Scheduler: create a daily trigger at 03:00 (spec section 10), action
REM = this file, "Start in" = the repo root. From an elevated prompt:
REM
REM   schtasks /create /tn "PhoneLoop eBay Daily" ^
REM     /tr "\"C:\Users\sampo\OneDrive\Desktop\Python projects\PhoneLoop\scripts\run_daily.cmd\"" ^
REM     /sc daily /st 03:00
REM
REM Each step below checkpoints on its own: bulk_ebay.py's existing
REM checkpoint/resume behaviour (contracts section "s2_html_transport") is
REM unchanged by this stream, and niche_board.py is read-only over the
REM store, so a step that fails here can simply be re-run -- by hand, or by
REM tomorrow's 03:00 trigger -- and it picks up where it left off rather
REM than redoing completed work.
REM
REM VERIFIED 2026-08-28 (session 2; docs/superpowers/proof, section 9):
REM   --leaf-sweep      sweeps catalog_ebay.IN_SCOPE with _fsrp=1 in the URL.
REM                     Without it eBay redirects a category-only search
REM                     to a /b/ browse node that lists nothing, and the
REM                     sweep would have booked that as an empty shard.
REM                     Replayed end to end on 12 recorded pages (4 hubs)
REM                     into a populated board.
REM   --pages-per-shard caps page loads PER HUB. 10 here = up to ~430 page
REM                     loads a night (~1 h at the measured ~9 s/page).
REM                     Raise it toward 42 (the 10,000-result cap) for full
REM                     Tier 1 coverage once that runtime is acceptable.
REM   checkpoint        dated per UTC day (bulk_ebay_checkpoint_YYYY-MM-DD
REM                     .json): a failed night resumes, the next night
REM                     starts clean -- Tier 3 needs the whole population
REM                     re-observed, not the pages past yesterday's stop.
REM
REM EGRESS (measured 2026-08-28): eBay refuses the bundled Playwright
REM Chromium build and any VPN/datacenter exit, and the residential proxy
REM exit was refused too. PHONELOOP_CHROME_EXE in ~\.phoneloop\.env points
REM at a Chrome for Testing build that is split-tunnelled out of Mullvad by
REM path (`mullvad split-tunnel get` lists it). Machine config, not repo
REM config -- nothing in this file sets it.
REM
REM PHONELOOP_HARNESS=live below is load-bearing. The harness default is
REM replay, under which a live night raises MissingRecording on its first
REM unrecorded page (proof section 9.6 shows exactly that raise). live =
REM real requests, no fixtures written; a deliberate recording session
REM sets record by hand instead.
REM   --deepen          NOW SHIPPED (round-2 integration fix): scripts/
REM                     bulk_ebay.py's `run_deepen` wires ebay_item.py's
REM                     item-page parser (parse_item, to_row_updates) into
REM                     MarketStore -- reads the latest snapshot's rows
REM                     lacking seller_type, replays/fetches each item page
REM                     under the same `item:{url}` name scripts/
REM                     record_ebay.py records under, and merges the result
REM                     back (extra JSON merged, not clobbered -- see
REM                     ebay_item.to_row_updates's docstring). Unit-tested in
REM                     tests/test_bulk_ebay_deepen.py (row selection) and
REM                     tests/test_ebay_item.py (the merge itself, against
REM                     real recorded item pages where real data exercises
REM                     it). Depends on the SWEEP step above having already
REM                     populated the store for today -- on an empty store it
REM                     prints "0 listings to deepen" and exits 0 rather than
REM                     failing, so it is safe to run daily even on a day
REM                     SWEEP found nothing new.

setlocal enabledelayedexpansion

set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
set "REPO=%~dp0.."

if not exist "%PY%" (
    echo [run_daily] installer python not found at "%PY%"
    echo [run_daily] the MSIX/WindowsApps python must NOT be used here -- it exits silently when launched without a console, per contracts section 0, rule 7.
    exit /b 1
)

cd /d "%REPO%"
if errorlevel 1 (
    echo [run_daily] could not cd to repo root "%REPO%"
    exit /b 1
)

set "PHONELOOP_HARNESS=live"

echo [run_daily] %DATE% %TIME% -- [1/3] SWEEP
"%PY%" scripts\bulk_ebay.py --leaf-sweep --pages-per-shard 10
if errorlevel 1 (
    echo [run_daily] SWEEP failed -- checkpointed, safe to re-run
    exit /b 1
)

echo [run_daily] %DATE% %TIME% -- [2/3] DEEPEN (Tier 2, item-page enrichment)
"%PY%" scripts\bulk_ebay.py --deepen
if errorlevel 1 (
    echo [run_daily] DEEPEN failed -- SWEEP already landed today's rows, safe to re-run
    exit /b 1
)

echo [run_daily] %DATE% %TIME% -- [3/3] BOARD (difference runs inside niche_board.py once 2+ snapshot dates exist)
"%PY%" scripts\niche_board.py --products --html
if errorlevel 1 (
    echo [run_daily] BOARD failed
    exit /b 1
)

echo [run_daily] %DATE% %TIME% -- done
exit /b 0
