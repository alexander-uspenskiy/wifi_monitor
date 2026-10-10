"""Daily-rotated log storage shared by the collector and the dashboard server.

Files live in <project>/logs (override with WIFI_LOG_DIR):
    wifi-monitor-YYYY-MM-DD.log   one line per sample, plus EVENT notes
    wifi-scan-YYYY-MM-DD.log      one line per nearby-network scan
    wifi-speed-YYYY-MM-DD.log     one line per internet download speed test

Today's file is plain text. Older files are gzip-compressed, and files older than
WIFI_LOG_KEEP_DAYS (default 14) are deleted. Readers handle both .log and .log.gz.
"""
import datetime as dt
import gzip
import json
import os
import re
import threading
import time
from urllib.parse import urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.environ.get("WIFI_LOG_DIR") or os.path.join(ROOT, "logs")
KEEP_DAYS = int(os.environ.get("WIFI_LOG_KEEP_DAYS", "14"))
KINDS = ("monitor", "scan", "speed")

_write_lock = threading.Lock()
_cache = {}


def _pattern(kind):
    return re.compile(r"^wifi-%s-(\d{4}-\d\d-\d\d)\.log(\.gz)?$" % kind)


def path_for(kind, when):
    return os.path.join(LOG_DIR, "wifi-%s-%s.log" % (kind, when.strftime("%Y-%m-%d")))


def append(kind, line, when=None):
    """Append one line to the file for `when` (default: now)."""
    when = when or dt.datetime.now()
    os.makedirs(LOG_DIR, exist_ok=True)
    with _write_lock, open(path_for(kind, when), "a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")


def list_files(kind):
    """[(YYYY-MM-DD, path)] oldest first. A plain .log wins over a .log.gz for the same day."""
    pat = _pattern(kind)
    found = {}
    try:
        names = os.listdir(LOG_DIR)
    except OSError:
        return []
    for name in names:
        m = pat.match(name)
        if m and (m.group(1) not in found or not name.endswith(".gz")):
            found[m.group(1)] = os.path.join(LOG_DIR, name)
    return sorted(found.items())


def read_file(path):
    """Lines of one log file (cached until the file changes)."""
    try:
        st = os.stat(path)
    except OSError:
        return []
    key = (st.st_mtime_ns, st.st_size)
    hit = _cache.get(path)
    if hit and hit[0] == key:
        return hit[1]
    opener = gzip.open if path.endswith(".gz") else open
    try:
        with opener(path, "rt", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except (OSError, EOFError):
        return []
    if len(_cache) > 20:
        _cache.pop(next(iter(_cache)))
    _cache[path] = (key, lines)
    return lines


def read_lines(kind, max_lines):
    """The newest `max_lines` lines across all files, in chronological order."""
    out = []
    for _, path in reversed(list_files(kind)):
        out = read_file(path) + out
        if len(out) >= max_lines:
            break
    return out[-max_lines:]


CONTROL_FILE = os.path.join(LOG_DIR, "control.json")
# None means "use the collector's command-line value". The page thresholds for gaps and stale data assume samples at most
# about 15 s apart, so slower sampling is not offered.
INTERVAL_CHOICES = (2, 5, 10, 15)               # seconds between samples (router ping, internet ping, Wi-Fi status)
SCAN_CHOICES = (0, 300, 900, 1800, 3600)        # seconds between nearby-network scans, 0 = off
SPEED_EVERY_CHOICES = (0, 600, 1800, 3600)      # seconds between download speed tests, 0 = one time (Run now only)
SPEED_MB_MIN, SPEED_MB_MAX, SPEED_MB_DEFAULT = 10, 100, 25  # megabytes (1 MB = 1,000,000 bytes) downloaded per test
SPEED_URL_MAX = 500
SPEED_DEFAULT_URL = "https://speed.cloudflare.com/__down?bytes={bytes}"
# speed_url None = the default server. speed_run is a millisecond stamp: a new value means "run a test now" (the collector ignores the one it saw at start-up).
CONTROL_DEFAULTS = {"scan_paused": False, "interval": None, "scan_every": None,
                    "speed_url": None, "speed_mb": SPEED_MB_DEFAULT, "speed_every": 0, "speed_run": None}


def _plain_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def valid_speed_url(value):
    """An http(s) address of up to SPEED_URL_MAX characters: no spaces or control characters, a host name and no user:password@."""
    if not isinstance(value, str) or not value or len(value) > SPEED_URL_MAX or not value.isascii():
        return False
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 or ch in '"<>\\^`' for ch in value):  # never valid in an address, and not worth sending
        return False
    try:
        parts = urlsplit(value)
        parts.port  # raises ValueError for http://host:abc/ and ports above 65535
        return parts.scheme in ("http", "https") and bool(parts.hostname) and "@" not in parts.netloc
    except ValueError:
        return False


