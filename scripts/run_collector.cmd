@echo off
REM Week-one collector: logs every listing, alerts on nothing.
REM
REM Uses the INSTALLER Python explicitly, not the WindowsApps/MSIX one. The MSIX
REM build fails silently when detached from a console, which is the same trap
REM that broke the scheduled deploy watchdog on this machine.
REM
REM PYTHONUNBUFFERED matters too: without it stdout buffers and the log stays
REM empty for hours, which looks identical to a dead process.

setlocal
set PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe
set PYTHONUNBUFFERED=1
set PHONELOOP_POLL_INTERVAL_S=120

REM Log and data both live OUTSIDE OneDrive. OneDrive takes handles on files
REM under it to sync them, and a collector writing every 120s loses that race.
set LOGDIR=%USERPROFILE%\.phoneloop
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

cd /d "%~dp0.."
echo [%date% %time%] collector starting >> "%LOGDIR%\collect.log"
"%PY%" -m phoneloop collect >> "%LOGDIR%\collect.log" 2>&1
echo [%date% %time%] collector exited with %ERRORLEVEL% >> "%LOGDIR%\collect.log"
endlocal
