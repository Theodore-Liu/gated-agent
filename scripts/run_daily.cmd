@echo off
rem ---------------------------------------------------------------------
rem run_daily.cmd - payload for the GatedAgentDaily scheduled task.
rem Pure ASCII on purpose: cmd.exe parses by byte offset and multi-byte
rem characters can desync the parser. Keep any non-ASCII text in Python.
rem
rem All output is appended to logs\daily.log - a scheduled task without
rem log redirection fails silently with nothing but rc=1.
rem ---------------------------------------------------------------------
setlocal
set "ROOT=%~dp0.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"
set "LOG=%ROOT%\logs\daily.log"
if not exist "%ROOT%\logs" mkdir "%ROOT%\logs"

rem A task registered with schtasks /TR has NO start-in directory: it inherits
rem %windir%\system32. Run from the repo so relative paths mean what they
rem meant in testing. The Python side no longer depends on this (paths.py
rem anchors every artifact on the repo root) - this is the second lock.
cd /d "%ROOT%"

echo ===== gated-agent daily run %DATE% %TIME% ===== >> "%LOG%"

if not exist "%ROOT%\.venv\Scripts\python.exe" (
  echo FATAL: interpreter not found at %ROOT%\.venv\Scripts\python.exe >> "%LOG%"
  exit /b 1
)

rem Default mode is --dry-run. On go-live day (8/28+), after keys are in
rem .env and a manual smoke test passed, change --dry-run to --live below
rem AND set ALPACA_HACKATHON_LIVE=1 here (two independent switches).
rem set "ALPACA_HACKATHON_LIVE=1"

"%ROOT%\.venv\Scripts\python.exe" -m gated_agent.run --dry-run >> "%LOG%" 2>&1
echo exit code %ERRORLEVEL% >> "%LOG%"
endlocal
