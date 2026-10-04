#!/usr/bin/env bash
# Install the paper forecaster as systemd USER units on this machine
# (provenance-1). Idempotent: re-run after editing deploy/systemd/forecast-*.
#
# Expects the checkout at ~/market-forecast with a venv at .venv:
#   git clone git@github.com:011-sam-110/market.git ~/market-forecast
#   python3 -m venv ~/market-forecast/.venv
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dest="$HOME/.config/systemd/user"
mkdir -p "$dest" "$HOME/.forecast"
cp "$here/systemd/forecast-daily.service" "$here/systemd/forecast-daily.timer" "$dest/"

systemctl --user daemon-reload
systemctl --user enable --now forecast-daily.timer

if [ "$(loginctl show-user "$USER" -p Linger --value)" != "yes" ]; then
    echo "WARNING: lingering is off. Run once: sudo loginctl enable-linger $USER"
fi
systemctl --user list-timers forecast-daily.timer --no-pager
