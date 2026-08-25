@echo off
rem ---------------------------------------------------------------------
rem register_task.cmd - registers the GatedAgentDaily scheduled task.
rem
rem   *** PREP ONLY until kickoff (2026-08-28). Do NOT run before then. ***
rem
rem What it registers:
rem   Task name : GatedAgentDaily
rem   Schedule  : weekdays (Mon-Fri) 09:20 local time (before market open;
rem               adjust /ST below if the box is not in US Eastern time)
rem   Action    : wscript //B run_hidden.vbs run_daily.cmd
rem               (run-hidden pattern: wscript window style 0 -> no console
rem               flash; the payload itself appends to logs\daily.log)
rem   Runs as   : the current user, only while logged on (no password stored)
rem
rem Pure ASCII on purpose - cmd.exe parses by byte offset and non-ASCII
rem bytes can desync the parser. Keep it that way.
rem
rem Verify after registering:   schtasks /Query /TN GatedAgentDaily /V /FO LIST
rem Run once manually:          schtasks /Run /TN GatedAgentDaily
rem Remove:                     schtasks /Delete /TN GatedAgentDaily /F
rem ---------------------------------------------------------------------
setlocal
set "ROOT=%~dp0.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"

if not exist "%ROOT%\scripts\run_daily.cmd" (
  echo ERROR: payload %ROOT%\scripts\run_daily.cmd not found.
  pause
  exit /b 1
)

echo Registering GatedAgentDaily (weekdays 09:20, hidden window)...
schtasks /Create /F /TN GatedAgentDaily ^
  /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 09:20 ^
  /TR "wscript.exe //B \"%ROOT%\scripts\run_hidden.vbs\" \"%ROOT%\scripts\run_daily.cmd\""

if errorlevel 1 (
  echo FAILED - see message above.
) else (
  echo OK. Verify with: schtasks /Query /TN GatedAgentDaily /V /FO LIST
  echo Logs will appear in %ROOT%\logs\daily.log
)
pause
endlocal
