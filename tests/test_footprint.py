"""The guard that makes a scale change safe to read.

Every test here exists because of one specific way a difference can lie about
the market when the sweep is what actually moved.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from phoneloop import footprint as fp


DAY_A = date(2026, 8, 29)
DAY_B = date(2026, 8, 30)


def checkpoint(leaves: dict[str, int], *, blocked: dict | None = None,
               band: str = "15-55", rows_per_page: int = 240) -> dict:
    """A checkpoint in bulk_ebay.py's own key shape.

    `leaves` maps a leaf id to how many pages that leaf completed.
    """
    done = {}
    for leaf, pages in leaves.items():
        for page in range(1, pages + 1):
            done[f"sacat:{leaf}:{band}:p{page}"] = rows_per_page
    return {"done": done, "blocked": blocked or {}}


def write(tmp_path, day: date, payload: dict) -> None:
    fp.checkpoint_path(tmp_path, day).write_text(
        json.dumps(payload), encoding="utf-8")


# --- reading a footprint ----------------------------------------------------

def test_the_real_checkpoint_shape_parses():
    """The key shape this module keys on is the one bulk_ebay.py writes.

    Taken verbatim from the production checkpoint of 2026-08-30. If
    bulk_ebay.py changes its key shape, this is the test that says so.
    """
    payload = {"done": {"sacat:26677:15-55:p1": 240,
                        "sacat:26677:15-55:p2": 240,
                        "sacat:26677:15-55:p3": 118,
                        "sacat:20444:15-55:p1": 240},
               "blocked": {}}
    f = fp.parse_checkpoint(payload, DAY_A)

    assert f.pages == {"26677": 3, "20444": 1}
    assert f.rows == {"26677": 598, "20444": 240}
    assert f.n_leaves == 2
    assert f.deepest_page == 3
    assert f.shards_done == 4
    assert f.unrecognised == ()


def test_a_swept_but_empty_shard_is_not_a_missing_shard():
    """`done[key] = 0` means swept and genuinely nothing there.

    Dropping it would make an exhausted leaf look like an unswept one, and
    the next night's identical sweep would then read as a leaf entering the
    board.
    """
    payload = {"done": {"sacat:246:15-55:p1": 0}, "blocked": {}}
    f = fp.parse_checkpoint(payload, DAY_A)

    assert f.pages == {"246": 1}
    assert f.rows == {"246": 0}
    assert f.n_leaves == 1


def test_an_unrecognised_key_is_collected_never_dropped():
    """A key-shape change must not make every night parse to nothing.

    If it did, every pair of nights would look identical and this guard would
    report comparable for the rest of time -- the exact silent failure it
    exists to prevent.
    """
    payload = {"done": {"sacat:246:15-55:p1": 240,
                        "leaf=246&band=15-55&page=1": 240},
               "blocked": {}}
    f = fp.parse_checkpoint(payload, DAY_A)

    assert f.unrecognised == ("leaf=246&band=15-55&page=1",)
    assert f.pages == {"246": 1}


def test_a_missing_checkpoint_is_not_an_empty_sweep(tmp_path):
    """"That night swept nothing" and "that night left no record" are
    different claims, and only one is a reason to distrust a difference."""
    f = fp.read_footprint(tmp_path, DAY_A)

    assert f.found is False
    assert f.n_leaves == 0


def test_an_unreadable_checkpoint_reads_as_not_found(tmp_path):
    fp.checkpoint_path(tmp_path, DAY_A).write_text("{ truncated", encoding="utf-8")
    f = fp.read_footprint(tmp_path, DAY_A)

    assert f.found is False


def test_read_footprint_round_trips_a_written_checkpoint(tmp_path):
    write(tmp_path, DAY_A, checkpoint({"26677": 3, "20444": 2}))
    f = fp.read_footprint(tmp_path, DAY_A)

    assert f.found is True
    assert f.pages == {"26677": 3, "20444": 2}


# --- comparing two nights ---------------------------------------------------

def test_two_identical_nights_are_comparable(tmp_path):
    write(tmp_path, DAY_A, checkpoint({"26677": 3, "20444": 3}))
    write(tmp_path, DAY_B, checkpoint({"26677": 3, "20444": 3}))

    c = fp.compare(fp.read_footprint(tmp_path, DAY_A),
                   fp.read_footprint(tmp_path, DAY_B))

    assert c.comparable is True
    assert c.note == ""
    assert c.depth_changed == {}


def test_raising_pages_per_shard_breaks_comparability(tmp_path):
    """THE scale-change trap, in one test.

    3 -> 10 pages sweeps roughly three times the population. Tier 3 books
    pages 4 to 10 as thousands of overnight arrivals and every count looks
    healthy. This is the assertion that stops that reading.
    """
    write(tmp_path, DAY_A, checkpoint({"26677": 3, "20444": 3}))
    write(tmp_path, DAY_B, checkpoint({"26677": 10, "20444": 10}))

    c = fp.compare(fp.read_footprint(tmp_path, DAY_A),
                   fp.read_footprint(tmp_path, DAY_B))

    assert c.comparable is False
    assert c.depth_changed == {"26677": (3, 10), "20444": (3, 10)}
    assert "page 3 to page 10" in c.note
    assert "does not present it as change over time" in c.note


def test_adding_leaves_breaks_comparability(tmp_path):
    """Re-crawling the category tree at a higher --max-leaves is the other
    scale lever, and it moves the population just as hard."""
    write(tmp_path, DAY_A, checkpoint({"26677": 3}))
    write(tmp_path, DAY_B, checkpoint({"26677": 3, "20444": 3, "262975": 3}))

    c = fp.compare(fp.read_footprint(tmp_path, DAY_A),
                   fp.read_footprint(tmp_path, DAY_B))

    assert c.comparable is False
    assert c.leaves_added == ("20444", "262975")
    assert c.leaves_removed == ()
    assert "2 leaves entered the sweep" in c.note


def test_a_blocked_leaf_reads_as_a_population_change(tmp_path):
    """A blocked shard is left OUT of `done`, so a night eBay partly blocked
    swept a smaller population -- which is exactly a reason not to read the
    difference as market movement."""
    write(tmp_path, DAY_A, checkpoint({"26677": 3, "20444": 3}))
    write(tmp_path, DAY_B, {**checkpoint({"26677": 3}),
                            "blocked": {"sacat:20444:15-55:p1": "captcha"}})

    c = fp.compare(fp.read_footprint(tmp_path, DAY_A),
                   fp.read_footprint(tmp_path, DAY_B))

    assert c.comparable is False
    assert c.leaves_removed == ("20444",)


def test_a_missing_checkpoint_refuses_to_certify(tmp_path):
    write(tmp_path, DAY_B, checkpoint({"26677": 3}))

    c = fp.compare(fp.read_footprint(tmp_path, DAY_A),
                   fp.read_footprint(tmp_path, DAY_B))

    assert c.comparable is False
    assert "No sweep checkpoint exists for 2026-08-29" in c.note


def test_an_unrecognised_key_refuses_to_certify(tmp_path):
    """Two nights whose footprints both parsed to nothing would compare
    equal. That must never read as comparable."""
    write(tmp_path, DAY_A, {"done": {"leaf=246&page=1": 240}, "blocked": {}})
    write(tmp_path, DAY_B, {"done": {"leaf=246&page=1": 240}, "blocked": {}})

    c = fp.compare(fp.read_footprint(tmp_path, DAY_A),
                   fp.read_footprint(tmp_path, DAY_B))

    assert c.comparable is False
    assert "does not recognise" in c.note


def test_a_leaf_that_ran_out_of_results_is_reported_not_hidden(tmp_path):
    """A leaf with fewer listings than the cap records a shallower depth, and
    this cannot tell that apart from a raised cap. It reports both.

    That is the safe direction: it over-reports a population change and never
    hides one. This test pins the choice so nobody 'fixes' it into silence.
    """
    write(tmp_path, DAY_A, checkpoint({"246": 3}))
    write(tmp_path, DAY_B, checkpoint({"246": 2}))

    c = fp.compare(fp.read_footprint(tmp_path, DAY_A),
                   fp.read_footprint(tmp_path, DAY_B))

    assert c.comparable is False
    assert c.depth_changed == {"246": (3, 2)}


def test_as_dict_is_json_serialisable(tmp_path):
    """The comparability travels to the browser in the deltas payload."""
    write(tmp_path, DAY_A, checkpoint({"26677": 3}))
    write(tmp_path, DAY_B, checkpoint({"26677": 10}))
    c = fp.compare(fp.read_footprint(tmp_path, DAY_A),
                   fp.read_footprint(tmp_path, DAY_B))

    round_tripped = json.loads(json.dumps(c.as_dict()))

    assert round_tripped["comparable"] is False
    assert round_tripped["depth_changed"] == {"26677": [3, 10]}


def test_the_production_checkpoint_parses_if_it_is_here():
    """A live check against the real store, skipped when it is absent.

    The fixture above claims to be the production shape. This is what proves
    the claim rather than repeating it.
    """
    from pathlib import Path
    prod = Path.home() / ".phoneloop" / "prod"
    path = fp.checkpoint_path(prod, date(2026, 8, 30))
    if not path.exists():
        pytest.skip("no production checkpoint on this machine")

    f = fp.read_footprint(prod, date(2026, 8, 30))

    assert f.found is True
    assert f.unrecognised == (), (
        "bulk_ebay.py writes a shard key phoneloop.footprint does not "
        "recognise, so every footprint it recovers is incomplete")
    assert f.n_leaves > 0
