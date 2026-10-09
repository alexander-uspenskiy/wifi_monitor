#!/usr/bin/env python3
"""Serves dashboard.html and JSON feeds parsed from the rotated logs in logs/. Local only (127.0.0.1)."""
import datetime as dt
import json
import os
import re
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import logstore

DIR = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8765"))
MAX_ROWS = 30000

LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (.*)$")
EVENT = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) EVENT (.*)$")
KV = re.compile(r"(\w+)=(\S+)")


def num(v):
    if v is None:
        return None
    m = re.match(r"-?\d+(\.\d+)?", v)
    return float(m.group()) if m else None


def parse(line):
    m = LINE.match(line.strip())
    if not m:
        return None
    ts, rest = m.groups()
    kv = dict(KV.findall(rest))
    gw = kv.get("gateway_ms", kv.get("gateway"))
    net = kv.get("internet_ms", kv.get("internet"))
    if gw is None and net is None:
        return None
    return {
        "t": ts,
        "gw": num(gw),
        "net": num(net),
        "gwLost": gw == "LOST",
        "netLost": net == "LOST",
        "assoc": "not associated" not in rest,
        "rssi": num(kv.get("rssi")),
        "noise": num(kv.get("noise")),
        "snr": num(kv.get("snr")),
        "ch": num(kv.get("ch")),
        "band": kv.get("band", "").replace("GHz", "") or None,
        "width": num(kv.get("width")),
        "tx": num(kv.get("tx")),
        "phy": kv.get("phy"),
    }


DAY = re.compile(r"^\d{4}-\d\d-\d\d$")


def read_rows(day=None):
    lines = logstore.read_day("monitor", day) if day else logstore.read_lines("monitor", MAX_ROWS)
    return [r for r in map(parse, lines) if r]


def read_events(day=None):
    out = []
    for line in (logstore.read_day("monitor", day) if day else logstore.read_all("monitor")):
        m = EVENT.match(line.strip())
        if m:
            out.append({"t": m.group(1), "text": m.group(2)})
    return out


def _secs(t):
    return int(t[11:13]) * 3600 + int(t[14:16]) * 60 + int(t[17:19])


def _pct(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * p))]


def _avg(vals):
    return round(sum(vals) / len(vals), 1) if vals else None


def summarize(rows, notes):
    """Per-day statistics shown in the dashboard's daily summary table."""
    n = len(rows)
    gw = [r["gw"] for r in rows if r["gw"] is not None]
    net = [r["net"] for r in rows if r["net"] is not None]
    outage, changes, prev_key = 0, 0, None
    for i, r in enumerate(rows):
        down = (r["gwLost"] and r["netLost"]) or not r["assoc"]
        if down and i + 1 < n:
            outage += min(_secs(rows[i + 1]["t"]) - _secs(r["t"]), 30)
        if r["ch"] is not None:
            key = (r["ch"], r["band"])
            if prev_key and key != prev_key:
                changes += 1
            prev_key = key
    pct = lambda k: round(100 * sum(1 for r in rows if r[k]) / n, 1) if n else None
    return {
        "samples": n,
        "gwLoss": pct("gwLost"), "netLoss": pct("netLost"),
        "gwP95": _pct(gw, .95), "netP95": _pct(net, .95), "netMedian": _pct(net, .5),
        "outageMin": round(outage / 60, 1), "changes": changes,
        "rssi": _avg([r["rssi"] for r in rows if r["rssi"] is not None]),
        "snr": _avg([r["snr"] for r in rows if r["snr"] is not None]),
        "notes": [x["text"] for x in notes],
    }


_summary_cache = {}


def read_days():
    """One summary per day that has a log file, newest first. Past days are cached."""
    out = []
    for day, path in logstore.list_files("monitor"):
        try:
            st = os.stat(path)
        except OSError:
            continue
        key = (st.st_mtime_ns, st.st_size)
        hit = _summary_cache.get(path)
        if not hit or hit[0] != key:
            hit = (key, summarize(read_rows(day), read_events(day)))
            _summary_cache[path] = hit
        out.append({"day": day, **hit[1]})
    return sorted(out, key=lambda d: d["day"], reverse=True)


