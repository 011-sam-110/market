"""Command line entry point.

    python -m phoneloop collect --once   log everything, alert nothing (week one)
    python -m phoneloop watch            the live loop
    python -m phoneloop stats            bucket coverage
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

from . import config
from .alerter import Alerter
from .pricebook import Pricebook
from .store import Store
from .vinted_client import VintedClient
from .watcher import Watcher

# Vinted titles routinely contain emoji, and Windows stdout defaults to cp1252,
# which raises on them. Reconfigure before anything from the feed is printed.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _build(cfg):
    store = Store(cfg.DATA_DIR, max_bytes=cfg.MAX_STORE_BYTES)
    pricebook = Pricebook(store, cfg)
    pricebook.refresh()
    return Watcher(VintedClient(), store, pricebook, Alerter(cfg), cfg)


def cmd_collect(args, cfg):
    """Week one: build the price distribution without acting on it.

    Nothing can be scored sensibly until the buckets are populated, and the
    survival-time and missing-storage questions both fall out of the same data.
    """
    cfg = config.load(env={**_env_from(cfg), "PHONELOOP_ALERTS_ENABLED": "0"})
    watcher = _build(cfg)
    print(f"collect mode — alerts disabled, writing to {cfg.DATA_DIR}")

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    while not stop.is_set():
        result = watcher.tick()
        print(
            f"  seen={result.seen:3} written={result.written:3} "
            f"parsed={result.parsed:3} errors={len(result.errors)}"
        )
        for err in result.errors[:3]:
            print(f"    ! {err}")
        if args.once:
            break
        stop.wait(watcher.poll_interval_s)


def cmd_watch(args, cfg):
    watcher = _build(cfg)
    if not cfg.TELEGRAM_TOKEN:
        print("warning: no telegram token set — alerts will be computed but not delivered")
    print(f"watching category {cfg.CATALOG_ID} every {watcher.poll_interval_s}s. ctrl-c to stop.")

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    watcher.run(stop)


def cmd_stats(args, cfg):
    store = Store(cfg.DATA_DIR)
    book = Pricebook(store, cfg)
    n = book.refresh()
    rows = book.stats()
    observations = store.observations_since(cfg.PRICE_WINDOW_DAYS)

    print(f"{len(observations)} observations in the last {cfg.PRICE_WINDOW_DAYS} days")
    print(f"{n} buckets at or above {cfg.MIN_BUCKET_SAMPLES} samples\n")
    if not rows:
        print("No priceable buckets yet. Run 'collect' for a few days first —")
        print("a reference built on fewer samples is worse than no reference.")
        return

    print(f"{'MODEL':<24}{'STORAGE':>9}{'CONDITION':>16}{'MEDIAN':>10}{'MAD':>8}{'N':>6}")
    for r in rows[:args.limit]:
        storage = f"{r['storage_gb']}GB" if r["storage_gb"] else "-"
        print(
            f"{(r['model'] or '?'):<24}{storage:>9}{r['condition'][:15]:>16}"
            f"{str(r['median']):>10}{str(r['mad']):>8}{r['samples']:>6}"
        )


def _env_from(cfg) -> dict:
    """Round-trip the parts of a Config that collect mode needs to override."""
    return {
        "PHONELOOP_DATA_DIR": str(cfg.DATA_DIR),
        "PHONELOOP_CATALOG_ID": str(cfg.CATALOG_ID),
        "PHONELOOP_POLL_INTERVAL_S": str(cfg.POLL_INTERVAL_S),
        "PHONELOOP_TELEGRAM_TOKEN": cfg.TELEGRAM_TOKEN,
        "PHONELOOP_TELEGRAM_CHAT_ID": cfg.TELEGRAM_CHAT_ID,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(prog="phoneloop", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_collect = sub.add_parser("collect", help="log listings without alerting")
    p_collect.add_argument("--once", action="store_true", help="single pass then exit")
    p_collect.set_defaults(func=cmd_collect)

    p_watch = sub.add_parser("watch", help="run the live alerting loop")
    p_watch.set_defaults(func=cmd_watch)

    p_stats = sub.add_parser("stats", help="show bucket coverage")
    p_stats.add_argument("--limit", type=int, default=30)
    p_stats.set_defaults(func=cmd_stats)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    return args.func(args, config.load()) or 0


if __name__ == "__main__":
    raise SystemExit(main())