def valid_control(key, value):
    if key == "scan_paused":
        return isinstance(value, bool)
    if key == "interval":
        return value is None or (isinstance(value, (int, float)) and not isinstance(value, bool) and value in INTERVAL_CHOICES)
    if key == "scan_every":
        return value is None or (isinstance(value, (int, float)) and not isinstance(value, bool) and value in SCAN_CHOICES)
    if key == "speed_url":
        return value is None or valid_speed_url(value)
    if key == "speed_mb":
        return _plain_int(value) and SPEED_MB_MIN <= value <= SPEED_MB_MAX
    if key == "speed_every":
        return _plain_int(value) and value in SPEED_EVERY_CHOICES  # None is not valid: one time is 0
    if key == "speed_run":
        return value is None or (_plain_int(value) and value >= 0)
    return False


class Control(dict):
    """The control settings. `ok` is False when the file exists but could not be read (busy, damaged), so the values are only defaults;
    a missing file is normal and counts as ok."""
    ok = True


def read_control():
    """Runtime switches shared by the dashboard and the collector (re-read every sample). Invalid values fall back to the default."""
    ok = True
    try:
        with open(CONTROL_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError):
        data, ok = {}, False
    if not isinstance(data, dict):
        data, ok = {}, False
    ctl = Control({k: (data[k] if k in data and valid_control(k, data[k]) else default) for k, default in CONTROL_DEFAULTS.items()})
    ctl.ok = ok
    return ctl


def write_control(**changes):
    ctl = {**read_control(), **{k: v for k, v in changes.items() if k in CONTROL_DEFAULTS and valid_control(k, v)}}
    os.makedirs(LOG_DIR, exist_ok=True)
    tmp = CONTROL_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ctl, f)
    os.replace(tmp, CONTROL_FILE)
    return ctl


INFO_FILE = os.path.join(LOG_DIR, "collector.json")


def write_info(info):
    """The collector's own settings (interval, scan period, target, gateway...), so the dashboard can show what really runs."""
    os.makedirs(LOG_DIR, exist_ok=True)
    tmp = INFO_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(info, f)
    os.replace(tmp, INFO_FILE)


def read_info():
    try:
        with open(INFO_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def speed_file():
    return os.path.join(LOG_DIR, "speed.json")  # looked up at call time, so tests can point LOG_DIR elsewhere


def write_speed_progress(data, tries=1):
    """Progress of the running download speed test, for the dashboard. Best effort: a failed write is skipped (`tries` > 1 retries it,
    for the final write, which a reader on Windows can briefly block)."""
    for attempt in range(tries):
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            tmp = speed_file() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, speed_file())
            return True
        except OSError:
            if attempt + 1 < tries:
                time.sleep(0.05)
    return False


def read_speed_progress():
    try:
        with open(speed_file(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def read_day(kind, day):
    """All lines for one day ('YYYY-MM-DD'), or [] if there is no file for it."""
    for d, path in list_files(kind):
        if d == day:
            return read_file(path)
    return []


def read_all(kind):
    """Every line across all kept files, in chronological order."""
    for _, path in list_files(kind):
        for line in read_file(path):
            yield line


def _compress(path):
    gz = path + ".gz"
    with open(path, "rb") as src:
        data = gzip.compress(src.read())
    if os.path.exists(gz):  # a straggler line arrived after compression: append a gzip member
        with open(gz, "ab") as dst:
            dst.write(data)
    else:
        tmp = gz + ".tmp"
        with open(tmp, "wb") as dst:
            dst.write(data)
        os.replace(tmp, gz)
    os.remove(path)


def maintain(today=None):
    """Compress files from before today and delete files older than KEEP_DAYS."""
    today = today or dt.date.today()
    oldest_kept = today - dt.timedelta(days=KEEP_DAYS - 1)
    for kind in KINDS:
        for day, path in list_files(kind):
            d = dt.date.fromisoformat(day)
            try:
                if d < oldest_kept:
                    for p in (path, path + ".gz" if not path.endswith(".gz") else path[:-3]):
                        if os.path.exists(p):
                            os.remove(p)
                elif d < today and not path.endswith(".gz"):
                    _compress(path)
            except OSError:
                pass  # file busy or already gone; the next pass retries
