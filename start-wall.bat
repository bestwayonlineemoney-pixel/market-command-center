@echo off
REM ============================================================
REM  Market Command Center - wall display launcher
REM  Only wall-server.py is required in THIS folder (the dashboard
REM  page is embedded inside it). Run this file to start/refresh.
REM ============================================================

cd /d "%~dp0"

REM --- stop any OLD server first, so the new wall-server.py always takes effect ---
taskkill /f /im python.exe >nul 2>nul
taskkill /f /im pythonw.exe >nul 2>nul
taskkill /f /im py.exe >nul 2>nul
timeout /t 1 /nobreak >nul

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

REM To close the display: press Alt+F4.  (Server keeps running minimized.)
exit
