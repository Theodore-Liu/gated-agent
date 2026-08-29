@echo off
rem ---------------------------------------------------------------------
rem stand_down.cmd - disarm the agent after the 2026-09-04 submission.
rem
rem   *** PREPARED IN ADVANCE. NOT SCHEDULED. RUN BY A HUMAN, ON PURPOSE. ***
rem
rem Thin wrapper: all logic lives in scripts\stand_down.py (see its
rem docstring for the two-stage plan). Usage:
rem
rem   stand_down.cmd          stage 1 - stop new opens; closes stay live
rem                           so R1 can flatten the 9/11 spreads for real
rem   stand_down.cmd all      stage 2 - closes stand down too (book flat)
rem
rem Pure ASCII on purpose - cmd.exe parses by byte offset; anything more
rem complex than argument passing belongs in the .py.
rem ---------------------------------------------------------------------
setlocal
set "ROOT=%~dp0.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"
if not exist "%ROOT%\.venv\Scripts\python.exe" (
  echo FATAL: interpreter not found at %ROOT%\.venv\Scripts\python.exe
  pause
  exit /b 1
)
"%ROOT%\.venv\Scripts\python.exe" "%ROOT%\scripts\stand_down.py" %*
pause
endlocal
