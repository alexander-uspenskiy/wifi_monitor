#!/usr/bin/env python3
"""Wi-Fi stability collector for macOS and Windows.

Every few seconds it pings the router and the internet and records the Wi-Fi link
details (signal, noise, channel, band, rate). Every few minutes it scans nearby
networks, and on request (or on a schedule) it measures the internet download speed.
Output goes to daily-rotated files in logs/ (see logstore.py).
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import ipaddress
import json
import os
import platform
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from urllib.parse import urlsplit

import logstore

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform.startswith("win")
HERE = os.path.dirname(os.path.abspath(__file__))
HELPER_SRC = os.path.join(HERE, "macos", "wifi-info.swift")
HELPER_BIN = os.path.join(logstore.ROOT, "bin", "wifi-info")
MAC_PHY = ["?", "a", "b", "g", "n", "ac", "ax"]


ERR = "ERR"  # a measurement that could not be taken, as opposed to LOST (a ping that got no reply) or a real disconnect
CREATE_NO_WINDOW = 0x08000000


class Cmd:
    """Outcome of one external command: exit code, text, stderr, and `error` when it could not run at all."""
    def __init__(self, rc=None, out="", err="", error=None):
        self.rc, self.out, self.err, self.error = rc, out, err, error


def _decode(b):
    return (b or b"").decode("oem" if IS_WIN else "utf-8", errors="replace")  # console tools write in the OEM code page on Windows


UNSUPPORTED = "unsupported Windows display language"  # reasons starting with this are announced at once and never clear by themselves


def non_english(text):
    """True when command output contains letters outside ASCII, which means a localised Windows."""
    return any(ch.isalpha() and ord(ch) > 127 for ch in text or "")


def display_language():
    """(name, is_english) for the Windows display language, e.g. ("ru_RU", False); None when unknown.
    Always None on macOS: the ping, route and CoreWLAN helper output used there does not depend on the system language."""
    if not IS_WIN:
        return None
    try:
        import ctypes
        import locale
        langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        return locale.windows_locale.get(langid, "0x%04x" % langid), (langid & 0x3FF) == 0x09
    except (AttributeError, OSError, ValueError):
        return None


def system_info():
    if IS_MAC:
        return "macOS %s" % (platform.mac_ver()[0] or "?")
    lang = display_language()
    return "Windows %s%s" % (platform.version(), ", display language %s" % lang[0] if lang else "")


def language_error(text):
    lang = display_language()
    return "%s%s: not English, which is not supported yet (the labels of netsh and ping are read in English). Output seen: %s" % (
        UNSUPPORTED, " (%s)" % lang[0] if lang else "", snippet(text))


def snippet(text, n=120):
    """One short line of command output for an error message."""
    return " ".join((text or "").split())[:n] or "no output"


def run_cmd(cmd, timeout=10):
    kw = {"creationflags": CREATE_NO_WINDOW} if IS_WIN else {}  # console tools get a hidden console, never a flashing window
    try:
        p = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout, **kw)
    except subprocess.TimeoutExpired:
        return Cmd(error="%s timed out after %ss" % (cmd[0], timeout))
    except OSError as e:
        return Cmd(error="could not start %s: %s" % (cmd[0], e))
    return Cmd(p.returncode, _decode(p.stdout), _decode(p.stderr))


def run(cmd, timeout=10):
    return run_cmd(cmd, timeout).out


# ---------------------------------------------------------------- network basics

def default_gateway():
    """(gateway, None); (None, None) when there is no default route; (ERR, reason) when the lookup itself failed."""
    if IS_MAC:
        r = run_cmd(["route", "-n", "get", "default"])
        if r.error:
            return ERR, r.error
        m = re.search(r"gateway:\s*(\S+)", r.out)
        return (m.group(1), None) if m else (None, None)
    r = run_cmd(["route", "print", "-4", "0.0.0.0"])
    if r.error:
        return ERR, r.error
    if not r.out.strip():
        return ERR, "route exit %s with no output: %s" % (r.rc, snippet(r.err))
    rows = re.findall(r"^\s*0\.0\.0\.0\s+0\.0\.0\.0\s+(\S+)\s+\S+\s+(\d+)", r.out, re.M)
    rows = [x for x in rows if re.match(r"\d+\.\d+\.\d+\.\d+$", x[0])]
    return (min(rows, key=lambda x: int(x[1]))[0], None) if rows else (None, None)


PING_TIME = re.compile(r"[=<]\s*([\d.]+)\s*(?:ms|\u043c\u0441)")
PING_STATS_WIN = re.compile(r"=\s*\d+,\s*[^=,]+=\s*(\d+),\s*[^=,]+=\s*\d+")  # Sent = 1, Received = 0, Lost = 1 (any language); group 1 = received
PING_STATS_MAC = re.compile(r"\d+ packets transmitted, (\d+) (?:packets )?received")
PING_UNREACHABLE = re.compile(r"unreachable|TTL expired", re.I)
PING_FAILURE = re.compile(r"general failure|transmit failed", re.I)


def ping(host):
    """(round-trip ms, None) for a reply; (None, None) for a ping that ran and got no reply (real loss);
    (ERR, reason) when the ping itself could not be done, so it must not count as packet loss."""
    if not host:
        return None, None
    cmd = ["ping", "-n", "1", "-w", "1000", host] if IS_WIN else ["ping", "-c", "1", "-W", "1000", host]
    r = run_cmd(cmd, timeout=5)
    if r.error:
        return ERR, r.error
    m = PING_TIME.search(r.out)
    if m:
        return float(m.group(1)), None
    text = r.out + " " + r.err
    if PING_FAILURE.search(text):
        return ERR, "ping failed: %s" % snippet(text)
    stats = (PING_STATS_WIN if IS_WIN else PING_STATS_MAC).search(r.out)
    if stats:
        if int(stats.group(1)) == 0 or PING_UNREACHABLE.search(text):
            return None, None  # no reply, or only an "unreachable" answer: the target cannot be reached
        if non_english(text):
            return ERR, language_error(text)
        return ERR, "ping got a reply but its time could not be read: %s" % snippet(text)
    return ERR, "ping exit %s without a result: %s" % (r.rc, snippet(text))


# ---------------------------------------------------------------- macOS (CoreWLAN helper)

def ensure_mac_helper():
    if os.path.exists(HELPER_BIN):
        return True
    if not shutil.which("swiftc"):
        print("warning: swiftc not found (install Xcode Command Line Tools: xcode-select --install); logging pings only")
        return False
    print("building the macOS Wi-Fi helper...")
    os.makedirs(os.path.dirname(HELPER_BIN), exist_ok=True)
    res = subprocess.run(["swiftc", "-O", HELPER_SRC, "-o", HELPER_BIN], capture_output=True, text=True)
    if res.returncode != 0:
        print("warning: helper build failed, logging pings only:\n" + res.stderr)
        return False
    return True


def mac_wifi():
    r = run_cmd([HELPER_BIN], timeout=5)
    if r.error:
        return {"error": r.error}
    out = r.out.strip()
    if out == "NA":
        return {"assoc": False}
    try:
        rssi, noise, ch, band, width, tx, phy = out.split()
        return {"assoc": True, "rssi": int(rssi), "noise": int(noise), "ch": int(ch), "band": band,
                "width": width, "tx": tx, "phy": MAC_PHY[int(phy)] if int(phy) < len(MAC_PHY) else "?"}
    except ValueError:
        return {"error": "Wi-Fi helper exit %s, unexpected output: %s" % (r.rc, snippet(out + " " + r.err))}


# ---------------------------------------------------------------- Windows (netsh)
# Parses English-language netsh output. Windows reports signal as a percentage; it is
# converted to an approximate dBm value (dBm = percent / 2 - 100). Noise is not available.

def _kv(out):
    kv = {}
    for line in out.splitlines():
        m = re.match(r"^\s*([A-Za-z() ]+?)\s*:\s*(.*)$", line)
        if m:
            kv.setdefault(m.group(1).strip().lower(), m.group(2).strip())  # first interface only
    return kv


def _win_band(kv_band, ch):
    m = re.match(r"([\d.]+)", kv_band or "")
    if m:
        return m.group(1)
    return "2.4" if ch <= 14 else "5"


def win_wifi():
    """Link details, {"assoc": False} for a real disconnect, or {"error": reason} when netsh gave nothing we can read."""
    r = run_cmd(["netsh", "wlan", "show", "interfaces"])
    if r.error:
        return {"error": r.error}
    kv = _kv(r.out)
    if "state" not in kv:
        if non_english(r.out) or kv:  # English output always has a "State" line, so labels in another language
            return {"error": language_error(r.out)}
        return {"error": "netsh exit %s, no interface state in its output: %s" % (r.rc, snippet(r.out + " " + r.err))}  # permission message, no adapter
    if kv["state"].lower() != "connected":
        return {"assoc": False}
    sig = re.match(r"(\d+)", kv.get("signal", ""))
    if not sig:
        return {"error": "netsh reports connected but gives no signal value: %s" % snippet(r.out)}
    ch = int(kv.get("channel", "0") or 0)
    return {"assoc": True, "rssi": round(int(sig.group(1)) / 2 - 100), "ch": ch,
            "band": _win_band(kv.get("band"), ch), "tx": kv.get("transmit rate (mbps)"),
            "phy": kv.get("radio type", "").replace("802.11", "") or None}


def win_scan():
    out = run(["netsh", "wlan", "show", "networks", "mode=bssid"], timeout=30)
    nets, cur = [], {}

    def flush():
        if "sig" in cur and "ch" in cur:
            nets.append([cur["ch"], _win_band(cur.get("band"), cur["ch"]), round(cur["sig"] / 2 - 100)])

    for line in out.splitlines():
        s = line.strip()
        if re.match(r"BSSID \d+\s*:", s):
            flush()
            cur = {}
        elif (m := re.match(r"Signal\s*:\s*(\d+)%", s)):
            cur["sig"] = int(m.group(1))
        elif (m := re.match(r"Channel\s*:\s*(\d+)", s)):
            cur["ch"] = int(m.group(1))
        elif (m := re.match(r"Band\s*:\s*(.+)", s)):
            cur["band"] = m.group(1)
    flush()
    own = win_wifi()
    own = [own["ch"], own["band"], "?"] if own and own.get("assoc") else None
    return {"own": own, "nets": nets}


# ---------------------------------------------------------------- sampling and logging

def get_wifi(have_mac_helper):
    if IS_MAC:
        return mac_wifi() if have_mac_helper else None
    return win_wifi()


def format_wifi(w):
    if w is None:
        return ""
    if "error" in w:
        return " wifi=ERR"
    if not w["assoc"]:
        return " rssi=NA (not associated)"
    parts = ["rssi=%d" % w["rssi"]]
    if w.get("noise") is not None:
        parts += ["noise=%d" % w["noise"], "snr=%d" % (w["rssi"] - w["noise"])]
    parts += ["ch=%d" % w["ch"], "band=%sGHz" % w["band"]]
    if w.get("width") and w["width"] != "?":
        parts.append("width=%sMHz" % w["width"])
    if w.get("tx"):
        parts.append("tx=%sMbps" % w["tx"])
    if w.get("phy") and w["phy"] != "?":
        parts.append("phy=11%s" % w["phy"])
    return " " + " ".join(parts)


def do_scan(have_mac_helper):
    if IS_MAC:
        if not have_mac_helper:
            return
        payload = run([HELPER_BIN, "scan"], timeout=30).strip()
        if not payload.startswith("{"):
            return
    else:
        payload = json.dumps(win_scan(), separators=(",", ":"))
    when = dt.datetime.now()  # stamped at completion so the line lands in the current day's file
    logstore.append("scan", "%s %s" % (when.strftime("%Y-%m-%d %H:%M:%S"), payload), when)


def run_scan(gate, have_mac_helper):
    try:
        do_scan(have_mac_helper)
    finally:
        gate.end_scan()


# ---------------------------------------------------------------- internet download speed

SPEED_SETTLE_S = 5          # a scan and a speed test never start within this many seconds of each other
SPEED_CAP_S = 60            # a test that is still downloading after this long is stopped, and still counts
SPEED_CHUNK = 65536
SPEED_PROGRESS_S = 0.5      # how often the running test writes logs/speed.json
SPEED_CONNECT_TIMEOUT = 15  # also the longest wait for any single piece of data
SPEED_HEARTBEAT_S = 0.5     # while a test runs, logs/speed.json is refreshed at least this often, even when no data arrives (slow DNS, stalled link)
SPEED_MIN_BYTES = 1000000   # a download that ended early with less than this cannot be measured
SPEED_MIN_S = 0.2           # a measured download shorter than this cannot be measured


class Gate:
    """A scan makes the radio leave the channel and a speed test fills the link: they must not overlap, nor follow each other closely.
    `now` (monotonic seconds) can be passed in by tests."""

    def __init__(self):
        self.lock = threading.Lock()
        self.scanning = self.speeding = False
        self.scan_end = self.speed_end = None

    def _settled(self, since, now):
        return since is None or now - since >= SPEED_SETTLE_S

    def try_begin_scan(self, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            if self.scanning or self.speeding or not self._settled(self.speed_end, now):
                return False
            self.scanning = True
            return True

    def end_scan(self, now=None):
        with self.lock:
            self.scanning, self.scan_end = False, time.monotonic() if now is None else now

    def try_begin_speed(self, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            if self.scanning or self.speeding or not self._settled(self.scan_end, now):
                return False
            self.speeding = True
            return True

    def end_speed(self, now=None):
        with self.lock:
            self.speeding, self.speed_end = False, time.monotonic() if now is None else now

    def speed_overlaps(self, since_monotonic):
        """Is a speed test running, or did one end after `since_monotonic`? Such a sample's pings are not representative."""
        with self.lock:
            return self.speeding or (self.speed_end is not None and self.speed_end >= since_monotonic)


