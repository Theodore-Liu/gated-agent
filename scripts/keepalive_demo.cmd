@echo off
rem Keep the judge-facing Streamlit demo awake through the judging window.
rem Community Cloud hibernates idle apps; a sleeping demo greets judges with
rem a wake-up screen. A real browser-engine page load counts as traffic
rem (curl gets a 303 at the edge and does NOT). Runs hourly; harmless after
rem 9/5 and unregistered by stand_down stage 2. Pure ASCII on purpose.
setlocal
set "LOG=%~dp0..\logs\keepalive.log"
set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
echo ===== keepalive %DATE% %TIME% ===== >> "%LOG%"
"%CHROME%" --headless --disable-gpu --window-size=800,600 --timeout=60000 --screenshot="%TEMP%\ga_keepalive.png" "https://gated-agent-live.streamlit.app" >> "%LOG%" 2>&1
echo exit code %ERRORLEVEL% >> "%LOG%"
endlocal
