#!/usr/bin/env python3
"""Run WiFiMonitor as a background service on macOS and Windows.

    service.py start       start the collector and dashboard in the background
    service.py stop        stop them
    service.py restart
    service.py status      running or not, and whether the dashboard answers
    service.py open        start if needed, then open the dashboard in the browser
    service.py install     start now and at every login
    service.py uninstall   stop, remove the login item (also available as uninstall.sh / uninstall.bat)

Extra arguments go to the collector, e.g. `service.py start --interval 10 --scan-every 600`.
`install` stores them for the login start. Standard library only, no admin rights needed.

One supervisor process (`service.py run`) owns the collector and the dashboard server, restarts
either one if it dies, and writes a pid file so start, stop and status work the same on both systems.
Its output goes to logs/service.log (rotated, at most about 1.5 MB).
"""
import argparse
import logging
import logging.handlers
import os
import plistlib
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

import logstore

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
SRC = os.path.dirname(os.path.abspath(__file__))
ROOT = logstore.ROOT
PID_FILE = os.path.join(logstore.LOG_DIR, "service.pid")
OUT_FILE = os.path.join(logstore.LOG_DIR, "service.log")
PORT = int(os.environ.get("PORT", "8765"))
URL = "http://127.0.0.1:%d" % PORT
PLIST = os.path.expanduser("~/Library/LaunchAgents/com.wifimonitor.agent.plist")
STARTUP_VBS = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs", "Startup", "WiFiMonitor.vbs")


def background_python():
    """pythonw.exe on Windows so no console window appears; the running interpreter elsewhere."""
    exe = sys.executable
    if IS_WIN and os.path.basename(exe).lower() == "python.exe":
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(w):
            return w
    return exe


# ---- pid file and process checks

def read_pid():
    try:
        with open(PID_FILE) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def is_ours(pid):
    """True if `pid` is a live WiFiMonitor supervisor (guards against a stale pid that was reused)."""
    if not pid:
        return False
    try:
        if IS_WIN:
            out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True, creationflags=0x08000000).stdout
            return '"%d"' % pid in out
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
        return "service.py" in out
    except OSError:
        return False


def running_pid():
    pid = read_pid()
    return pid if is_ours(pid) else None


def dashboard_up():
    try:
        with urllib.request.urlopen(URL + "/api/control", timeout=1.5) as r:
            return r.status == 200
    except (OSError, ValueError):
        return False


# ---- supervisor (what actually runs in the background)

def supervise(collector_args):
    os.makedirs(logstore.LOG_DIR, exist_ok=True)
    other = read_pid()
    if other and other != os.getpid() and is_ours(other):
        return 0  # already running, e.g. a login start after a manual start
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    log = logging.getLogger("wifimonitor")
    log.setLevel(logging.INFO)
    handler = logging.handlers.RotatingFileHandler(OUT_FILE, maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(handler)

    py = background_python()
    env = dict(os.environ, PYTHONUNBUFFERED="1", WIFI_SERVICE="1")  # WIFI_SERVICE lets the dashboard offer a Stop button
    flags = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW
    jobs = {
        "collector": [py, os.path.join(SRC, "collector.py")] + collector_args,
        "dashboard": [py, os.path.join(SRC, "dashboard_server.py")],
    }
    procs, next_try, delay, started = {}, {n: 0.0 for n in jobs}, {n: 2.0 for n in jobs}, {}
    stopping = threading.Event()

    def pump(name, proc):
        for line in proc.stdout:
            log.info("[%s] %s", name, line.rstrip())

    def launch(name):
        p = subprocess.Popen(jobs[name], cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, errors="replace", creationflags=flags)
        threading.Thread(target=pump, args=(name, p), daemon=True).start()
        procs[name], started[name] = p, time.time()
        log.info("[service] started %s (pid %d)", name, p.pid)

    for sig in (signal.SIGINT, signal.SIGTERM) + ((signal.SIGBREAK,) if IS_WIN else ()):
        signal.signal(sig, lambda *_: stopping.set())

    log.info("[service] supervisor started (pid %d)", os.getpid())
    try:
        while not stopping.wait(1.0):
            now = time.time()
            for name in jobs:
                p = procs.get(name)
                if p is not None and p.poll() is not None:
                    log.info("[service] %s exited with code %s", name, p.returncode)
                    # a child that ran for a minute counts as healthy; a crash loop backs off up to a minute
                    delay[name] = 2.0 if now - started[name] > 60 else min(delay[name] * 2, 60.0)
                    next_try[name], procs[name] = now + delay[name], None
                if procs.get(name) is None and now >= next_try[name]:
                    launch(name)
    finally:
        for p in procs.values():
            if p is not None and p.poll() is None:
                p.terminate()
        deadline = time.time() + 5
        for p in procs.values():
            if p is not None:
                try:
                    p.wait(max(0.1, deadline - time.time()))
                except subprocess.TimeoutExpired:
                    p.kill()
        log.info("[service] stopped")
        if read_pid() == os.getpid():
            try:
                os.remove(PID_FILE)
            except OSError:
                pass
    return 0


# ---- controls

def start(extra):
    pid = running_pid()
    if pid:
        print("already running (pid %d)" % pid)
        return 0
    cmd = [background_python(), os.path.join(SRC, "service.py"), "run"] + (["--"] + extra if extra else [])
    kwargs = {"creationflags": 0x00000008 | 0x00000200} if IS_WIN else {"start_new_session": True}  # detached, own process group
    subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    for _ in range(40):
        time.sleep(0.5)
        if running_pid() and dashboard_up():
            print("started: dashboard at %s" % URL)
            return 0
    print("started, but the dashboard is not answering yet. Check %s" % OUT_FILE)
    return 1


def stop():
    pid = running_pid()
    if not pid:
        print("not running")
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
        return 0
    if IS_WIN:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=0x08000000)
    else:
        os.kill(pid, signal.SIGTERM)
    for _ in range(40):
        if not is_ours(pid):
            break
        time.sleep(0.25)
    else:
        if not IS_WIN:
            try:
                os.killpg(pid, signal.SIGKILL)  # the supervisor leads its own process group
            except OSError:
                pass
    try:
        os.remove(PID_FILE)
    except OSError:
        pass
    print("stopped")
    return 0


