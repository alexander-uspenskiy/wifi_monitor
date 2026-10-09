"""Shared test setup: makes src/ importable and points the log directory at a throwaway folder."""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
os.environ.setdefault("WIFI_LOG_DIR", tempfile.mkdtemp(prefix="wifimonitor-tests-"))  # tests never touch the real logs/
if SRC not in sys.path:
    sys.path.insert(0, SRC)