def log_event(text):
    when = dt.datetime.now()
    logstore.append("monitor", "%s EVENT %s" % (when.strftime("%Y-%m-%d %H:%M:%S"), text), when)
    print(text, flush=True)


def speed_request(url, mb):
    """The GET for one test. The default server takes the size in the address; any other gets a Range header (and a server that
    ignores it is simply read for mb MB and then closed)."""
    n = int(mb) * 1000000
    custom = bool(url) and url != logstore.SPEED_DEFAULT_URL
    req = urllib.request.Request((url if custom else logstore.SPEED_DEFAULT_URL).replace("{bytes}", str(n)))
    req.add_header("Accept-Encoding", "identity")
    req.add_header("Cache-Control", "no-cache")
    req.add_header("User-Agent", "WiFiMonitor/1")
    if custom:
        req.add_header("Range", "bytes=0-%d" % (n - 1))
    return req


def speed_host(url):
    try:
        return urlsplit(url or logstore.SPEED_DEFAULT_URL).hostname
    except ValueError:
        return None


def local_host(host):
    """Is `host` this computer or its link-local network (localhost, 127.x, ::1, 169.254.x, fe80::)? Names are not resolved."""
    host = (host or "").lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return False
    ip = getattr(ip, "ipv4_mapped", None) or ip
    return ip.is_loopback or ip.is_link_local


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Follows at most 3 redirects, and only to an http or https address with a host name (never file:, ftp: or user:password@).
    It does not downgrade https to http, and a server on the internet cannot send the download to this computer or its local
    network (127.x, ::1, 169.254.x): that is only followed when the address being redirected from is itself local."""
    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            old, new = urlsplit(req.full_url), urlsplit(newurl)
            ok = new.scheme in ("http", "https") and bool(new.hostname) and "@" not in new.netloc
            ok = ok and not (old.scheme == "https" and new.scheme == "http")
            ok = ok and (local_host(old.hostname) or not local_host(new.hostname))
        except ValueError:
            ok = False
        if not ok:
            raise urllib.error.URLError("the server redirected to an address that is not allowed")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def speed_opener():
    """http and https only (urllib's default opener would also open file: and ftp: addresses), through the system proxy if there is one."""
    op = urllib.request.OpenerDirector()
    for h in (urllib.request.ProxyHandler(), urllib.request.UnknownHandler(), urllib.request.HTTPHandler(), urllib.request.HTTPSHandler(),
              SafeRedirect(), urllib.request.HTTPDefaultErrorHandler(), urllib.request.HTTPErrorProcessor()):
        op.add_handler(h)
    return op


def speed_error(exc, host=None):
    """(kind, short reason without the address) for a failed test. kind: dns, timeout, http, tls, connect or other."""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            host = urlsplit(exc.url or "").hostname or host
        except ValueError:
            pass
        if 300 <= exc.code < 400:  # urllib refuses a redirect to a non-http address, or after 3 hops, with the redirect's own code
            return "http", "HTTP %s: the server redirected to an address that cannot be used (at most 3 redirects, to http or https only)" % exc.code
        text = "HTTP %s %s from %s" % (exc.code, snippet(exc.reason if isinstance(exc.reason, str) else "", 40), host or "the server")
        if 400 <= exc.code < 500:
            text += "; the server refused the download, try a different address in the Internet download speed card"
        return "http", " ".join(text.split())
    cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(cause, socket.gaierror):
        return "dns", "could not look up %s (no DNS answer)" % (host or "the server")
    if isinstance(cause, ssl.SSLError):
        text = "%s (TLS error)" % snippet(str(cause))
        if IS_MAC:
            text += "; python.org Python on macOS needs its certificates installed: run 'Install Certificates.command' from the Python folder in Applications"
        return "tls", text
    if isinstance(cause, (socket.timeout, TimeoutError)):
        return "timeout", "timed out waiting for %s" % (host or "the server")
    if isinstance(cause, OSError):
        return "connect", "could not connect to %s: %s" % (host or "the server", snippet(str(cause), 80))
    if isinstance(cause, str):
        return "other", snippet(cause)
    return "other", snippet("%s: %s" % (type(cause).__name__, cause))


def do_speedtest(url, mb, trigger, wifi=None):
    """One single-stream download of `mb` MB, measured after the first chunk arrived (so connect, TLS and the request are not counted).
    Synchronous; returns the speed log record. A failure is ok:false with a reason, never a speed of 0."""
    n = int(mb) * 1000000
    started = dt.datetime.now()
    stamp = started.strftime("%Y-%m-%d %H:%M:%S")
    host = speed_host(url)
    custom = bool(url) and url != logstore.SPEED_DEFAULT_URL
    res = {"v": 1, "start": stamp, "trigger": trigger, "ok": False, "mbps": None, "ttfb_ms": None, "bytes": 0, "dur_s": None,
           "total_s": None, "status": None, "host": host, "mb": int(mb), "range": custom, "capped": False, "kind": None, "reason": None,
           "wifi": wifi}
    state = {"running": True, "phase": "connecting", "pid": os.getpid(), "id": stamp, "trigger": trigger, "host": host, "mb": int(mb),
             "target_bytes": n, "bytes": 0, "elapsed_s": 0.0, "mbps": None, "avg_mbps": None, "ttfb_ms": None, "next_at": None}
    t0, wall0 = time.monotonic(), time.time()
    total = first_len = 0
    t_first = t_end = None
    write_lock, last = threading.Lock(), {}

    def report(now, rate=None, avg=None):
        with write_lock:
            last.clear()
            last.update(state, bytes=total, elapsed_s=round(now - t0, 2), mbps=rate, avg_mbps=avg)
            logstore.write_speed_progress({**last, "updated": time.time()})

    stop_beat = threading.Event()

    def heartbeat():
        """The dashboard calls a running test stale when its file is not refreshed for 5 s, so keep it fresh while nothing arrives."""
        while not stop_beat.wait(SPEED_HEARTBEAT_S):
            with write_lock:
                if last:
                    logstore.write_speed_progress({**last, "elapsed_s": round(time.monotonic() - t0, 2), "updated": time.time()})

    def slept():
        return (time.time() - wall0) - (time.monotonic() - t0) > 10  # the wall clock ran on while the monotonic one stood still

    beat = threading.Thread(target=heartbeat, daemon=True)
    try:
        report(t0)
        beat.start()
        req = speed_request(url, mb)
        with speed_opener().open(req, timeout=SPEED_CONNECT_TIMEOUT) as resp:
            res["status"] = getattr(resp, "status", None)
            first = resp.read1(min(SPEED_CHUNK, n))  # whatever arrives first, so the time to first byte is real
            t_first = t_end = time.monotonic()
            total = first_len = len(first)
            res["ttfb_ms"] = round((t_first - t0) * 1000, 1)
            state.update(phase="downloading", ttfb_ms=res["ttfb_ms"])
            window, last_report, eof = deque([(t_first, total)]), t_first, not first
            while total < n and not eof:
                if time.monotonic() - t0 > SPEED_CAP_S:  # checked before every receive, so a slow trickle stops near the cap
                    res["capped"] = True
                    t_end = time.monotonic()  # a stall at the end counts against the speed
                    break
                chunk = resp.read1(min(SPEED_CHUNK, n - total))
                if not chunk:
                    eof = True
                    break
                total += len(chunk)
                t_end = time.monotonic()
                window.append((t_end, total))
                if t_end - last_report >= SPEED_PROGRESS_S:
                    last_report = t_end
                    while len(window) > 2 and t_end - window[0][0] > 1.0:
                        window.popleft()
                    span = t_end - window[0][0]
                    report(t_end, round((total - window[0][1]) * 8 / span / 1e6, 2) if span > 0 else None,
                           round((total - first_len) * 8 / (t_end - t_first) / 1e6, 2) if t_end > t_first else None)
        cut = eof and (getattr(resp, "length", None) or 0) > 0  # the server promised more than it sent
        res["bytes"] = total
        res["total_s"] = round(t_end - t0, 2)
        dur = t_end - t_first
        res["dur_s"] = round(dur, 2)
        if slept():
            res.update(kind="other", reason="interrupted (computer slept)")
        elif res["capped"] and total == first_len:  # nothing but the first chunk in the whole time: a stall, not a speed
            res.update(kind="timeout", reason="the download stalled: no data after the first chunk in %d s" % SPEED_CAP_S)
        elif cut:
            res.update(kind="connect", reason="the server closed the connection after %.1f MB, before the download was complete" % (total / 1e6))
        elif eof and total < min(n, SPEED_MIN_BYTES):
            res.update(kind="short", reason="download was too short to measure: only %.1f MB arrived before the server ended it; choose a larger file or MB" % (total / 1e6))
        elif dur < SPEED_MIN_S:
            res.update(kind="short", reason="download was too short to measure (%.1f MB in %.2f s); choose a larger file or MB" % (total / 1e6, dur))
        else:
            res.update(ok=True, mbps=round((total - first_len) * 8 / dur / 1e6, 2))
    except Exception as e:  # any failure is reported as an ERR result, never as a speed
        t_end = time.monotonic()
        res["bytes"], res["total_s"], res["dur_s"] = total, round(t_end - t0, 2), None
        if slept():
            res.update(kind="other", reason="interrupted (computer slept)")
        else:
            res["kind"], res["reason"] = speed_error(e, host)
    finally:
        stop_beat.set()
        if beat.is_alive():
            beat.join(2)
    return res


def run_speedtest_thread(gate, tracker, url, mb, trigger, wifi, every=0):
    """Runs one test to the end: events, the speed log line, the final progress file (which also says when the next scheduled test is
    due, `every` seconds after this one). The caller already holds gate.try_begin_speed()."""
    try:
        if trigger == "manual":
            log_event("Speed test started (manual, %s MB from %s)" % (mb, speed_host(url)))
        res = do_speedtest(url, mb, trigger, wifi)
        when = dt.datetime.now()  # stamped at completion so the line lands in the current day's file
        stamp = when.strftime("%Y-%m-%d %H:%M:%S")
        logstore.append("speed", "%s %s" % (stamp, json.dumps(res, separators=(",", ":"))), when)
        if res["ok"]:
            log_event("Speed test finished: %.1f Mbps (%.1f MB in %.1f s)" % (res["mbps"], res["bytes"] / 1e6, res["dur_s"]))
        else:
            log_event("Speed test failed: %s" % res["reason"])
        next_at = (when + dt.timedelta(seconds=every)).strftime("%Y-%m-%d %H:%M:%S") if every else None
        logstore.write_speed_progress({"running": False, "phase": "done" if res["ok"] else "error", "pid": os.getpid(), "id": res["start"],
                                       "trigger": trigger, "host": res["host"], "mb": res["mb"], "target_bytes": res["mb"] * 1000000,
                                       "bytes": res["bytes"], "elapsed_s": res["total_s"], "mbps": res["mbps"], "avg_mbps": res["mbps"],
                                       "ttfb_ms": res["ttfb_ms"], "next_at": next_at, "updated": time.time(), "result": {**res, "t": stamp}}, tries=5)
        event = tracker.update("speed test", None if res["ok"] else res["reason"])
        if event:
            log_event(event)
    finally:
        gate.end_speed()


def wifi_brief(w):
    """The link details stored with a speed test, from the latest sample; None when there is no Wi-Fi link to describe."""
    if not w or "error" in w or not w.get("assoc"):
        return None
    try:
        tx = int(float(w["tx"])) if w.get("tx") else None
    except ValueError:
        tx = None
    return {"rssi": w["rssi"], "snr": w["rssi"] - w["noise"] if w.get("noise") is not None else None, "ch": w["ch"], "band": w["band"], "tx": tx}


def effective(args):
    """The control file (set from the dashboard) overrides the command-line values. Returns (control, interval, scan_every)."""
    ctl = logstore.read_control()
    return ctl, ctl["interval"] or args.interval, args.scan_every if ctl["scan_every"] is None else ctl["scan_every"]


def pause(start, args):
    """Wait until the next sample is due, in 1 s steps, so an interval changed on the dashboard applies within a second."""
    while True:
        remaining = start + effective(args)[1] - time.time()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 1.0))


