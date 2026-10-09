@echo off
rem Background service control: service.bat start^|stop^|restart^|status^|open^|install^|uninstall
if "%PYTHON%"=="" set PYTHON=python
%PYTHON% "%~dp0src\service.py" %*
