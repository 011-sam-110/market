"""Enter and list supplier quotes by hand (docs/dropship-process.md, stage 2).

Quotes go to ~/.phoneloop/suppliers/quotes.jsonl, outside the repo
(override: PHONELOOP_SUPPLIERS_DIR). Money is pounds and pence on the
command line and integer pence on disk.

    python scripts/supplier_quote.py add \\
        --product-key "cluster:9355:12" --leaf 9355 \\
        --label "3x3M Gazebo Top Replacement Canopy" \\
        --supplier "Example Wholesale Ltd" --kind wholesale \\
        --unit-cost 6.40 --postage 2.10 --ship-from GB \\
        --latest-delivery-days 4 --returns-terms "30 days, buyer pays return" \\
        --quote-date 2026-10-05 --source-url https://example.com/quote/123

    python scripts/supplier_quote.py list

A quote from a retail marketplace that ships to your buyer (kind
marketplace_retail) is refused: eBay does not allow it. The verdict for each
quote (clears, fails or unknown, after the returns allowance) prints on the
board: python scripts/niche_board.py --with-quotes
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phoneloop import supplier  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _add(args: argparse.Namespace, book: supplier.QuoteBook) -> int:
    try:
        quote = supplier.Quote(
            product_key=args.product_key, supplier=args.supplier,
            supplier_kind=args.kind,
            unit_cost_pence=supplier.parse_pence(args.unit_cost),
            postage_pence=supplier.parse_pence(args.postage),
            ship_from=args.ship_from.upper(),
            latest_delivery_days=args.latest_delivery_days,
            returns_terms=args.returns_terms,
            quote_date=date.fromisoformat(args.quote_date),
            source_url=args.source_url,
            leaf_category_id=args.leaf, label=args.label, notes=args.notes or "",
        )
    except supplier.QuoteRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"not stored: {exc}", file=sys.stderr)
        return 2
    stored = book.add(quote)
    print(f"stored {stored.quote_id}: {stored.product_key} from {stored.supplier}, "
          f"landed {supplier.fmt_pence(stored.landed_pence)} -> {book.path}")
    if stored.supplier_kind == "dropship_agent":
        print("note: eBay's policy does not define 'wholesale supplier'. Keep a "
              "written agreement that names you as the seller.")
    if stored.product_key.startswith("cluster:") and not stored.label:
        print("note: a cluster key can name a different product on another "
              "night. Add --label next time, so the board can flag a change.")
    return 0


def _list(book: supplier.QuoteBook) -> int:
    quotes = book.list()
    allowance, source = supplier.load_allowance(book.dir)
    if not quotes:
        print(f"no quotes in {book.path}")
        return 0
    for q in quotes:
        print(f"{q.quote_id}  {q.quote_date}  {q.product_key}  {q.supplier} "
              f"({q.supplier_kind}, from {q.ship_from})")
        print(f"      unit {supplier.fmt_pence(q.unit_cost_pence)} + postage "
              f"{supplier.fmt_pence(q.postage_pence)} = landed "
              f"{supplier.fmt_pence(q.landed_pence)}; returns allowance "
              f"{supplier.fmt_pence(supplier.allowance_pence(q, allowance))}; "
              f"latest delivery {q.latest_delivery_days} days")
        print(f"      returns: {q.returns_terms}")
        print(f"      source: {q.source_url}")
    print(f"{len(quotes)} quotes in {book.path}; allowance: {source}")
    print("verdicts: python scripts/niche_board.py --with-quotes")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add", help="store one quote")
    add.add_argument("--product-key", required=True,
                     help="the board's product key, e.g. cluster:9355:12 or epid:123")
    add.add_argument("--leaf", help="the product's leaf_category_id (speeds up the board join)")
    add.add_argument("--label", help="the board's product label today")
    add.add_argument("--supplier", required=True, help="the supplier's name")
    add.add_argument("--kind", required=True, choices=supplier.SUPPLIER_KINDS,
                     help="marketplace_retail is refused for eBay")
    add.add_argument("--unit-cost", required=True, help="per unit, e.g. 6.40")
    add.add_argument("--postage", required=True,
                     help="supplier's charge to post one unit to your buyer, e.g. 2.10")
    add.add_argument("--ship-from", required=True, help="two-letter country code, e.g. GB")
    add.add_argument("--latest-delivery-days", required=True, type=int,
                     help="latest day after the order that the supplier says it arrives")
    add.add_argument("--returns-terms", required=True, help="the supplier's returns terms")
    add.add_argument("--quote-date", default=date.today().isoformat(),
                     help="YYYY-MM-DD, default today")
    add.add_argument("--source-url", required=True, help="where the quote came from")
    add.add_argument("--notes", help="anything else")

    sub.add_parser("list", help="list stored quotes")

    args = ap.parse_args(argv)
    try:
        book = supplier.QuoteBook(supplier.suppliers_dir())
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.cmd == "add":
        return _add(args, book)
    return _list(book)


if __name__ == "__main__":
    raise SystemExit(main())
