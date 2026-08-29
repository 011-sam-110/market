"""Knob presets: a file the dashboard writes and the nightly run obeys.

Two properties carry all the weight here.

**The environment always wins.** A preset arrives over the network and can be
hours stale; an env var is somebody at the keyboard now. If the remote value
could override that, the only way to know what a run actually used would be to
go and read a file on another machine.

**A fetch can never fail the night.** The sweep is the valuable part of 3am and
it runs fine on yesterday's knobs. Every failure path leaves the existing file
alone and exits 0 -- and every one of them is exercised below, because "it
handles errors" is a claim, not a test.

No test here opens a socket. The failure cases are injected by monkeypatching
`fetch`, and the success case parses a payload directly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetch_preset  # noqa: E402

from phoneloop.knobs import Knobs  # noqa: E402


def _preset(**knobs) -> dict:
    return {"schema_version": 1, "knobs": knobs}


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# --- Knobs.load ---------------------------------------------------------------

def test_a_missing_file_is_normal_and_degrades_to_from_env(tmp_path):
    """Before anyone has ever saved a preset there is no file. That is the
    ordinary state, not an error."""
    got = Knobs.load(path=tmp_path / "nope.json", env={})
    assert got == Knobs()


def test_the_file_moves_a_knob(tmp_path):
    p = _write(tmp_path / "knobs.json", _preset(max_eta_days=3, max_competitors=8))
    got = Knobs.load(path=p, env={})
    assert got.max_eta_days == 3
    assert got.max_competitors == 8
    assert got.price_from_pence == Knobs().price_from_pence


def test_the_environment_beats_the_file(tmp_path):
    """The whole point of the layering. A stale preset must never quietly
    override the value somebody just typed."""
    p = _write(tmp_path / "knobs.json", _preset(max_eta_days=3, max_competitors=8))
    got = Knobs.load(path=p, env={"PHONELOOP_MAX_ETA_DAYS": "14"})

    assert got.max_eta_days == 14, "the env var must win"
    assert got.max_competitors == 8, "and must not disturb the rest of the file"


def test_a_malformed_file_raises_rather_than_reverting_to_defaults(tmp_path):
    p = tmp_path / "knobs.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="could not be read as JSON"):
        Knobs.load(path=p, env={})


def test_an_unknown_key_is_refused_not_ignored(tmp_path):
    """Silently dropping the one field you came to set is worse than failing."""
    p = _write(tmp_path / "knobs.json", _preset(max_eta_dayz=3))
    with pytest.raises(ValueError, match="not a knob"):
        Knobs.load(path=p, env={})


def test_a_non_integer_value_is_refused(tmp_path):
    p = _write(tmp_path / "knobs.json", _preset(max_eta_days="3"))
    with pytest.raises(ValueError, match="not an integer"):
        Knobs.load(path=p, env={})


def test_a_bool_is_not_an_integer_here(tmp_path):
    """`bool` subclasses `int` in Python, so `True` would sail into a pence
    field as 1 without this check."""
    p = _write(tmp_path / "knobs.json", _preset(max_eta_days=True))
    with pytest.raises(ValueError, match="not an integer"):
        Knobs.load(path=p, env={})


def test_a_bare_map_is_refused_rather_than_guessed_at(tmp_path):
    p = _write(tmp_path / "knobs.json", {"max_eta_days": 3})
    with pytest.raises(ValueError, match="bare map"):
        Knobs.load(path=p, env={})


def test_the_preset_path_is_beside_the_store_not_inside_it():
    """Rebuilding the store must not mean losing your settings."""
    assert Knobs.default_path({}).name == "knobs.json"
    assert Knobs.default_path({}).parent.name == ".phoneloop"
    assert Knobs.default_path({"PHONELOOP_KNOBS_FILE": "/tmp/k.json"}) == Path("/tmp/k.json")


# --- fetch_preset -------------------------------------------------------------

def test_no_url_configured_is_a_clean_no_op(tmp_path, capsys):
    out = tmp_path / "knobs.json"
    _write(out, _preset(max_eta_days=3))
    before = out.read_text(encoding="utf-8")

    assert fetch_preset.main(["--out", str(out)], env={}) == 0
    assert out.read_text(encoding="utf-8") == before
    assert "no PHONELOOP_PRESET_URL" in capsys.readouterr().err


@pytest.mark.parametrize("boom", [
    OSError("connection reset"),
    TimeoutError("timed out"),
    ValueError("HTTP 503"),
])
def test_a_fetch_failure_preserves_the_existing_file_and_exits_zero(
        tmp_path, monkeypatch, capsys, boom):
    out = tmp_path / "knobs.json"
    _write(out, _preset(max_eta_days=3))
    before = out.read_text(encoding="utf-8")

    def explode(*_a, **_kw):
        raise boom
    monkeypatch.setattr(fetch_preset, "fetch", explode)

    rc = fetch_preset.main(["--url", "https://example.invalid/p", "--out", str(out)],
                           env={})
    assert rc == 0, "a failed fetch must never fail the night"
    assert out.read_text(encoding="utf-8") == before
    assert "keeping the existing" in capsys.readouterr().err
    assert Knobs.load(path=out, env={}).max_eta_days == 3


def test_a_bad_payload_never_lands_on_disk(tmp_path, monkeypatch, capsys):
    """Validated before writing, so one bad response cannot break tomorrow too."""
    out = tmp_path / "knobs.json"
    _write(out, _preset(max_eta_days=3))
    before = out.read_text(encoding="utf-8")

    monkeypatch.setattr(fetch_preset, "fetch",
                        lambda *a, **k: _preset(max_eta_dayz=99))

    assert fetch_preset.main(["--url", "https://x/p", "--out", str(out)], env={}) == 0
    assert out.read_text(encoding="utf-8") == before
    assert "refusing the fetched preset" in capsys.readouterr().err


def test_a_good_payload_is_written_and_then_loads(tmp_path, monkeypatch):
    out = tmp_path / "knobs.json"
    monkeypatch.setattr(fetch_preset, "fetch", lambda *a, **k: {
        "schema_version": 1,
        "knobs": {"max_eta_days": 5, "max_competitors": 25},
        "label": "wider net",
    })

    assert fetch_preset.main(["--url", "https://x/p", "--out", str(out)], env={}) == 0

    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["knobs"] == {"max_eta_days": 5, "max_competitors": 25}
    assert written["label"] == "wider net"

    got = Knobs.load(path=out, env={})
    assert got.max_eta_days == 5
    assert got.max_competitors == 25


def test_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    out = tmp_path / "knobs.json"
    monkeypatch.setattr(fetch_preset, "fetch",
                        lambda *a, **k: _preset(max_eta_days=5))

    assert fetch_preset.main(
        ["--url", "https://x/p", "--out", str(out), "--dry-run"], env={}) == 0
    assert not out.exists()
    assert "would write" in capsys.readouterr().out


def test_an_oversized_response_is_refused_before_it_is_parsed(monkeypatch):
    """A preset is a dozen integers. Reading an unbounded body from a broken or
    hostile endpoint is how a 3am cron becomes an OOM."""
    class FakeResp:
        status = 200
        def read(self, n=None):
            return b"x" * (fetch_preset.MAX_BYTES + 1)
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(fetch_preset.urllib.request, "urlopen",
                        lambda *a, **k: FakeResp())
    with pytest.raises(ValueError, match="larger than"):
        fetch_preset.fetch("https://x/p")


def test_the_token_rides_as_a_bearer_header(monkeypatch):
    seen = {}

    class FakeResp:
        status = 200
        def read(self, n=None):
            return json.dumps(_preset(max_eta_days=4)).encode()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def capture(req, timeout=None):
        seen["auth"] = req.get_header("Authorization")
        return FakeResp()

    monkeypatch.setattr(fetch_preset.urllib.request, "urlopen", capture)
    fetch_preset.fetch("https://x/p", "s3cret")
    assert seen["auth"] == "Bearer s3cret"
