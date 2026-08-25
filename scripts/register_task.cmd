@echo off
rem ---------------------------------------------------------------------
rem register_task.cmd - registers BOTH gated-agent scheduled tasks.
rem
rem   *** PREP ONLY until kickoff (2026-08-28). Do NOT run before then. ***
rem
rem What it registers (times are LOCAL; this box is US Pacific - the /ST
rem values below are chosen so they line up with the frozen close_rules
rem check_times, which are defined in US Eastern market time):
rem
rem   Task name : GatedAgentDaily
rem   Schedule  : weekdays 07:00 PT  (= 10:00 ET = market open + 30min)
rem   Action    : run_daily.cmd  -> close checks round 1, then signal ->
rem               options mapping -> gates -> red team -> orders
rem
rem   Task name : GatedAgentCloseCheck
rem   Schedule  : weekdays 12:15 PT  (= 15:15 ET = market close - 45min)
rem   Action    : run_close_check.cmd -> close checks round 2 only
rem
rem   Both      : wscript //B run_hidden.vbs <payload> (no console flash;
rem               payloads append to logs\daily.log / logs\close_check.log)
rem   Runs as   : the current user, only while logged on (no password)
rem
rem   If the box is NOT in US Pacific time, recompute both /ST values:
rem   target 10:00 and 15:15 US Eastern market time.
rem
rem Pure ASCII on purpose - cmd.exe parses by byte offset and non-ASCII
rem bytes can desync the parser. Keep it that way.
rem
rem Verify after registering:   schtasks /Query /TN GatedAgentDaily /V /FO LIST
rem                             schtasks /Query /TN GatedAgentCloseCheck /V /FO LIST
rem Run once manually:          schtasks /Run /TN GatedAgentDaily
rem Remove:                     schtasks /Delete /TN GatedAgentDaily /F
rem                             schtasks /Delete /TN GatedAgentCloseCheck /F
rem ---------------------------------------------------------------------
setlocal
set "ROOT=%~dp0.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"

if not exist "%ROOT%\scripts\run_daily.cmd" (
  echo ERROR: payload %ROOT%\scripts\run_daily.cmd not found.
  pause
  exit /b 1
)
if not exist "%ROOT%\scripts\run_close_check.cmd" (
  echo ERROR: payload %ROOT%\scripts\run_close_check.cmd not found.
  pause
  exit /b 1
)

echo Registering GatedAgentDaily (weekdays 07:00 PT = open+30min ET)...
schtasks /Create /F /TN GatedAgentDaily ^
  /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 07:00 ^
  /TR "wscript.exe //B \"%ROOT%\scripts\run_hidden.vbs\" \"%ROOT%\scripts\run_daily.cmd\""
if errorlevel 1 (
  echo FAILED - see message above.
  pause
  exit /b 1
)

echo Registering GatedAgentCloseCheck (weekdays 12:15 PT = close-45min ET)...
schtasks /Create /F /TN GatedAgentCloseCheck ^
  /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 12:15 ^
  /TR "wscript.exe //B \"%ROOT%\scripts\run_hidden.vbs\" \"%ROOT%\scripts\run_close_check.cmd\""
if errorlevel 1 (
  echo FAILED - see message above.
  pause
  exit /b 1
)

echo OK. Verify with:
echo   schtasks /Query /TN GatedAgentDaily /V /FO LIST
echo   schtasks /Query /TN GatedAgentCloseCheck /V /FO LIST
echo Logs: %ROOT%\logs\daily.log and %ROOT%\logs\close_check.log
pause
endlocal
