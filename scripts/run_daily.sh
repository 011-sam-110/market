#!/usr/bin/env bash
# eBay niche finder -- the daily pipeline on Linux (provenance-1, since
# 2026-09-27). The same steps in the same order as scripts/run_daily.cmd,
# plus two:
#
#   [0/9] UPDATE    fast-forward this checkout to origin/main, so the box runs
#                   the code the repo says it runs. Never fatal: a failed pull
#                   runs last night's code and says so.
#   [7/9] PUSH      commit web/ and push it to origin/main. Vercel builds main
#                   on every push, so this is the step that puts tonight's
#                   reports on https://market-ivory-three.vercel.app.
#
# Read run_daily.cmd for WHY each step and each flag is what it is. The
# measured budgets, the pages-per-shard comparability rule and the egress
# findings all live there and apply here unchanged.
#
# Do not run this directly from a timer. The systemd unit runs
# scripts/nightly.py, which owns the lock, the day-guard, the log and the
# heartbeat, and which launches this file as its one child (see
# nightly.default_command). The banners below are the format nightly.py
# parses: "[run_daily] <time> -- [n/m] NAME" and "[run_daily] NAME failed".
#
# The body is one function, called on the last line. bash reads a script as
# it runs it, and step 0 can rewrite this file with a pull; a function is
# parsed whole before any of it runs, so the pull cannot change the steps of
# the run that made it.

main() {
    set -u
    local REPO PY
    REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" || return 1
    cd "$REPO" || { echo "[run_daily] could not cd to repo root $REPO"; return 1; }

    PY="${PHONELOOP_PYTHON:-$REPO/.venv/bin/python}"
    if [ ! -x "$PY" ]; then
        echo "[run_daily] python not found at $PY -- see deploy/README.md"
        return 1
    fi

    # Load-bearing, as in run_daily.cmd: the harness default is replay, and
    # a live night in replay raises MissingRecording on its first page.
    export PHONELOOP_HARNESS=live
    export PHONELOOP_DATA_DIR="${PHONELOOP_DATA_DIR:-$HOME/.phoneloop/prod}"
    export PYTHONUNBUFFERED=1

    banner() { echo "[run_daily] $(date '+%a %d/%m/%Y %H:%M:%S') -- [$1/9] $2"; }

    # The sweep and deepen drive a HEADED Chrome (bulk_ebay.py is run without
    # --headless, exactly as on Windows). Under a timer there is no display,
    # so give the browser steps a virtual one. A manual run from a desktop
    # session keeps its real display.
    browser() {
        if [ -n "${DISPLAY:-}" ]; then
            "$@"
        else
            xvfb-run -a -s "-screen 0 1920x1080x24" "$@"
        fi
    }

    banner 0 "UPDATE (never fatal)"
    if git pull --ff-only -q origin main; then
        echo "[run_daily] code at $(git rev-parse --short HEAD)"
    else
        echo "[run_daily] WARNING: pull failed, running the local code at $(git rev-parse --short HEAD)"
    fi

    banner 1 "FETCH PRESET (never fatal)"
    "$PY" scripts/fetch_preset.py

    banner 2 "SWEEP"
    if ! browser "$PY" scripts/bulk_ebay.py --leaf-sweep --pages-per-shard 3; then
        echo "[run_daily] SWEEP failed -- checkpointed, safe to re-run"
        return 1
    fi

    # --deepen-max-minutes: deepen runs before the board. On Windows the
    # task's 3h limit killed the chain inside a 5,000-item deepen on most
    # nights after 2026-09-08, so no report was written. 120 minutes is
    # ~1,190 items at the measured 6.04 s each; the rest keep their place in
    # tomorrow's batch.
    banner 3 "DEEPEN (Tier 2, item-page enrichment)"
    if ! browser "$PY" scripts/bulk_ebay.py --deepen --deepen-limit 5000 --deepen-max-minutes 120; then
        echo "[run_daily] DEEPEN failed -- SWEEP already landed today's rows, safe to re-run"
        return 1
    fi

    banner 4 "BOARD (difference runs inside niche_board.py once 2+ snapshot dates exist)"
    if ! "$PY" scripts/niche_board.py --products --html; then
        echo "[run_daily] BOARD failed"
        return 1
    fi

    banner 5 "POPULATION REVIEW"
    if ! "$PY" scripts/population_report.py --html --summary; then
        echo "[run_daily] POPULATION failed"
        return 1
    fi

    banner 6 "PUBLISH (stages web/ for deploy)"
    if ! "$PY" scripts/publish_web.py; then
        echo "[run_daily] PUBLISH failed -- reports were still written under DATA_DIR/reports"
        return 1
    fi

    # Only web/ is staged, by path. Nothing else in the checkout is ever
    # committed by the night.
    banner 7 "PUSH (web/ to origin/main; Vercel deploys main)"
    git add -- web
    if git diff --cached --quiet -- web; then
        echo "[run_daily] web/ unchanged, nothing to push"
    else
        git commit -q -m "data: nightly reports $(date -u +%Y-%m-%d)" \
            -m "Swept, built and published by $(hostname) (scripts/run_daily.sh)." -- web \
            || { echo "[run_daily] PUSH failed -- could not commit web/"; return 1; }
    fi
    if ! { git pull --rebase -q origin main && git push -q origin HEAD:main; }; then
        git rebase --abort 2>/dev/null
        echo "[run_daily] PUSH failed -- web/ is committed locally and goes out with the next push"
        return 1
    fi
    echo "[run_daily] pushed $(git rev-parse --short HEAD) to origin/main"

    banner 8 "EXPORT DASHBOARD (bundle only, no deploy)"
    if ! "$PY" scripts/export_dashboard.py; then
        echo "[run_daily] EXPORT failed -- the reports are already published; the board keeps serving the previous bundle"
        return 1
    fi

    echo "[run_daily] $(date '+%a %d/%m/%Y %H:%M:%S') -- done"
    return 0
}

main "$@"; exit $?
