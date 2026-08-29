"""Deterministic JSON for the dashboard bundle.

Deterministic because `scripts/export_knob_golden.py` commits its output and
a test regenerates it and demands byte equality: a dict that serialised in a
different key order on a different day would make that alarm cry wolf every
run, and an alarm that cries wolf gets deleted.

The encoder deliberately refuses anything it does not recognise rather than
falling back to `str(obj)`. A `Decimal` silently becoming the string
"12.5" is the kind of type drift that reaches the browser as `"12.5" > 5`
being false, and nothing upstream would have gone red.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path


def _default(obj: object) -> object:
    if is_dataclass(obj) and not isinstance(obj, type):
        # Every dataclass in this codebase (Metrics, KnobResult, BoardRow,
        # ListingDelta, Difference) is a plain data record, and several of the
        # population panels return them directly rather than dicts. Recursing
        # into them is the deliberate handler, not a fallback.
        return asdict(obj)
    if isinstance(obj, datetime):
        # Always UTC ISO-8601. `rows_on` hands back tz-aware timestamps; a
        # naive one here would be ambiguous, so it is stamped as UTC rather
        # than guessed at.
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        # DuckDB hands back Decimal for some aggregates. Money in this
        # codebase is integer pence, so a Decimal here is a ratio; float is
        # the honest target type and the precision loss is irrelevant at the
        # scale a coverage fraction is read.
        return float(obj)
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    raise TypeError(
        f"{type(obj).__name__} has no agreed JSON form in the dashboard bundle. "
        "Add one to phoneloop.dashexport.jsonio._default deliberately rather "
        "than letting it stringify: a type that reaches the browser as an "
        "unexpected string compares wrong and nothing goes red."
    )


def _scrub(obj: object) -> object:
    """Replace non-finite floats with None, recursively.

    JSON has no NaN or Infinity. Python's encoder emits the bare literals
    `NaN` / `Infinity`, which are valid to `json.loads` and a syntax error to
    `JSON.parse` in every browser -- so the payload would parse in the tests
    and fail on the page. A non-finite coverage or ratio is an unknown, and
    unknown is null everywhere else in this bundle.
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub(v) for v in obj]
    return obj


def dumps(obj: object, *, indent: int | None = None) -> str:
    """Canonical JSON: sorted keys, no NaN, compact separators when unindented."""
    return json.dumps(
        _scrub(obj),
        default=_default,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        indent=indent,
        separators=(",", ":") if indent is None else None,
    )


def write_json(path: Path | str, obj: object, *, indent: int | None = None) -> int:
    """Write `obj` to `path` atomically. Returns the byte size written.

    Atomic because the nightly deploy reads this directory straight after the
    exporter writes it: a half-written file that still parses (a truncated
    array closes on the next `]`) would publish a silently short dataset.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = dumps(obj, indent=indent)
    if indent is not None:
        # A pretty-printed file is one somebody commits and reads in a diff, so
        # it gets the trailing newline every other text file has. The compact
        # shipping payloads do not: they are parsed, never read.
        text += "\n"
    payload = text.encode("utf-8")

    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return len(payload)


def round_floats(obj: object, dp: int = 6) -> object:
    """Round every float in a structure, recursively.

    Used by the columnar encoders and nowhere else. A coverage of
    0.03333333333333333 is 18 characters of JSON carrying about 5 characters
    of meaning -- every denominator in this store is an integer under 40,000,
    so nothing past six decimal places is a measurement.

    Deliberately NOT done inside `dumps`: a serialiser that quietly changes
    values is a serialiser nobody can reason about, and the payloads that use
    this declare `float_dp` so the reader knows it happened.
    """
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        return obj if not math.isfinite(obj) else round(obj, dp)
    if isinstance(obj, dict):
        return {k: round_floats(v, dp) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [round_floats(v, dp) for v in obj]
    return obj
