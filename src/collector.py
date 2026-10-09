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


def run(cmd, timeout=10):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


# ---------------------------------------------------------------- network basics

def default_gateway():
    if IS_MAC:
        m = re.search(r"gateway:\s*(\S+)", run(["route", "-n", "get", "default"]))
        return m.group(1) if m else None
    rows = re.findall(r"^\s*0\.0\.0\.0\s+0\.0\.0\.0\s+(\S+)\s+\S+\s+(\d+)", run(["route", "print", "-4", "0.0.0.0"]), re.M)
    rows = [r for r in rows if re.match(r"\d+\.\d+\.\d+\.\d+$", r[0])]
    return min(rows, key=lambda r: int(r[1]))[0] if rows else None


def ping(host):
    """Round-trip time in ms, or None if the ping was lost."""
    if not host:
        return None
    cmd = ["ping", "-n", "1", "-w", "1000", host] if IS_WIN else ["ping", "-c", "1", "-W", "1000", host]
    m = re.search(r"time[=<]\s*([\d.]+)\s*ms", run(cmd, timeout=5))
    return float(m.group(1)) if m else None


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
    out = run([HELPER_BIN], timeout=5).strip()
    if out == "NA":
        return {"assoc": False}
    try:
        rssi, noise, ch, band, width, tx, phy = out.split()
        return {"assoc": True, "rssi": int(rssi), "noise": int(noise), "ch": int(ch), "band": band,
                "width": width, "tx": tx, "phy": MAC_PHY[int(phy)] if int(phy) < len(MAC_PHY) else "?"}
    except ValueError:
        return None


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
    kv = _kv(run(["netsh", "wlan", "show", "interfaces"]))
    if not kv:
        return None
    sig = re.match(r"(\d+)", kv.get("signal", ""))
    if kv.get("state", "").lower() != "connected" or not sig:
        return {"assoc": False}
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


def ms(v):
    return "LOST" if v is None else "%.3f" % v


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

    pool = cf.ThreadPoolExecutor(max_workers=2)
    gw, gw_at, last_day, next_scan = None, 0.0, None, 0.0
    warned_no_wifi = False
    try:
        while True:
            start = time.time()
            now = dt.datetime.now()
            if now.date() != last_day:
                logstore.maintain(now.date())
                last_day = now.date()
            if gw is None or start - gw_at > 60:
                gw, gw_at = default_gateway(), start
            f_gw, f_net = pool.submit(ping, gw), pool.submit(ping, args.host)
            wifi = get_wifi(have_helper)
            if IS_WIN and wifi is None and not warned_no_wifi:
                warned_no_wifi = True
                print("warning: `netsh wlan show interfaces` returned nothing usable, so only pings are logged. "
                      "Check that Wi-Fi is on, that Location is enabled for desktop apps (Windows 11 24H2 and newer), "
                      "and that the Windows display language is English.")
            line = "%s gateway_ms=%s internet_ms=%s%s" % (
                now.strftime("%Y-%m-%d %H:%M:%S"), ms(f_gw.result()), ms(f_net.result()), format_wifi(wifi))
            logstore.append("monitor", line, now)
            print(line, flush=True)
            if logstore.read_control()["scan_paused"]:
                next_scan = start + args.scan_every  # on resume, wait a full interval before scanning
            elif args.scan_every and start >= next_scan:
                threading.Thread(target=do_scan, args=(have_helper,), daemon=True).start()
                next_scan = start + args.scan_every
            time.sleep(max(0.0, args.interval - (time.time() - start)))
    except KeyboardInterrupt:
        print("stopped")


if __name__ == "__main__":
    main()