def ms(v):
    return "LOST" if v is None else v if v == ERR else "%.3f" % v


class ErrorTracker:
    """Turns repeated measurement errors into one event, and the recovery into another, so a flapping fault does not flood the log."""
    AFTER = 3  # consecutive failed samples before an error is announced

    def __init__(self):
        self.streak, self.announced = {}, {}

    def update(self, what, reason):
        """`reason` is None when the measurement worked. Returns the event text to log, if any."""
        if reason is None:
            self.streak[what] = 0
            if self.announced.pop(what, False):
                return "Measurement recovered: %s is being measured again" % what
            return None
        self.streak[what] = self.streak.get(what, 0) + 1
        needed = 1 if reason.startswith(UNSUPPORTED) else self.AFTER  # a language problem will not fix itself, so say so at once
        if self.streak[what] >= needed and not self.announced.get(what):
            self.announced[what] = True
            return "Measurement error: %s could not be measured (%s). Not counted as packet loss or a disconnect." % (what, reason)
        return None


def main():
    env = os.environ.get
    ap = argparse.ArgumentParser(description="Wi-Fi stability collector")
    ap.add_argument("--interval", type=float, default=float(env("WIFI_INTERVAL", 5)), help="seconds between samples (default 5)")
    ap.add_argument("--scan-every", type=int, default=int(env("WIFI_SCAN_EVERY", 900)), help="seconds between nearby-network scans, 0 = off (default 900)")
    ap.add_argument("--host", default=env("WIFI_PING_HOST", "1.1.1.1"), help="internet ping target (default 1.1.1.1)")
    args = ap.parse_args()

    if not (IS_MAC or IS_WIN):
        sys.exit("Unsupported platform: this collector supports macOS and Windows.")
    have_helper = ensure_mac_helper() if IS_MAC else False
    print("logging to %s (keeping %d days); Ctrl-C to stop" % (logstore.LOG_DIR, logstore.KEEP_DAYS))
    print("system: %s" % system_info())
    lang = display_language()
    if lang and not lang[1]:
        print("warning: the Windows display language is %s. Only English is supported yet, so Wi-Fi details will be "
              "reported as measurement errors." % lang[0])

    pool = cf.ThreadPoolExecutor(max_workers=2)
    gw, gw_reason, gw_at, last_day, next_scan = None, None, 0.0, None, 0.0
    last_scan_every = args.scan_every
    tracker = ErrorTracker()
    gate = Gate()
    raw = effective(args)[0]
    ctl, scan_every = {**logstore.CONTROL_DEFAULTS, **raw}, args.scan_every
    # A "Run now" stamp already in the control file at start-up is old: never replayed. If that first read failed, the first good read is the baseline.
    baseline = getattr(raw, "ok", True)
    seen_run, last_speed_every = ctl["speed_run"] if baseline else None, ctl["speed_every"]
    boot = time.time()
    next_speed = boot + last_speed_every if last_speed_every else float("inf")  # no test at boot; a schedule counts from now
    speed_pending, seen_speed_end, last_idle, latest_wifi, hold_done = None, None, None, None, False
    info = {"pid": os.getpid(), "started": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "system": system_info(),
            "interval": args.interval, "scan_every": args.scan_every, "host": args.host, "gateway": None, "mac_helper": have_helper, "speed": True}
    logstore.write_info(info)  # lets the dashboard show the real probe settings
    try:
        while True:
            start, mono = time.time(), time.monotonic()
            now = dt.datetime.now()
            if now.date() != last_day:
                logstore.maintain(now.date())
                last_day = now.date()
            if gw in (None, ERR) or start - gw_at > 60:
                (gw, gw_reason), gw_at = default_gateway(), start
                shown = gw if gw not in (None, ERR) else None
                if shown != info["gateway"]:
                    info["gateway"] = shown
                    logstore.write_info(info)
            f_gw = pool.submit(ping, gw) if gw != ERR else None
            f_net = pool.submit(ping, args.host)
            wifi = get_wifi(have_helper)
            latest_wifi = wifi
            gw_val, gw_err = f_gw.result() if f_gw else (ERR, gw_reason)
            net_val, net_err = f_net.result()
            line = "%s gateway_ms=%s internet_ms=%s%s" % (now.strftime("%Y-%m-%d %H:%M:%S"), ms(gw_val), ms(net_val), format_wifi(wifi))
            if gate.speed_overlaps(mono):
                line += " speed=1"  # a download test was running: these pings are not representative
            logstore.append("monitor", line, now)
            print(line, flush=True)
            for what, reason in (("router ping", gw_err), ("internet ping", net_err),
                                 ("Wi-Fi status", wifi.get("error") if wifi else None)):
                event = tracker.update(what, reason)
                if event:
                    logstore.append("monitor", "%s EVENT %s" % (now.strftime("%Y-%m-%d %H:%M:%S"), event), now)
                    print(event, flush=True)
            raw, _, new_scan_every = effective(args)
            fresh = getattr(raw, "ok", True)
            if fresh:
                ctl, scan_every = {**logstore.CONTROL_DEFAULTS, **raw}, new_scan_every
            # else the file could not be read this time (busy, damaged): keep the last values rather than falling back to defaults
            if scan_every != last_scan_every:  # changed on the dashboard: wait a full new period before the next scan
                last_scan_every, next_scan = scan_every, start + scan_every
            if ctl["scan_paused"]:
                next_scan = start + scan_every  # on resume, wait a full interval before scanning
            elif scan_every and start >= next_scan and gate.try_begin_scan():  # blocked by a speed test: try again next sample
                threading.Thread(target=run_scan, args=(gate, have_helper), daemon=True).start()
                next_scan = start + scan_every

            speed_every = ctl["speed_every"]
            if gate.speed_end != seen_speed_end:  # a test finished: the next scheduled one is a full period after it
                seen_speed_end = gate.speed_end
                next_speed = time.time() - (time.monotonic() - seen_speed_end) + speed_every if speed_every else float("inf")
                hold_done = True  # the test left its result in speed.json: leave it there until something else needs the file
            if speed_every != last_speed_every:
                last_speed_every, next_speed, hold_done = speed_every, start + speed_every if speed_every else float("inf"), False
            if not speed_every and speed_pending == "scheduled":
                speed_pending = None  # the schedule was switched off while the test was waiting for a scan to end
            if fresh:
                run = ctl["speed_run"]
                if not baseline:
                    seen_run, baseline = run, True
                elif run is not None and (seen_run is None or run > seen_run):  # "Run now" on the dashboard
                    seen_run = run
                    if gate.speeding:
                        log_event("Speed test already running, request ignored")
                    else:
                        speed_pending = "manual"
            if speed_pending is None and speed_every and start >= next_speed:
                speed_pending = "scheduled"
            if speed_pending and gate.try_begin_speed():  # refused while a scan runs or just ended: stays pending, tried again next sample
                url, mb = ctl["speed_url"] or logstore.SPEED_DEFAULT_URL, ctl["speed_mb"]
                threading.Thread(target=run_speedtest_thread, args=(gate, tracker, url, mb, speed_pending, wifi_brief(latest_wifi), speed_every), daemon=True).start()
                speed_pending, next_speed, hold_done = None, float("inf"), False
            if not gate.speeding and not hold_done:
                idle = {"running": False, "phase": "idle",
                        "next_at": dt.datetime.fromtimestamp(next_speed).strftime("%Y-%m-%d %H:%M:%S") if next_speed != float("inf") else None}
                if idle != last_idle and logstore.write_speed_progress({**idle, "updated": time.time()}):  # a failed write is retried next sample
                    last_idle = idle
            pause(start, args)
    except KeyboardInterrupt:
        print("stopped")


if __name__ == "__main__":
    main()
