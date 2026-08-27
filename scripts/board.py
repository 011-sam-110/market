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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phoneloop import config, spread  # noqa: E402
from phoneloop.parser import parse  # noqa: E402
from phoneloop.vinted_client import VintedClient  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def load(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def cex_cash(table: dict, model: str, storage_gb: int | None,
             grade: str) -> int | None:
    """Prefer a capacity-specific box; fall back to the model-wide figure.

    A model-wide CeX price is the cheapest capacity CeX stocks, so using it for
    a 512GB handset understates the exit. That is the safe direction to be
    wrong in, but it is still wrong, and the caller is told which it got.
    """
    for entry in table.values():
        if entry.get("model") == model and entry.get("storage_gb") == storage_gb:
            return (entry.get("cash_pence") or {}).get(grade)
    for entry in table.values():
        if entry.get("model") == model and entry.get("storage_gb") is None:
            return (entry.get("cash_pence") or {}).get(grade)
    return None


def ebay_ceiling(table: dict, model: str, storage_gb: int | None) -> int | None:
    for entry in table.values():
        if entry.get("model") == model and entry.get("storage_gb") == storage_gb:
            return entry.get("ceiling_pence")
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--grade", default="B", choices=["A", "B", "C"],
                    help="CeX grade to price the exit at (B is the safe read)")
    ap.add_argument("--private", action="store_true",
                    help="assume a fee-free private eBay account - VERIFY FIRST")
    ap.add_argument("--limit", type=int, default=25)
    args = ap.parse_args()

    cfg = config.load()
    cex_table = load(cfg.DATA_DIR / "cex_prices.json")
    ebay_table = load(cfg.DATA_DIR / "ebay_ceilings.json")
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

        s = spread.evaluate(
            buy_pence,
            cex_cash(cex_table, spec.model, spec.storage_gb, args.grade),
            ebay_ceiling(ebay_table, spec.model, spec.storage_gb),
            fees=fees,
        )
        if s is None:
            unpriceable += 1
            continue
        rows.append((s, spec, it))

    rows.sort(key=lambda r: r[0].margin_pence, reverse=True)

    print(f"{len(items)} live  |  {disqualified} disqualified  |  "
          f"{unpriceable} no priceable exit  |  {len(rows)} on the board\n")

    covered = [r for r in rows if r[0].downside_covered]
    print(f"{len(covered)} of {len(rows)} clear the guaranteed floor "
          "(the only ones that cannot lose)\n")

    hdr = f"{'buy':>8} {'exit':>8} {'net':>8} {'margin':>8} {'floor':>8}  safe  handset"
    print(hdr)
    print("-" * len(hdr))
    for s, spec, it in rows[:args.limit]:
        fm = s.floor_margin_pence
        print(f"£{s.buy_pence/100:>7.0f} {s.best.venue:>8} "
              f"£{s.best.net_pence/100:>7.0f} "
              f"£{s.margin_pence/100:>7.0f} "
              f"{('£%.0f' % (fm/100)) if fm is not None else '   -':>8}  "
              f"{'YES ' if s.downside_covered else 'no  '}  "
              f"{spec.model} {spec.storage_gb or '?'}GB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
