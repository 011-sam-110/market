"""Vinted's internal catalogue API.

The catalogue half needs no login — seven cookies from the homepage are the whole
handshake. Item *detail* is auth-gated (`/api/v2/items/{id}/details` returns 403),
but the public HTML page is not and carries the description, so the verifier
reads that instead.

Verified live 2026-08-27.
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode

import requests

log = logging.getLogger("phoneloop.client")

BASE_URL = "https://www.vinted.co.uk"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class RateLimited(Exception):
    """Vinted asked us to slow down. The caller backs off; it does not retry here."""


class SchemaChanged(Exception):
    """The payload was not the shape we expect.

    Raised rather than returning an empty list, because a silent empty is
    indistinguishable from a quiet market and would hide an API change for as
    long as it took someone to notice the alerts had stopped.
    """


class VintedClient:
    def __init__(self, base_url: str = BASE_URL, session=None, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout
        self._handshaked = False

    def handshake(self) -> None:
        """Fetch the homepage to pick up an anonymous session cookie."""
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Referer": f"{self.base_url}/",
        })
        self.session.get(f"{self.base_url}/", timeout=self.timeout)
        self._handshaked = True
        log.debug("handshake complete")

    def _ensure_session(self) -> None:
        if not self._handshaked:
            self.handshake()

    def search(
        self,
        catalog_ids: int,
        per_page: int = 96,
        order: str = "newest_first",
        **filters,
    ) -> list[dict]:
        """Query the catalogue.

        Raises:
            RateLimited: on HTTP 429.
            PermissionError: if the session is still rejected after one re-handshake.
            SchemaChanged: if the payload has no `items` key.
        """
        self._ensure_session()
        params = {"catalog_ids": catalog_ids, "per_page": per_page, "order": order}
        params.update({k: v for k, v in filters.items() if v is not None})
        url = f"{self.base_url}/api/v2/catalog/items?{urlencode(params)}"

        response = self.session.get(url, timeout=self.timeout)

        if response.status_code == 429:
            raise RateLimited("vinted returned 429")

        if response.status_code in (401, 403):
            # Sessions expire; that is normal. Re-handshake exactly once — a
            # loop here would turn one expiry into hammering the homepage.
            log.info("session rejected (%s), re-handshaking once", response.status_code)
            self.handshake()
            response = self.session.get(url, timeout=self.timeout)
            if response.status_code in (401, 403):
                raise PermissionError(
                    f"still {response.status_code} after re-handshake — "
                    "credentials or anti-bot posture may have changed"
                )

        if response.status_code != 200:
            raise SchemaChanged(f"unexpected status {response.status_code}")

        payload = response.json()
        if "items" not in payload:
            raise SchemaChanged(
                f"no 'items' key in response; got keys {sorted(payload)[:8]}"
            )
        return payload["items"]

    def fetch_public_page(self, path: str) -> str:
        """Fetch a public listing page.

        Roughly 1.9 MB each, so this is candidates-only — never the hot path.
        """
        self._ensure_session()
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        response = self.session.get(
            url, timeout=self.timeout, headers={"Accept": "text/html,*/*"}
        )
        if response.status_code == 429:
            raise RateLimited("vinted returned 429 on a public page")
        if response.status_code != 200:
            raise SchemaChanged(f"public page returned {response.status_code}")
        return response.text
