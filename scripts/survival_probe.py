"""Measure how long a Vinted phone listing stays available.

This answers the spec's first open question. Everything downstream depends on
it: if a bargain is gone in ninety seconds, the loop has to poll hard and alert
instantly; if it sits for six hours, polling hard is just rudeness with extra
steps.

Method. Track a rolling cohort of listings and ask each one, on a schedule,
whether it is still for sale. The signal is `offers.availability` in the
JSON-LD block the public page embeds -- a schema.org surface, which is far more
stable than the internal Next.js state blob, and it is the same block the
verifier already reads the description from. A deleted listing 404s instead.

Cost control. A listing page is ~315 KB on the wire (1.9 MB decoded), and the
endpoint rate-limits hard: thirty of them back-to-back drew a 429 inside twenty
seconds. So probes trickle -- one page at a time, spaced -- rather than running
in rounds, and the cohort is capped to what that spacing can service. Within
that budget probes are two-speed: fresh listings are asked often because that is
where the resolution matters, mature ones rarely. Items are retired after
MAX_TRACK_H as right-censored -- "still alive at 6h" is a real observation, not
a missing one, and survival analysis handles it.

Output is JSONL, one record per probe, appended and flushed. The probe can die
at any point without losing what it already learned.

    python scripts/survival_probe.py            # run until ctrl-c
    python scripts/survival_probe.py --hours 12 # stop after 12h
"""

from __future__ import annotations

import argparse
import json
import re
import signal
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phoneloop.vinted_client import RateLimited, VintedClient  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OUT = Path.home() / ".phoneloop" / "survival.jsonl"

# Measured, not guessed: thirty item pages fetched back-to-back drew a 429
# after roughly five requests in twenty seconds. The catalogue endpoint was
# unaffected -- the collector kept polling through it -- so the limit is on the
# public listing pages specifically.
#
# Hence a trickle rather than rounds: at most one page every PROBE_SPACING_S,
# which spreads the same cohort over time instead of bursting it. The cohort is
# then sized to what that spacing can actually service: twelve items on a
# five-minute cadence needs one probe every twenty-five seconds.
COHORT_MAX = 12          # concurrent tracked listings
PROBE_SPACING_S = 25     # hard floor between two listing-page fetches
FRESH_H = 1.0            # under this age in cohort, probe often
FRESH_EVERY_S = 300      # 5 min
MATURE_EVERY_S = 1200    # 20 min
SEED_EVERY_S = 600       # 10 min
MAX_TRACK_H = 6.0        # retire as censored past this
PRICE_FROM = 80
BACKOFF_S = 600          # after a 429

# schema.org availability values that genuinely mean "no longer for sale".
# Anything outside this set -- including a page we could not read -- is unknown,
# and unknown must never be recorded as a death. A probe that cannot tell has to
# say so and ask again, or the survival curve fills up with listings that were
# never actually sold.
_GONE = frozenset({
    "SoldOut", "OutOfStock", "Discontinued", "LimitedAvailability",
    "http://schema.org/SoldOut", "https://schema.org/SoldOut",
    "http://schema.org/OutOfStock", "https://schema.org/OutOfStock",
})
_UNREADABLE = frozenset({"no-ld", "no-offers", ""})

_LD = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)


def now() -> float:
    return time.time()


def availability(html: str) -> str:
    """Read offers.availability out of the page's JSON-LD Product block.

    Returns the raw string so an unfamiliar value shows up in the data rather
    than being flattened into a boolean here. We know "InStock"; whatever Vinted
    uses for sold is exactly what this study is trying to observe.
    """
    for block in _LD.findall(html):
        try:
            doc = json.loads(block)
        except json.JSONDecodeError:
            continue
        if doc.get("@type") == "Product":
            return str((doc.get("offers") or {}).get("availability", "")) or "no-offers"
    return "no-ld"


def photo_ts(item: dict) -> float | None:
    """Upload time of the main photo, the closest thing to a listing timestamp.

    The catalogue carries no created_at. This is a lower bound -- a seller can
    upload photos before publishing, or relist an old one -- so it is recorded
    as evidence, never used as the clock. Survival is measured from first sight.
    """
    hr = (item.get("photo") or {}).get("high_resolution") or {}
    ts = hr.get("timestamp")
    return float(ts) if ts else None


