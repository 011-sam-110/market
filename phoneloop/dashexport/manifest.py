"""What this bundle is, which store it came from, and what it cannot yet show.

The manifest is the first thing the dashboard reads and the thing that stops
it lying about its own freshness. Two fields carry that weight:

`dates` is every snapshot date on file, so the UI can say "2 days" instead of
implying a trend from a single pair; and `unlocks` says, in the project's own
words, which reads are live at this many dates and which are still locked and
at how many they open. A locked panel that renders as an empty chart reads as
"no activity". A locked panel that renders as "opens at 7 dates, 5 to go"
reads as the truth.
"""

from __future__ import annotations

from datetime import datetime

from phoneloop.population import UNLOCK_TABLE, Build


def _counts_by_date(b: Build) -> list[dict]:
    """Raw observation rows and distinct listings, per snapshot date.

    Both, not one: eBay rows use snapshot semantics (every observation is
    written, not only changed ones), so "rows" is bigger than "listings" and
    a dashboard quoting either alone invites the reader to compare it against
    the other one somewhere else on the page.
    """
    if b.day is None:
        # An empty store has no parquet files for the view to read. Returning
        # an empty list is the honest answer; letting DuckDB raise here would
        # turn "the store is empty" into "the exporter is broken".
        return []
    rel = b.store.sql(
        "SELECT (seen_at AT TIME ZONE 'UTC')::DATE AS d, "
        "count(*) AS n_rows, count(DISTINCT source_id) AS n_listings "
        "FROM market WHERE source = 'ebay' GROUP BY 1 ORDER BY 1"
    )
    return [
        {"date": d, "rows": int(n_rows), "listings": int(n_listings)}
        for d, n_rows, n_listings in rel.fetchall()
    ]


def build_manifest(b: Build, *, git_sha: str, generated_at: datetime,
                   schema_version: int, store_path: str) -> dict:
    """The bundle header.

    `generated_at` is a parameter rather than a clock read so the exporter
    stamps one time across every file in a run, and so a test can pin the
    output byte-for-byte.
    """
    n_dates = len(b.dates)
    unlocks = [
        {
            "at_dates": at,
            "unlocks": text,
            "state": "live" if n_dates >= at else "locked",
            "dates_to_go": max(0, at - n_dates),
        }
        for at, text in UNLOCK_TABLE
    ]

    return {
        "schema_version": schema_version,
        "generated_at": generated_at,
        "git_sha": git_sha,
        "store_path": store_path,
        "source": "ebay",
        "dates": list(b.dates),
        "n_dates": n_dates,
        "latest_date": b.day,
        "counts_by_date": _counts_by_date(b),
        "latest": {
            "listings": b.n,
            # The denominator almost every figure on the page divides by, named
            # here once so a panel never has to invent its own.
            "denominator_field": "latest.listings",
        },
        "in_scope_leaves": sorted(b.in_scope),
        "n_in_scope_leaves": len(b.in_scope),
        "has_difference": b.diff is not None,
        "difference_window": (
            {"date_a": b.diff.date_a, "date_b": b.diff.date_b,
             "n_listings": b.diff.n_listings, "n_known": b.diff.n_known,
             "coverage": b.diff.coverage}
            if b.diff is not None else None
        ),
        "unlocks": unlocks,
        "empty": b.day is None,
    }
