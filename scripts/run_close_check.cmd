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
set "LOG=%ROOT%\logs\close_check.log"
if not exist "%ROOT%\logs" mkdir "%ROOT%\logs"

echo ===== gated-agent close check %DATE% %TIME% ===== >> "%LOG%"

rem Default mode evaluates only (dry run). On go-live day (8/28+), add
rem --live below so confirmed closes actually submit through the CLI path.

"%ROOT%\.venv\Scripts\python.exe" -m gated_agent.position_manager >> "%LOG%" 2>&1
echo exit code %ERRORLEVEL% >> "%LOG%"
endlocal
