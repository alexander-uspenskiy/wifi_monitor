#!/usr/bin/env bash
# Starts the collector (background) and the dashboard (foreground). Ctrl-C stops both.
# Extra arguments go to the collector, e.g.: ./start.sh --interval 10 --scan-every 600
cd "$(dirname "$0")" || exit 1
PY="${PYTHON:-python3}"
"$PY" src/collector.py "$@" &
COLLECTOR=$!
trap 'kill "$COLLECTOR" 2>/dev/null' EXIT INT TERM
"$PY" src/dashboard_server.py
