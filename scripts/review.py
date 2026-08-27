"""Run the whole pipeline against recorded fixtures and report what it found.

This is the checking half of the harness. It touches nothing live, so two runs
over the same cassette produce byte-identical output -- which means a diff in
the report is a diff caused by a code change, not by the feed moving underneath
it. That was the missing piece when the eBay iPhone 12 median went from 212 to
152: there was no way to attribute the shift.

    python scripts/review.py                 # the report
    python scripts/review.py --diff last     # against the previous run
    python scripts/review.py --html out.html # something to look at

Every listing is verified here, because the fixtures already hold the listing
pages -- offline there is no rate limit to ration, so the excuse for ranking on
titles disappears entirely.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phoneloop import config, harness, pricing, spread  # noqa: E402
from phoneloop.harness import Cassette, HarnessSession, MissingRecording  # noqa: E402
from phoneloop.parser import parse  # noqa: E402
from phoneloop.verifier import extract_description  # noqa: E402
from phoneloop.vinted_client import VintedClient  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

LAST_RUN = Path(__file__).resolve().parent.parent / "fixtures" / "last-review.txt"


def build_report(cassette: Cassette, cfg, grade: str, fees) -> list[str]:
    """The report as lines. Deterministic: no clock, no ordering by chance."""
    out: list[str] = []
    cex_table = pricing.load_table(cfg.DATA_DIR / "cex_prices.json")
    ebay_table = pricing.load_table(cfg.DATA_DIR / "ebay_ceilings.json")

    session = HarnessSession(cassette, harness_mode=harness.REPLAY)
    client = VintedClient(session=session)
    client.handshake()
    items = client.search(catalog_ids=cfg.CATALOG_ID, per_page=96,
                          price_from=cfg.PRICE_FROM)

    out.append(f"feed              {len(items)} listings (recorded)")
    out.append(f"cex table         {len(pricing.coverage(cex_table))} models")
    out.append(f"ebay table        {len(pricing.coverage(ebay_table))} models")
    out.append(f"assumptions       grade {grade}, fees {fees.name}, "
               f"undercut {spread.UNDERCUT:.0%}, post {spread.SHIP_PENCE}p")
    out.append("")

    rows, binned, unpriceable, rejected, unread = [], [], [], [], []

    for item in sorted(items, key=lambda i: str(i.get("id"))):
        title = item.get("title", "")
        spec = parse(title)
        if spec.disqualifiers:
            binned.append((title, spec.disqualifiers))
            continue

        buy = (item.get("total_item_price") or {}).get("amount")
        if buy is None or not spec.model:
            unpriceable.append(title)
            continue
        buy_pence = round(float(buy) * 100)

        # Verify from the recorded page. No rate limit offline, so every
        # listing gets inspected rather than only the top of the board.
        url = item.get("url") or ""
        try:
            page = session.get(url, timeout=30)
            desc = extract_description(page.text) if page.status_code == 200 else None
        except MissingRecording:
            desc = None
        if desc is None:
            unread.append(title)
        else:
            full = parse(title, desc)
            if full.disqualifiers:
                rejected.append((buy_pence, spec.model, full.disqualifiers, desc))
                continue

        cash, _ = pricing.cex_cash(cex_table, spec.model, spec.storage_gb, grade)
        ceiling, exact = pricing.ebay_ceiling(ebay_table, spec.model, spec.storage_gb)
        result = spread.evaluate(buy_pence, cash, ceiling, fees=fees)
        if result is None:
            unpriceable.append(title)
            continue
        rows.append((result, spec, exact, desc is not None))

    rows.sort(key=lambda r: (-r[0].margin_pence, r[1].model or ""))

    out.append(f"disqualified on title      {len(binned)}")
    out.append(f"rejected on description    {len(rejected)}")
    out.append(f"no priceable exit          {len(unpriceable)}")
    out.append(f"description not recorded   {len(unread)}")
    out.append(f"on the board               {len(rows)}")
    covered = [r for r in rows if r[0].downside_covered]
    out.append(f"clear the guaranteed floor {len(covered)}")
    out.append("")

    if rejected:
        out.append("REJECTED ON INSPECTION (the title said nothing)")
        for buy_pence, model, why, desc in sorted(rejected):
            out.append(f"  £{buy_pence/100:>7.0f}  {model:<20} {','.join(why)}")
            out.append(f"           {desc[:70]!r}")
        out.append("")

    out.append("BOARD")
    out.append(f"  {'buy':>8} {'exit':>6} {'net':>8} {'margin':>8} "
               f"{'floor':>8}  safe  seen  handset")
    for result, spec, exact, verified in rows[:20]:
        floor = result.floor_margin_pence
        out.append(
            f"  £{result.buy_pence/100:>7.0f} {result.best.venue:>6} "
            f"£{result.best.net_pence/100:>7.0f} £{result.margin_pence/100:>7.0f} "
            f"{('£%.0f' % (floor/100)) if floor is not None else '-':>8}  "
            f"{'YES ' if result.downside_covered else 'no  '}  "
            f"{'ok  ' if verified else '??  '}  "
            f"{spec.model} {spec.storage_gb or '?'}GB"
            f"{'' if exact or result.best.venue != 'ebay' else ' ~'}"
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--grade", default="B", choices=["A", "B", "C"])
    ap.add_argument("--private", action="store_true",
                    help="assume a fee-free eBay account - VERIFY FIRST")
    ap.add_argument("--root", type=Path, default=harness.DEFAULT_ROOT)
    ap.add_argument("--diff", choices=["last"], help="compare with the previous run")
    ap.add_argument("--html", type=Path, help="also write a page to look at")
    args = ap.parse_args()

    cassette = Cassette(args.root)
    if len(cassette) == 0:
        print(f"no fixtures in {cassette.root}")
        print("capture some first:  PHONELOOP_HARNESS=record python scripts/capture.py")
        return 1

    fees = spread.PRIVATE if args.private else spread.BUSINESS
    try:
        lines = build_report(cassette, config.load(), args.grade, fees)
    except MissingRecording as exc:
        print("the cassette is missing something this run needs:\n")
        print(f"  {exc}")
        return 1

    report = "\n".join(lines)
    print(report)

    if args.diff == "last" and LAST_RUN.exists():
        previous = LAST_RUN.read_text(encoding="utf-8").split("\n")
        delta = list(difflib.unified_diff(previous, lines, "previous", "this run",
                                          lineterm="", n=1))
        print("\n" + ("-" * 60))
        print("\n".join(delta) if delta else "identical to the previous run")

    LAST_RUN.parent.mkdir(parents=True, exist_ok=True)
    LAST_RUN.write_text(report, encoding="utf-8")

    if args.html:
        args.html.write_text(_html(lines), encoding="utf-8")
        print(f"\nwrote {args.html}")
    return 0


def _html(lines: list[str]) -> str:
    body = "\n".join(
        line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        for line in lines
    )
    return (
        "<!doctype html><meta charset='utf-8'>"
        "<title>PhoneLoop review</title>"
        "<style>"
        ":root{color-scheme:light dark}"
        "body{margin:0;padding:2rem;font:13px/1.55 ui-monospace,'IBM Plex Mono',monospace;"
        "background:Canvas;color:CanvasText}"
        "pre{margin:0;white-space:pre;overflow-x:auto}"
        "</style>"
        f"<pre>{body}</pre>"
    )


if __name__ == "__main__":
    raise SystemExit(main())