def status():
    pid = running_pid()
    up = dashboard_up()
    print("service:   %s" % ("running (pid %d)" % pid if pid else "stopped"))
    print("dashboard: %s" % ("%s is answering" % URL if up else "not answering"))
    print("at login:  %s" % ("yes" if autostart_installed() else "no"))
    print("log:       %s" % OUT_FILE)
    return 0 if pid else 3


def autostart_installed():
    return os.path.exists(PLIST if IS_MAC else STARTUP_VBS)


def install(extra):
    run_args = [background_python(), os.path.join(SRC, "service.py"), "run"] + (["--"] + extra if extra else [])
    if IS_MAC:
        os.makedirs(os.path.dirname(PLIST), exist_ok=True)
        with open(PLIST, "wb") as f:
            plistlib.dump({"Label": "com.wifimonitor.agent", "ProgramArguments": run_args, "WorkingDirectory": ROOT,
                           "RunAtLoad": True, "ProcessType": "Background"}, f)
    elif IS_WIN:
        os.makedirs(os.path.dirname(STARTUP_VBS), exist_ok=True)
        quoted = " ".join('""%s""' % a for a in run_args)
        with open(STARTUP_VBS, "w") as f:
            f.write('CreateObject("WScript.Shell").Run "%s", 0, False\r\n' % quoted)  # window style 0 = hidden
    else:
        print("install is only supported on macOS and Windows")
        return 2
    print("will start at every login (%s)" % (PLIST if IS_MAC else STARTUP_VBS))
    return start(extra)


def uninstall():
    """Stop the service and remove the login item. Logs and the project folder are left alone."""
    stop()
    path = PLIST if IS_MAC else STARTUP_VBS if IS_WIN else None
    if path and os.path.exists(path):
        os.remove(path)
        print("removed the login item %s" % path)
    else:
        print("no login item was installed")
    if dashboard_up():
        print("A dashboard is still answering on %s. It was probably started in a terminal with start.sh or start.bat --terminal: "
              "press Ctrl-C in that window to stop it." % URL)
        return 1
    print("WiFiMonitor is uninstalled and nothing is running. Your logs in %s are kept." % logstore.LOG_DIR)
    return 0


def main():
    ap = argparse.ArgumentParser(description="WiFiMonitor background service. Unknown arguments are passed to the collector.")
    ap.add_argument("command", choices=["start", "stop", "restart", "status", "open", "install", "uninstall", "run"])
    args, extra = ap.parse_known_args()
    extra = [a for a in extra if a != "--"]
    if args.command == "run":
        return supervise(extra)
    if args.command == "start":
        return start(extra)
    if args.command == "stop":
        return stop()
    if args.command == "restart":
        stop()
        return start(extra)
    if args.command == "status":
        return status()
    if args.command == "open":
        rc = start(extra)
        webbrowser.open(URL)
        return rc
    if args.command == "install":
        return install(extra)
    return uninstall()


if __name__ == "__main__":
    sys.exit(main())
