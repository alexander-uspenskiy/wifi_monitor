@echo off
rem Starts the collector in its own window and the dashboard in this one.
rem Extra arguments go to the collector, e.g.: start.bat --interval 10 --scan-every 600
cd /d "%~dp0"
if "%PYTHON%"=="" set PYTHON=python
start "WiFi Monitor - collector" cmd /k %PYTHON% src\collector.py %*
%PYTHON% src\dashboard_server.py