def overlaps(own, ch, band):
    """Does a network on (ch, band) share spectrum with our own channel/width?"""
    if not own or own[1] != band:
        return False
    oc, _, ow = own
    # width unknown (Windows): assume 80 MHz on 5 GHz
    ow = int(ow) if str(ow).isdigit() else (80 if band == "5" else 20)
    if band == "2.4":
        return abs(ch - oc) <= 4
    if ow >= 80:
        blocks = [(36, 48), (52, 64), (100, 112), (116, 128), (132, 144), (149, 161)]
        return any(lo <= oc <= hi and lo <= ch <= hi for lo, hi in blocks)
    if ow == 40:
        base = 149 if oc >= 149 else 100 if oc >= 100 else 36
        lo = base + ((oc - base) // 8) * 8
        return lo <= ch <= lo + 4
    return ch == oc


BLOCKS5 = [("36-48", 36, 48, False), ("52-64", 52, 64, True), ("100-112", 100, 112, True),
           ("116-128", 116, 128, True), ("132-144", 132, 144, True), ("149-161", 149, 161, False)]


def block_stats(nets, own):
    """Crowding per candidate channel block: 5 GHz 80 MHz blocks and 2.4 GHz channels 1/6/11."""
    out, mine = [], None
    for name, lo, hi, dfs in BLOCKS5:
        m = [n for n in nets if n[1] == "5" and lo <= n[0] <= hi]
        out.append({"k": f"5G {name}", "dfs": dfs, "total": len(m), "strong": sum(1 for n in m if n[2] >= -75)})
        if own and own[1] == "5" and lo <= own[0] <= hi:
            mine = f"5G {name}"
    for c in (1, 6, 11):
        m = [n for n in nets if n[1] == "2.4" and abs(n[0] - c) <= 4]
        out.append({"k": f"2.4G ch{c}", "dfs": False, "total": len(m), "strong": sum(1 for n in m if n[2] >= -75)})
    if own and own[1] == "2.4":
        mine = f"2.4G ch{min((1, 6, 11), key=lambda c: abs(c - own[0]))}"
    return out, mine


def read_scans(day=None):
    out = []
    for line in (logstore.read_day("scan", day) if day else logstore.read_lines("scan", 3000)):
        m = LINE.match(line.strip())
        if not m:
            continue
        try:
            j = json.loads(m.group(2))
        except ValueError:
            continue
        nets, own = j.get("nets", []), j.get("own")
        if not nets:
            continue
        mine = [n for n in nets if overlaps(own, n[0], n[1])]
        counts = {}
        for ch, band, rssi in nets:
            key = f"{band}:{ch}"
            counts[key] = counts.get(key, 0) + 1
        blocks, mine_key = block_stats(nets, own)
        out.append({
            "t": m.group(1), "own": own, "total": len(nets), "blocks": blocks, "mine": mine_key,
            "overlap": len(mine), "strong": sum(1 for n in mine if n[2] >= -75),
            "counts": counts,
        })
    return out


def note_event(text):
    when = dt.datetime.now()
    logstore.append("monitor", f"{when:%Y-%m-%d %H:%M:%S} EVENT {text}", when)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parts = urlsplit(self.path)
        path = parts.path
        day = (parse_qs(parts.query).get("day") or [None])[0]
        if day is not None and not DAY.match(day):
            return self._send(400, b"bad day", "text/plain")
        if path == "/api/data":
            self._send(200, json.dumps(read_rows(day)).encode(), "application/json")
        elif path == "/api/events":
            self._send(200, json.dumps(read_events(day)).encode(), "application/json")
        elif path == "/api/scan":
            self._send(200, json.dumps(read_scans(day)).encode(), "application/json")
        elif path == "/api/control":
            self._send(200, json.dumps(logstore.read_control()).encode(), "application/json")
        elif path == "/api/service":
            # controllable only when the supervisor in service.py started this server
            self._send(200, json.dumps({"controllable": bool(os.environ.get("WIFI_SERVICE"))}).encode(), "application/json")
        elif path == "/api/days":
            self._send(200, json.dumps(read_days()).encode(), "application/json")
        elif path in ("/", "/index.html"):
            with open(os.path.join(DIR, "dashboard.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        # Only accept same-origin JSON posts from the dashboard itself (blocks other web pages hitting localhost).
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        same = origin is None or origin == f"http://{host}"
        path = self.path.split("?")[0]
        if path not in ("/api/event", "/api/control", "/api/service") or not same or "application/json" not in self.headers.get("Content-Type", ""):
            return self._send(403, b"forbidden", "text/plain")
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 2000)
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._send(400, b"bad request", "text/plain")
        if path == "/api/service":
            # stop the whole background service. Also checks Host, so a page reaching localhost through DNS rebinding cannot stop it.
            if body.get("action") != "stop" or not os.environ.get("WIFI_SERVICE") or host not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
                return self._send(403, b"forbidden", "text/plain")
            self._send(200, json.dumps({"ok": True}).encode(), "application/json")
            kwargs = {"creationflags": 0x00000008 | 0x00000200} if sys.platform.startswith("win") else {"start_new_session": True}
            subprocess.Popen([sys.executable, os.path.join(DIR, "service.py"), "stop"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)  # detached: it ends this process too
            return
        if path == "/api/control":
            paused = body.get("scan_paused")
            if not isinstance(paused, bool):
                return self._send(400, b"scan_paused must be true or false", "text/plain")
            changed = logstore.read_control()["scan_paused"] != paused
            ctl = logstore.write_control(scan_paused=paused)
            if changed:
                note_event("Scanning paused" if paused else "Scanning resumed")
            return self._send(200, json.dumps(ctl).encode(), "application/json")
        text = " ".join(str(body.get("text", "")).split())[:200]
        if not text:
            return self._send(400, b"empty", "text/plain")
        note_event(text)
        self._send(200, json.dumps({"ok": True}).encode(), "application/json")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(f"Wi-Fi dashboard on http://127.0.0.1:{PORT}  (reading {logstore.LOG_DIR})")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
