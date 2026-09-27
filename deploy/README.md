# Where the nightly runs

Since 2026-09-27 the eBay sweep runs on **provenance-1**, the Fedora machine on
the home LAN (`ssh provenance-1`). It publishes straight to Vercel:

    03:00  phoneloop-nightly.timer
             -> scripts/nightly.py        lock, day-guard, log, heartbeat
             -> scripts/run_daily.sh      update, sweep, deepen, board,
                                          population, publish, PUSH, export
    PUSH   git push web/ to origin/main  -> Vercel builds main
                                          -> https://market-ivory-three.vercel.app

The Windows task `PhoneLoop eBay Daily` is **disabled**, not deleted. Two
machines sweeping from one IP is a block risk, and both would write their own
store. Re-enable it only after you stop the timer here.

## Why this box

eBay refuses datacenter and VPN egress. provenance-1 leaves through the home
Virgin Media address, which is the only kind of exit that has had a 200. It is
on all night, so the Windows failure modes do not apply here: the 3h task
limit, sleep, and battery.

## Layout on provenance-1

| What | Where |
|---|---|
| Checkout (main) | `~/market` |
| Python | `~/market/.venv` (system python3) |
| Store | `~/.phoneloop/prod` (parquet, reports, logs, health) |
| Units | `~/.config/systemd/user/phoneloop-nightly.{service,timer}` |
| Push credential | `~/.ssh/market_deploy`, a write deploy key on this repo only |
| Browser | the installed `google-chrome`, headed, under `xvfb-run` |

## Everyday commands

    systemctl --user list-timers phoneloop-nightly.timer
    systemctl --user start phoneloop-nightly.service     # run tonight's now
    journalctl --user -u phoneloop-nightly -n 50
    tail -f ~/.phoneloop/prod/logs/nightly-$(date -u +%F).log
    cat ~/.phoneloop/prod/health/last_run.json

## Set up from scratch

    git clone git@github.com:011-sam-110/market.git ~/market   # with the deploy key
    cd ~/market && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    # copy ~/.phoneloop/prod from the old host (Tier 3 needs the history)
    bash deploy/install-systemd.sh

Lingering must be on (`loginctl show-user $USER -p Linger`), or the timer
stops at logout.
