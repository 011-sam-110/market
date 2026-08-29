"""JSON export of the whole niche-finder store, for the browser dashboard.

Every module here answers the same question in a different slice: what does
the store actually contain, and how much of it is real? Nothing in this
package computes a new figure. It reads `phoneloop.population.build()` once
and serialises what the already-tested board, metrics and population panels
return, because a second implementation of a number is a second chance to
disagree with the first -- which is exactly how the hardcoded `67`
denominator and the "(no rows)" board both happened.

Three rules the whole package holds to, and the tests pin:

1. **Every figure carries its denominator.** There is no path through this
   package that emits a value without the count it was computed over. A
   number without a denominator is unfalsifiable on screen.

2. **`denominator == 0` means UNKNOWN, never a real zero**, and unknown
   serialises as JSON `null` -- never `false`, never `0`. `passed=None` on a
   knob means "the data to judge this does not exist", which on a
   `score = passed - failed` board is a completely different thing from a
   failure, and the dashboard renders them differently.

3. **Nothing is capped silently.** Where a limit is applied, the payload
   carries the limit and how many rows it dropped, so the UI can say so.
"""

from __future__ import annotations

# Bumped on any BREAKING change to an exported file's shape. The dashboard
# refuses to render a bundle whose version it does not know, rather than
# rendering a field that moved as though it were missing.
SCHEMA_VERSION = 1

from phoneloop.dashexport.board_export import (  # noqa: E402
    export_fees,
    export_knobs,
    export_niches,
    product_payloads,
)
from phoneloop.dashexport.columnar import encode_listings, export_deltas  # noqa: E402
from phoneloop.dashexport.coverage import export_coverage  # noqa: E402
from phoneloop.dashexport.golden import build_golden  # noqa: E402
from phoneloop.dashexport.jsonio import dumps, round_floats, write_json  # noqa: E402
from phoneloop.dashexport.manifest import build_manifest  # noqa: E402

__all__ = [
    "SCHEMA_VERSION",
    "build_golden",
    "build_manifest",
    "dumps",
    "encode_listings",
    "export_coverage",
    "export_deltas",
    "export_fees",
    "export_knobs",
    "export_niches",
    "product_payloads",
    "round_floats",
    "write_json",
]
