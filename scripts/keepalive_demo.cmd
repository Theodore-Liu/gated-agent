@echo off
rem Keep the judge-facing Streamlit demo awake through the judging window.
rem Community Cloud hibernates an app after ~12h without a viewer SESSION.
rem The old body here (headless chrome.exe --screenshot) only produced a page
rem hit: every screenshot was blank white (JS never ran) and the app was found
rem asleep on 2026-09-03 evening anyway. keepalive_demo.py drives a real
rem browser via Playwright, waits for the dashboard to render, holds the
rem session, and clicks the wake-up button if the app has dozed off.
rem Runs hourly; harmless after 9/5 and unregistered by stand_down stage 2.
rem Pure ASCII on purpose.
setlocal
set "LOG=%~dp0..\logs\keepalive.log"
set "PY=%~dp0..\.venv\Scripts\python.exe"
echo ===== keepalive %DATE% %TIME% ===== >> "%LOG%"
"%PY%" "%~dp0keepalive_demo.py" --png "%TEMP%\ga_keepalive.png" >> "%LOG%" 2>&1
echo exit code %ERRORLEVEL% >> "%LOG%"
endlocal
