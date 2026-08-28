"""Which Chromium the live eBay scripts drive.

Measured 2026-08-28, all from the same home address (86.20.84.13) within a
few minutes, eBay UK homepage, no login:

    Playwright bundled Chromium 148                      403 "Something went wrong"
    ... with --disable-blink-features=AutomationControlled
        and --enable-automation removed                  403
    Chrome for Testing 152 (Google build)                200, search pages work

So the build is the discriminator -- not the address, not the automation
flags. The binary is configurable through PHONELOOP_CHROME_EXE in the
machine-local secrets file (~/.phoneloop/.env, never the repo) and falls
back to the installed Chrome (`channel="chrome"`) when unset, which is the
build every live measurement before today used.

Why a separate binary at all, when the installed Chrome works: a machine-
wide VPN (Mullvad, in use on this machine) puts direct egress on a
datacenter exit eBay refuses outright, and the residential proxy exit was
refused on the homepage too the same night. Mullvad can exempt ONE
executable path from the tunnel (`mullvad split-tunnel app add <exe>`), so
a dedicated automation binary lets the sweep go out on the home line while
everything else on the machine, the user's own browser included, stays
inside the VPN. Exempting the installed Chrome would exempt that browser.
"""
from __future__ import annotations

import os
from pathlib import Path

from phoneloop.proxies import load_env_file

ENV_KEY = "PHONELOOP_CHROME_EXE"


def launch_kwargs(env: dict[str, str] | None = None) -> dict:
    """Keyword arguments for `pw.chromium.launch` that pick the binary.

    Precedence matches proxies.proxy_url: an explicitly passed mapping, then
    the real environment, then the secrets file. A blank value is unset.
    """
    if env is not None:
        exe = env.get(ENV_KEY) or ""
    else:
        exe = os.environ.get(ENV_KEY) or load_env_file().get(ENV_KEY) or ""
    exe = exe.strip()
    if exe:
        return {"executable_path": exe}
    return {"channel": "chrome"}


def launch(pw, *, headless: bool, proxy: dict | None = None):
    """`pw.chromium.launch` with the configured binary.

    Raises before Playwright does if the configured path does not exist,
    naming the variable to fix -- a stale path after a Chrome for Testing
    upgrade would otherwise surface as a Playwright launch error with no
    hint of where the path came from.
    """
    kw = launch_kwargs()
    exe = kw.get("executable_path")
    if exe and not Path(exe).exists():
        raise FileNotFoundError(f"{ENV_KEY} points at a missing binary: {exe}")
    return pw.chromium.launch(headless=headless, proxy=proxy, **kw)
