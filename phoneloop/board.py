"""Rank eBay niches, then products within them, at the best identity tier
each supports (design spec section 4 BOARD stage; contracts section 2.13).

This module composes three other streams' work and reimplements none of it:
`market_store` for the rows, `metrics` for every figure and its denominator,
`fees` for what a sale nets after eBay's cut, and `identity` for which
listings are the same product. If a number needs recomputing here rather
than being read straight off one of those four modules, that is a bug in
this file, not a shortcut.

**Niche = leaf_category_id.** ROW_FIELDS documents `leaf_category_id` as
"the niche key, and the fee lookup key" (contracts section 1.1, column 34) --
that is not a convenience reading, it is the definition this module uses. A
composite leaf+price-band key was considered (spec section 5 literally says
"niche (leaf x price band)"), but the price band is a sweep-time sharding
decision recorded only in the free-text `shard` column, whose exact string
format belongs to the transport streams (s2/s3) and is not specified here.
Grouping on the well-typed, guaranteed `leaf_category_id` column is the
reading that needs no knowledge of another stream's string format. Recorded
in `unverified[]`.

**score** is `passed - failed` across a row's `KnobResult` list (an unknown
knob, `passed is None`, counts as neither), ties broken by
`str_window_bp or 0` then `n_listings`. Both tie-break values are read
straight off `Metrics` at sort time rather than duplicated onto `BoardRow`,
so there is exactly one place either can drift out of sync with the row it
describes.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from phoneloop import fees, identity
from phoneloop import metrics as metrics_mod
from phoneloop.knobs import Knobs
from phoneloop.market_store import MarketStore
from phoneloop.metrics import KnobResult, Metrics
from phoneloop.snapshots import Difference

_REPORT_DIR_NAME = "reports"


@dataclass(frozen=True)
class BoardRow:
    key: str                              # niche key or product key
    label: str                            # human-readable
    level: str                            # "niche" | "product"
    identity_tier: str
    metrics: Metrics
    knobs: list[KnobResult]
    max_cost_at_floor_pence: int | None
    max_cost_at_median_pence: int | None
    str_source: str                       # "window" | "lifetime" - printed on the row
    score: float


# --- internals --------------------------------------------------------------

def _latest_rows(store: MarketStore, *, source: str = "ebay") -> list[dict]:
    """The most recent snapshot's rows -- the board ranks the CURRENT
    population, not every historical observation the snapshot log holds
    (which would count a listing once per day it was ever swept, since
    market_store's ALWAYS_SNAPSHOT_SOURCES writes every eBay observation)."""
    dates = store.snapshot_dates(source=source)
    if not dates:
        return []
    return store.rows_on(dates[-1], source=source)


def _deltas_for_metrics(difference: Difference | None):
    """Difference.listings reshaped to what metrics.compute's `deltas`
    parameter expects. Unknown listings (delta_sold=None) are passed
    through rather than filtered out here -- compute() already skips a row
    whose delta is None (metrics.py, "demand, windowed"), so filtering
    again in this module would just duplicate that check in a second place
    that could drift from the first.
    """
    if difference is None:
        return None
    return {sid: (ld.delta_sold, ld.days) for sid, ld in difference.listings.items()}


def _score(knob_results: Sequence[KnobResult]) -> float:
    """passed - failed. An unknown knob (`passed is None`) counts as
    neither -- see the module docstring."""
    passed = sum(1 for k in knob_results if k.passed is True)
    failed = sum(1 for k in knob_results if k.passed is False)
    return float(passed - failed)


def _sort_key(row: BoardRow) -> tuple[float, int, int]:
    return (row.score, row.metrics.str_window_bp or 0, row.metrics.n_listings)


def _max_costs(price_floor_pence: int | None, price_median_pence: int | None,
               leaf_category_id: str | None, knobs: Knobs) -> tuple[int | None, int | None]:
    floor = (fees.max_cost(price_floor_pence, leaf_category_id, knobs)
             if price_floor_pence is not None else None)
    median = (fees.max_cost(price_median_pence, leaf_category_id, knobs)
              if price_median_pence is not None else None)
    return floor, median


def _build_row(*, key: str, label: str, level: str, identity_tier: str,
               rows: list[dict], knobs: Knobs, deltas, leaf_category_id: str | None,
               competitors: int) -> BoardRow:
    m = metrics_mod.compute(rows, deltas=deltas)
    max_floor, max_median = _max_costs(
        m.price_floor_pence, m.price_median_pence, leaf_category_id, knobs)
    knob_results = metrics_mod.check(
        m, knobs, competitors=competitors,
        max_cost_at_floor_pence=max_floor, max_cost_at_median_pence=max_median,
    )
    return BoardRow(
        key=key, label=label, level=level, identity_tier=identity_tier,
        metrics=m, knobs=knob_results,
        max_cost_at_floor_pence=max_floor, max_cost_at_median_pence=max_median,
        str_source="window" if m.str_window_bp is not None else "lifetime",
        score=_score(knob_results),
    )


# --- ranking -----------------------------------------------------------------

def rank_niches(store: MarketStore, knobs: Knobs, *,
                difference: Difference | None = None,
                limit: int = 100) -> list[BoardRow]:
    """Rank niches (leaf_category_id) by knobs passed, then demand, then size.

    Competitors at niche level is `n_sellers` -- distinct sellers across the
    whole niche's listings. That is coarser than knob 6's "distinct sellers
    per product" (the per-product count `rank_products` computes below), and
    is a niche-level reading, not a substitute for it.
    """
    rows = _latest_rows(store)
    deltas = _deltas_for_metrics(difference)

    groups: dict[str, list[dict]] = {}
    for r in rows:
        leaf = r.get("leaf_category_id")
        if not leaf:
            continue
        groups.setdefault(leaf, []).append(r)

    board_rows = []
    for leaf, niche_rows in groups.items():
        seller_ids = {r.get("seller_id") for r in niche_rows if r.get("seller_id")}
        board_rows.append(_build_row(
            key=leaf, label=f"leaf:{leaf}", level="niche", identity_tier="none",
            rows=niche_rows, knobs=knobs, deltas=deltas, leaf_category_id=leaf,
            competitors=len(seller_ids),
        ))

    board_rows.sort(key=_sort_key, reverse=True)
    return board_rows[:limit]


def rank_products(store: MarketStore, knobs: Knobs,
                  niches: Sequence[str], *,
                  difference: Difference | None = None,
                  limit: int = 100) -> list[BoardRow]:
    """Rank products within the given niches, at the best identity tier each
    supports (spec section 5). Competitors here is the exact knob-6 reading:
    distinct sellers on the listings grouped into ONE product, via
    `identity.tier_for` / `identity.product_key` / `identity.cluster`.
    """
    rows = _latest_rows(store)
    deltas = _deltas_for_metrics(difference)
    wanted = set(niches)

    by_leaf: dict[str, list[dict]] = {}
    for r in rows:
        leaf = r.get("leaf_category_id")
        if leaf in wanted:
            by_leaf.setdefault(leaf, []).append(r)

    board_rows = []
    for leaf, leaf_rows in by_leaf.items():
        tiers = [identity.tier_for(r) for r in leaf_rows]

        cluster_rows = [r for r, t in zip(leaf_rows, tiers) if t == "cluster"]
        cluster_ids = identity.cluster(cluster_rows) if cluster_rows else []
        cluster_id_by_sid = {r["source_id"]: cid for r, cid in zip(cluster_rows, cluster_ids)}

        groups: dict[str, list[dict]] = {}
        tier_of_key: dict[str, str] = {}
        for r, t in zip(leaf_rows, tiers):
            if t == "none":
                continue
            key = (identity.product_key(r, t, cluster_id=cluster_id_by_sid[r["source_id"]])
                  if t == "cluster" else identity.product_key(r, t))
            if key is None:
                continue
            groups.setdefault(key, []).append(r)
            tier_of_key[key] = t

        for key, prod_rows in groups.items():
            seller_ids = {r.get("seller_id") for r in prod_rows if r.get("seller_id")}
            label = next((r.get("title") for r in prod_rows if r.get("title")), key)
            board_rows.append(_build_row(
                key=key, label=label, level="product", identity_tier=tier_of_key[key],
                rows=prod_rows, knobs=knobs, deltas=deltas, leaf_category_id=leaf,
                competitors=len(seller_ids),
            ))

    board_rows.sort(key=_sort_key, reverse=True)
    return board_rows[:limit]


# --- rendering -----------------------------------------------------------------

def _fmt(value: object) -> str:
    if value is None:
        return "?"
    if isinstance(value, float):
        return f"{value:.3g}"
    return str(value)


def _fmt_pence(pence: int | None) -> str:
    if pence is None:
        return "?"
    sign = "-" if pence < 0 else ""
    p = abs(pence)
    return f"{sign}£{p // 100}.{p % 100:02d}"


def _fmt_pass(passed: bool | None) -> str:
    if passed is None:
        return "?"
    return "PASS" if passed else "FAIL"


def render_table(rows: Sequence[BoardRow]) -> str:
    """The terminal board: one line per row, every knob value with its
    pass/fail and denominator coverage, plus identity tier, str_source (with
    a visible note when it is lifetime, not window) and both max_cost bases.
    """
    if not rows:
        return "(no rows)"

    lines: list[str] = []
    header = (f"{'LEVEL':<8}{'KEY':<26}{'TIER':<11}{'STR_SRC':<9}{'SCORE':>6}  "
              f"{'MAXCOST@FLOOR':>14}{'MAXCOST@MED':>14}")
    lines.append(header)
    lines.append("-" * len(header))
    for r in rows:
        note = (" (lifetime - not comparable to the sell-through threshold)"
               if r.str_source == "lifetime" else "")
        line = (f"{r.level:<8}{r.key[:25]:<26}{r.identity_tier:<11}"
                f"{r.str_source:<9}{r.score:>6.1f}  "
                f"{_fmt_pence(r.max_cost_at_floor_pence):>14}"
                f"{_fmt_pence(r.max_cost_at_median_pence):>14}{note}")
        lines.append(line)
        lines.append(f"        {r.label}")
        for k in r.knobs:
            lines.append(
                f"          {k.knob:<14} value={_fmt(k.value):<16} "
                f"threshold={_fmt(k.threshold):<18} "
                f"[{_fmt_pass(k.passed)}] denom={k.denominator} cov={k.coverage:.0%}"
                + (f" -- {k.note}" if k.note else "")
            )
    return "\n".join(lines)


_STYLE = """
body{font-family:-apple-system,Segoe UI,Arial,sans-serif;margin:2rem;background:#0b0e13;color:#e6e6e6}
h1{font-size:1.3rem;margin-bottom:0.2rem}
.meta{color:#9aa4b2;margin-bottom:1rem}
table{border-collapse:collapse;width:100%;font-size:0.82rem}
th,td{border:1px solid #2a2f3a;padding:4px 8px;text-align:left;vertical-align:top}
th{background:#161b22;position:sticky;top:0}
tr:nth-child(even){background:#11151c}
.pass{color:#4caf50}
.fail{color:#e05555}
.unknown{color:#9aa4b2}
.lifetime-note{color:#e0a955}
"""


def render_html(rows: Sequence[BoardRow], *, title: str,
                generated_at: datetime) -> str:
    """A self-contained HTML board -- no CDN, no external font, no fetch.
    Everything is one inline `<style>` block and literal table markup; the
    guard is that no `http://` or `https://` substring appears anywhere in
    the output (contracts section 5, s5_snapshots_board tests)."""
    knob_names = list(dict.fromkeys(k.knob for r in rows for k in r.knobs))
    knob_headers = "".join(f"<th>{html.escape(name)}</th>" for name in knob_names)

    rows_html: list[str] = []
    for r in rows:
        knob_by_name = {k.knob: k for k in r.knobs}
        knob_cells = []
        for name in knob_names:
            k = knob_by_name.get(name)
            if k is None:
                knob_cells.append("<td>-</td>")
                continue
            cls = "pass" if k.passed else ("unknown" if k.passed is None else "fail")
            knob_cells.append(
                f"<td>{html.escape(_fmt(k.value))} / {html.escape(_fmt(k.threshold))}"
                f"<br><span class=\"{cls}\">{html.escape(_fmt_pass(k.passed))}</span>"
                f" cov={k.coverage:.0%}</td>"
            )

        str_note = ""
        if r.str_source == "lifetime":
            str_note = (' <span class="lifetime-note">(lifetime -- not '
                       'comparable to the sell-through threshold)</span>')

        rows_html.append(
            "<tr>"
            f"<td>{html.escape(r.level)}</td>"
            f"<td>{html.escape(r.key)}<br><small>{html.escape(r.label)}</small></td>"
            f"<td>{html.escape(r.identity_tier)}</td>"
            f"<td>{html.escape(r.str_source)}{str_note}</td>"
            f"<td>{r.score:.1f}</td>"
            f"<td>{html.escape(_fmt_pence(r.max_cost_at_floor_pence))}</td>"
            f"<td>{html.escape(_fmt_pence(r.max_cost_at_median_pence))}</td>"
            + "".join(knob_cells)
            + "</tr>"
        )

    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        "<body>"
        f"<h1>{html.escape(title)}</h1>"
        f"<div class=\"meta\">generated {html.escape(generated_at.isoformat())}"
        f" -- {len(rows)} rows</div>"
        "<table><thead><tr>"
        "<th>level</th><th>key</th><th>tier</th><th>str_source</th>"
        "<th>score</th><th>max_cost@floor</th><th>max_cost@median</th>"
        f"{knob_headers}</tr></thead>"
        f"<tbody>{''.join(rows_html)}</tbody></table>"
        "</body></html>"
    )


def write_report(rows: Sequence[BoardRow], data_dir: Path | str, *,
                 name: str | None = None) -> Path:
    """Write the HTML board under DATA_DIR/reports/. Returns the path."""
    out_dir = Path(data_dir) / _REPORT_DIR_NAME
    out_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(UTC)
    filename = name or f"niche-board-{generated_at.strftime('%Y%m%dT%H%M%SZ')}.html"
    path = out_dir / filename
    path.write_text(
        render_html(rows, title="eBay niche board", generated_at=generated_at),
        encoding="utf-8",
    )
    return path
