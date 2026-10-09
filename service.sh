#!/usr/bin/env bash
# Background service control: ./service.sh start|stop|restart|status|open|install|uninstall
exec "${PYTHON:-python3}" "$(dirname "$0")/src/service.py" "$@"
