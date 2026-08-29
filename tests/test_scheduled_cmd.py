"""Every scripts/*.cmd must pin the INSTALLER python.

The MSIX/WindowsApps python exits SILENTLY -- exit code 0, no output -- when
launched detached with no console, which is exactly how Task Scheduler
launches things. It looks identical to a successful no-op run, so nothing
downstream notices the sweep never ran.

`python` on PATH resolves to the MSIX stub on this machine, so a bare
`python` in a scheduled .cmd is that failure waiting to happen. The precedent
scripts elsewhere on this box (.claude/gym/gym_sentinel.cmd,
.claude/scheduled/undeployed_sweep.cmd) both call bare `python` -- the habit
is one copy-paste away, which is why this guard is a test and not a comment.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
PINNED = r"%LOCALAPPDATA%\Programs\Python\Python313\python.exe"

# A bare interpreter INVOCATION: the token sits where a command name goes --
# at the head of a line, or after `&`, `|` or `(`. That is what distinguishes
# `python x.py` from `"%PY%" scripts\bulk_ebay.py`, where "py" is only ever a
# file extension, and from a path like ...\Python313\python.exe.
_BARE = re.compile(
    r'(?:^|[&|(]\s*)(?:@)?(?:call\s+|start\s+)?(python3?|py|python\.exe)(?=\s|$)',
    re.IGNORECASE)


def _cmd_files() -> list[Path]:
    return sorted(SCRIPTS.glob("*.cmd"))


def _code_lines(path: Path) -> list[str]:
    """Lines that can actually LAUNCH something.

    REM/:: lines are documentation and are allowed to spell out `python`
    (run_daily.cmd's header does, at length). `echo` lines are output, not
    invocation -- run_daily.cmd's own MSIX warning prints the word "python"
    and must not be read as calling it.
    """
    out = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.upper().startswith("REM") or line.startswith("::"):
            continue
        if line.lower().startswith("echo"):
            continue
        out.append(line)
    return out


class TestScheduledWrapperExists:
    def test_run_daily_scheduled_cmd_exists(self):
        """The Task Scheduler action. Its ONLY job is pinning the interpreter
        and capturing output that Python cannot log about itself."""
        assert (SCRIPTS / "run_daily_scheduled.cmd").exists()

    def test_it_pins_the_installer_python(self):
        text = (SCRIPTS / "run_daily_scheduled.cmd").read_text(encoding="utf-8")
        assert PINNED in text

    def test_it_fails_loudly_when_the_installer_python_is_missing(self):
        text = (SCRIPTS / "run_daily_scheduled.cmd").read_text(encoding="utf-8")
        assert "exit /b 1" in text
        assert "MSIX" in text or "WindowsApps" in text

    def test_it_logs_outside_the_repo(self):
        """OneDrive holds file handles on anything under the repo, and a log
        inside the repo would also dirty a tree that must stay clean."""
        text = (SCRIPTS / "run_daily_scheduled.cmd").read_text(encoding="utf-8")
        assert "%USERPROFILE%\\.phoneloop" in text
        assert ">>" in text                      # appends, never truncates

    def test_it_invokes_nightly_not_run_daily_directly(self):
        """nightly.py owns the lock, day-guard, heartbeat and exit codes."""
        text = (SCRIPTS / "run_daily_scheduled.cmd").read_text(encoding="utf-8")
        assert "nightly.py" in text


class TestTheDetectorItself:
    """A guard nobody watched fire is decoration. These pin the detector so a
    later 'tidy' of the regex cannot quietly turn the guard below into a
    test that passes over anything."""

    @pytest.mark.parametrize("line", [
        "python scripts\\nightly.py",
        "py -3 scripts\\nightly.py",
        "@python scripts\\nightly.py",
        "call python scripts\\nightly.py",
        'if not exist "%PY%" ( python scripts\\nightly.py )',
    ])
    def test_catches_a_bare_invocation(self, line):
        assert _BARE.search(line), line

    @pytest.mark.parametrize("line", [
        '"%PY%" scripts\\bulk_ebay.py --leaf-sweep',
        'set "PY=%LOCALAPPDATA%\\Programs\\Python\\Python313\\python.exe"',
        '"%PY%" "%~dp0nightly.py" >> "%LOG%" 2>&1',
    ])
    def test_does_not_flag_a_pinned_invocation(self, line):
        assert not _BARE.search(line), line


@pytest.mark.parametrize("path", _cmd_files(), ids=lambda p: p.name)
class TestNoBarePython:
    def test_no_bare_python_invocation(self, path):
        offenders = [ln for ln in _code_lines(path) if _BARE.search(ln)]
        assert offenders == [], (
            f"{path.name} invokes a bare interpreter: {offenders}. "
            "PATH resolves `python` to the MSIX stub, which exits silently "
            "under Task Scheduler."
        )


class TestRunDailyDataDir:
    def test_run_daily_defaults_the_data_dir(self):
        """Without this, a scheduled night writes to ~/.phoneloop/data -- 68
        parquet files, all source=vinted, zero eBay -- and orphans the
        29,602-listing eBay snapshot. Every run would still exit 0 and the
        board would print 'only one snapshot date' forever."""
        text = (SCRIPTS / "run_daily.cmd").read_text(encoding="utf-8")
        assert "PHONELOOP_DATA_DIR" in text

    def test_the_default_is_overridable_by_the_caller(self):
        """`if not defined` is load-bearing: every documented manual run
        exports the variable by hand and must keep winning."""
        text = (SCRIPTS / "run_daily.cmd").read_text(encoding="utf-8")
        assert re.search(r"if\s+not\s+defined\s+PHONELOOP_DATA_DIR", text,
                         re.IGNORECASE)
