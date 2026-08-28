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
REM ASSUMED CLI, unverified against streams not merged into this worktree:
REM   --leaf-sweep     is s2's own flag, confirmed only by the offline
REM                     end-to-end command in contracts section 6.3
REM                     ("bulk_ebay.py --leaf-sweep --max-pages 3 --fresh").
REM                     Used here WITHOUT --max-pages / --fresh, for a full
REM                     unbounded daily sweep rather than the bounded smoke
REM                     test that example runs.
REM   --deepen          is NOT named anywhere in the build contracts for any
REM                     stream (s2 owns ebay_item.py's item-page parser, s3
REM                     owns ebay_api.py's getItem call, but neither
REM                     contract names a script or flag that drives Tier 2
REM                     end to end). This is this stream's best guess at
REM                     the CLI s2/s3 will actually ship. If it does not
REM                     exist at integration, the fix is to change ONE line
REM                     below, not this file's structure.
REM Both are named explicitly in this stream's unverified[].

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

echo [run_daily] %DATE% %TIME% -- [2/3] DEEPEN
"%PY%" scripts\bulk_ebay.py --deepen
if errorlevel 1 (
    echo [run_daily] DEEPEN failed -- checkpointed, safe to re-run
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
