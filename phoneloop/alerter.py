"""Telegram alerts.

A phone below market is gone in minutes, so the message has to carry enough to
decide without opening a browser: what it is, what it costs landed, what the
market says, and how contested it already is.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone

import requests

from .config import Config
from .parser import PhoneSpec
from .pricebook import Reference
from .scorer import Band, Verdict

log = logging.getLogger("phoneloop.alerter")

_BAND_PREFIX = {
    Band.STRONG_BUY: "DEEP",
    Band.BUY: "BUY",
}


class Alerter:
    def __init__(self, cfg: Config, sender=None, now_fn=None):
        """
        Args:
            sender: Callable taking the message text, returning True on success.
                Injected so tests never reach Telegram.
            now_fn: Clock, injected so rate-limit windows are testable.
        """
        self.cfg = cfg
        self._sender = sender or self._telegram_sender
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._sent: dict[tuple, int] = defaultdict(int)

    # --- rate limiting -----------------------------------------------------

    def _window_key(self, bucket: tuple) -> tuple:
        now = self._now()
        return (bucket, now.strftime("%Y-%m-%dT%H"))

    def _would_exceed(self, bucket: tuple) -> bool:
        return self._sent[self._window_key(bucket)] >= self.cfg.ALERTS_PER_BUCKET_HOUR

    # --- message -----------------------------------------------------------

    def format(self, item: dict, spec: PhoneSpec, verdict: Verdict, reference: Reference) -> str:
        storage = f"{spec.storage_gb}GB" if spec.storage_gb else "storage not stated"
        model = (spec.model or "unknown").replace("-", " ").title()
        landed = item.get("total_item_price", {}).get("amount", "?")
        currency = item.get("total_item_price", {}).get("currency_code", "GBP")
        favourites = item.get("favourite_count", 0)
        url = item.get("url") or f"https://www.vinted.co.uk{item.get('path', '')}"

        lines = [
            f"{_BAND_PREFIX.get(verdict.band, 'BUY')}  {model} — {storage}",
            f"{currency} {landed} landed   ({verdict.discount_pct:.0f}% under market)",
            f"market {currency} {reference.median}  ·  {reference.samples} comps"
            + ("  ·  storage inferred" if reference.inferred_from_lowest_tier else ""),
            f"condition: {item.get('status', 'unknown')}  ·  {favourites} watching",
            url,
        ]
        return "\n".join(lines)

    # --- sending -----------------------------------------------------------

    def _telegram_sender(self, text: str) -> bool:
        if not self.cfg.TELEGRAM_TOKEN or not self.cfg.TELEGRAM_CHAT_ID:
            log.warning("telegram not configured; alert dropped")
            return False
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{self.cfg.TELEGRAM_TOKEN}/sendMessage",
                json={
                    "chat_id": self.cfg.TELEGRAM_CHAT_ID,
                    "text": text,
                    "disable_web_page_preview": False,
                },
                timeout=10,
            )
            return response.status_code == 200
        except Exception as exc:                  # noqa: BLE001 - deliberate
            log.warning("telegram send failed: %s", exc)
            return False

    def send(
        self,
        item: dict,
        spec: PhoneSpec,
        verdict: Verdict,
        reference: Reference,
    ) -> bool:
        """Send one alert.

        Returns False rather than raising on every refusal path — a failed alert
        must never take down the loop that produced it.
        """
        if not self.cfg.ALERTS_ENABLED:
            return False
        if not verdict.alertable:
            return False

        bucket = (spec.make, spec.model, spec.storage_gb)
        if self._would_exceed(bucket):
            # One mispriced reference could otherwise produce a flood, and a
            # flood is indistinguishable from noise.
            log.info("rate limit hit for bucket %s; alert suppressed", bucket)
            return False

        try:
            ok = bool(self._sender(self.format(item, spec, verdict, reference)))
        except Exception as exc:                  # noqa: BLE001 - deliberate
            # Telegram being down must never stop the watcher. The listing is
            # already recorded; losing the notification is the cheap failure.
            log.warning("alert send raised, swallowing: %s", exc)
            return False

        if ok:
            self._sent[self._window_key(bucket)] += 1
        return ok
