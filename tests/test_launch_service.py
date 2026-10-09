"""start.sh / start.bat launcher (mode choice) and service helpers (login item files, pid handling)."""
import io
import os
import plistlib
import sys
import tempfile
import unittest
from unittest import mock

import context  # noqa: F401
import launch
import service


_real_stdout = sys.stdout


def setUpModule():  # the code under test prints status messages; keep the test output clean
    sys.stdout = io.StringIO()


def tearDownModule():
    sys.stdout = _real_stdout


def write(path, text=""):
    with open(path, "w") as f:
        f.write(text)


def read(path):
    with open(path) as f:
        return f.read()


class FakeStream:
    def __init__(self, tty):
        self.tty = tty

    def isatty(self):
        return self.tty

    def write(self, _):
        pass

    def flush(self):
        pass


class LaunchModeTests(unittest.TestCase):
    def main(self, argv, env=None, tty=False, answer=None):
        env = {"WIFI_MODE": ""} if env is None else env
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(launch.sys, "stdin", FakeStream(tty)), mock.patch.object(launch.sys, "stdout", FakeStream(tty)), \
                mock.patch.object(launch, "ask", return_value=answer) as ask, \
                mock.patch.object(launch, "run_terminal", return_value=0) as terminal, \
                mock.patch.object(launch.service, "start", return_value=0) as start, \
                mock.patch.object(launch.service, "install", return_value=0) as install:
            rc = launch.main(argv)
        return rc, ask, terminal, start, install

    def test_flags_choose_without_asking(self):
        rc, ask, terminal, start, install = self.main(["--terminal"], tty=True)
        self.assertEqual((rc, terminal.call_count, start.call_count, install.call_count, ask.call_count), (0, 1, 0, 0, 0))
        rc, ask, terminal, start, install = self.main(["--service"], tty=True)
        self.assertEqual((start.call_count, install.call_count, terminal.call_count, ask.call_count), (1, 0, 0, 0))
        rc, ask, terminal, start, install = self.main(["--login"], tty=True)
        self.assertEqual((install.call_count, start.call_count, ask.call_count), (1, 0, 0))

    def test_other_arguments_go_to_the_collector(self):
        _, _, _, start, _ = self.main(["--service", "--interval", "10", "--scan-every", "600"])
        start.assert_called_once_with(["--interval", "10", "--scan-every", "600"])

    def test_interactive_terminal_asks(self):
        _, ask, terminal, start, install = self.main([], tty=True, answer="service")
        self.assertEqual((ask.call_count, start.call_count, terminal.call_count), (1, 1, 0))

    def test_no_terminal_never_asks_and_starts_the_service(self):
        rc, ask, terminal, start, install = self.main([], tty=False)
        self.assertEqual((rc, ask.call_count, start.call_count, terminal.call_count), (0, 0, 1, 0))

    def test_environment_variable_picks_the_mode(self):
        _, ask, terminal, start, install = self.main([], env={"WIFI_MODE": "login"}, tty=True)
        self.assertEqual((install.call_count, ask.call_count), (1, 0))

    def test_bad_input_is_rejected(self):
        self.assertEqual(self.main(["--terminal", "--service"])[0], 2)
        self.assertEqual(self.main([], env={"WIFI_MODE": "bogus"})[0], 2)

    def test_ask_accepts_default_and_retries_bad_answers(self):
        with mock.patch("builtins.input", side_effect=["", ]):
            self.assertEqual(launch.ask(), "terminal")
        with mock.patch("builtins.input", side_effect=["x", "3"]):
            self.assertEqual(launch.ask(), "login")
        with mock.patch("builtins.input", side_effect=EOFError):
            self.assertEqual(launch.ask(), "service")


class LoginItemTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_macos_launch_agent(self):
        plist = os.path.join(self.tmp.name, "agent.plist")
        with mock.patch.object(service, "IS_MAC", True), mock.patch.object(service, "IS_WIN", False), \
                mock.patch.object(service, "PLIST", plist), mock.patch.object(service, "start", return_value=0) as start:
            self.assertEqual(service.install(["--interval", "10"]), 0)
        with open(plist, "rb") as f:
            data = plistlib.load(f)
        self.assertTrue(data["RunAtLoad"])
        self.assertEqual(data["ProgramArguments"][-4:], ["run", "--", "--interval", "10"])
        start.assert_called_once_with(["--interval", "10"])

    def test_windows_startup_script_is_hidden_and_quoted(self):
        vbs = os.path.join(self.tmp.name, "WiFiMonitor.vbs")
        with mock.patch.object(service, "IS_MAC", False), mock.patch.object(service, "IS_WIN", True), \
                mock.patch.object(service, "STARTUP_VBS", vbs), mock.patch.object(service, "start", return_value=0), \
                mock.patch.object(service, "background_python", return_value=r"C:\Program Files\Python\pythonw.exe"):
            service.install([])
        text = read(vbs)
        self.assertIn('CreateObject("WScript.Shell").Run', text)
        self.assertIn(r'""C:\Program Files\Python\pythonw.exe""', text)  # paths with spaces are quoted for VBScript
        self.assertIn(", 0, False", text)  # hidden window, do not wait

    def test_uninstall_removes_the_item_and_stops(self):
        plist = os.path.join(self.tmp.name, "agent.plist")
        write(plist)
        with mock.patch.object(service, "IS_MAC", True), mock.patch.object(service, "IS_WIN", False), \
                mock.patch.object(service, "PLIST", plist), mock.patch.object(service, "stop") as stop, \
                mock.patch.object(service, "dashboard_up", return_value=False):
            self.assertEqual(service.uninstall(), 0)
        self.assertFalse(os.path.exists(plist))
        stop.assert_called_once()

    def test_uninstall_warns_when_a_terminal_run_is_still_up(self):
        with mock.patch.object(service, "IS_MAC", True), mock.patch.object(service, "PLIST", os.path.join(self.tmp.name, "none")), \
                mock.patch.object(service, "stop"), mock.patch.object(service, "dashboard_up", return_value=True):
            self.assertEqual(service.uninstall(), 1)


class PidTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pidfile = os.path.join(self.tmp.name, "service.pid")
        patcher = mock.patch.object(service, "PID_FILE", self.pidfile)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_read_pid(self):
        self.assertIsNone(service.read_pid())
        write(self.pidfile, "abc")
        self.assertIsNone(service.read_pid())
        write(self.pidfile, "1234\n")
        self.assertEqual(service.read_pid(), 1234)

    def test_stale_pid_is_not_running_and_stop_cleans_it_up(self):
        write(self.pidfile, "999999")
        with mock.patch.object(service, "is_ours", return_value=False):
            self.assertIsNone(service.running_pid())
            self.assertEqual(service.stop(), 0)
        self.assertFalse(os.path.exists(self.pidfile))

    def test_collector_children_get_the_service_flag(self):
        # the dashboard only offers its Stop button when WIFI_SERVICE is set by the supervisor
        self.assertIn('WIFI_SERVICE="1"', read(os.path.join(context.SRC, "service.py")))


if __name__ == "__main__":
    unittest.main()
