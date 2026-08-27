"""The loop: watch, parse, price, score, verify, alert.

Not a crawl. Stages 2-4 are pure local computation, so end-to-end latency is
essentially the poll interval plus one page fetch — which makes the poll interval
the only number worth optimising.

**tick() never raises.** A loop that dies on one malformed listing stops finding
deals entirely, so every stage is wrapped and every failure lands in `errors`.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .alerter import Alerter
from .config import Config
from .parser import parse
from .pricebook import Pricebook
from .scorer import score
from .store import Store
from .vinted_client import RateLimited, SchemaChanged
from .verifier import verify

log = logging.getLogger("phoneloop.watcher")

# Backoff bounds. The ceiling stops a run of 429s parking the loop for hours.
_MAX_POLL_INTERVAL_S = 900
_RECOVERY_FACTOR = 0.75


@dataclass
class TickResult:
    seen: int = 0
    written: int = 0
    parsed: int = 0
    candidates: int = 0
    alerted: int = 0
    errors: list[str] = field(default_factory=list)


def _money(blob) -> Decimal:
    """Vinted sends money as {"amount": "3.43", "currency_code": "GBP"}."""
    if isinstance(blob, dict):
        blob = blob.get("amount", "0")
    try:
        return Decimal(str(blob))
    except (InvalidOperation, TypeError):
        return Decimal("0")


class Watcher:
    def __init__(
        self,
        client,
        store: Store,
        pricebook: Pricebook,
        alerter: Alerter,
        cfg: Config,
    ):
        self.client = client
        self.store = store
        self.pricebook = pricebook
        self.alerter = alerter
        self.cfg = cfg
        self.poll_interval_s = cfg.POLL_INTERVAL_S
        # Alert-once memory. A bargain sitting in the feed should notify once,
        # not every minute until someone buys it.
        self._alerted: set[int] = set()

    # --- helpers -----------------------------------------------------------

    # A listing without these is not a listing. Coercing the gaps to defaults
    # would write a £0 observation and call it data — the kind of silent
    # acceptance that hides an upstream change until the numbers look wrong.
    _REQUIRED_FIELDS = ("id", "title", "total_item_price")

    def _to_observation(self, item: dict) -> dict:
        missing = [f for f in self._REQUIRED_FIELDS if not item.get(f)]
        if missing:
            raise ValueError(f"listing missing required fields: {', '.join(missing)}")
        return {
            "item_id": int(item["id"]),
            "seen_at": datetime.now(timezone.utc),
            "title": item.get("title", ""),
            "price": _money(item.get("price")),
            "total_item_price": _money(item.get("total_item_price")),
            "service_fee": _money(item.get("service_fee")),
            "currency": (item.get("total_item_price") or {}).get("currency_code", "GBP"),
            "condition": item.get("status", ""),
            "brand_title": item.get("brand_title", ""),
            "favourite_count": item.get("favourite_count", 0),
            "view_count": item.get("view_count", 0),
            "photo_count": len(item.get("photos") or []),
            "seller_id": (item.get("user") or {}).get("id", 0),
            "url": item.get("url", ""),
        }

    def _back_off(self) -> None:
        self.poll_interval_s = min(self.poll_interval_s * 2, _MAX_POLL_INTERVAL_S)
        log.warning("backing off; poll interval now %ss", self.poll_interval_s)

    def _recover(self) -> None:
        if self.poll_interval_s > self.cfg.POLL_INTERVAL_S:
            self.poll_interval_s = max(
                self.cfg.POLL_INTERVAL_S, int(self.poll_interval_s * _RECOVERY_FACTOR)
            )

    # --- one pass ----------------------------------------------------------

    def tick(self) -> TickResult:
        """One full pass. Synchronous, testable, and guaranteed not to raise."""
        result = TickResult()

        try:
            items = self.client.search(
                catalog_ids=self.cfg.CATALOG_ID,
                per_page=96,
                order="newest_first",
                price_from=self.cfg.PRICE_FROM,
            )
        except RateLimited as exc:
            result.errors.append(f"rate limit: {exc}")
            self._back_off()
            return result
        except SchemaChanged as exc:
            # Loud on purpose. A schema change that degrades to silence looks
            # exactly like a quiet market and could go unnoticed for days.
            result.errors.append(f"SCHEMA CHANGE: {exc}")
            log.error("vinted payload shape changed: %s", exc)
            return result
        except Exception as exc:                  # noqa: BLE001 - deliberate
            result.errors.append(f"search failed: {exc}")
            return result

        result.seen = len(items)
        self._recover()

        observations, parsed_items = [], []
        for item in items:
            try:
                observations.append(self._to_observation(item))
                parsed_items.append((item, parse(item.get("title", ""))))
            except Exception as exc:              # noqa: BLE001 - deliberate
                # One malformed payload must not cost us the rest of the batch.
                result.errors.append(f"item {item.get('id', '?')}: {exc}")

        try:
            result.written = self.store.record(observations)
        except Exception as exc:                  # noqa: BLE001 - deliberate
            result.errors.append(f"store write failed: {exc}")

        try:
            self.pricebook.refresh()
        except Exception as exc:                  # noqa: BLE001 - deliberate
            result.errors.append(f"pricebook refresh failed: {exc}")

        for item, spec in parsed_items:
            try:
                if not spec.is_phone:
                    continue
                result.parsed += 1

                condition = item.get("status", "")
                reference = self.pricebook.reference(spec, condition)
                verdict = score(
                    spec, _money(item.get("total_item_price")),
                    reference.median if reference else None, self.cfg,
                )
                if not verdict.alertable:
                    continue

                item_id = int(item["id"])
                if item_id in self._alerted:
                    continue

                result.candidates += 1

                # The one expensive call, and only for things already worth it.
                check = verify(self.client, item, spec)
                if not check.ok:
                    log.info("candidate %s failed verification: %s", item_id, check.reason)
                    continue

                if self.alerter.send(item, spec, verdict, reference):
                    self._alerted.add(item_id)
                    result.alerted += 1
            except Exception as exc:              # noqa: BLE001 - deliberate
                result.errors.append(f"scoring item {item.get('id', '?')}: {exc}")

        return result

    # --- the loop ----------------------------------------------------------

    def run(self, stop_event: threading.Event | None = None) -> None:
        """tick() on an interval until stopped."""
        stop_event = stop_event or threading.Event()
        while not stop_event.is_set():
            started = time.monotonic()
            result = self.tick()
            log.info(
                "tick: seen=%s written=%s parsed=%s candidates=%s alerted=%s errors=%s",
                result.seen, result.written, result.parsed,
                result.candidates, result.alerted, len(result.errors),
            )
            for err in result.errors:
                log.warning("  %s", err)
            elapsed = time.monotonic() - started
            stop_event.wait(max(0.0, self.poll_interval_s - elapsed))
