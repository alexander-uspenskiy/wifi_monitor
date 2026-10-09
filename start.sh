#!/usr/bin/env bash
# Starts WiFiMonitor in this terminal or as a background service.
#   ./start.sh --terminal | --service | --login      pick without being asked
#   ./start.sh                                       asks (interactive terminal only)
# Other arguments go to the collector, e.g.: ./start.sh --service --interval 10 --scan-every 600
cd "$(dirname "$0")" || exit 1
exec "${PYTHON:-python3}" src/launch.py "$@"
