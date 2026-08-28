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
REM ASSUMED CLI, checked at integration (2026-08-28):
REM   --leaf-sweep     is s2's own flag; argparse accepts it and it takes the
REM                     shape this file assumes. The section 6.3 offline
REM                     end-to-end run ("bulk_ebay.py --leaf-sweep
REM                     --max-pages 3 --fresh") does NOT get past its first
REM                     shard against the merged tree: scripts/record_ebay.py
REM                     records keyword search pages (_ipg=60, no price
REM                     band), while --leaf-sweep fetches a different URL
REM                     shape (_sacat=, _ipg=240, a price band) under a
REM                     different cassette key, so no recording satisfies the
REM                     first shard and it raises MissingRecording -- correct
REM                     per contracts section 6.3 ("a missing recording
REM                     raises rather than producing an empty board"), but it
REM                     means this flag is unverified end to end offline.
REM                     See the integration report's unverified[].
REM   --deepen          CONFIRMED NOT TO EXIST: `bulk_ebay.py --deepen`
REM                     fails argparse ("unrecognized arguments: --deepen").
REM                     No accepted stream wired a Tier-2 driver -- s2 owns
REM                     ebay_item.py's item-page parser (parse_item,
REM                     to_row_updates) and s3 (ebay_api.py's getItem call)
REM                     was not accepted -- so there is nothing to point
REM                     this at yet, and inventing a flag/script here would
REM                     be new feature work, not an integration fix. The
REM                     DEEPEN step below is skipped rather than left in to
REM                     crash the 03:00 trigger every night; see the SKIP
REM                     block's own comment. Re-enable it, one line, once a
REM                     real Tier-2 driver ships.

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

echo [run_daily] %DATE% %TIME% -- [1/2] SWEEP
"%PY%" scripts\bulk_ebay.py --leaf-sweep
if errorlevel 1 (
    echo [run_daily] SWEEP failed -- checkpointed, safe to re-run
    exit /b 1
)

REM DEEPEN (Tier 2, item-page enrichment) is skipped -- no accepted stream
REM ships a script or flag that drives it end to end (see the comment block
REM above). Skipping rather than calling a nonexistent `--deepen` keeps this
REM trigger from failing every night over a step that cannot succeed either
REM way; sweep + board still run daily on their own data. Restore a DEEPEN
REM step here, renumbering back to [1/3]/[2/3]/[3/3], once ebay_item.py's
REM parser (or ebay_api.py's, if s3 lands) is actually wired to a script.
echo [run_daily] %DATE% %TIME% -- DEEPEN skipped: no Tier-2 driver script exists yet (see comment header)

echo [run_daily] %DATE% %TIME% -- [2/2] BOARD (difference runs inside niche_board.py once 2+ snapshot dates exist)
"%PY%" scripts\niche_board.py --products --html
if errorlevel 1 (
    echo [run_daily] BOARD failed
    exit /b 1
)

echo [run_daily] %DATE% %TIME% -- done
exit /b 0
