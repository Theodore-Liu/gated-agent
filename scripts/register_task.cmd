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
rem   NOTE on the working directory: schtasks /TR cannot set a start-in
rem   folder, so both tasks inherit %windir%\system32 as their CWD. Each
rem   payload therefore does `cd /d` to the repo itself, and the Python side
rem   anchors every artifact on the repo root (src/gated_agent/paths.py)
rem   rather than the CWD. Do not "simplify" either one away: a CWD-relative
rem   ledger silently forks the one file that dedup, the once-per-day guard,
rem   the direction-flip guard and the daily loss halt all read.
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

rem A task registered by schtasks does NOT get StartWhenAvailable: if the box
rem is asleep, rebooting or switched off at 07:00, that run is skipped and
rem nothing anywhere says so - a silent no-trade day in a one-week contest.
rem schtasks has no flag for it, so set it through the Scheduler cmdlets.
rem Safe to catch up late: the day is idempotent (run_complete) and the dedup
rem key no longer drifts with the quote, so a late start cannot double send.
rem Non-fatal - the tasks themselves are registered by this point.
echo Enabling StartWhenAvailable on both tasks (catch up a missed start)...
rem -ErrorAction Stop is load-bearing: Set-ScheduledTask fails NON-terminating,
rem so without it the catch never fires and the user sees a red PowerShell
rem error blob in the middle of a script that otherwise succeeded.
powershell -NoProfile -ExecutionPolicy Bypass -Command "foreach ($n in 'GatedAgentDaily','GatedAgentCloseCheck') { try { $s = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew; Set-ScheduledTask -TaskName $n -Settings $s -ErrorAction Stop | Out-Null; Write-Host ('  ' + $n + ': StartWhenAvailable set') } catch { Write-Host ('  ' + $n + ': could not set StartWhenAvailable - ' + $_.Exception.Message) } }"

echo OK. Verify with:
echo   schtasks /Query /TN GatedAgentDaily /V /FO LIST
echo   schtasks /Query /TN GatedAgentCloseCheck /V /FO LIST
echo Logs: %ROOT%\logs\daily.log and %ROOT%\logs\close_check.log
pause
endlocal
