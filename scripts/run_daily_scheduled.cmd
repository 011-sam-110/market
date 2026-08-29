@echo off
REM The Task Scheduler ACTION. Its only job is to pin the interpreter and to
REM capture the output Python cannot log about itself.
REM
REM Deliberately tiny. Everything that makes a night legible -- the lock, the
REM day-guard, the two-phase heartbeat, the exit code -- lives in
REM scripts\nightly.py, where it is unit-tested (tests\test_nightly.py). A
REM .cmd file is not testable, so nothing decision-shaped belongs in one.
REM
REM WHY THE PINNED PYTHON. `python` on PATH resolves to the MSIX/WindowsApps
REM stub on this machine, and that build exits SILENTLY -- exit code 0, no
REM output -- when launched detached with no console attached, which is
REM exactly how Task Scheduler launches things. It is indistinguishable from
REM a successful no-op run, so nothing downstream would ever notice the sweep
REM had not happened. tests\test_scheduled_cmd.py enforces the pin across
REM every scripts\*.cmd, because two scheduled scripts elsewhere on this
REM machine already call bare `python` and the habit is one copy-paste away.
REM
REM WHY THE REDIRECT, given nightly.py writes its own dated log. Belt and
REM braces beneath Python's own logging: this catches the cases Python cannot
REM log about itself -- an interpreter that fails to start, an ImportError, a
REM traceback raised before logging is configured. Appends (>>), never
REM truncates.
REM
REM WHY ONE UNDATED FILE HERE. The per-night dated log is nightly.py's
REM (nightly-YYYY-MM-DD.log), written by something that owns a real datetime
REM library. Computing a date in .cmd is a trap: %DATE% is locale-dependent
REM ("Fri 29/08/2026" on this box, whose slashes read as a path), and the
REM usual `wmic os get LocalDateTime` workaround DOES NOT EXIST on this
REM machine any more -- Windows 11 26200 has removed wmic, and the idiom
REM fails to an empty string, i.e. a wrong filename, silently. So this
REM catch-all appends to one file and adds ~4 lines a night.
REM
REM WHY THE LOG LIVES OUTSIDE THE REPO. The repo is under OneDrive, which
REM takes file handles on anything beneath it and has already killed a
REM parquet writer in this project once (see phoneloop\config.py's DATA_DIR
REM comment). A log inside the repo would also add an untracked path to a
REM tree that must stay clean.
REM
REM Exit codes are nightly.py's, passed straight through so Task Scheduler's
REM LastTaskResult is readable on its own:
REM     0   ok (including "already done today")
REM     1   a step of run_daily.cmd failed
REM     75  another run holds the lock -- backed off, NOT a failure
REM     2   internal error

setlocal

REM THE STORE, set HERE and inherited by everything downstream.
REM
REM nightly.py owns the lock, the day-guard, the log and the heartbeat, and
REM resolves their paths from PHONELOOP_DATA_DIR -- falling back to
REM config.py's default (%USERPROFILE%\.phoneloop\data, Vinted rows only)
REM when it is unset. run_daily.cmd, launched as its CHILD, defaults the same
REM variable to ...\prod. A Task Scheduler action carries no environment of
REM its own, so with nothing set here the night split in two: the sweep wrote
REM its data to prod while the watcher wrote its heartbeat to data.
REM
REM Measured 2026-08-29 with `python scripts\nightly.py --dry-run`:
REM     data_dir   ...\.phoneloop\data
REM     heartbeat  ...\.phoneloop\data\health\last_run.json
REM while register_daily_task.ps1 -Status reads
REM                ...\.phoneloop\prod\health\last_run.json
REM
REM That is a GUARANTEED FALSE ALARM on every successful night -- the status
REM check finding no heartbeat where it looks, for a night that ran fine --
REM plus a day-guard that cannot see the other trigger's completed run, and
REM _checkpoint_counts reading a file bulk_ebay never writes there. None of
REM it goes red on its own; the sweep works, only the watching is broken.
REM
REM Setting it here fixes both halves at once: nightly.py reads this value,
REM and run_daily.cmd's `if not defined` guard yields to it rather than
REM overwriting it. The two files must keep naming the SAME store --
REM tests/test_scheduled_store_agreement.py fails if they drift.
set "PHONELOOP_DATA_DIR=%USERPROFILE%\.phoneloop\prod"

set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
set "LOGDIR=%USERPROFILE%\.phoneloop\prod\logs"

if not exist "%PY%" (
    echo [run_daily_scheduled] installer python not found at "%PY%"
    echo [run_daily_scheduled] the MSIX/WindowsApps build must NOT be used here -- it exits silently when launched without a console, per contracts section 0, rule 7.
    exit /b 1
)

if not exist "%LOGDIR%" mkdir "%LOGDIR%"

set "LOG=%LOGDIR%\scheduled.log"

echo. >> "%LOG%"
echo === run_daily_scheduled start %DATE% %TIME% === >> "%LOG%"
"%PY%" "%~dp0nightly.py" >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
echo === run_daily_scheduled exit %RC% (%DATE% %TIME%) === >> "%LOG%"
exit /b %RC%
