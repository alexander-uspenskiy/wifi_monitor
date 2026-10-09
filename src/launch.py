#!/usr/bin/env python3
"""Launcher behind start.sh and start.bat: run WiFiMonitor in this terminal or as a background service.

    start.sh --terminal   run here, stop with Ctrl-C
    start.sh --service    run in the background (no window); stop from the dashboard or `service.sh stop`
    start.sh --login      like --service, and also start it at every login
    start.sh              ask which one (interactive terminal only)

Any other arguments go to the collector, e.g. `start.sh --service --interval 10 --scan-every 600`.
WIFI_MODE=terminal|service|login works like the flags. With no flag, no WIFI_MODE and no interactive
terminal (an AI agent, a script) it never prompts and starts the background service, because a
foreground run would block the caller forever.
"""
import os
import subprocess
import sys
import time

import service

IS_WIN = sys.platform.startswith("win")
MODES = {"--terminal": "terminal", "--service": "service", "--login": "login"}
CONTROL = "service.bat" if IS_WIN else "./service.sh"
CHOICES = {"1": "terminal", "2": "service", "3": "login"}


def ask():
    print("How do you want to run WiFiMonitor?")
    print("  1) In this terminal window   (stop with Ctrl-C)                          [default]")
    print("  2) As a background service   (no window; stop from the dashboard or %s stop)" % CONTROL)
    print("  3) As a background service, and start it at every login")
    while True:
        try:
            answer = input("Choose 1, 2 or 3: ").strip() or "1"
        except EOFError:
            return "service"
        if answer in CHOICES:
            return CHOICES[answer]
        print("Please type 1, 2 or 3.")


def run_terminal(extra):
    pid = service.running_pid()
    if pid:
        print("The background service is already running (pid %d). Run `%s stop` first, or just open %s" % (pid, CONTROL, service.URL))
        return 1
    py = sys.executable
    kids = {"collector": subprocess.Popen([py, os.path.join(service.SRC, "collector.py")] + extra, cwd=service.ROOT),
            "dashboard": subprocess.Popen([py, os.path.join(service.SRC, "dashboard_server.py")], cwd=service.ROOT)}
    print("Dashboard: %s   (Ctrl-C stops both)" % service.URL)
    code = 0
    try:
        while True:
            time.sleep(0.5)
            gone = [n for n, p in kids.items() if p.poll() is not None]
            if gone:
                code = kids[gone[0]].returncode or 0
                print("%s exited (code %s), stopping." % (gone[0], code))
                break
    except KeyboardInterrupt:
        pass
    finally:
        for p in kids.values():
            if p.poll() is None:
                p.terminate()
        for p in kids.values():
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
    return code


def main(argv):
    if argv[:1] in (["-h"], ["--help"]):
        print(__doc__)
        return 0
    flags = [a for a in argv if a in MODES]
    extra = [a for a in argv if a not in MODES]
    if len(set(flags)) > 1:
        print("Choose only one of --terminal, --service, --login.")
        return 2
    mode = MODES[flags[0]] if flags else os.environ.get("WIFI_MODE", "").strip().lower()
    if mode not in MODES.values():
        if mode:
            print("WIFI_MODE must be terminal, service or login (got %r)." % mode)
            return 2
        mode = ask() if sys.stdin.isatty() and sys.stdout.isatty() else "service"
    if mode == "terminal":
        return run_terminal(extra)
    rc = service.install(extra) if mode == "login" else service.start(extra)
    if rc == 0:
        print("Running in the background. Stop it from the dashboard's Stop button or with `%s stop`; check it with `%s status`." % (CONTROL, CONTROL))
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
