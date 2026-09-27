#!/usr/bin/env bash
# Install the nightly as systemd USER units on this machine (provenance-1).
# Idempotent: re-run after editing deploy/systemd/*.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dest="$HOME/.config/systemd/user"
mkdir -p "$dest"
cp "$here/systemd/phoneloop-nightly.service" "$here/systemd/phoneloop-nightly.timer" "$dest/"

systemctl --user daemon-reload
systemctl --user enable --now phoneloop-nightly.timer

# Without lingering, every user timer stops when the user logs out.
if [ "$(loginctl show-user "$USER" -p Linger --value)" != "yes" ]; then
    echo "WARNING: lingering is off. Run once: sudo loginctl enable-linger $USER"
fi
systemctl --user list-timers phoneloop-nightly.timer --no-pager
