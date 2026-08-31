"""What a night's sweep actually touched, and whether two nights can be compared.

A day-over-day difference makes one assumption it never states: that both
nights swept the SAME population. Nothing in the pipeline has enforced that.

The failure is not hypothetical and it is not subtle in its effect, only in
its appearance. `--pages-per-shard` was 10 in `run_daily.cmd` while nights one
and two were both swept at 3. Left at 10, the first scheduled night would have
swept roughly three times the population, and Tier 3 would have booked pages
four to ten as thousands of overnight arrivals. Every count would have looked
healthy. The board would have reported a market that grew 200% in a night.

So a scale change is safe only when the board can see it. This module reads
each night's own checkpoint, recovers which leaves that night swept and how
deep it went into each one, and compares two nights. A difference across two
unlike footprints is not wrong to compute. It is wrong to read as change over
time, and the board has to say so on its face.

**The checkpoint is the evidence, not the config file.** `run_daily.cmd` says
what was asked for. The checkpoint says what happened, including the shards
eBay blocked, and a blocked shard is exactly the case where the two differ.

Key shape, as `bulk_ebay.py` writes it:

    "sacat:26677:15-55:p1"  ->  leaf 26677, price band 15-55, page 1

`done[key] = n` means the shard was swept and returned n rows, so `0` means
"swept, genuinely nothing there". A blocked shard is left OUT of `done`
entirely and recorded in `blocked`. Both distinctions matter here: a leaf that
returned nothing was still measured, and a leaf that was blocked was not.

**Not every population change reaches the shard keys.** A key carries the
leaf, the band and the page, so a change to those three is visible here by
construction. The results VIEW is not in the key and changes what a page
returns: measured 2026-08-31, one leaf came back with a seller line on 97.3%
of rows in list view and 3.1% in gallery, under identical shard keys. So
`bulk_ebay.py` writes a `sweep` block naming the view, the configured
`pages_per_shard` and the price band, and `compare` refuses to certify a pair
whose sweep blocks disagree. A night written before that block existed reads
as an unknown shape, not as a match -- two such nights still compare (neither
is known, and nothing distinguishes them), but an unknown against a known
does not.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

__all__ = [
    "Footprint",
    "Comparability",
    "checkpoint_path",
    "parse_checkpoint",
    "read_footprint",
    "compare",
]

# `sacat:<leaf>:<band>:p<page>`. Anchored on purpose. A key this does not
# match is reported as unrecognised rather than skipped -- see `parse_checkpoint`.
_KEY_RE = re.compile(r"^sacat:(?P<leaf>[^:]+):(?P<band>[^:]+):p(?P<page>\d+)$")


@dataclass(frozen=True)
class Footprint:
    """Which leaves one night swept, and how deep it went into each.

    `pages` maps a leaf id to the deepest page number that leaf actually
    completed. It is the per-leaf depth, not the configured
    `--pages-per-shard`: a leaf that ran out of results at page 2 under a
    setting of 10 records 2, which is the honest figure and is also what
    makes a real depth change distinguishable from a leaf that simply has
    fewer listings than the cap allows.
    """

    day: date
    #: leaf id -> deepest page completed for that leaf
    pages: dict[str, int]
    #: leaf id -> rows returned across every page of that leaf
    rows: dict[str, int]
    #: shard keys the checkpoint recorded as blocked, verbatim
    blocked: dict[str, object]
    #: keys in `done` that do not match the documented shape
    unrecognised: tuple[str, ...]
    #: True when this night wrote a checkpoint at all
    found: bool
    #: the `sweep` block verbatim -- view, pages_per_shard, price_band.
    #: Empty for a night written before bulk_ebay.py recorded one, which is
    #: "shape unknown", never "shape matched".
    sweep: dict = field(default_factory=dict)

    @property
    def view(self) -> str | None:
        """The results view this night swept in, or None when unrecorded."""
        v = self.sweep.get("view")
        return v if isinstance(v, str) else None

    @property
    def n_leaves(self) -> int:
        return len(self.pages)

    @property
    def deepest_page(self) -> int:
        """The deepest page reached on ANY leaf. The closest single number to
        "what --pages-per-shard was set to", and never larger than it."""
        return max(self.pages.values(), default=0)

    @property
    def shards_done(self) -> int:
        return sum(self.pages.values())

    def as_dict(self) -> dict:
        return {
            "day": self.day.isoformat(),
            "found": self.found,
            "n_leaves": self.n_leaves,
            "deepest_page": self.deepest_page,
            "shards_done": self.shards_done,
            "n_blocked": len(self.blocked),
            "unrecognised": list(self.unrecognised),
            "sweep": dict(self.sweep),
        }


@dataclass(frozen=True)
class Comparability:
    """Whether two nights swept a population that can be differenced.

    `comparable` False does NOT mean the difference is wrong. It means the
    difference measures the sweep as well as the market, and no consumer may
    present it as change over time.
    """

    date_a: date
    date_b: date
    comparable: bool
    #: leaf ids swept on b and not on a
    leaves_added: tuple[str, ...]
    #: leaf ids swept on a and not on b
    leaves_removed: tuple[str, ...]
    #: leaf id -> (pages on a, pages on b), for leaves whose depth moved
    depth_changed: dict[str, tuple[int, int]]
    #: field name -> (value on a, value on b), for sweep settings that moved
    sweep_changed: dict[str, tuple[object, object]]
    #: one sentence, in the board's own voice, or "" when comparable
    note: str

    @property
    def n_shared_leaves(self) -> int:
        return len(self.leaves_added) + len(self.leaves_removed)

    def as_dict(self) -> dict:
        return {
            "date_a": self.date_a.isoformat(),
            "date_b": self.date_b.isoformat(),
            "comparable": self.comparable,
            "leaves_added": list(self.leaves_added),
            "leaves_removed": list(self.leaves_removed),
            "depth_changed": {k: list(v) for k, v in self.depth_changed.items()},
            "sweep_changed": {k: list(v) for k, v in self.sweep_changed.items()},
            "note": self.note,
        }


def checkpoint_path(data_dir: Path | str, day: date) -> Path:
    return Path(data_dir) / f"bulk_ebay_checkpoint_{day.isoformat()}.json"


def parse_checkpoint(payload: dict, day: date) -> Footprint:
    """A checkpoint dict into a Footprint.

    An unrecognised key is collected, never dropped. If `bulk_ebay.py` ever
    changes its key shape, every night starts parsing to an empty footprint,
    every pair of nights looks identical, and this guard reports "comparable"
    for the rest of time. `unrecognised` is what makes that visible, and
    `compare` refuses to certify a pair when either side carries one.
    """
    done = payload.get("done") or {}
    pages: dict[str, int] = {}
    rows: dict[str, int] = {}
    unrecognised: list[str] = []

    for key, value in done.items():
        m = _KEY_RE.match(str(key))
        if m is None:
            unrecognised.append(str(key))
            continue
        leaf = m.group("leaf")
        page = int(m.group("page"))
        pages[leaf] = max(pages.get(leaf, 0), page)
        # `value` is the row count the shard returned. A non-integer is not
        # worth guessing at, so it contributes nothing rather than a zero
        # that would read as a measured empty page.
        if isinstance(value, int):
            rows[leaf] = rows.get(leaf, 0) + value

    blocked = payload.get("blocked") or {}
    sweep = payload.get("sweep")
    return Footprint(
        day=day,
        pages=pages,
        rows=rows,
        blocked=dict(blocked) if isinstance(blocked, dict) else {},
        unrecognised=tuple(sorted(unrecognised)),
        found=True,
        sweep=dict(sweep) if isinstance(sweep, dict) else {},
    )


def read_footprint(data_dir: Path | str, day: date) -> Footprint:
    """One night's footprint, off its own checkpoint.

    A missing or unreadable checkpoint returns `found=False` rather than an
    empty footprint, because "that night swept nothing" and "that night left
    no record of what it swept" are different claims and only one of them is
    a reason to distrust a difference.
    """
    path = checkpoint_path(data_dir, day)
    if not path.exists():
        return Footprint(day=day, pages={}, rows={}, blocked={},
                         unrecognised=(), found=False)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return Footprint(day=day, pages={}, rows={}, blocked={},
                         unrecognised=(), found=False)
    if not isinstance(payload, dict):
        return Footprint(day=day, pages={}, rows={}, blocked={},
                         unrecognised=(), found=False)
    return parse_checkpoint(payload, day)


def _shape(value: object) -> str:
    """A sweep setting as it should read in a sentence.

    A night written before `bulk_ebay.py` recorded a sweep block carries
    nothing for the field, and "unrecorded" is the honest word for that.
    "None" would read as a value the sweep actually held.
    """
    return "unrecorded" if value is None else repr(value)


def compare(a: Footprint, b: Footprint) -> Comparability:
    """Can these two nights be differenced as change over time?

    Five things break comparability, and each produces its own sentence:

    1. either night left no checkpoint -- nothing is known about what it swept;
    2. either checkpoint carried a key this module does not recognise -- the
       footprint is incomplete and a silent "comparable" would be worse than
       an admitted unknown;
    3. the set of leaves moved;
    4. the depth into a shared leaf moved;
    5. the recorded sweep shape moved -- the results view above all, which
       changes what a page returns without touching a single shard key.

    A leaf whose depth changed because it simply ran out of results is
    indistinguishable here from one whose cap was raised, and this reports
    both. That is the safe direction to be wrong in: it over-reports a
    population change and never hides one.
    """
    if not a.found or not b.found:
        missing = [f.day.isoformat() for f in (a, b) if not f.found]
        return Comparability(
            date_a=a.day, date_b=b.day, comparable=False,
            leaves_added=(), leaves_removed=(), depth_changed={},
            sweep_changed={},
            note=(f"No sweep checkpoint exists for {' and '.join(missing)}. "
                  "The board cannot say whether both nights swept the same "
                  "population, so it does not present this difference as "
                  "change over time."),
        )

    if a.unrecognised or b.unrecognised:
        return Comparability(
            date_a=a.day, date_b=b.day, comparable=False,
            leaves_added=(), leaves_removed=(), depth_changed={},
            sweep_changed={},
            note=("A sweep checkpoint carried a shard key this board does not "
                  "recognise, so the footprint it recovered is incomplete. "
                  "Read bulk_ebay.py's key shape before trusting this "
                  "difference."),
        )

    # Ahead of the leaf and depth comparisons on purpose. A sweep run in a
    # different view is not "the same leaves plus a bit of drift" -- every row
    # on every shared leaf came off a different card -- so it deserves its own
    # sentence rather than being folded into a depth report.
    keys = sorted(set(a.sweep) | set(b.sweep))
    sweep_changed = {k: (a.sweep.get(k), b.sweep.get(k))
                     for k in keys if a.sweep.get(k) != b.sweep.get(k)}
    if sweep_changed:
        moved = ", ".join(
            f"{k} {_shape(old)} -> {_shape(new)}"
            for k, (old, new) in sorted(sweep_changed.items()))
        return Comparability(
            date_a=a.day, date_b=b.day, comparable=False,
            leaves_added=(), leaves_removed=(), depth_changed={},
            sweep_changed=sweep_changed,
            note=(f"The two nights swept under different settings: {moved}. "
                  "A setting that changes what a page returns changes the "
                  "population without changing a single shard key, so this "
                  "board does not present the difference as change over "
                  "time."),
        )

    leaves_a, leaves_b = set(a.pages), set(b.pages)
    added = tuple(sorted(leaves_b - leaves_a))
    removed = tuple(sorted(leaves_a - leaves_b))
    depth_changed = {
        leaf: (a.pages[leaf], b.pages[leaf])
        for leaf in sorted(leaves_a & leaves_b)
        if a.pages[leaf] != b.pages[leaf]
    }

    if not added and not removed and not depth_changed:
        return Comparability(
            date_a=a.day, date_b=b.day, comparable=True,
            leaves_added=(), leaves_removed=(), depth_changed={},
            sweep_changed={}, note="",
        )

    parts: list[str] = []
    if added:
        parts.append(f"{len(added)} leaves entered the sweep")
    if removed:
        parts.append(f"{len(removed)} leaves left it")
    if depth_changed:
        deepest_a = max((v[0] for v in depth_changed.values()), default=0)
        deepest_b = max((v[1] for v in depth_changed.values()), default=0)
        parts.append(
            f"{len(depth_changed)} leaves changed depth, the deepest going "
            f"from page {deepest_a} to page {deepest_b}")

    return Comparability(
        date_a=a.day, date_b=b.day, comparable=False,
        leaves_added=added, leaves_removed=removed, depth_changed=depth_changed,
        sweep_changed={},
        note=(f"The two nights did not sweep the same population. "
              f"{'. '.join(p[0].upper() + p[1:] for p in parts)}. "
              "A count that moves between them measures the sweep as well as "
              "the market, so this board does not present it as change over "
              "time."),
    )
