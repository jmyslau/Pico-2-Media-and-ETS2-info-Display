@echo off
REM Double-click to install the ETS2 telemetry plugin (needed for the truck dashboard).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_plugin.ps1"
echo.
pause
