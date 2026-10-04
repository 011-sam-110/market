"""Log a sample order or a test listing (docs/dropship-process.md, stages 4
and 5). Every call adds a line to ~/.phoneloop/trials/trials.jsonl (override:
PHONELOOP_TRIALS_DIR). Call again with the same id to update a record: the
newest value of each field wins, and the history stays.

    # stage 4: a sample order, then its arrival
    python scripts/trial.py sample --id s1 --quote q1 --ordered 2026-10-06 \\
        --promised-days 7 --cost 8.50
    python scripts/trial.py sample --id s1 --arrived 2026-10-11 \\
        --condition as_described --slip mine

    # stage 5: a test listing, then each change to it
    python scripts/trial.py listing --id l1 --quote q1 --listed 2026-10-14 --price 24.99
    python scripts/trial.py listing --id l1 --as-of 2026-10-21 --units-sold 2 \\
        --first-sale 2026-10-16 --returns 0 --cases 0 --net 7.40

    python scripts/trial.py list

The first `listing` call with --quote freezes the board's predictions for
that quote, from the last `scripts/niche_board.py --with-quotes` run. Run
that first. `scripts/trial_report.py` prints predicted against realised.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phoneloop import supplier, trials  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _date(text: str | None) -> date | None:
    return date.fromisoformat(text) if text else None


def _pence(text: str | None) -> int | None:
    return supplier.parse_pence(text) if text is not None else None


def _sample(args, log: trials.TrialLog, quotes: supplier.QuoteBook) -> int:
    product_key = None
    if args.quote:
        q = quotes.get(args.quote)
        if q is None:
            print(f"not stored: no quote {args.quote!r} in {quotes.path}", file=sys.stderr)
            return 2
        product_key = q.product_key
    rec = log.record_sample(
        args.id, quote_id=args.quote, product_key=product_key,
        ordered_on=_date(args.ordered), arrived_on=_date(args.arrived),
        promised_days=args.promised_days, condition=args.condition,
        name_on_slip=args.slip, cost_pence=_pence(args.cost))
    ok, reasons = rec.gate()
    state = {True: "PASS", False: "FAIL", None: "?"}[ok]
    days = rec.delivery_days if rec.delivery_days is not None else "?"
    print(f"logged sample {rec.sample_id}: delivery {days} days, gate [{state}]"
          + ("" if not reasons else " -- " + "; ".join(reasons)))
    return 0


def _listing(args, log: trials.TrialLog, quotes: supplier.QuoteBook) -> int:
    product_key = None
    predicted = None
    if args.quote:
        q = quotes.get(args.quote)
        if q is None:
            print(f"not stored: no quote {args.quote!r} in {quotes.path}", file=sys.stderr)
            return 2
        product_key = q.product_key
        if args.id not in log.listings():
            allowance, _ = supplier.load_allowance(quotes.dir)
            predicted = trials.predictions_for_quote(quotes.dir, args.quote, allowance)
            if not predicted:
                print("note: no board join for this quote, so no predictions "
                      "are frozen. Run scripts/niche_board.py --with-quotes first.")
    rec = log.record_listing(
        args.id, predicted=predicted, quote_id=args.quote, product_key=product_key,
        listed_on=_date(args.listed), price_pence=_pence(args.price),
        units_sold=args.units_sold, first_sale_on=_date(args.first_sale),
        returns=args.returns, cases=args.cases, realised_net_pence=_pence(args.net),
        as_of=_date(args.as_of) or (date.today() if args.units_sold is not None else None),
        ended_on=_date(args.ended))
    print(f"logged listing {rec.listing_id}: price {supplier.fmt_pence(rec.price_pence)}, "
          f"units sold {rec.units_sold if rec.units_sold is not None else '?'}, "
          f"realised net {supplier.fmt_pence(rec.realised_net_pence)}, "
          f"predictions {'frozen' if rec.predicted else 'none'}")
    return 0


def _list(log: trials.TrialLog) -> int:
    samples, listings = log.samples(), log.listings()
    for s in samples.values():
        print(f"sample  {s.sample_id}  quote {s.quote_id}  ordered {s.ordered_on}  "
              f"arrived {s.arrived_on}  condition {s.condition}  slip {s.name_on_slip}  "
              f"cost {supplier.fmt_pence(s.cost_pence)}")
    for lst in listings.values():
        print(f"listing {lst.listing_id}  quote {lst.quote_id}  listed {lst.listed_on}  "
              f"price {supplier.fmt_pence(lst.price_pence)}  sold {lst.units_sold}  "
              f"returns {lst.returns}  cases {lst.cases}  "
              f"net {supplier.fmt_pence(lst.realised_net_pence)}  as of {lst.as_of}")
    print(f"{len(samples)} samples, {len(listings)} listings in {log.path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sample", help="add or update a sample order")
    s.add_argument("--id", required=True)
    s.add_argument("--quote", help="the quote id the sample was ordered from")
    s.add_argument("--ordered", help="YYYY-MM-DD")
    s.add_argument("--arrived", help="YYYY-MM-DD")
    s.add_argument("--promised-days", type=int,
                   help="the delivery time you would promise a buyer")
    s.add_argument("--condition", choices=trials.CONDITIONS)
    s.add_argument("--slip", choices=trials.SLIP_NAMES,
                   help="whose name was on the slip or invoice in the parcel")
    s.add_argument("--cost", help="what you paid, e.g. 8.50")

    li = sub.add_parser("listing", help="add or update a test listing")
    li.add_argument("--id", required=True)
    li.add_argument("--quote", help="the quote the stock comes from")
    li.add_argument("--listed", help="YYYY-MM-DD")
    li.add_argument("--price", help="listed price incl. postage the buyer pays, e.g. 24.99")
    li.add_argument("--units-sold", type=int)
    li.add_argument("--first-sale", help="YYYY-MM-DD")
    li.add_argument("--returns", type=int)
    li.add_argument("--cases", type=int, help="eBay cases opened by buyers")
    li.add_argument("--net", help="realised net to date: all money in minus all money out")
    li.add_argument("--as-of", help="YYYY-MM-DD the counts are true on (default today)")
    li.add_argument("--ended", help="YYYY-MM-DD the listing ended")

    sub.add_parser("list", help="list logged samples and listings")

    args = ap.parse_args(argv)
    try:
        log = trials.TrialLog(trials.trials_dir())
        quotes = supplier.QuoteBook(supplier.suppliers_dir())
        if args.cmd == "sample":
            return _sample(args, log, quotes)
        if args.cmd == "listing":
            return _listing(args, log, quotes)
        return _list(log)
    except ValueError as exc:
        print(f"not stored: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
