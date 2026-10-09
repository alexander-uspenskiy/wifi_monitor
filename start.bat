@echo off
rem Starts WiFiMonitor in this window or as a background service.
rem   start.bat --terminal ^| --service ^| --login     pick without being asked
rem   start.bat                                        asks (interactive window only)
rem Other arguments go to the collector, e.g.: start.bat --service --interval 10 --scan-every 600
cd /d "%~dp0"
if "%PYTHON%"=="" set PYTHON=python
%PYTHON% src\launch.py %*