class Probe:
    def __init__(self, client: VintedClient, out: Path):
        self.client = client
        self.out = out
        self.out.parent.mkdir(parents=True, exist_ok=True)
        self.fh = self.out.open("a", encoding="utf-8")
        self.tracked: dict[int, dict] = {}
        self.resolved = 0
        self.censored = 0

    def write(self, **rec) -> None:
        rec["t"] = datetime.now(UTC).isoformat()
        self.fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.fh.flush()

    def seed(self) -> int:
        """Add newly-seen listings to the cohort, newest first."""
        if len(self.tracked) >= COHORT_MAX:
            return 0
        try:
            items = self.client.search(
                catalog_ids=3565, per_page=96, price_from=PRICE_FROM
            )
        except Exception as exc:  # noqa: BLE001 - a failed seed must not stop probing
            self.write(event="seed_failed", error=str(exc))
            return 0

        added = 0
        for item in items:
            if len(self.tracked) >= COHORT_MAX:
                break
            item_id = item.get("id")
            if not item_id or item_id in self.tracked:
                continue
            self.tracked[item_id] = {
                "url": item.get("url") or "",
                "first_seen": now(),
                "next_probe": now(),
                "probes": 0,
            }
            # The whole listing is captured at entry so the analysis can band it
            # by discount later. No pricebook exists yet -- buckets need eight
            # samples each -- so the scoring question is deferred, not dropped.
            self.write(
                event="enter",
                item_id=item_id,
                title=item.get("title", ""),
                price=(item.get("total_item_price") or {}).get("amount"),
                status=item.get("status", ""),
                brand=item.get("brand_title", ""),
                photo_ts=photo_ts(item),
                url=item.get("url", ""),
            )
            added += 1
        return added

    def probe_one(self, item_id: int, rec: dict) -> bool:
        """Ask one listing whether it is still for sale. True if it resolved."""
        try:
            html = self.client.fetch_public_page(rec["url"])
            avail = availability(html)
            status = 200
        except RateLimited:
            # Not a probe failure — a signal to stop asking for a while. Backing
            # off here rather than pressing on is what keeps the collector, which
            # shares this IP, from being taken down with the probe.
            self.write(event="rate_limited", item_id=item_id)
            print("  ! rate limited, backing off")
            time.sleep(BACKOFF_S)
            return False
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            # A 404 is a result, not an error: the listing is gone.
            if "returned 404" in msg:
                self.write(
                    event="resolved",
                    item_id=item_id,
                    reason="404",
                    alive_s=round(now() - rec["first_seen"]),
                    probes=rec["probes"] + 1,
                )
                return True
            self.write(event="probe_failed", item_id=item_id, error=msg)
            return False

        rec["probes"] += 1

        if avail in _UNREADABLE:
            # Could not tell. Say so and ask again. A 200 with no Product block
            # is most likely an anti-bot interstitial, which is exactly the sort
            # of thing that would otherwise be scored as a sale.
            rec["unreadable"] = rec.get("unreadable", 0) + 1
            self.write(event="unreadable", item_id=item_id, avail=avail,
                       count=rec["unreadable"])
            return False

        if avail in _GONE:
            self.write(
                event="resolved",
                item_id=item_id,
                reason=avail,
                http=status,
                alive_s=round(now() - rec["first_seen"]),
                probes=rec["probes"],
            )
            return True

        if avail != "InStock":
            # A value we have not seen before. Record it loudly and keep the
            # listing in the cohort rather than guessing which way it means.
            self.write(event="unknown_availability", item_id=item_id, avail=avail)
            return False

        # Still for sale. Worth a line of its own: death happened somewhere
        # between the last confirmed-alive probe and the one that found it gone,
        # and without this the analysis only has the upper bound of that
        # interval. It also proves the probe was actually running.
        self.write(event="alive", item_id=item_id,
                   alive_s=round(now() - rec["first_seen"]), probes=rec["probes"])
        return False

    def retire_expired(self) -> None:
        """Drop listings that outlived the tracking window, as censored.

        "Still for sale at six hours" is a real observation and the analysis
        needs it. Dropping these silently would bias the median downwards by
        keeping only the listings that happened to sell fast.
        """
        for item_id, rec in list(self.tracked.items()):
            if (now() - rec["first_seen"]) / 3600 >= MAX_TRACK_H:
                self.write(
                    event="censored",
                    item_id=item_id,
                    alive_s=round(now() - rec["first_seen"]),
                    probes=rec["probes"],
                )
                del self.tracked[item_id]
                self.censored += 1

    def probe_next_due(self) -> bool:
        """Probe at most ONE listing: the most overdue. True if one was probed.

        Deliberately one at a time. Bursting the whole cohort is what drew the
        429; spacing the same requests out costs nothing in resolution, because
        the cohort is sized so every item still gets asked on schedule.
        """
        due = [(r["next_probe"], i) for i, r in self.tracked.items()
               if r["next_probe"] <= now()]
        if not due:
            return False

        _, item_id = min(due)
        rec = self.tracked[item_id]
        age_h = (now() - rec["first_seen"]) / 3600

        if self.probe_one(item_id, rec):
            del self.tracked[item_id]
            self.resolved += 1
        else:
            every = FRESH_EVERY_S if age_h < FRESH_H else MATURE_EVERY_S
            rec["next_probe"] = now() + every
        return True

    def run(self, stop: threading.Event, hours: float | None) -> None:
        started = now()
        last_seed = 0.0
        self.write(event="probe_started", cohort_max=COHORT_MAX)
        print(f"survival probe -> {self.out}")
        print(f"cohort<={COHORT_MAX}, fresh every {FRESH_EVERY_S}s, "
              f"mature every {MATURE_EVERY_S}s, censor at {MAX_TRACK_H}h")

        last_report = 0.0
        while not stop.is_set():
            if now() - last_seed >= SEED_EVERY_S:
                added = self.seed()
                last_seed = now()
                if added:
                    print(f"  +{added} seeded")

            self.retire_expired()
            self.probe_next_due()

            # One line every ten minutes, not every tick. At a 25s spacing the
            # per-probe chatter would bury the events worth reading.
            if now() - last_report >= 600:
                print(
                    f"  tracking={len(self.tracked):>3} resolved={self.resolved:>3} "
                    f"censored={self.censored:>3}  ({(now()-started)/3600:.1f}h)"
                )
                last_report = now()

            if hours and (now() - started) / 3600 >= hours:
                break
            stop.wait(PROBE_SPACING_S)

        self.write(event="probe_stopped", resolved=self.resolved,
                   censored=self.censored, still_tracking=len(self.tracked))
        self.fh.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=float, default=None, help="stop after N hours")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    Probe(VintedClient(), args.out).run(stop, args.hours)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
