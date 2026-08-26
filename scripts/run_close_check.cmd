@echo off
rem ---------------------------------------------------------------------
rem run_close_check.cmd - payload for the GatedAgentCloseCheck task
rem (afternoon round of close_rules check_times: close-45min ET).
rem Evaluates R1-R4 exit rules on open option structures; no new opens.
rem Pure ASCII on purpose: cmd.exe parses by byte offset and multi-byte
rem characters can desync the parser. Keep any non-ASCII text in Python.
rem
rem All output is appended to logs\close_check.log - a scheduled task
rem without log redirection fails silently with nothing but rc=1.
rem ---------------------------------------------------------------------
setlocal
set "ROOT=%~dp0.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"
set "LOG=%ROOT%\logs\close_check.log"
if not exist "%ROOT%\logs" mkdir "%ROOT%\logs"

rem A task registered with schtasks /TR has NO start-in directory: it inherits
rem %windir%\system32. Run from the repo so relative paths mean what they
rem meant in testing. The Python side no longer depends on this (paths.py
rem anchors every artifact on the repo root) - this is the second lock.
cd /d "%ROOT%"

echo ===== gated-agent close check %DATE% %TIME% ===== >> "%LOG%"

if not exist "%ROOT%\.venv\Scripts\python.exe" (
  echo FATAL: interpreter not found at %ROOT%\.venv\Scripts\python.exe >> "%LOG%"
  exit /b 1
)

rem Default mode evaluates only (dry run). On go-live day (8/28+), add
rem --live below so confirmed closes actually submit through the CLI path,
rem AND uncomment the ALPACA_HACKATHON_LIVE line - BOTH are required, the
rem same two independent switches run_daily.cmd uses.
rem
rem 08-26 adversarial review: this script documented NEITHER switch, so
rem following the go-live procedure written in run_daily.cmd produced a close
rem path that raised "refusing live order: ALPACA_HACKATHON_LIVE != 1" on
rem every unwind, all week, while opens kept working normally. An agent that
rem can open and cannot close is worse than one that does neither. The entry
rem point now refuses up front with rc=2 rather than raising per structure.
rem Both switches stay inside this setlocal - nothing leaks to the machine.

rem set "ALPACA_HACKATHON_LIVE=1"

"%ROOT%\.venv\Scripts\python.exe" -m gated_agent.position_manager >> "%LOG%" 2>&1
echo exit code %ERRORLEVEL% >> "%LOG%"
endlocal
