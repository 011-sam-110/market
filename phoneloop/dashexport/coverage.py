"""The honesty payload: what is in the store, and what is missing from it.

This module computes nothing. Every figure below comes from a panel function
in `phoneloop.population` that already has tests pinning it. The whole job
here is serialisation plus a small amount of re-shaping so the dashboard can
put four specific things where they cannot be missed:

- **Per-department seller coverage as an array, never a mean.** The global
  figure is around 87%, which reads as "mostly fine". Cut by department, one
  department carries the entire deficit -- a different problem with a
  different fix, and invisible to any single tile.
- **The green-because-broken list.** `score = passed - failed`, so a missing
  column scores exactly like a satisfied one. A niche can top the board
  because almost nothing about it is known.
- **Stored vs computed `identity_tier`.** The parquet column and
  `identity.tier_for` disagree on tens of thousands of rows; anything reading
  the raw column undercounts clustered products massively.
- **Constant columns.** `promoted` is one value on every row. A "% promoted"
  tile would report a meaningless 100% forever.
"""

from __future__ import annotations

from phoneloop import population
from phoneloop.population import TIER2_COLUMNS, Build

# Every panel exported, in reading order. Each is `(b) -> (data, note)`.
# `panel_explorer` is deliberately absent: it exists to embed a row payload in
# a self-contained HTML page, and this bundle ships the full population
# through `columnar.encode_listings` instead.
PANELS: tuple[tuple[str, str], ...] = (
    ("provenance", "Which store, which dates, how big"),
    ("trust_ledger", "How many knob verdicts rest on how much evidence"),
    ("knob_matrix", "Every knob against every niche"),
    ("green_because_broken", "Niches scoring well because data is absent"),
    ("coverage_ladder", "Every stored column by how much of it is there"),
    ("category_integrity", "The same question cut by department"),
    ("constants_and_contradictions", "Columns with one value, and three "
                                     "places the data contradicts its contract"),
    ("competition_density", "Sellers per niche"),
    ("seller_entry_exit", "Who arrived and who left between snapshots"),
    ("seller_footprint", "How wide a seller ranges across leaves"),
    ("landed_price", "Price with postage, business sellers only"),
    ("leaf_price_map", "Where each leaf sits on price"),
    ("price_drift", "Movement between snapshots"),
    ("saturation_calibration", "What the competitor cap actually cuts"),
    ("demand_evidence", "What the sold counter supports"),
    ("origin_and_fulfilment", "Where stock ships from"),
    ("deepen_budget", "Tier-2 coverage and what full coverage would cost"),
    ("sweep_footprint", "What the sweep actually reached"),
    ("date_ledger", "Dates on file and what each new one unlocks"),
    ("sell_through", "The knob that is still withholding its verdict"),
    ("population_churn", "Which listings returned"),
    ("coverage_trend", "Whether coverage is improving"),
)


def _run_panel(b: Build, name: str) -> dict:
    fn = getattr(population, f"panel_{name}")
    try:
        data, note = fn(b)
    except Exception as exc:  # noqa: BLE001 - a broken panel must not kill the bundle
        return {
            "id": name,
            "state": "error",
            "data": None,
            "note": f"{type(exc).__name__}: {exc}",
        }
    return {
        "id": name,
        # A panel returns data=None when it cannot answer -- no snapshots, not
        # enough dates. That is "unavailable", and the note says why. It is
        # never rendered as a zero, because a zero is an answer and this is not.
        "state": "live" if data is not None else "unavailable",
        "data": data,
        "note": note,
    }


def _column_table(ladder: list[dict] | None) -> list[dict]:
    """The coverage ladder, plus which pipeline tier is responsible for each
    column. The cliff between tier 1 and tier 2 IS the shape of the data, so
    the tier is a field the UI can group and colour by rather than something
    the reader has to know."""
    if not ladder:
        return []
    out = []
    for entry in ladder:
        col = entry.get("col")
        if entry.get("non_null") == 0:
            tier = "dead"
        elif col in TIER2_COLUMNS:
            tier = "tier2_deepen"
        elif col in ("content_hash", "date (derived)"):
            tier = "derived"
        else:
            tier = "tier1_search_row"
        out.append({**entry, "tier": tier})
    return out


def export_coverage(b: Build) -> dict:
    """Every population panel, plus the four call-outs the UI is built around."""
    panels = {name: _run_panel(b, name) for name, _desc in PANELS}
    descriptions = {name: desc for name, desc in PANELS}

    ladder_panel = panels.get("coverage_ladder", {})
    ladder_data = (ladder_panel.get("data") or {}) if ladder_panel else {}
    columns = _column_table(ladder_data.get("ladder"))

    integrity = (panels.get("category_integrity", {}).get("data") or {})
    per_department = integrity.get("rows") or []

    green = (panels.get("green_because_broken", {}).get("data") or {})
    contradictions = (panels.get("constants_and_contradictions", {}).get("data") or {})
    deepen = (panels.get("deepen_budget", {}).get("data") or {})

    return {
        "n_listings": b.n,
        "latest_date": b.day,
        "panels": [
            {**panels[name], "title": descriptions[name]}
            for name, _d in PANELS
        ],
        "columns": columns,
        "tier2_columns": list(TIER2_COLUMNS),
        "density_min_seller_coverage": population.DENSITY_MIN_SELLER_COVERAGE,
        # --- the four call-outs, lifted out of their panels so the UI does not
        # have to know which panel each one lives in ---
        "call_outs": {
            "per_department_seller_coverage": per_department,
            "global_seller_coverage": integrity.get("global_seller_cov"),
            "worst_department": integrity.get("worst_category"),
            "green_because_broken": green,
            "constants_and_contradictions": contradictions,
            "deepen_budget": deepen,
        },
        "note": "Nothing in this payload is computed here. Every figure is the "
                "return value of a tested panel in phoneloop.population, "
                "serialised. A second implementation of a number is a second "
                "chance to disagree with the first.",
    }
