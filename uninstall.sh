#!/usr/bin/env bash
# Stops the WiFiMonitor service and web server and removes the login item. Logs are kept.
cd "$(dirname "$0")" || exit 1
exec "${PYTHON:-python3}" src/service.py uninstall
