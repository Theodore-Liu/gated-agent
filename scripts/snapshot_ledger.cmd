@echo off
rem ---------------------------------------------------------------------
rem snapshot_ledger.cmd - payload for the GatedAgentLedgerSnapshot task.
rem
rem Deliberately a SEPARATE task rather than a line inside run_daily.cmd:
rem that file is the competition's critical path during a live week, and
rem an additive backup is not worth any risk to it. This one only reads
rem the ledger and writes copies elsewhere, so it cannot affect trading.
rem
rem Pure ASCII on purpose: cmd.exe parses by byte offset and a multi-byte
rem character can desync the parser. Any non-ASCII belongs in the Python.
rem
rem Output is appended to logs\ledger-snapshot.log - a scheduled task with
rem no log redirection fails silently with nothing but a return code.
rem ---------------------------------------------------------------------
setlocal
set "ROOT=%~dp0.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"
set "LOG=%ROOT%\logs\ledger-snapshot.log"
if not exist "%ROOT%\logs" mkdir "%ROOT%\logs"
cd /d "%ROOT%"

echo ===== ledger snapshot %DATE% %TIME%  >> "%LOG%"

if not exist "%ROOT%\.venv\Scripts\python.exe" (
  echo FATAL: interpreter not found at %ROOT%\.venv\Scripts\python.exe >> "%LOG%"
  exit /b 1
)

"%ROOT%\.venv\Scripts\python.exe" "%ROOT%\scripts\snapshot_ledger.py" >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
echo exit code %RC% >> "%LOG%"
endlocal & exit /b %RC%
