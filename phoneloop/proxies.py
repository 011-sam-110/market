"""Egress through a rotating residential proxy.

Why this exists, concretely: polling Vinted from one home connection earned a
403 on their catalogue endpoint within an afternoon, while the listing-page
endpoint kept working. One address is a single point of failure, and widening
to several marketplaces multiplies the exposure rather than spreading it.

The provider here (proxy-cheap's hub) is a *gateway*: one host:port that hands
out a different exit IP per connection. So there is no pool to manage, no
rotation logic, and no health-checking of individual peers -- the rotation
happens upstream. That keeps this module small on purpose.

Credentials live in ~/.phoneloop/.env, outside the repo and outside OneDrive,
and are never logged. Everything here that touches a URL goes through redact()
before it can reach a log line or an exception message.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger("phoneloop.proxies")

ENV_PATH = Path.home() / ".phoneloop" / ".env"

_CRED_RE = re.compile(r"//[^/@]*@")


def redact(url: str | None) -> str:
    """Strip credentials from a proxy URL so it is safe to log.

    Applied to every path a proxy URL could escape by: log lines, exception
    text, CLI output. A proxy URL carries a password in userinfo, and a stack
    trace is a very ordinary way for one to end up somewhere it should not be.
    """
    if not url:
        return "(none)"
    return _CRED_RE.sub("//***:***@", url)


def load_env_file(path: Path = ENV_PATH) -> dict[str, str]:
    """Read a minimal KEY=VALUE file. Missing file is normal, not an error.

    Deliberately not python-dotenv: this needs to parse five lines, and a
    dependency that reaches for os.environ by default is the wrong shape for
    something holding credentials.
    """
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def proxy_url(env: dict[str, str] | None = None) -> str | None:
    """The configured gateway URL, or None if egress should be direct.

    Precedence: an explicitly passed mapping, then the real environment, then
    the secrets file. The environment wins over the file so a single run can be
    pointed elsewhere without editing credentials on disk.
    """
    if env is not None:
        return env.get("PHONELOOP_PROXY_URL") or None
    return (
        os.environ.get("PHONELOOP_PROXY_URL")
        or load_env_file().get("PHONELOOP_PROXY_URL")
        or None
    )


def proxies_for(url: str | None) -> dict[str, str]:
    """Build a requests-style proxies mapping. Empty mapping means direct."""
    if not url:
        return {}
    return {"http": url, "https": url}


def apply(session, url: str | None = None) -> bool:
    """Route a requests.Session through the gateway. True if a proxy was set.

    Returns rather than raises when unconfigured, because running direct is a
    legitimate mode -- the collector worked that way for its first afternoon --
    and a hard failure here would take down a loop that could still do useful
    work.
    """
    resolved = url if url is not None else proxy_url()
    mapping = proxies_for(resolved)
    if not mapping:
        log.debug("no proxy configured, egress is direct")
        return False
    session.proxies.update(mapping)
    log.info("egress via proxy %s", redact(resolved))
    return True


def scrub(text: str, url: str | None = None) -> str:
    """Remove a proxy password from arbitrary text.

    requests puts the full proxy URL, credentials and all, into the message of
    a ProxyError. That message gets logged, and on this project it would then
    be written to a log file under the user's home directory.
    """
    resolved = url if url is not None else proxy_url()
    if not resolved:
        return text
    parts = urlsplit(resolved)
    if parts.password:
        text = text.replace(parts.password, "***")
    if parts.username:
        text = text.replace(parts.username, "***")
    bare = urlunsplit(("", parts.hostname or "", "", "", ""))
    return _CRED_RE.sub("//***:***@", text) if bare else text
