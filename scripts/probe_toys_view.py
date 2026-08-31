"""Why does toys_games carry a seller on 2% of rows and every sibling on 97%?

Measured offline first, against the recorded pages already in the cassette:
all five toys_games leaves sit at 1.6-2.1% seller coverage, every one of the
eight sibling departments at 96.6-97.9%. Every percent-bearing line in the
3,672 unmatched toys_games rows is a promotion ("Save up to 15% with
Multi-buy") or product-title text. There is no third seller-line shape, so
this is NOT a regex miss: the seller line is absent from the card itself.

This probe tests the one hypothesis that would explain a whole department
rendering a different card -- eBay's results VIEW. `_dmd=1` asks for list
view, `_dmd=2` for gallery. The sweep URL sets neither, so eBay picks per
category, and a gallery card has no room for a seller line.

Six live page loads, no store writes and no fixtures written. The control
leaf is what tells a real fix apart from a good day on eBay.

    PHONELOOP_HARNESS=live python scripts/probe_toys_view.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright  # noqa: E402

from phoneloop.ebay import _SELLER_ALT_RE, _SELLER_PRIMARY_RE  # noqa: E402
from scripts.bulk_ebay import (  # noqa: E402
    _ROW_JS, NAV_TIMEOUT_MS, is_blocked, open_session,
)

# One toys_games leaf and one control from a department that already works.
LEAVES = (("toys_games", "246"), ("pet_supplies", "20742"))
VIEWS = (("as the sweep asks today", ""),
         ("list view", "&_dmd=1"),
         ("gallery view", "&_dmd=2"))

BASE = ("https://www.ebay.co.uk/sch/i.html?_sacat={leaf}&_fsrp=1&_ipg=240"
        "&_pgn=1&_udlo=15&_udhi=55")


def seller_share(rows) -> tuple[int, int, str]:
    hit, example = 0, ""
    for r in rows:
        for ln in (x.strip() for x in (r["text"] or "").split("\n")):
            if _SELLER_PRIMARY_RE.match(ln) or _SELLER_ALT_RE.match(ln):
                hit += 1
                example = example or ln
                break
    return hit, len(rows), example


def main() -> int:
    with sync_playwright() as pw:
        browser, page, used_proxy = open_session(pw, headless=False, force_direct=False)
        print(f"egress: {'residential proxy' if used_proxy else 'direct'}\n")
        try:
            for slug, leaf in LEAVES:
                for label, suffix in VIEWS:
                    url = BASE.format(leaf=leaf) + suffix
                    page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
                    page.wait_for_timeout(2500)
                    body = page.inner_text("body")
                    if is_blocked(body, page.url):
                        print(f"{slug:<14} {label:<22} BLOCKED -- nothing read from this load")
                        time.sleep(12)
                        continue
                    rows = page.evaluate(_ROW_JS)
                    hit, total, example = seller_share(rows)
                    share = f"{hit / total * 100:.1f}%" if total else "no rows"
                    print(f"{slug:<14} {label:<22} {total:>4} rows  seller {hit:>4}  {share:>7}"
                          + (f"   e.g. {example}" if example else ""))
                    time.sleep(4)
                print()
        finally:
            browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
