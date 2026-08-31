@echo off
REM eBay niche finder -- daily pipeline (design spec section 10; contracts
REM section 4, s5_snapshots_board's "run_daily.cmd" deliverable).
REM
REM Runs FETCH PRESET -> SWEEP -> DEEPEN -> BOARD -> POPULATION -> PUBLISH
REM -> EXPORT DASHBOARD, in that order,
REM once a day.
REM
REM BUDGET, measured on the 2026-08-30 run and used to size every step below:
REM   sweep   10.0 s per page load   (128 loads, 11:16:53 -> 11:38:14)
REM   deepen   6.04 s per item page  (200 items, 11:38:14 -> 11:58:22)
REM   board 23 s, population 57 s, publish <1 s, export 87 s
REM A step sized against anything other than these two numbers is a guess. DIFFERENCE (Tier 3) is NOT a separate step here:
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
REM   --pages-per-shard caps page loads PER HUB, and is 3 because that is
REM                     what the 2026-08-28 and 2026-08-29 snapshots were
REM                     swept at. It is a COMPARABILITY setting, not a
REM                     throughput one.
REM
REM                     This read 10 until 2026-08-29. Left there, the first
REM                     scheduled night would have swept ~3x the population of
REM                     the two nights already in the store, and Tier 3 --
REM                     which differences the two most recent dates -- would
REM                     have booked thousands of listings from pages 4-10 as
REM                     NEW arrivals overnight. That is the failure this
REM                     project keeps hitting: not a missing number, a
REM                     confident wrong one, and nothing about it goes red.
REM
REM                     Changing the depth is legitimate; doing it silently
REM                     between two nights that get differenced is not.
REM
REM                     That is no longer left to whoever reads this comment.
REM                     phoneloop/footprint.py reads each night's own sweep
REM                     checkpoint, compares the two, and the board prints
REM                     NOT COMPARABLE with the settings that moved rather
REM                     than presenting the difference as change over time.
REM                     Raise this toward 42 (the 10,000-result cap) when full
REM                     Tier 1 coverage is wanted; the first diff after the
REM                     change reports itself.
REM                     3 pages x 43 hubs = ~128 page loads, ~21 min measured
REM                     (11:16:53 to 11:38:14 on 2026-08-30, so 10.0 s a page).
REM   RESULTS VIEW      pinned to list (&_dmd=1) in phoneloop/ebay.py since
REM                     2026-08-31. eBay picks the view PER CATEGORY, and the
REM                     gallery card carries no seller line: Toys & Games sat
REM                     at 2.0% seller coverage for four nights while all
REM                     eight sibling departments sat at 96.6-97.9%. Forcing
REM                     Pet Supplies into gallery reproduced the outage
REM                     exactly (96.9% -> 3.1%), which is what made it a
REM                     cause. Gallery also returned fewer rows: 169 against
REM                     255 on the same leaf, band and page.
REM
REM                     Tonight is the first night in list view, so tonight's
REM                     difference against last night is genuinely NOT
REM                     comparable and says so. Every recording captured
REM                     before that date is a gallery-era page and no longer
REM                     matches its key, so an offline replay run needs a
REM                     fresh scripts/record_ebay.py session.
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
REM   [5/7] POPULATION REVIEW writes the self-contained population page
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
REM   [6/7] PUBLISH     refreshes web\ from the reports just written and
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

REM Pulls the knob preset the dashboard published, into
REM %%USERPROFILE%%\.phoneloop\knobs.json, so tonight ranks against what the
REM operator last set rather than against whatever Knobs() defaults to.
REM
REM Every failure path inside fetch_preset.py exits 0 and leaves the existing
REM file untouched -- an unreachable endpoint, a bad envelope, an oversized
REM body. A preset that cannot be fetched must never stop a sweep, and a
REM half-written knobs.json would silently re-rank the whole board.
REM
REM Needs PHONELOOP_PRESET_URL to do anything at all; without it the step is
REM a no-op that says so. An explicit PHONELOOP_* variable still beats the
REM file, per Knobs.load.
echo [run_daily] %DATE% %TIME% -- [1/7] FETCH PRESET (never fatal)
"%PY%" scripts\fetch_preset.py

echo [run_daily] %DATE% %TIME% -- [2/7] SWEEP
"%PY%" scripts\bulk_ebay.py --leaf-sweep --pages-per-shard 3
if errorlevel 1 (
    echo [run_daily] SWEEP failed -- checkpointed, safe to re-run
    exit /b 1
)

REM --deepen-limit 5000, raised from the default 200 on 2026-08-31.
REM
REM Measured, not chosen. The store holds 29,824 listings, and only 1,896
REM product groups of two or more listings exist across them, holding 4,750
REM listings. That is the entire rankable population -- the other 25,074 are
REM singletons no board row is made of. 4,707 of the 4,750 had never been
REM deepened, so min_margin, price_band and delivery read "?" on almost every
REM row the board ranks.
REM
REM At the measured 6.04 s an item (200 items between 11:38:14 and 11:58:22
REM on 2026-08-30), 5,000 is about 8.4 hours. That is a one-off catch-up, not
REM a nightly cost: rows_to_deepen only ever returns listings with no
REM seller_type, so once the rankable population is enriched the nightly
REM batch is whatever arrived that day.
REM
REM 200 a night against a 4,707 backlog was 23.5 nights to the same place.
REM
REM This does NOT affect comparability. The Tier 3 difference is computed
REM off the sweep checkpoint, and deepening enriches listings the sweep
REM already recorded rather than adding any. phoneloop/footprint.py is
REM therefore silent about this change, correctly.
REM
REM Unattended for eight hours, so bulk_ebay.py stops after 25 consecutive
REM item-page failures rather than spending the rest of the budget on a
REM source that has started refusing the session. It exits 0 when it does:
REM the rows already deepened are real and stored, and the board is worth
REM building on them.
echo [run_daily] %DATE% %TIME% -- [3/7] DEEPEN (Tier 2, item-page enrichment)
"%PY%" scripts\bulk_ebay.py --deepen --deepen-limit 5000
if errorlevel 1 (
    echo [run_daily] DEEPEN failed -- SWEEP already landed today's rows, safe to re-run
    exit /b 1
)

echo [run_daily] %DATE% %TIME% -- [4/7] BOARD (difference runs inside niche_board.py once 2+ snapshot dates exist)
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
echo [run_daily] %DATE% %TIME% -- [5/7] POPULATION REVIEW
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
echo [run_daily] %DATE% %TIME% -- [6/7] PUBLISH (stages web\ for deploy)
"%PY%" scripts\publish_web.py
if errorlevel 1 (
    echo [run_daily] PUBLISH failed -- reports were still written under DATA_DIR\reports
    exit /b 1
)

REM Writes dashboard\public\data -- the nine JSON files the board reads.
REM Nothing is sampled and nothing is capped; the payloads carry `capped:
REM false` and their own limit so a future truncation shows up on screen
REM rather than as a quietly shorter board.
REM
REM Deliberately does NOT deploy. `python scripts\publish_dashboard.py
REM --deploy` is the separate, deliberate step, and it refuses to publish a
REM bundle whose date is older than the newest snapshot in the store -- a
REM failed export would otherwise republish yesterday under today's date and
REM look completely healthy doing it.
echo [run_daily] %DATE% %TIME% -- [7/7] EXPORT DASHBOARD (bundle only, no deploy)
"%PY%" scripts\export_dashboard.py
if errorlevel 1 (
    echo [run_daily] EXPORT DASHBOARD failed -- the sweep and reports already landed; the board keeps serving the previous bundle
    exit /b 1
)

echo [run_daily] %DATE% %TIME% -- done
exit /b 0
