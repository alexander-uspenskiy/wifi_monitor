#!/usr/bin/env python3
"""Wi-Fi stability collector for macOS and Windows.

Every few seconds it pings the router and the internet and records the Wi-Fi link
details (signal, noise, channel, band, rate). Every few minutes it scans nearby
networks. Output goes to daily-rotated files in logs/ (see logstore.py).
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time

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
    info = {"pid": os.getpid(), "started": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "system": system_info(),
            "interval": args.interval, "scan_every": args.scan_every, "host": args.host, "gateway": None, "mac_helper": have_helper}
    logstore.write_info(info)  # lets the dashboard show the real probe settings
    try:
        while True:
            start = time.time()
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
            gw_val, gw_err = f_gw.result() if f_gw else (ERR, gw_reason)
            net_val, net_err = f_net.result()
            line = "%s gateway_ms=%s internet_ms=%s%s" % (now.strftime("%Y-%m-%d %H:%M:%S"), ms(gw_val), ms(net_val), format_wifi(wifi))
            logstore.append("monitor", line, now)
            print(line, flush=True)
            for what, reason in (("router ping", gw_err), ("internet ping", net_err),
                                 ("Wi-Fi status", wifi.get("error") if wifi else None)):
                event = tracker.update(what, reason)
                if event:
                    logstore.append("monitor", "%s EVENT %s" % (now.strftime("%Y-%m-%d %H:%M:%S"), event), now)
                    print(event, flush=True)
            ctl, _, scan_every = effective(args)
            if scan_every != last_scan_every:  # changed on the dashboard: wait a full new period before the next scan
                last_scan_every, next_scan = scan_every, start + scan_every
            if ctl["scan_paused"]:
                next_scan = start + scan_every  # on resume, wait a full interval before scanning
            elif scan_every and start >= next_scan:
                threading.Thread(target=do_scan, args=(have_helper,), daemon=True).start()
                next_scan = start + scan_every
            pause(start, args)
    except KeyboardInterrupt:
        print("stopped")


if __name__ == "__main__":
    main()
