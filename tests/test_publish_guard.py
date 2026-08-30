"""The guard that stops yesterday being republished as today.

`export_dashboard.py` writes into `dashboard/public/data`, and the Vite build
copies whatever is sitting there into `dist`. So an export that fails does not
produce a failed deploy — it produces a healthy deploy of yesterday's figures
with today's date nowhere on it. The board looks right, it reads as today, and
nothing anywhere goes red.

That makes `check_freshness` the only thing standing between a broken export
and a confidently wrong board, so its FAILURE paths are what these tests
exercise. A guard that has only ever been watched succeed is decoration.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import publish_dashboard as pub


@pytest.fixture
def store(tmp_path: Path) -> Path:
    """A store shaped the way the real one is: Hive `date=` partitions."""
    root = tmp_path / "store"
    for day in ("2026-08-27", "2026-08-28", "2026-08-29"):
        (root / "source=ebay" / f"date={day}").mkdir(parents=True)
    return root


def _write_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                    latest_date: str | None) -> Path:
    data = tmp_path / "bundle"
    data.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {} if latest_date is None else {
        "latest_date": latest_date}
    (data / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(pub, "DATA", data)
    return data


def test_the_newest_partition_is_the_store_date(store: Path):
    assert pub.store_latest_date(store) == "2026-08-29"


def test_a_store_with_no_partitions_has_no_date(tmp_path: Path):
    assert pub.store_latest_date(tmp_path) is None


def test_it_passes_when_the_bundle_matches_the_store(
    store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_manifest(tmp_path, monkeypatch, "2026-08-29")
    ok, why = pub.check_freshness(store)
    assert ok, why
    assert "2026-08-29" in why


def test_it_STOPS_when_the_export_did_not_run_tonight(
    store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The whole reason this guard exists.

    A sweep landed 2026-08-29 into the store; the export failed, so the bundle
    on disk still says 2026-08-28. Publishing it would put yesterday's board
    up under today's date.
    """
    _write_manifest(tmp_path, monkeypatch, "2026-08-28")
    ok, why = pub.check_freshness(store)
    assert not ok
    assert "2026-08-28" in why and "2026-08-29" in why
    assert "as though it were today" in why


def test_it_stops_when_there_is_no_bundle_at_all(
    store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(pub, "DATA", tmp_path / "does-not-exist")
    ok, why = pub.check_freshness(store)
    assert not ok
    assert "export_dashboard.py" in why


def test_it_stops_when_the_store_has_no_dates_rather_than_assuming_a_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """An unreadable store must not pass the guard.

    The tempting shortcut is `if newest is None: return True` — nothing to
    compare against, so let it through. That inverts the guard exactly when
    the store is the thing that is broken.
    """
    _write_manifest(tmp_path, monkeypatch, "2026-08-29")
    ok, why = pub.check_freshness(tmp_path / "empty-store")
    assert not ok
    assert "cannot run" in why


def test_a_manifest_with_no_date_field_does_not_pass(
    store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`manifest.get("latest_date")` returns None, and None must not compare
    equal to a real date by some later refactor's idea of falsy."""
    _write_manifest(tmp_path, monkeypatch, None)
    ok, _ = pub.check_freshness(store)
    assert not ok
