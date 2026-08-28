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
REM ASSUMED CLI, checked at integration (2026-08-28, round 2):
REM   --leaf-sweep     is s2's own flag; argparse accepts it and it takes the
REM                     shape this file assumes. The section 6.3 offline
REM                     end-to-end run ("bulk_ebay.py --leaf-sweep
REM                     --max-pages 3 --fresh") still does NOT get past its
REM                     first shard against fixtures/ebay/ as they exist
REM                     today: scripts/record_ebay.py and scripts/bulk_ebay.py
REM                     now build the SAME URL and the SAME cassette key
REM                     (phoneloop.ebay.search_url / .search_key, round-2
REM                     integration fix -- they used to drift, see
REM                     phoneloop/ebay.py's module docstring), but the
REM                     recordings under PHONELOOP_FIXTURES were captured
REM                     BEFORE that fix, under the old narrower shape, so
REM                     they still raise MissingRecording -- correct per
REM                     contracts section 6.3 ("a missing recording raises
REM                     rather than producing an empty board"), and this flag
REM                     remains unverified end to end offline until a fresh
REM                     recording session runs under the now-aligned code.
REM                     See the integration report's unverified[].
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

echo [run_daily] %DATE% %TIME% -- [1/3] SWEEP
"%PY%" scripts\bulk_ebay.py --leaf-sweep
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
