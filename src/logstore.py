"""Daily-rotated log storage shared by the collector and the dashboard server.

Files live in <project>/logs (override with WIFI_LOG_DIR):
    wifi-monitor-YYYY-MM-DD.log   one line per sample, plus EVENT notes
    wifi-scan-YYYY-MM-DD.log      one line per nearby-network scan

Today's file is plain text. Older files are gzip-compressed, and files older than
WIFI_LOG_KEEP_DAYS (default 14) are deleted. Readers handle both .log and .log.gz.
"""
import datetime as dt
import gzip
import json
import os
import re
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.environ.get("WIFI_LOG_DIR") or os.path.join(ROOT, "logs")
KEEP_DAYS = int(os.environ.get("WIFI_LOG_KEEP_DAYS", "14"))
KINDS = ("monitor", "scan")

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
CONTROL_DEFAULTS = {"scan_paused": False, "interval": None, "scan_every": None}


def valid_control(key, value):
    if key == "scan_paused":
        return isinstance(value, bool)
    if key == "interval":
        return value is None or (isinstance(value, (int, float)) and not isinstance(value, bool) and value in INTERVAL_CHOICES)
    if key == "scan_every":
        return value is None or (isinstance(value, (int, float)) and not isinstance(value, bool) and value in SCAN_CHOICES)
    return False


def read_control():
    """Runtime switches shared by the dashboard and the collector (re-read every sample). Invalid values fall back to the default."""
    try:
        with open(CONTROL_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {k: (data[k] if k in data and valid_control(k, data[k]) else default) for k, default in CONTROL_DEFAULTS.items()}


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
