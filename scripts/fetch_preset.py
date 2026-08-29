"""Fetch the knob preset saved from the dashboard, for the nightly run.

    python scripts/fetch_preset.py            # normal, run as step 0 of the night
    python scripts/fetch_preset.py --dry-run  # print what it would write

The dashboard is a static page on Vercel; when you save a preset there it goes
to a Vercel Blob behind the project's own authentication. This pulls it down to
`~/.phoneloop/knobs.json`, which `Knobs.load()` reads.

**This script cannot fail the night.** Every failure path -- no URL configured,
DNS gone, a 500, a timeout, a truncated body, a payload that is not a valid
preset -- leaves the existing file exactly as it was, writes one line to
stderr, and exits 0. The sweep that follows is the valuable part of the night
and it runs perfectly well on yesterday's knobs; a preset fetch that could
abort it would be a net loss the first time the internet hiccuped at 3am.

The payload is validated with `Knobs.parse_preset` -- the same function
`Knobs.load` uses -- BEFORE anything is written, so a bad remote payload can
never land on disk and break the following night too.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phoneloop.knobs import Knobs  # noqa: E402

URL_ENV = "PHONELOOP_PRESET_URL"
TOKEN_ENV = "PHONELOOP_PRESET_TOKEN"
TIMEOUT_S = 15
# A preset is a dozen small integers. Anything larger is not a preset, and
# reading it into memory before finding that out is how a hostile or broken
# endpoint turns a 3am cron into an OOM.
MAX_BYTES = 64 * 1024


def _warn(msg: str) -> None:
    print(f"fetch_preset: {msg}", file=sys.stderr)


def fetch(url: str, token: str | None = None, *,
          timeout: float = TIMEOUT_S) -> object:
    """GET and parse the preset. Raises on anything that is not a clean read."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        if resp.status != 200:
            raise ValueError(f"HTTP {resp.status}")
        raw = resp.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError(
            f"response is larger than {MAX_BYTES} bytes -- a preset is a dozen "
            "integers, so this is not one")
    return json.loads(raw.decode("utf-8"))


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    e = dict(os.environ if env is None else env)
    parser = argparse.ArgumentParser(
        description="Fetch the dashboard's knob preset for the nightly run")
    parser.add_argument("--url", default=e.get(URL_ENV))
    parser.add_argument("--out", default=None,
                        help=f"where to write (default: {Knobs.default_path(e)})")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be written and write nothing")
    parser.add_argument("--timeout", type=float, default=TIMEOUT_S)
    args = parser.parse_args(argv)

    out = Path(args.out) if args.out else Knobs.default_path(e)

    if not args.url:
        _warn(f"no {URL_ENV} set -- keeping whatever is already at {out}")
        return 0

    try:
        payload = fetch(args.url, e.get(TOKEN_ENV), timeout=args.timeout)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            ValueError, TimeoutError) as exc:
        _warn(f"could not fetch {args.url}: {type(exc).__name__}: {exc}")
        _warn(f"keeping the existing {out} -- the night runs on those knobs")
        return 0

    # Validated with the loader's own parser, before anything touches disk.
    try:
        overrides = Knobs.parse_preset(payload)
    except ValueError as exc:
        _warn(f"refusing the fetched preset: {exc}")
        _warn(f"keeping the existing {out}")
        return 0

    envelope = {
        "schema_version": 1,
        "knobs": overrides,
        "fetched_from": args.url,
    }
    for passthrough in ("label", "saved_at", "saved_by"):
        if isinstance(payload, dict) and passthrough in payload:
            envelope[passthrough] = payload[passthrough]

    text = json.dumps(envelope, indent=2, sort_keys=True) + "\n"

    if args.dry_run:
        print(f"would write {out}:")
        print(text)
        return 0

    try:
        _write_atomic(out, text)
    except OSError as exc:
        _warn(f"could not write {out}: {exc}")
        return 0

    label = envelope.get("label")
    changed = ", ".join(f"{k}={v}" for k, v in sorted(overrides.items()))
    _warn(f"wrote {out}" + (f" ({label})" if label else ""))
    _warn(f"  {changed or 'no overrides -- every knob is at its default'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
