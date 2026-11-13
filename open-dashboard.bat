@echo off
REM ============================================================
REM  Market Command Center - open the HOSTED dashboard fullscreen
REM  After you deploy to Render, replace the URL below with your
REM  Render URL (looks like https://market-command-center.onrender.com/)
REM  Then just double-click this file on the wall-display PC.
REM  (No Python needed here anymore - Render runs the server.)
REM ============================================================
start chrome --kiosk --autoplay-policy=no-user-gesture-required "https://YOUR-APP-NAME.onrender.com/"
exit
