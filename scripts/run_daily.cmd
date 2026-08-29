@echo off
REM eBay niche finder -- daily pipeline (design spec section 10; contracts
REM section 4, s5_snapshots_board's "run_daily.cmd" deliverable).
REM
REM Runs SWEEP -> DEEPEN -> BOARD -> POPULATION -> PUBLISH, in that order,
REM once a day. DIFFERENCE (Tier 3) is NOT a separate step here:
REM scripts\niche_board.py calls
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
REM Task Scheduler: do NOT point a trigger at this file directly. The task
REM action is scripts\run_daily_scheduled.cmd, which pins the interpreter and
REM runs scripts\nightly.py -- the lock, the day-guard, the log and the
REM heartbeat all live there, and without them a skipped or killed night
REM leaves no trace at all. Register it with:
REM
REM   .\scripts\register_daily_task.ps1 -Force
REM
REM PHONELOOP_DATA_DIR is defaulted BELOW, inside this file, not in the task
REM definition -- a Task Scheduler action carries no environment of its own,
REM so a variable set there would simply be absent. Without the default a
REM scheduled night writes to ~\.phoneloop\data (68 parquet files, all
REM source=vinted, zero eBay) and orphans the eBay snapshot corpus: the board
REM would print "only one snapshot date" forever while every run exited 0.
REM `if not defined` is load-bearing -- every documented manual run exports
REM the variable by hand and must keep winning.
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
REM   [4/5] POPULATION REVIEW writes the self-contained population page
REM                     (scripts\population_report.py). Read-only over the
REM                     store, ~20 s, and regenerated rather than one-off on
REM                     purpose: a one-off HTML is a snapshot of today's
REM                     problems, a regenerated one is the instrument that
REM                     shows them shrinking, and it means the page's locked
REM                     panels fill themselves the night date 2 lands.
REM
REM                     The PHONELOOP_DATA_DIR blocker this block used to
REM                     describe is RESOLVED -- see the default set below.
REM                     It read: "this file still sets no PHONELOOP_DATA_DIR,
REM                     so every step reads config.py's default store
REM                     (%%USERPROFILE%%\.phoneloop\data, Vinted rows only)".
REM                     That was true and is no longer; the promoted store is
REM                     %%USERPROFILE%%\.phoneloop\prod, which holds the
REM                     2026-08-28 and 2026-08-29 eBay snapshots, and the
REM                     population page's locked panels duly filled the night
REM                     date 2 landed. Kept as a note rather than deleted
REM                     because a comment that describes a fixed blocker in
REM                     the present tense is how it gets "re-fixed" later.
REM   [5/5] PUBLISH     refreshes web\ from the reports just written and
REM                     rebuilds its index. Stages content only -- it runs no
REM                     git command, deliberately (this working tree carries
REM                     untracked scratch files, and an automated `git add`
REM                     is exactly how one of them reaches a remote).

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
if not defined PHONELOOP_DATA_DIR set "PHONELOOP_DATA_DIR=%USERPROFILE%\.phoneloop\prod"

echo [run_daily] %DATE% %TIME% -- [1/5] SWEEP
"%PY%" scripts\bulk_ebay.py --leaf-sweep --pages-per-shard 10
if errorlevel 1 (
    echo [run_daily] SWEEP failed -- checkpointed, safe to re-run
    exit /b 1
)

echo [run_daily] %DATE% %TIME% -- [2/5] DEEPEN (Tier 2, item-page enrichment)
"%PY%" scripts\bulk_ebay.py --deepen
if errorlevel 1 (
    echo [run_daily] DEEPEN failed -- SWEEP already landed today's rows, safe to re-run
    exit /b 1
)

echo [run_daily] %DATE% %TIME% -- [3/5] BOARD (difference runs inside niche_board.py once 2+ snapshot dates exist)
"%PY%" scripts\niche_board.py --products --html
if errorlevel 1 (
    echo [run_daily] BOARD failed
    exit /b 1
)

REM --summary, not the full page. Both render the same panels; the full one
REM additionally embeds a row-level explorer and weighs ~5 MB against ~215 KB
REM (measured, 2026-08-29). Only the summary is publishable -- publish_web.py
REM skips anything over 1 MB rather than committing 5 MB a night to the repo,
REM so generating the full page here would leave the hosted site with no
REM population review at all. The explorer is still a command away when it is
REM actually wanted:
REM     python scripts\population_report.py --html
echo [run_daily] %DATE% %TIME% -- [4/5] POPULATION REVIEW
"%PY%" scripts\population_report.py --html --summary
if errorlevel 1 (
    echo [run_daily] POPULATION REVIEW failed
    exit /b 1
)

REM Refreshes web\ from the reports just written and rebuilds its index. The
REM Vercel project is connected to the GitHub repo, so a commit of web\ is
REM what actually deploys; this step only stages the content locally.
REM Deliberately NOT a git commit -- this repo holds untracked scratch files
REM and an automated `git add` here is how one of them reaches a remote.
echo [run_daily] %DATE% %TIME% -- [5/5] PUBLISH (stages web\ for deploy)
"%PY%" scripts\publish_web.py
if errorlevel 1 (
    echo [run_daily] PUBLISH failed -- reports were still written under DATA_DIR\reports
    exit /b 1
)

echo [run_daily] %DATE% %TIME% -- done
exit /b 0
