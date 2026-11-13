@echo off
REM ============================================================
REM  Market Command Center - wall display launcher
REM  Starts the local data server (wall-server.py) and opens
REM  the dashboard fullscreen in Chrome.
REM  Only wall-server.py is required in THIS folder (the dashboard page
REM  is embedded inside it). An optional market-wall.html, if present, overrides it.
REM ============================================================

cd /d "%~dp0"

REM --- start the data server (serves the page + live data) ---
where python >nul 2>nul
if %errorlevel%==0 (
  start "wall-server" /min python wall-server.py
) else (
  where py >nul 2>nul && start "wall-server" /min py wall-server.py
)

REM --- give it a moment to come up ---
timeout /t 2 /nobreak >nul

REM --- open fullscreen in Chrome ---
start chrome --kiosk --autoplay-policy=no-user-gesture-required "http://localhost:8750/"

REM To close the display: press Alt+F4.  (Server keeps running in a minimized window.)
exit
