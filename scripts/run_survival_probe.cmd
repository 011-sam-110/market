@echo off
REM Survival probe: measures how long a Vinted phone listing stays for sale.
REM
REM Same launcher discipline as run_collector.cmd -- installer Python, not the
REM WindowsApps/MSIX one, which exits silently when detached from a console;
REM and PYTHONUNBUFFERED so an empty log means "dead" rather than "buffered".
REM
REM Runs alongside the collector and shares its IP. The probe paces itself to
REM one listing page every 25s for that reason: the listing-page endpoint 429s
REM after roughly five requests in twenty seconds, while the catalogue endpoint
REM the collector uses does not.

setlocal
set PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe
set PYTHONUNBUFFERED=1

set LOGDIR=%USERPROFILE%\.phoneloop
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

cd /d "%~dp0.."
echo [%date% %time%] survival probe starting >> "%LOGDIR%\survival.log"
"%PY%" scripts\survival_probe.py >> "%LOGDIR%\survival.log" 2>&1
echo [%date% %time%] survival probe exited with %ERRORLEVEL% >> "%LOGDIR%\survival.log"
endlocal
