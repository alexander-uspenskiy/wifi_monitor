@echo off
rem Stops the WiFiMonitor service and web server and removes the login item. Logs are kept.
cd /d "%~dp0"
if "%PYTHON%"=="" set PYTHON=python
%PYTHON% src\service.py uninstall
