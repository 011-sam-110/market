"""Identify the listings no price-based scanner can read.

The premise, measured rather than assumed: of 96 live Vinted phone listings,
59 had no model or no storage in the title. Every competing tool prices by
matching a title, so it must skip those -- and a seller who cannot describe
what they have prices it by guesswork.

This runs the residue through a vision model and reports what it recovers,
which is the number that decides whether the idea is worth building on.

    python scripts/identify_unreadable.py               # live feed
    python scripts/identify_unreadable.py --limit 10
    python scripts/identify_unreadable.py --dry-run     # cost nothing, just count
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phoneloop import config, llm  # noqa: E402
from phoneloop.identify import Cache, VisionIdentifier, needs_vision  # noqa: E402
from phoneloop.parser import parse  # noqa: E402
from phoneloop.vinted_client import VintedClient  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be sent, spend nothing")
    args = ap.parse_args()

    cfg = config.load()
    conf = llm.settings()

    items = VintedClient().search(
        catalog_ids=cfg.CATALOG_ID, per_page=96, price_from=cfg.PRICE_FROM
    )
    scored = [(it, parse(it.get("title", ""))) for it in items]
    residue = [(it, s) for it, s in scored if needs_vision(s)]

    no_model = sum(1 for _, s in residue if s.model is None)
    print(f"{len(items)} live listings")
    print(f"{len(residue)} need vision  "
          f"({no_model} with no model at all, "
          f"{len(residue) - no_model} missing only storage)")
    value = sum(
        float((it.get("total_item_price") or {}).get("amount") or 0)
        for it, _ in residue
    )
    print(f"£{value:,.0f} of listed value that no title-matching tool can price\n")

    if args.dry_run:
        for it, s in residue[:args.limit]:
            price = (it.get("total_item_price") or {}).get("amount")
            print(f"  £{price:>7}  {it.get('title','')[:44]:<46} "
                  f"model={s.model or '-'} storage={s.storage_gb or '-'}")
        return 0

    ok, detail = llm.available(conf["base_url"], conf["key"])
    print(f"vision endpoint: {'up' if ok else 'UNAVAILABLE'} ({detail})")
    if not ok:
        print("nothing was identified because the endpoint could not answer -")
        print("which is not the same as finding nothing. Try again when it is up.")
        return 1

    cache = Cache(cfg.DATA_DIR / "identifications.json")
    idf = VisionIdentifier(
        llm.make_chat(conf["base_url"], conf["key"]), conf["model"], cache=cache
    )
    print(f"model: {conf['model']}, cache holds {len(cache)}\n")

    recovered = 0
    for it, spec in residue[:args.limit]:
        price = (it.get("total_item_price") or {}).get("amount")
        ident = idf.identify(it)
        title = it.get("title", "")[:38]
        if ident is None:
            print(f"  £{price:>7}  {title:<40} -> unidentified")
            continue
        recovered += 1
        gained = []
        if spec.model is None and ident.model:
            gained.append("model")
        if spec.storage_gb is None and ident.storage_gb:
            gained.append("storage")
        print(f"  £{price:>7}  {title:<40} -> {ident.model} "
              f"{ident.storage_gb or '?'}GB  ({ident.confidence:.2f}) +{'+'.join(gained) or 'nothing new'}")
        print(f"           {ident.evidence[:96]}")

    cache.save()
    asked = min(len(residue), args.limit)
    print(f"\nrecovered {recovered}/{asked} "
          f"({recovered / asked * 100:.0f}%)" if asked else "\nnothing to ask")
    print(f"{idf.calls} model calls, {idf.cache_hits} cache hits")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
