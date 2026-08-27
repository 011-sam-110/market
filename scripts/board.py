"""The board: every live Vinted listing priced against every exit that exists.

This is where the three sources finally meet. For each listing the parser can
read, look up what CeX will commit to and what eBay's UK market is asking, and
report the margin -- separating the guaranteed part from the speculative part,
because they are not the same kind of number and a single "profit" column that
mixes them is how paper margins get believed.

    python scripts/board.py                  # live feed
    python scripts/board.py --private        # assume a fee-free eBay account
    python scripts/board.py --grade A        # price against a better CeX grade
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phoneloop import config, pricing, spread  # noqa: E402
from phoneloop.parser import parse  # noqa: E402
from phoneloop.verifier import extract_description  # noqa: E402
from phoneloop.vinted_client import VintedClient  # noqa: E402

# Vinted 429s after roughly five listing pages in twenty seconds. This is the
# measured trickle rate, not a guess, and it is why verification is capped.
PROBE_SPACING_S = 22

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--grade", default="B", choices=["A", "B", "C"],
                    help="CeX grade to price the exit at (B is the safe read)")
    ap.add_argument("--private", action="store_true",
                    help="assume a fee-free private eBay account - VERIFY FIRST")
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--verify", type=int, default=8, metavar="N",
                    help="inspect the top N listings' descriptions (0 to skip)")
    args = ap.parse_args()

    cfg = config.load()
    cex_table = pricing.load_table(cfg.DATA_DIR / "cex_prices.json")
    ebay_table = pricing.load_table(cfg.DATA_DIR / "ebay_ceilings.json")
    fees = spread.PRIVATE if args.private else spread.BUSINESS

    print(f"CeX table: {len(cex_table)} models   "
          f"eBay ceilings: {len(ebay_table)} queries   "
          f"fees: {fees.name}")
    if args.private:
        print("  ! fee-free private selling is ASSUMED, not verified")
    print(f"  undercut {spread.UNDERCUT:.0%}, postage "
          f"£{spread.SHIP_PENCE / 100:.2f}, CeX grade {args.grade} "
          "- all assumptions, see phoneloop/spread.py\n")

    items = VintedClient().search(
        catalog_ids=cfg.CATALOG_ID, per_page=96, price_from=cfg.PRICE_FROM
    )

    rows, unpriceable, disqualified = [], 0, 0
    for it in items:
        spec = parse(it.get("title", ""))
        if spec.disqualifiers:
            disqualified += 1
            continue
        if not spec.model:
            unpriceable += 1
            continue
        buy = (it.get("total_item_price") or {}).get("amount")
        if buy is None:
            continue
        buy_pence = round(float(buy) * 100)

        cash, _ = pricing.cex_cash(cex_table, spec.model, spec.storage_gb,
                                   args.grade)
        ceiling, exact = pricing.ebay_ceiling(ebay_table, spec.model,
                                              spec.storage_gb)
        s = spread.evaluate(buy_pence, cash, ceiling, fees=fees)
        if s is None:
            unpriceable += 1
            continue
        rows.append((s, spec, it, exact))

    rows.sort(key=lambda r: r[0].margin_pence, reverse=True)

    # Verify before presenting. A title tells you almost nothing about whether
    # a handset is sellable: of the four best-looking margins ever found by
    # this project, four were traps, and every one of them was declared in the
    # description while the title stayed clean. A board that ranks on titles
    # alone ranks traps to the top by construction, because the cheapest
    # listing for a given model is usually cheap for a reason.
    rejected = []
    unverified = set()
    if args.verify:
        client = VintedClient()
        client.handshake()          # listing pages 403 without the cookie set
        checked = 0
        for row in list(rows[:args.verify]):
            _, spec, it, _ = row
            url = it.get("url")
            if not url:
                continue
            if checked:
                time.sleep(PROBE_SPACING_S)
            checked += 1
            try:
                resp = client.session.get(url, timeout=30)
                if resp.status_code in (401, 403) and client.rotate():
                    # Listing pages rate-limit per exit IP, and the whole point
                    # of the rotating gateway is that another one is available.
                    # Without this every verification in a run fails together
                    # and the board presents its entire top as unverified.
                    client.handshake()
                    resp = client.session.get(url, timeout=30)
                if resp.status_code != 200:
                    print(f"  ? {url} -> HTTP {resp.status_code}")
                    unverified.add(id(row))
                    continue
                desc = extract_description(resp.text)
            except Exception as exc:                     # noqa: BLE001
                print(f"  ? could not inspect {url}: {str(exc)[:60]}")
                unverified.add(id(row))
                continue
            full = parse(it.get("title", ""), desc)
            if full.disqualifiers:
                rows.remove(row)
                rejected.append((row, full.disqualifiers, desc))

    print(f"{len(items)} live  |  {disqualified} disqualified  |  "
          f"{unpriceable} no priceable exit  |  {len(rows)} on the board\n")

    if rejected:
        print(f"{len(rejected)} of the top {args.verify} rejected on "
              "inspection - the title said nothing:")
        print()
        for (sp, spec, it, _), why, desc in rejected:
            print(f"  £{sp.buy_pence/100:>7.0f}  {spec.model:<18} "
                  f"{','.join(why)}")
            print(f"           \"{desc[:76]}\"")
        print()

    covered = [r for r in rows if r[0].downside_covered]
    print(f"{len(covered)} of {len(rows)} clear the guaranteed floor "
          "(the only ones that cannot lose)\n")

    hdr = (f"{'buy':>8} {'exit':>8} {'net':>8} {'margin':>8} {'floor':>8}"
           "  safe  handset")
    print(hdr)
    print("-" * len(hdr))
    for row in rows[:args.limit]:
        s, spec, it, exact = row
        fm = s.floor_margin_pence
        mark = "" if exact or s.best.venue != "ebay" else " ~"
        if id(row) in unverified:
            mark += " ?"
        print(f"£{s.buy_pence/100:>7.0f} {s.best.venue:>8} "
              f"£{s.best.net_pence/100:>7.0f} "
              f"£{s.margin_pence/100:>7.0f} "
              f"{('£%.0f' % (fm/100)) if fm is not None else '   -':>8}  "
              f"{'YES ' if s.downside_covered else 'no  '}  "
              f"{spec.model} {spec.storage_gb or '?'}GB{mark}")
    if unverified:
        print()
        print("  ? description could not be read, so this row is UNVERIFIED -")
        print("    every trap found so far was invisible in the title.")
    if any(not e and s.best.venue == "ebay" for s, _, _, e in rows[:args.limit]):
        print()
        print("  ~ eBay ceiling is capacity-mixed. Measured against "
              "capacity-specific queries it")
        print("    ran from -1% to +70% off, so it is not a bound in "
              "either direction -")
        print("    it is a placeholder until that capacity is fetched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
