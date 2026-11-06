@echo off
rem Double-click to start OpenReSense (see run.ps1).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
if errorlevel 1 pause
