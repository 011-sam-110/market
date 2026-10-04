"""The population review page's data layer: what was scraped, and how much of
it can actually be read.

The board (`phoneloop.board`) answers "which niche is best". This module
answers the question that has to be settled first -- **is there enough data
under that ranking to act on it?** On the store as it stands, 154 of 258 knob
cells render no verdict at all, and 46 of the 104 that do rest on a
denominator of 1. A board row cannot say that about itself; this module makes
it the headline.

**Data only.** No HTML, no printing, no filesystem writes. Rendering lives in
`phoneloop.population_html`, and the split is not tidiness: the locked panels
(the ones that need a second snapshot date) are testable exactly because the
decision "this cannot be computed" is made *here*, returned as a string, and
the renderer has no way to invent a number in its absence.

**Everything expensive is computed once.** `Build` holds the rows, the parsed
`extra` JSON, the ranked niches and the per-leaf product groups; a panel that
recomputed any of them would triple the page's wall clock (rows_on 1.2s,
rank_niches ~1.5s, group_products_in_leaf across 67 leaves 7.6s).

**Every SQL panel goes through `MarketStore.sql()`**, never `duckdb.read_
parquet` directly -- the store owns the glob and the `union_by_name=true`
contract -- and every one is prefixed with `_LATEST`, the newest-row-per-
source_id CTE. Without that window the raw table is 31,251 rows against
29,602 listings, a 5.6% overstatement that would inflate every count on the
page. `date` is a hive partition, not a stored column and not exposed by
`sql()`, so every date is derived as `(seen_at AT TIME ZONE 'UTC')::DATE`.

**No knob threshold is ever a literal here.** Every band, cap and floor
interpolates from the `Knobs` bundle, so a re-tuned knob moves this page with
it instead of leaving it quietly describing last month's thresholds.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from phoneloop import catalog_ebay, config, ebay, identity
from phoneloop.board import BoardRow, group_products_in_leaf, rank_niches
from phoneloop.knobs import Knobs
from phoneloop.market_store import ROW_FIELDS, MarketStore
from phoneloop.footprint import (Comparability, compare,
                                read_footprint)
from phoneloop.snapshots import (Baseline, Difference, difference,
                                 pick_baseline)

# How many niches rank_niches is asked for. The in-scope catalogue holds 43
# leaves and the store 67, so this is "all of them" with headroom, not a
# top-N -- a review page that ranked only the top 20 would hide exactly the
# unreadable rows it exists to surface.
_NICHE_LIMIT = 1000

# The newest observation per listing on one UTC day. Every SQL panel is
# prefixed with this; see the module docstring for why.
_LATEST = """
WITH latest AS (
  SELECT * EXCLUDE (rn) FROM (
    SELECT *, row_number() OVER (
      PARTITION BY source_id ORDER BY seen_at DESC) AS rn
    FROM market
    WHERE source = 'ebay'
      AND (seen_at AT TIME ZONE 'UTC')::DATE = DATE '{day}'
  ) WHERE rn = 1
)
"""

# The five columns Tier 2 (scripts/bulk_ebay.py --deepen) is the only writer
# of. They are reported as ONE band in the coverage ladder rather than five
# independent sparse columns, because they are not independent: a row has all
# five or none of them. `panel_coverage_ladder` verifies that claim with a
# five-way AND rather than asserting it.
TIER2_COLUMNS: tuple[str, ...] = (
    "brand", "seller_type", "listed_at", "store_joined", "store_items_sold")

# Below this seller coverage a leaf's competition figures are not thin, they
# are fiction: a top-5 share computed over 3 known sellers out of 361 rows is
# 100% by construction and ranks the leaf as the store's most concentrated
# market. Panels that would otherwise print such a figure gate on this and
# report the leaf in a named excluded bucket instead.
DENSITY_MIN_SELLER_COVERAGE = 0.50
DENSITY_GATE = 0.90

# Denominator buckets. Colour on the page encodes THIS, not the verdict --
# a PASS on n=1 must never look like a PASS on n=681.
DENOM_BUCKETS: tuple[str, ...] = ("0", "1", "2-5", "6-30", ">30")

# Explorer whitelist. This is a whitelist, NOT a default projection: `url` is
# absent by construction (a 29,602-row page carrying every listing url is a
# redistribution of eBay's index, and the self-contained guard would not
# catch it because the guard looks for the substring, which a url in a
# *title* also trips). tests/test_population.py enforces both halves.
EXPLORER_FIELDS: tuple[str, ...] = (
    "source_id", "title", "price_pence", "total_pence", "leaf_category_id",
    "category", "seller_id", "seller_feedback_bp", "seller_feedback_n",
    "sold_qty", "origin_country", "condition", "eta_days", "seller_type",
    "brand", "epid")
EXPLORER_EXTRA_FIELDS: tuple[str, ...] = ("watchers", "stock_hint")

# What a string value becomes when it carries a url. Applied BEFORE encoding,
# because a title can legitimately contain one and would otherwise sail
# straight past the self-contained guard.
LINK_PLACEHOLDER = "[link removed]"

# The sweep's page cap, as configured in scripts/run_daily.cmd
# (--pages-per-shard 10, three price-band pages actually recorded per leaf at
# ~240 results a page). Stated so the leaf-size histogram's right-hand pile-up
# reads as "the cap", not "the market".
SWEEP_LEAF_CAP = 720


def unlock(*, need: int, have: int, latest: date | None) -> str:
    """The exact words a panel that cannot be computed yet must say.

    One helper, because the wording is asserted on in tests and drifts the
    moment two panels phrase it themselves. The date count and the latest
    date are read off the store, never hardcoded -- this text has to still be
    true on the night date 2 lands.

    The blocker names the real one: `scripts/run_daily.cmd` is written and
    verified but has never been scheduled, and it sets no
    `PHONELOOP_DATA_DIR`, so as written it would write to config.py's default
    store rather than extend the one this page just read.
    """
    default_dir = config.load(env={}).DATA_DIR
    headline = ("NEEDS A 2ND SWEEP" if need == 2
                else f"NEEDS {need} SNAPSHOT DATES")
    when = latest.isoformat() if latest is not None else "none on file"
    return "\n".join([
        headline,
        f"{have} of {need} snapshot dates on file ({when}).",
        "No number is shown here because none can be computed — nothing "
        "on this page is estimated, sampled or placeheld.",
        "Blocker: scripts/run_daily.cmd is written and verified but has "
        "never been scheduled, and it sets no PHONELOOP_DATA_DIR — as "
        f"written it would write to {default_dir} (Vinted rows only) and "
        "start a NEW eBay store rather than extend this one.",
        "[what unlocks this ->]",
    ])


@dataclass(frozen=True)
class Build:
    """Everything expensive, computed exactly once and threaded to every panel."""

    store: MarketStore
    data_dir: Path
    knobs: Knobs
    dates: list[date]
    day: date | None
    rows: list[dict]
    extra: list[dict]
    in_scope: set[str]
    niches: list[BoardRow]
    groups: dict[str, tuple[dict, dict]]
    diff: Difference | None
    #: Whether the two differenced nights swept the same population. None
    #: when there is no difference to qualify. A difference across two unlike
    #: footprints is not wrong to compute -- it is wrong to read as change
    #: over time, and every consumer of `diff` has to say so. See
    #: phoneloop/footprint.py.
    comparability: Comparability | None = None
    #: Which two dates `diff` covers, and whether the pair is the long
    #: baseline or the fallback. None when there is no difference. See
    #: snapshots.pick_baseline.
    baseline: Baseline | None = None
    # Memo for the handful of results two panels genuinely share. Every
    # `store.sql()` call opens a connection and rebuilds the parquet view, so
    # a query run twice costs about as much as a whole extra panel; two
    # panels reading the same numbers from two runs could also, in principle,
    # disagree. Not part of the data contract -- a panel must still be
    # correct if this is empty.
    cache: dict = field(default_factory=dict)

    @property
    def n(self) -> int:
        """Distinct listings on the latest snapshot date -- the denominator
        almost every figure on the page divides by."""
        return len(self.rows)

    def sql(self, query: str):
        """`query` with the `_LATEST` CTE prefixed and `{day}` filled."""
        if self.day is None:
            raise RuntimeError(
                "no snapshot date: every SQL panel is scoped to one day and "
                "there is none, which the renderer must handle as the named "
                "empty-store state rather than as a zeroed page")
        return self.store.sql(_LATEST.format(day=self.day.isoformat()) + query)


def _parse_extra(raw: object) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def build(data_dir: Path | str, knobs: Knobs) -> Build:
    """Read the store once. Every panel below reads this and nothing else."""
    data_dir = Path(data_dir)
    store = MarketStore(data_dir)
    dates = store.snapshot_dates("ebay")
    day = dates[-1] if dates else None

    if day is None:
        return Build(store=store, data_dir=data_dir, knobs=knobs, dates=[],
                     day=None, rows=[], extra=[],
                     in_scope=set(catalog_ebay.top_level_of().keys()),
                     niches=[], groups={}, diff=None)

    rows = store.rows_on(day)
    extra = [_parse_extra(r.get("extra")) for r in rows]
    in_scope = set(catalog_ebay.top_level_of().keys())

    # The newest date against a baseline 25 or more dates older, not
    # against last night. See snapshots.pick_baseline.
    baseline = pick_baseline(dates)
    diff = (difference(store, baseline.date_a, baseline.date_b)
            if baseline is not None else None)
    # Read off each night's own checkpoint, not off run_daily.cmd. The config
    # says what was asked for; the checkpoint says what happened, including
    # the shards eBay blocked, and those are exactly the nights that differ.
    # The two nights compared are the two nights differenced.
    comparability = (
        compare(read_footprint(data_dir, baseline.date_a),
                read_footprint(data_dir, baseline.date_b))
        if baseline is not None else None)

    niches = [r for r in rank_niches(store, knobs, difference=diff,
                                     limit=_NICHE_LIMIT)
              if r.leaf_category_id in in_scope]

    # ONE grouping pass over every leaf in the store, including the strays --
    # the saturation panel's point is partly that the store swept 24 leaves
    # the catalogue does not know about, and dropping them here would hide it.
    by_leaf: dict[str, list[dict]] = {}
    for r in rows:
        leaf = r.get("leaf_category_id")
        if leaf:
            by_leaf.setdefault(leaf, []).append(r)
    groups = {leaf: group_products_in_leaf(leaf_rows)
              for leaf, leaf_rows in by_leaf.items()}

    return Build(store=store, data_dir=data_dir, knobs=knobs, dates=dates,
                 day=day, rows=rows, extra=extra, in_scope=in_scope,
                 niches=niches, groups=groups, diff=diff,
                 comparability=comparability, baseline=baseline)


# --- shared helpers ----------------------------------------------------------

def denom_bucket(n: int) -> str:
    """Which evidence bucket a denominator falls in. Colour encodes this."""
    if n <= 0:
        return "0"
    if n == 1:
        return "1"
    if n <= 5:
        return "2-5"
    if n <= 30:
        return "6-30"
    return ">30"


def _leaf_coverage(b: Build) -> dict[str, dict]:
    """Per leaf: rows, rows with a known seller, and the ratio. The join key
    for both the missing-data-pass flag and the density gate, computed once
    because two panels need exactly the same numbers and must not disagree."""
    if "leaf_coverage" in b.cache:
        return b.cache["leaf_coverage"]
    rel = b.sql("""
    SELECT leaf_category_id,
           count(*) AS n,
           count(seller_id) AS seller_known,
           count(seller_id) / count(*)::DOUBLE AS seller_cov
    FROM latest WHERE leaf_category_id IS NOT NULL GROUP BY 1
    """)
    out = {leaf: {"leaf": leaf, "n": n, "seller_known": known,
                  "seller_cov": cov}
           for leaf, n, known, cov in rel.fetchall()}
    b.cache["leaf_coverage"] = out
    return out


def _knob(row: BoardRow, name: str):
    return next((k for k in row.knobs if k.knob == name), None)


def _column_counts(b: Build) -> list[tuple[str, int, int]]:
    """(column, non-null count, distinct count) for every stored column.

    A 37-way UNION ALL over the whole population, so the coverage ladder and
    the constants panel share one run of it rather than one each -- and,
    more importantly, cannot disagree about which columns are constant.
    """
    if "column_counts" in b.cache:
        return b.cache["column_counts"]
    parts = [f"SELECT '{c}' AS col, count(\"{c}\") AS non_null, "
             f"count(DISTINCT \"{c}\") AS distinct_vals FROM latest"
             for c in ROW_FIELDS]
    out = b.sql(" UNION ALL ".join(parts)).fetchall()
    b.cache["column_counts"] = out
    return out


# --- ZONE 0: provenance -------------------------------------------------------

def panel_provenance(b: Build) -> tuple[object, str | None]:
    """Which store, which dates, how big, and can the rest be trusted.

    The raw-row count is deliberately taken over `market` rather than over
    the deduped CTE: "31,251 raw rows -> 29,602 distinct listings" is the
    whole claim, and a count(*) inside `latest` would report the same number
    twice and quietly retire the question.
    """
    if b.day is None:
        return None, "no eBay snapshots"

    raw_rows, listings, first_seen, last_seen, leaves = b.sql(f"""
    SELECT (SELECT count(*) FROM market
            WHERE source = 'ebay'
              AND (seen_at AT TIME ZONE 'UTC')::DATE
                  = DATE '{b.day.isoformat()}')      AS raw_rows,
           count(DISTINCT source_id)                  AS listings,
           min(seen_at AT TIME ZONE 'UTC')            AS first_seen_utc,
           max(seen_at AT TIME ZONE 'UTC')            AS last_seen_utc,
           count(DISTINCT leaf_category_id)           AS leaves
    FROM latest
    """).fetchone()

    swept = {r["leaf_category_id"] for r in b.rows if r.get("leaf_category_id")}
    stray = sorted(swept - b.in_scope)
    stray_rows = sum(1 for r in b.rows
                     if r.get("leaf_category_id") in set(stray))

    store_bytes = sum(p.stat().st_size
                      for p in (b.data_dir / "market").rglob("*.parquet"))

    return {
        "data_dir": str(b.data_dir),
        "dates": list(b.dates),
        "raw_rows": raw_rows,
        "listings": listings,
        "first_seen": first_seen,
        "last_seen": last_seen,
        "leaves_swept": leaves,
        "in_scope": len(b.in_scope),
        "swept_in_scope": len(swept & b.in_scope),
        "stray_leaves": stray,
        "stray_rows": stray_rows,
        "store_bytes": store_bytes,
    }, None


# --- ZONE 1: can I read it ----------------------------------------------------

def panel_trust_ledger(b: Build) -> tuple[object, str | None]:
    """Should I act on any row of tonight's board?

    One number would be a lie here in either direction, so this reports a
    ratio with both terms visible and a bucket histogram beside it: 104 of
    258 cells rendered a verdict, and 46 of those rest on a denominator of 1.
    """
    if b.day is None:
        return None, "no eBay snapshots"

    unreadable = 0
    verdicts = 0
    buckets = Counter()
    cells = []
    for row in b.niches:
        for k in row.knobs:
            if k.passed is None:
                unreadable += 1
                cells.append({"niche": row.key, "knob": k.knob,
                              "verdict": None, "denominator": k.denominator})
            else:
                verdicts += 1
                buckets[denom_bucket(k.denominator)] += 1
                cells.append({"niche": row.key, "knob": k.knob,
                              "verdict": k.passed,
                              "denominator": k.denominator})

    return {
        "n_niches": len(b.niches),
        "n_knobs": len(b.niches[0].knobs) if b.niches else 0,
        "cells": len(cells),
        "unreadable": unreadable,
        "verdicts": verdicts,
        "buckets": {name: buckets.get(name, 0) for name in DENOM_BUCKETS},
        "grid": cells,
    }, None


def panel_knob_matrix(b: Build) -> tuple[object, str | None]:
    """Which knob do I spend the next scrape hour on?

    Same 43x6 grid as the trust ledger, read the other way: per knob rather
    than per niche, so the answer is a column, not a search.
    """
    if b.day is None:
        return None, "no eBay snapshots"

    knob_names = list(dict.fromkeys(k.knob for r in b.niches for k in r.knobs))
    per_knob: dict[str, dict] = {
        name: {"knob": name, "unreadable": 0, "pass": 0, "fail": 0,
               "denominators": []} for name in knob_names}

    grid = []
    for row in b.niches:
        cells = []
        for name in knob_names:
            k = _knob(row, name)
            if k is None:
                cells.append(None)
                continue
            slot = per_knob[name]
            if k.passed is None:
                slot["unreadable"] += 1
            elif k.passed:
                slot["pass"] += 1
            else:
                slot["fail"] += 1
            slot["denominators"].append(k.denominator)
            cells.append({
                "knob": name, "verdict": k.passed,
                "denominator": k.denominator, "bucket": denom_bucket(k.denominator),
                "value": k.value, "threshold": k.threshold,
                "coverage": k.coverage, "note": k.note,
            })
        grid.append({"niche": row.key, "leaf": row.leaf_category_id,
                     "score": row.score, "cells": cells})

    sat = [c for r in grid for c in r["cells"]
           if c and c["knob"] == "saturation" and c["value"] is not None]
    sat_values = [c["value"] for c in sat]
    saturation_note = (
        "UNREADABLE BY CONSTRUCTION. board.py:193-234 passes the whole "
        f"niche's distinct-seller count ({min(sat_values) if sat_values else '?'} "
        f"to {max(sat_values) if sat_values else '?'}) to a per-product "
        f"threshold of {b.knobs.max_competitors}, and metrics.py:445-449 sets "
        'denominator=1 to mean "the count was supplied". This is a judgement '
        "about another module's output, stated here rather than silently "
        "applied."
    )

    return {
        "knob_names": knob_names,
        "per_knob": per_knob,
        "grid": grid,
        "n_niches": len(b.niches),
        "saturation_note": saturation_note,
    }, None


def panel_green_because_broken(b: Build) -> tuple[object, str | None]:
    """Which rows do I strike out before reading the ranking?

    A knob whose denominator is missing scores identically to a satisfied
    one, because board.py's score is `passed - failed` and an unknown counts
    as neither. So a leaf where the seller column is 99% absent can score
    above a leaf where it is present and bad. This panel names those rows.
    """
    if b.day is None:
        return None, "no eBay snapshots"

    coverage = _leaf_coverage(b)
    flagged = []
    scoring = 0
    for rank, row in enumerate(b.niches, start=1):
        if row.score > 0:
            scoring += 1
        cov = coverage.get(row.leaf_category_id or "", {})
        seller_cov = cov.get("seller_cov")
        if seller_cov is None or seller_cov >= DENSITY_MIN_SELLER_COVERAGE:
            continue
        passing = [k.knob for k in row.knobs if k.passed is True]
        if not passing:
            continue
        flagged.append({
            "niche": row.key,
            "leaf": row.leaf_category_id,
            "rank": rank,
            "score": row.score,
            "seller_cov": seller_cov,
            "seller_known": cov.get("seller_known"),
            "n": cov.get("n"),
            "passing": passing,
            "price_median_denominator": row.metrics.price_median_denominator,
            "saturation": _knob(row, "saturation"),
            "seller_quality": _knob(row, "seller_quality"),
        })

    return {
        "flagged": flagged,
        "n_scoring_above_zero": scoring,
        "n_niches": len(b.niches),
        "threshold": DENSITY_MIN_SELLER_COVERAGE,
        "critique": (
            "score = passed - failed, so a missing column scores identically "
            "to a satisfied one. This needs a minimum-evidence gate."),
    }, None


def panel_coverage_ladder(b: Build) -> tuple[object, str | None]:
    """Every stored column, by how much of it is actually there.

    Sorted ascending on purpose: the cliff between the columns Tier 1 fills
    and the columns only Tier 2 touches IS the shape of this panel, and any
    other ordering hides it.
    """
    if b.day is None:
        return None, "no eBay snapshots"

    # The derived date is not a stored column (it is a hive partition, and
    # `sql()` does not expose it), so it is counted separately and appended
    # to the shared per-column counts rather than smuggled into ROW_FIELDS.
    derived = b.sql(
        "SELECT 'date (derived)' AS col, "
        "count((seen_at AT TIME ZONE 'UTC')::DATE) AS non_null, "
        "count(DISTINCT (seen_at AT TIME ZONE 'UTC')::DATE) AS distinct_vals "
        "FROM latest").fetchall()
    counted = sorted(_column_counts(b) + derived, key=lambda r: r[1])

    n = b.n
    ladder = []
    for col, non_null, distinct_vals in counted:
        ladder.append({
            "col": col, "non_null": non_null, "distinct": distinct_vals,
            "fill": (non_null / n) if n else 0.0,
            "band": _coverage_band(col, non_null, distinct_vals, n),
        })

    # Two scalars in one pass. `cohort_and` verifies the tier-2 cohort claim
    # rather than asserting it: if all five columns really are one cohort, a
    # five-way AND returns the same count as each column alone. `bp_zero` is
    # the amber flag -- a 0bp positive-feedback rate sitting beside a real
    # rating count is not "0% positive", it is an unread field, and NULL
    # means unknown everywhere else on the row.
    cohort_and, bp_zero = b.sql(
        "SELECT count(*) FILTER (WHERE "
        + " AND ".join(f'"{c}" IS NOT NULL' for c in TIER2_COLUMNS)
        + "), count(*) FILTER (WHERE seller_feedback_bp = 0 "
          "AND seller_feedback_n > 0) FROM latest"
    ).fetchone()

    condition_row = next((r for r in ladder if r["col"] == "condition"), None)

    return {
        "ladder": ladder,
        "n": n,
        "tier2_columns": list(TIER2_COLUMNS),
        "tier2_cohort_and": cohort_and,
        "feedback_bp_zero_with_ratings": bp_zero,
        "condition_null_pct": (1 - condition_row["fill"]) if condition_row else None,
    }, None


def _coverage_band(col: str, non_null: int, distinct: int, n: int) -> str:
    if non_null == 0:
        return "DEAD"
    if col in TIER2_COLUMNS:
        return "TIER-2 COHORT"
    if non_null == n and distinct <= 1:
        return "CONSTANT"
    fill = non_null / n if n else 0.0
    if fill < 0.03:
        return "SPARSE <3%"
    if fill < 0.90:
        return "PARTIAL"
    if fill < 1.0:
        return "NEAR-FULL"
    return "FULL"


def panel_category_integrity(b: Build) -> tuple[object, str | None]:
    """The same coverage question, cut by department rather than by column.

    A global 86.68% seller coverage reads as "mostly fine". Cut this way it
    is one department carrying the whole deficit, which is a different
    problem with a different fix.
    """
    if b.day is None:
        return None, "no eBay snapshots"

    rel = b.sql("""
    SELECT category, count(*) AS n,
      count(seller_id)/count(*)::DOUBLE          AS seller_cov,
      count(seller_feedback_bp)/count(*)::DOUBLE AS feedback_cov,
      count(sold_qty)/count(*)::DOUBLE           AS sold_cov,
      count(eta_days)/count(*)::DOUBLE           AS eta_cov,
      count(condition)/count(*)::DOUBLE          AS condition_cov,
      count(total_pence)/count(*)::DOUBLE        AS landed_cov,
      count(origin_country)/count(*)::DOUBLE     AS origin_cov
    FROM latest GROUP BY 1 ORDER BY n DESC
    """)
    cols = ("seller_cov", "feedback_cov", "sold_cov", "eta_cov",
            "condition_cov", "landed_cov", "origin_cov")
    rows = [dict(zip(("category", "n") + cols, r)) for r in rel.fetchall()]

    # Summed from the per-category rows rather than re-queried: two passes
    # over the same population that disagreed by a rounding step would make
    # the caption argue with the table above it.
    total = sum(r["n"] for r in rows)
    known = sum(r["seller_cov"] * r["n"] for r in rows)
    global_seller = (known / total) if total else 0.0
    worst = min(rows, key=lambda r: r["seller_cov"]) if rows else None

    return {
        "rows": rows, "cov_cols": cols,
        "global_seller_cov": global_seller,
        "worst_category": worst,
    }, None


def panel_constants_and_contradictions(b: Build) -> tuple[object, str | None]:
    """Columns with exactly one value, and three places the data contradicts
    the contract that describes it.

    A constant column is not automatically a bug -- `currency` being GBP on
    every row of a UK sweep is the correct answer. What matters is telling
    "scraped, and it really is constant" apart from "never populated", which
    a distinct-count of 1 alone cannot do.
    """
    if b.day is None:
        return None, "no eBay snapshots"

    counted = _column_counts(b)
    n = b.n
    single = [(col, non_null) for col, non_null, dv in counted if dv == 1]
    lo, hi = b.knobs.price_from_pence, b.knobs.price_to_pence

    # ONE pass for every constant column's value plus the promoted and
    # out-of-band scalars. A query per column read the same 29,602 rows eight
    # times over and cost more than any other panel on the page.
    # CAST to VARCHAR so one expression works for every column type -- these
    # values are for display beside the column name, never for arithmetic.
    sample_cols = ", ".join(f'max(CAST("{col}" AS VARCHAR)) AS "s_{i}"'
                            for i, (col, _nn) in enumerate(single))
    scalars = b.sql(
        "SELECT count(*) FILTER (WHERE promoted), "
        "count(*) FILTER (WHERE NOT promoted), "
        "count(*) FILTER (WHERE promoted IS NULL), "
        f"count(*) FILTER (WHERE total_pence < {lo}), "
        f"count(*) FILTER (WHERE total_pence > {hi})"
        + (", " + sample_cols if sample_cols else "")
        + " FROM latest").fetchone()
    promoted_true, promoted_false, promoted_null, below, above = scalars[:5]
    samples = scalars[5:]

    constants = [{
        # Sanitised like every other string that reaches the page: a store
        # with one row would make `url` a "constant" column and print a live
        # listing url straight past the self-contained guard.
        "col": col, "non_null": non_null, "value": _sanitise(samples[i]),
        "kind": ("constant over every row" if non_null == n
                 else "constant WHERE PRESENT (the rest is unpopulated)"),
    } for i, (col, non_null) in enumerate(single)]
    never = [{"col": c, "non_null": nn} for c, nn, _dv in counted if nn == 0]

    computed = Counter(identity.tier_for(r) for r in b.rows)
    stored = Counter(str(r.get("identity_tier")) for r in b.rows)
    bands = Counter(ebay.band_of(r.get("shard") or "") for r in b.rows)

    return {
        "constants": sorted(constants, key=lambda r: r["col"]),
        "never_populated": never,
        "n": n,
        "contradictions": [
            {
                "name": "promoted is never False and never NULL",
                "contract": ('market_store.py:99 -- "unknown for Vinted / '
                             'unreadable eBay rows; True/False both real on eBay"'),
                "observed": {"true": promoted_true, "false": promoted_false,
                             "null": promoted_null},
                "reading": ("every row reads promoted=True, so the column "
                            "separates nothing and metrics.promoted_pct is "
                            "1.0 by construction, not by measurement"),
            },
            {
                "name": "identity_tier stored disagrees with identity.tier_for",
                "contract": ("identity.py tier_for() is the authority; the "
                             "stored column is what the transport wrote"),
                "observed": {"computed": dict(computed), "stored": dict(stored)},
                "reading": ("board.py groups products on the COMPUTED tier and "
                            "so does this page; the stored column is recorded, "
                            "not read"),
            },
            {
                "name": "one price band swept, listings outside it stored",
                "contract": (f"the shard band is the sweep filter "
                             f"(knobs {lo}-{hi} pence landed)"),
                "observed": {"bands": {str(k): v for k, v in bands.items()},
                             "below_floor": below, "above_ceiling": above},
                "reading": ("the whole sweep ran in one band, yet the landed "
                            "price panel counts rows on both sides of it -- "
                            "eBay's band filter is on the headline price, not "
                            "the landed one"),
            },
        ],
    }, None


# --- ZONE 2: what the market looks like ---------------------------------------

def panel_competition_density(b: Build) -> tuple[object, str | None]:
    """Where is competition thin?

    Gated hard on seller coverage. Without the gate the five leaves whose
    seller column is 99% absent show a 100% top-5 share off 1-8 known sellers
    and rank as the store's most concentrated markets -- the exact opposite
    of the truth, produced entirely by absence.
    """
    if b.day is None:
        return None, "no eBay snapshots"

    coverage = _leaf_coverage(b)
    rel = b.sql("""
    SELECT leaf_category_id, seller_id, count(*) AS c
    FROM latest
    WHERE seller_id IS NOT NULL AND leaf_category_id IS NOT NULL
    GROUP BY 1, 2
    """)
    per_leaf: dict[str, list[int]] = {}
    for leaf, _seller, c in rel.fetchall():
        per_leaf.setdefault(leaf, []).append(c)

    category_of: dict[str, str] = {}
    for r in b.rows:
        leaf = r.get("leaf_category_id")
        if leaf and leaf not in category_of:
            category_of[leaf] = r.get("category") or "(unknown)"

    measured, excluded = [], []
    for leaf in sorted(b.in_scope & set(coverage)):
        cov = coverage[leaf]
        counts = sorted(per_leaf.get(leaf, []), reverse=True)
        known = sum(counts)
        entry = {
            "leaf": leaf,
            "category": category_of.get(leaf, "(unknown)"),
            "n": cov["n"],
            "seller_known": cov["seller_known"],
            "seller_cov": cov["seller_cov"],
            "sellers": len(counts),
            "top5_share": (sum(counts[:5]) / known) if known else None,
            "hhi": (sum((c / known) ** 2 for c in counts)) if known else None,
        }
        (measured if cov["seller_cov"] >= DENSITY_GATE else excluded).append(entry)

    measured.sort(key=lambda e: (e["top5_share"] if e["top5_share"] is not None else 2))
    excluded.sort(key=lambda e: e["seller_cov"])

    shares = [e["top5_share"] for e in measured if e["top5_share"] is not None]
    hhis = [e["hhi"] for e in measured if e["hhi"] is not None]
    return {
        "measured": measured,
        "excluded": excluded,
        "gate": DENSITY_GATE,
        "in_scope": len(b.in_scope),
        "share_min": min(shares) if shares else None,
        "share_max": max(shares) if shares else None,
        "hhi_min": min(hhis) if hhis else None,
        "hhi_max": max(hhis) if hhis else None,
    }, None


def panel_seller_entry_exit(b: Build) -> tuple[object, str | None]:
    """Which sellers arrived and which left. Needs two dates."""
    if len(b.dates) < 2:
        return None, unlock(need=2, have=len(b.dates), latest=b.day)
    prev = {r["seller_id"] for r in b.store.rows_on(b.dates[-2])
            if r.get("seller_id")}
    now = {r.get("seller_id") for r in b.rows if r.get("seller_id")}
    return {"entered": sorted(now - prev), "exited": sorted(prev - now),
            "persisting": len(now & prev)}, None


def panel_seller_footprint(b: Build) -> tuple[object, str | None]:
    """Who is actually in this market, by depth and by breadth.

    Sellers are named by `seller_id` only. A review page is not a place to
    accumulate a profile of a named trader.
    """
    if b.day is None:
        return None, "no eBay snapshots"

    rel = b.sql("""
    SELECT seller_id, count(*) AS listings,
           count(DISTINCT leaf_category_id) AS leaves,
           count(DISTINCT category) AS categories
    FROM latest WHERE seller_id IS NOT NULL
    GROUP BY 1 ORDER BY listings DESC
    """)
    sellers = [dict(zip(("seller_id", "listings", "leaves", "categories"), r))
               for r in rel.fetchall()]

    one_leaf = sum(1 for s in sellers if s["leaves"] == 1)
    two_plus = sum(1 for s in sellers if s["leaves"] >= 2)
    five_plus = sum(1 for s in sellers if s["leaves"] >= 5)
    return {
        "n_sellers": len(sellers),
        "known_rows": sum(s["listings"] for s in sellers),
        "n": b.n,
        "by_depth": sellers[:15],
        "by_breadth": sorted(sellers, key=lambda s: (-s["leaves"], -s["listings"]))[:15],
        "one_leaf": one_leaf, "two_plus": two_plus, "five_plus": five_plus,
        "max_leaves": max((s["leaves"] for s in sellers), default=0),
        # The denominator the breadth column divides by. Counted from the
        # rows in hand, never a constant: the renderer used to divide by a
        # literal 67, which is the size of nothing this project sweeps (the
        # in-scope catalogue holds 43), so a seller present in every leaf we
        # look at rendered as 43-of-67 -- a trader with the whole market
        # shown as covering two thirds of it.
        "leaves_swept": b.sql(
            "SELECT count(DISTINCT leaf_category_id) FROM latest "
            "WHERE leaf_category_id IS NOT NULL").fetchone()[0],
    }, None


def panel_landed_price(b: Build) -> tuple[object, str | None]:
    """The landed-price population against the price-band knob.

    `total_pence` is landed (item + postage) and NULL when delivery was not
    stated, so "unknown" is a real third bucket here and is never folded into
    "free delivery".
    """
    if b.day is None:
        return None, "no eBay snapshots"

    lo, hi = b.knobs.price_from_pence, b.knobs.price_to_pence
    fields = ("n", "known", "in_band", "below", "above", "unknown",
              "free_delivery", "paid_delivery", "impossible", "median_uplift")
    row = b.sql(f"""
    SELECT count(*) AS n, count(total_pence) AS known,
      count(*) FILTER (WHERE total_pence BETWEEN {lo} AND {hi})  AS in_band,
      count(*) FILTER (WHERE total_pence < {lo})                 AS below,
      count(*) FILTER (WHERE total_pence > {hi})                 AS above,
      count(*) FILTER (WHERE total_pence IS NULL)                AS unknown,
      count(*) FILTER (WHERE total_pence = price_pence)          AS free_delivery,
      count(*) FILTER (WHERE total_pence > price_pence)          AS paid_delivery,
      count(*) FILTER (WHERE total_pence < price_pence)          AS impossible,
      median(total_pence - price_pence)
        FILTER (WHERE total_pence > price_pence)                 AS median_uplift
    FROM latest
    """).fetchone()
    summary = dict(zip(fields, row))

    hist = b.sql("""
    SELECT (total_pence // 250) * 250 AS bin, count(*) AS c
    FROM latest WHERE total_pence IS NOT NULL GROUP BY 1 ORDER BY 1
    """).fetchall()

    summary["histogram"] = [{"bin": int(binv), "count": c} for binv, c in hist]
    summary["band"] = (lo, hi)
    return summary, None


def panel_leaf_price_map(b: Build) -> tuple[object, str | None]:
    """Every in-scope leaf's landed-price spread against the knob band.

    A right gutter is reserved, sized and labelled for the price drift a
    second sweep unlocks. It holds no marks today and no pixel moves when it
    fills -- a panel that grows a new column on date 2 is a panel nobody can
    compare against today's screenshot.
    """
    if b.day is None:
        return None, "no eBay snapshots"

    lo, hi = b.knobs.price_from_pence, b.knobs.price_to_pence
    rel = b.sql(f"""
    SELECT leaf_category_id, count(*) AS n, count(total_pence) AS known,
      quantile_cont(total_pence, 0.25) AS p25,
      median(total_pence)              AS p50,
      quantile_cont(total_pence, 0.75) AS p75,
      count(*) FILTER (WHERE total_pence < {lo})
        / nullif(count(total_pence),0)::DOUBLE AS below_floor
    FROM latest WHERE leaf_category_id IS NOT NULL GROUP BY 1 ORDER BY p50
    """)
    fields = ("leaf", "n", "known", "p25", "p50", "p75", "below_floor")
    rows = [dict(zip(fields, r)) for r in rel.fetchall()
            if r[0] in b.in_scope]

    drift, drift_reason = panel_price_drift(b)
    return {"rows": rows, "band": (lo, hi), "drift": drift,
            "drift_reason": drift_reason,
            "gutter_label": "price drift — needs a 2nd sweep"}, None


def panel_price_drift(b: Build) -> tuple[object, str | None]:
    """Per-leaf median landed price, one point per snapshot date. Needs two."""
    if len(b.dates) < 2:
        return None, unlock(need=2, have=len(b.dates), latest=b.day)
    series: dict[str, list] = {}
    for d in b.dates:
        rel = b.store.sql(f"""
            SELECT leaf_category_id, median(total_pence) FROM (
                SELECT * EXCLUDE (rn) FROM (
                    SELECT *, row_number() OVER (
                        PARTITION BY source_id ORDER BY seen_at DESC) AS rn
                    FROM market WHERE source='ebay'
                      AND (seen_at AT TIME ZONE 'UTC')::DATE = DATE '{d.isoformat()}'
                ) WHERE rn = 1)
            WHERE leaf_category_id IS NOT NULL GROUP BY 1
        """)
        for leaf, p50 in rel.fetchall():
            series.setdefault(leaf, []).append({"date": d, "p50": p50})
    return {"series": series, "dates": list(b.dates)}, None


def panel_saturation_calibration(b: Build) -> tuple[object, str | None]:
    """Is knob 6 measuring anything?

    The knob caps distinct sellers per product. If almost every product is
    one listing by one seller, the cap excludes nothing and the real finding
    is that the cluster tier is not finding rivals -- which is a scraping
    problem, not a knob-tuning one.
    """
    if b.day is None:
        return None, "no eBay snapshots"

    seller_of = {r["source_id"]: r.get("seller_id") for r in b.rows}
    sizes = Counter()
    sellers_per_product = Counter()
    tiers = Counter()
    over_cap = 0
    total_groups = 0
    for _leaf, (groups, tier_of_key) in b.groups.items():
        for key, prod_rows in groups.items():
            total_groups += 1
            sizes[len(prod_rows)] += 1
            tiers[tier_of_key.get(key, "?")] += 1
            distinct = len({seller_of.get(r["source_id"]) for r in prod_rows
                            if seller_of.get(r["source_id"])})
            sellers_per_product[distinct] += 1
            if distinct > b.knobs.max_competitors:
                over_cap += 1

    singletons = sizes.get(1, 0)

    by_epid_sellers = {}
    by_epid_leaves = {}
    for r in b.rows:
        epid = r.get("epid")
        if not epid:
            continue
        by_epid_sellers.setdefault(epid, set()).add(r.get("seller_id"))
        by_epid_leaves.setdefault(epid, set()).add(r.get("leaf_category_id"))
    multi_seller = sum(1 for v in by_epid_sellers.values()
                       if len({x for x in v if x}) > 1)
    multi_leaf = sum(1 for v in by_epid_leaves.values()
                     if len({x for x in v if x}) > 1)

    return {
        "total_groups": total_groups,
        "singletons": singletons,
        "size_histogram": dict(sorted(sizes.items())),
        "sellers_per_product": dict(sorted(sellers_per_product.items())),
        "tiers": dict(tiers),
        "over_cap": over_cap,
        "max_competitors": b.knobs.max_competitors,
        "n_epids": len(by_epid_sellers),
        "epid_multi_seller": multi_seller,
        "epid_multi_leaf": multi_leaf,
        "verdict": (
            f"knob 6 currently excludes {over_cap} groups in {total_groups} "
            "-- it is inert, and the singleton rate says the cluster tier is "
            "not finding rivals, which matters more."),
    }, None


def panel_demand_evidence(b: Build) -> tuple[object, str | None]:
    """What evidence of demand exists per leaf, with the three caveats that
    stop it being read as a rate.

    `sold_qty` is a ROUNDED LIFETIME badge; `watchers` at 0 means "no badge
    shown", not a measured zero; and lifetime volume is not velocity, because
    `listed_at` exists on a fraction of a percent of rows.
    """
    if b.day is None:
        return None, "no eBay snapshots"

    rel = b.sql("""
    SELECT leaf_category_id, count(*) AS n,
      count(sold_qty) AS sold_known,
      median(sold_qty) FILTER (WHERE sold_qty IS NOT NULL) AS sold_median,
      count(*) FILTER (
        WHERE TRY_CAST(json_extract_string(extra,'$.watchers') AS INTEGER) > 0
      ) AS watched,
      count(*) FILTER (
        WHERE json_extract_string(extra,'$.stock_hint')
              IN ('Last one','Almost gone')
      ) AS scarce
    FROM latest WHERE leaf_category_id IS NOT NULL GROUP BY 1 ORDER BY n DESC
    """)
    fields = ("leaf", "n", "sold_known", "sold_median", "watched", "scarce")
    rows = [dict(zip(fields, r)) for r in rel.fetchall()]

    hot = []
    for r, ex in zip(b.rows, b.extra):
        try:
            watchers = int(ex.get("watchers") or 0)
        except (TypeError, ValueError):
            watchers = 0
        if watchers > 0 and (ex.get("stock_hint") in ("Last one", "Almost gone")):
            hot.append({"source_id": r["source_id"],
                        "leaf": r.get("leaf_category_id"),
                        "watchers": watchers,
                        "stock_hint": ex.get("stock_hint")})

    listed_at_rows = sum(1 for r in b.rows if r.get("listed_at") is not None)
    sold_values = [r["sold_qty"] for r in b.rows if r.get("sold_qty") is not None]
    return {
        "rows": rows,
        "hot": hot,
        "n": b.n,
        "sold_known": len(sold_values),
        "sold_max": max(sold_values) if sold_values else None,
        "listed_at_rows": listed_at_rows,
        "caveats": [
            "sold_qty is a rounded LIFETIME badge (round literals like 10,000 "
            "and 350,000 appear verbatim) -- median and buckets only, never a "
            "mean, never a rate.",
            'watchers=0 means "no badge shown", not a measured zero, so it is '
            "only ever a >0 subpopulation.",
            "lifetime volume is not velocity, because listed_at exists on a "
            "fraction of a percent of rows.",
        ],
    }, None


def panel_origin_and_fulfilment(b: Build) -> tuple[object, str | None]:
    """Where the goods ship from, and what the delivery knob can see.

    The delivery knob is measured on a couple of percent of the population
    and almost every known value is the same number, so it currently
    separates nothing.
    """
    if b.day is None:
        return None, "no eBay snapshots"

    # Both distributions in one pass, tagged by `kind`. Two GROUP BYs over
    # the same 29,602 rows is two full reads for one panel.
    tagged = b.sql("""
    SELECT 'origin' AS kind, coalesce(origin_country,'(unknown)') AS k,
           count(*) AS n FROM latest GROUP BY 1, 2
    UNION ALL
    SELECT 'eta', CAST(eta_days AS VARCHAR), count(*) FROM latest
    WHERE eta_days IS NOT NULL GROUP BY 1, 2
    """).fetchall()
    origins = sorted(((k, n) for kind, k, n in tagged if kind == "origin"),
                     key=lambda kv: -kv[1])
    etas = sorted(((int(k), n) for kind, k, n in tagged if kind == "eta"))

    eta_rows = [{"eta_days": e, "n": c} for e, c in etas]
    eta_known = sum(r["n"] for r in eta_rows)
    modal = max(eta_rows, key=lambda r: r["n"]) if eta_rows else None

    # "(unknown)" is pulled out as its own bucket rather than left to compete
    # for a top-4 slot: it is not a country, and letting it rank as one is how
    # "most listings ship from unknown" becomes a geography claim.
    known = [(c, n) for c, n in origins if c != "(unknown)"]
    unknown = next((n for c, n in origins if c == "(unknown)"), 0)
    top = [{"country": c, "n": n} for c, n in known[:4]]
    other = sum(n for _c, n in known[4:])

    return {
        "top": top, "other": other, "unknown": unknown,
        "n": b.n, "n_countries": len(known),
        "eta": eta_rows, "eta_known": eta_known, "eta_modal": modal,
        "max_eta_days": b.knobs.max_eta_days,
    }, None


# --- ZONE 3: the whole population ---------------------------------------------

def _sanitise(value: object) -> object:
    """Strip any url out of a string value BEFORE it is encoded.

    A listing title can legitimately contain a url, and it would carry an
    `https://` substring straight into the page and past the self-contained
    guard -- which looks for exactly that substring and cannot tell a title
    from a stylesheet link.
    """
    if not isinstance(value, str):
        return value
    if "http://" in value or "https://" in value:
        return value.replace("https://", LINK_PLACEHOLDER).replace(
            "http://", LINK_PLACEHOLDER)
    return value


def explorer_payload(b: Build, *, limit: int | None = None) -> dict:
    """Every scraped listing, columnar and dictionary-encoded.

    Whitelisted columns only, copied field by field -- no row dict is passed
    through wholesale, so a column added to ROW_FIELDS later cannot silently
    join the payload.
    """
    rows = b.rows if limit is None else b.rows[:limit]
    extra = b.extra if limit is None else b.extra[:limit]

    columns: dict[str, list] = {f: [] for f in EXPLORER_FIELDS}
    for f in EXPLORER_EXTRA_FIELDS:
        columns[f] = []
    for r, ex in zip(rows, extra):
        for f in EXPLORER_FIELDS:
            columns[f].append(_sanitise(r.get(f)))
        for f in EXPLORER_EXTRA_FIELDS:
            columns[f].append(_sanitise(ex.get(f)))

    n = len(rows)
    encoded: dict[str, dict] = {}
    for name, values in columns.items():
        strings = [v for v in values if isinstance(v, str)]
        if strings and len(strings) == sum(1 for v in values if v is not None):
            distinct = sorted({v for v in strings})
            if len(distinct) * 3 <= max(n, 1):
                index = {v: i for i, v in enumerate(distinct)}
                encoded[name] = {
                    "t": "dict", "d": distinct,
                    "v": [None if v is None else index[v] for v in values]}
                continue
        encoded[name] = {"t": "raw", "v": values}

    return {"n": n, "fields": list(columns), "cols": encoded}


def panel_explorer(b: Build, *, limit: int | None = None) -> tuple[object, str | None]:
    """Every scraped listing, filterable. The panel that makes the rest
    checkable: any figure above can be walked back to the rows behind it."""
    if b.day is None:
        return None, "no eBay snapshots"
    payload = explorer_payload(b, limit=limit)
    facets = {
        "category": sorted({r.get("category") for r in b.rows if r.get("category")}),
        "leaf": sorted({r.get("leaf_category_id") for r in b.rows
                        if r.get("leaf_category_id")}),
        "origin": sorted({r.get("origin_country") for r in b.rows
                          if r.get("origin_country")}),
    }
    return {"payload": payload, "facets": facets, "n": b.n,
            "shown": payload["n"], "band": (b.knobs.price_from_pence,
                                            b.knobs.price_to_pence)}, None


# --- ZONE 4: what to do next ---------------------------------------------------

def panel_deepen_budget(b: Build) -> tuple[object, str | None]:
    """What Tier 2 has bought, and what the rest would cost.

    Deepening is the only way `price_band`, `min_margin` and `delivery` get a
    denominator, because all three read business-seller rows and only the
    item page says who is a business. This panel prices that as arithmetic
    rather than as a recommendation.
    """
    if b.day is None:
        return None, "no eBay snapshots"

    deepened = [r for r in b.rows if r.get("seller_type") is not None]
    business = [r for r in deepened if r.get("seller_type") == "business"]
    private = [r for r in deepened if r.get("seller_type") == "private"]

    per_leaf: dict[str, dict] = {}
    for r in deepened:
        leaf = r.get("leaf_category_id")
        if not leaf:
            continue
        slot = per_leaf.setdefault(leaf, {"leaf": leaf, "deepened": 0,
                                          "business": 0, "priced": 0, "eta": 0})
        slot["deepened"] += 1
        if r.get("seller_type") == "business":
            slot["business"] += 1
            if r.get("total_pence") is not None:
                slot["priced"] += 1
            if r.get("eta_days") is not None:
                slot["eta"] += 1

    for row in b.niches:
        leaf = row.leaf_category_id
        if leaf in per_leaf:
            per_leaf[leaf]["price_median_denominator"] = \
                row.metrics.price_median_denominator

    in_scope_rows = sorted(
        (v for k, v in per_leaf.items() if k in b.in_scope),
        key=lambda v: -v["deepened"])

    zero_denominator = sum(1 for row in b.niches
                           if row.metrics.price_median_denominator == 0)
    target = 30
    needed_business = target * len(b.in_scope)
    rate = (len(business) / len(deepened)) if deepened else None
    needed_listings = int(round(needed_business / rate)) if rate else None

    return {
        "deepened": len(deepened),
        "n": b.n,
        "business": len(business),
        "private": len(private),
        "leaves_touched": len(per_leaf),
        # Leaves Tier 2 reached, out of the leaves that exist to reach --
        # counted, not the literal 67 this used to divide by. See the note
        # on "leaves_swept" in panel_seller_footprint.
        "leaves_swept": len({r.get("leaf_category_id") for r in b.rows
                             if r.get("leaf_category_id")}),
        "rows": in_scope_rows,
        "zero_denominator": zero_denominator,
        "n_niches": len(b.niches),
        "target": target,
        "needed_business": needed_business,
        "business_rate": rate,
        "needed_listings": needed_listings,
        "multiple": (needed_listings / len(deepened)) if deepened and needed_listings else None,
        "closing": ("sell_through needs zero extra deepening — only a "
                    "second date."),
    }, None


def panel_sweep_footprint(b: Build) -> tuple[object, str | None]:
    """Did the sweep actually work, and what did it touch?

    `exit 0` is not "the night worked". A sweep where eBay blocked every
    shard still writes a partition and still exits 0, so the blocked map and
    the row count are the evidence, not the exit code.
    """
    if b.day is None:
        return None, "no eBay snapshots"

    per_date = b.store.sql("""
    SELECT (seen_at AT TIME ZONE 'UTC')::DATE AS d,
           count(*) AS raw_rows, count(DISTINCT source_id) AS listings,
           min(seen_at AT TIME ZONE 'UTC') AS first_seen,
           max(seen_at AT TIME ZONE 'UTC') AS last_seen
    FROM market WHERE source='ebay' GROUP BY 1 ORDER BY 1
    """).fetchall()
    date_fields = ("date", "raw_rows", "listings", "first_seen", "last_seen")

    tagged = b.sql("""
    SELECT 'leaf' AS kind, leaf_category_id AS k, count(*) AS n FROM latest
    WHERE leaf_category_id IS NOT NULL GROUP BY 1, 2
    UNION ALL
    SELECT 'shard', shard, count(*) FROM latest WHERE shard IS NOT NULL
    GROUP BY 1, 2
    """).fetchall()
    per_leaf = sorted(((k, n) for kind, k, n in tagged if kind == "leaf"),
                      key=lambda kv: -kv[1])
    per_shard = sorted(((k, n) for kind, k, n in tagged if kind == "shard"),
                       key=lambda kv: -kv[1])

    # Re-observations, and how many of them moved. Deliberately over `market`
    # rather than the deduped CTE -- the whole point is the rows the CTE
    # collapses.
    reobserved, reobserved_moved = b.store.sql(f"""
    SELECT count(*) FILTER (WHERE c > 1), count(*) FILTER (WHERE p > 1) FROM (
      SELECT source_id, count(*) AS c, count(DISTINCT price_pence) AS p
      FROM market
      WHERE source='ebay'
        AND (seen_at AT TIME ZONE 'UTC')::DATE = DATE '{b.day.isoformat()}'
      GROUP BY 1)
    """).fetchone()

    cp_path = b.data_dir / f"bulk_ebay_checkpoint_{b.day.isoformat()}.json"
    checkpoint: dict | None = None
    if cp_path.exists():
        try:
            checkpoint = json.loads(cp_path.read_text(encoding="utf-8"))
        except ValueError:
            checkpoint = None

    strays = sorted({r.get("leaf_category_id") for r in b.rows
                     if r.get("leaf_category_id")} - b.in_scope)
    stray_rows = [{"leaf": leaf, "n": n} for leaf, n in per_leaf
                  if leaf in set(strays)]

    return {
        "per_date": [dict(zip(date_fields, r)) for r in per_date],
        "per_leaf": [{"leaf": leaf, "n": n} for leaf, n in per_leaf],
        "per_shard": [{"shard": s, "n": n} for s, n in per_shard],
        "cap": SWEEP_LEAF_CAP,
        "reobserved": reobserved,
        "reobserved_moved": reobserved_moved,
        "checkpoint_path": str(cp_path),
        "checkpoint_found": checkpoint is not None,
        "shards_done": len(checkpoint.get("done", {})) if checkpoint else None,
        "blocked": checkpoint.get("blocked", {}) if checkpoint else None,
        "strays": stray_rows,
        "exit_note": ('exit 0 is not "the night worked" — a sweep where '
                      "eBay blocked every shard still writes a partition and "
                      "still exits 0, so check the blocked map and the row "
                      "count."),
    }, None


# --- ZONE 5: locked -------------------------------------------------------------

UNLOCK_TABLE: tuple[tuple[int, str], ...] = (
    (1, "population size, column coverage, store structure, price and origin "
        "distributions, competition density — everything above this zone."),
    (2, "sell_through as a WINDOW (str_source flips from lifetime to window "
        "on every niche), listing churn, price drift, seller entry and exit, "
        "and the first coverage trend."),
    (7, "new_7d from observation instead of the listed_at column, and a "
        "1-day delta on a rounded lifetime badge becomes worth acting on."),
    (14, "day-of-week effects and the first read on seasonality."),
)


def panel_date_ledger(b: Build) -> tuple[object, str | None]:
    """The dates on file and what each additional one unlocks.

    Always rendered, never locked -- this is the panel every locked card
    links to, so it has to hold whatever the store has, including nothing.
    """
    default_dir = config.load(env={}).DATA_DIR
    blockers = [
        ("scripts/run_daily.cmd has never been scheduled",
         "the file is written and verified (SWEEP -> DEEPEN -> BOARD), but "
         "no schtasks entry runs it, so no second date can arrive on its own."),
        ("run_daily.cmd sets no PHONELOOP_DATA_DIR",
         f"as written it would write to {default_dir}, which holds Vinted "
         "rows only — scheduling it unchanged starts a NEW eBay store rather "
         f"than extending {b.data_dir}."),
    ]
    gaps = None
    if len(b.dates) >= 2:
        gaps = [(b.dates[i + 1] - b.dates[i]).days
                for i in range(len(b.dates) - 1)]
    return {
        "dates": list(b.dates),
        "have": len(b.dates),
        "unlocks": [{"need": need, "what": what} for need, what in UNLOCK_TABLE],
        "blockers": blockers,
        "gaps": gaps,
        "consecutiveness": (
            "snapshots.difference() pairs two named dates; a gap does not "
            "merely thin the window, it breaks the pair — a 3-day hole "
            "makes the next difference a 4-day window silently labelled as "
            "one."),
    }, None


def panel_sell_through(b: Build) -> tuple[object, str | None]:
    """Knob 2 as a window rather than a lifetime average. Needs two dates.

    The scaffold renders at N=1 too: all 43 niches, every window cell an
    em-dash. A panel that appeared out of nowhere on date 2 would not be
    comparable against what is on the screen today.
    """
    rows = []
    for r in b.niches:
        k = _knob(r, "sell_through")
        rows.append({
            "niche": r.key,
            "leaf": r.leaf_category_id,
            "n_listings": r.metrics.n_listings,
            "str_window_bp": r.metrics.str_window_bp,
            "n_known": r.metrics.str_window_denominator,
            "str_source": r.str_source,
            "threshold_bp": b.knobs.str_threshold_bp,
            # The knob's own verdict. None = withheld (no window, or a
            # window too short for the 30-day threshold).
            "passed": k.passed if k is not None else None,
            "note": k.note if k is not None else "",
        })
    unreadable = sum(1 for r in rows if r["str_window_bp"] is None)
    pair = None
    if b.baseline is not None and b.diff is not None:
        pair = {"date_a": b.diff.date_a, "date_b": b.diff.date_b,
                "gap_days": b.baseline.gap_days,
                "long_enough": b.baseline.long_enough,
                "n_on_both": b.diff.n_on_both,
                "n_listings": b.diff.n_listings}
    payload = {"rows": rows, "unreadable": unreadable, "total": len(rows),
               "threshold_bp": b.knobs.str_threshold_bp, "pair": pair}
    if len(b.dates) < 2:
        return payload, unlock(need=2, have=len(b.dates), latest=b.day)
    return payload, None


def panel_population_churn(b: Build) -> tuple[object, str | None]:
    """Listings new, gone and persisting; price movement; sold_qty deltas.
    Needs two dates."""
    scaffold = {
        "would_compare": b.n,
        "series": ["listings new", "listings disappeared", "listings persisting"],
    }
    if len(b.dates) < 2 or b.diff is None:
        return scaffold, unlock(need=2, have=len(b.dates), latest=b.day)

    prev = {r["source_id"]: r for r in b.store.rows_on(b.dates[-2])}
    now = {r["source_id"]: r for r in b.rows}
    new = sorted(set(now) - set(prev))
    gone = sorted(set(prev) - set(now))
    both = sorted(set(now) & set(prev))
    moved = [sid for sid in both
             if now[sid].get("price_pence") is not None
             and prev[sid].get("price_pence") is not None
             and now[sid]["price_pence"] != prev[sid]["price_pence"]]
    return {
        "new": len(new), "gone": len(gone), "persisting": len(both),
        "price_moved": len(moved), "price_known": sum(
            1 for sid in both
            if now[sid].get("price_pence") is not None
            and prev[sid].get("price_pence") is not None),
        "sold_known": b.diff.n_known, "sold_listings": b.diff.n_listings,
        "sold_pair": f"{b.diff.date_a} -> {b.diff.date_b}",
        "series": scaffold["series"],
    }, None


def panel_coverage_trend(b: Build) -> tuple[object, str | None]:
    """Each column's fill rate, one line per snapshot date. Needs two.

    A drop in a column's fill rate between two dates is the earliest possible
    signal that eBay changed its markup or started blocking.
    """
    columns = list(ROW_FIELDS)
    scaffold = {"columns": columns, "dates": list(b.dates), "series": None}
    if len(b.dates) < 2:
        return scaffold, unlock(need=2, have=len(b.dates), latest=b.day)

    series: dict[str, list] = {}
    for d in b.dates:
        rows = b.store.rows_on(d)
        n = len(rows) or 1
        for c in columns:
            series.setdefault(c, []).append(
                sum(1 for r in rows if r.get(c) is not None) / n)
    scaffold["series"] = series
    return scaffold, None
