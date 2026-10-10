"""Collector: command running, ping and Wi-Fi classification (real loss vs measurement error), language checks, log line format,
and the internet download speed test (against a local server: no internet is used)."""
import contextlib
import datetime as dt
import io
import itertools
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import context  # noqa: F401  (sets up sys.path and a throwaway log directory)
import collector as c

Cmd = c.Cmd


@contextlib.contextmanager
def platform(win, result=None):
    """Pretend to be Windows (win=True) or macOS, and make every external command return `result`."""
    with mock.patch.object(c, "IS_WIN", win), mock.patch.object(c, "IS_MAC", not win), \
            mock.patch.object(c, "run_cmd", return_value=result):
        yield


WIN_OK = ("Pinging 1.1.1.1 with 32 bytes of data:\nReply from 1.1.1.1: bytes=32 time=19ms TTL=57\n\n"
          "Ping statistics for 1.1.1.1:\n    Packets: Sent = 1, Received = 1, Lost = 0 (0% loss),\n")
WIN_FAST = "Reply from 192.168.1.1: bytes=32 time<1ms TTL=64\n\nPackets: Sent = 1, Received = 1, Lost = 0 (0% loss),\n"
WIN_LOST = ("Pinging 1.1.1.1 with 32 bytes of data:\nRequest timed out.\n\nPing statistics for 1.1.1.1:\n"
            "    Packets: Sent = 1, Received = 0, Lost = 1 (100% loss),\n")
WIN_UNREACHABLE = ("Reply from 192.168.1.5: Destination host unreachable.\n\nPing statistics for 1.1.1.1:\n"
                   "    Packets: Sent = 1, Received = 1, Lost = 0 (0% loss),\n")
WIN_GENERAL_FAILURE = ("Pinging 1.1.1.1 with 32 bytes of data:\nPING: transmit failed. General failure.\n\n"
                       "Ping statistics for 1.1.1.1:\n    Packets: Sent = 1, Received = 0, Lost = 1 (100% loss),\n")
WIN_NO_HOST = "Ping request could not find host foo. Please check the name and try again.\n"
WIN_DE_LOST = "Ping-Statistik für 1.1.1.1:\n    Pakete: Gesendet = 1, Empfangen = 0, Verloren = 1 (100% Verlust),\n"
WIN_RU_OK = ("Ответ от 1.1.1.1: число байт=32 время=20 мс TTL=57\nСтатистика Ping для 1.1.1.1:\n"
             "    Пакетов: отправлено = 1, получено = 1, потеряно = 0 (0% потерь)\n")
WIN_RU_NO_TIME = ("Ответ от 1.1.1.1: число байт=32 TTL=57\nСтатистика Ping для 1.1.1.1:\n"
                  "    Пакетов: отправлено = 1, получено = 1, потеряно = 0 (0% потерь)\n")
MAC_OK = ("64 bytes from 1.1.1.1: icmp_seq=0 ttl=57 time=18.5 ms\n\n--- 1.1.1.1 ping statistics ---\n"
          "1 packets transmitted, 1 packets received, 0.0% packet loss\n")
MAC_LOST = "--- 1.1.1.1 ping statistics ---\n1 packets transmitted, 0 packets received, 100.0% packet loss\n"

NETSH_OK = ("There is 1 interface on the system:\n\n    Name                   : Wi-Fi\n    State                  : connected\n"
            "    Channel                : 13\n    Radio type             : 802.11n\n    Band                   : 2.4 GHz\n"
            "    Receive rate (Mbps)    : 72\n    Transmit rate (Mbps)   : 72\n    Signal                 : 100%\n")
NETSH_DISCONNECTED = "There is 1 interface on the system:\n\n    Name                   : Wi-Fi\n    State                  : disconnected\n"
NETSH_LOCATION = "Network shell commands need location permission to access WLAN information. Turn on Location services on the Settings app's Location page.\n"
NETSH_NO_ADAPTER = "There is no wireless interface on the system.\n"
NETSH_DE = "    Name                   : WLAN\n    Status                 : verbunden\n    Signal                 : 80%\n"
NETSH_RU = "    Имя                    : Беспроводная сеть\n    Состояние              : подключено\n"


class RunCmdTests(unittest.TestCase):
    def test_captures_output_and_exit_code(self):
        r = c.run_cmd([sys.executable, "-c", "import sys; print('hi'); sys.stderr.write('oops'); sys.exit(3)"])
        self.assertEqual((r.rc, r.out.strip(), r.err.strip(), r.error), (3, "hi", "oops", None))

    def test_missing_program_is_an_error_not_empty_output(self):
        r = c.run_cmd(["definitely-not-a-real-program-xyz"])
        self.assertIsNone(r.rc)
        self.assertTrue(r.error.startswith("could not start"))

    def test_timeout_is_an_error(self):
        r = c.run_cmd([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.5)
        self.assertIn("timed out", r.error)

    def test_run_keeps_returning_plain_text(self):
        self.assertEqual(c.run([sys.executable, "-c", "print('x')"]).strip(), "x")
        self.assertEqual(c.run(["definitely-not-a-real-program-xyz"]), "")

    def _spawn_kwargs(self, win):
        done = mock.Mock(returncode=0, stdout=b"", stderr=b"")
        with mock.patch.object(c, "IS_WIN", win), mock.patch.object(c, "_decode", side_effect=lambda b: ""), \
                mock.patch.object(c.subprocess, "run", return_value=done) as run:
            c.run_cmd(["x"])
        return run.call_args.kwargs

    def test_windows_commands_get_a_hidden_console_and_no_stdin(self):
        kw = self._spawn_kwargs(win=True)
        self.assertEqual(kw["creationflags"], 0x08000000)
        self.assertEqual(kw["stdin"], subprocess.DEVNULL)

    def test_mac_commands_have_no_windows_flags(self):
        kw = self._spawn_kwargs(win=False)
        self.assertNotIn("creationflags", kw)
        self.assertEqual(kw["stdin"], subprocess.DEVNULL)


class PingTests(unittest.TestCase):
    def ping(self, win, out="", rc=0, err="", error=None, host="1.1.1.1"):
        with platform(win, Cmd(rc, out, err, error)):
            return c.ping(host)

    def test_windows_replies(self):
        self.assertEqual(self.ping(True, WIN_OK), (19.0, None))
        self.assertEqual(self.ping(True, WIN_FAST), (1.0, None))
        self.assertEqual(self.ping(True, WIN_RU_OK), (20.0, None))

    def test_windows_real_loss_is_not_an_error(self):
        self.assertEqual(self.ping(True, WIN_LOST, rc=1), (None, None))
        self.assertEqual(self.ping(True, WIN_DE_LOST, rc=1), (None, None))  # statistics are matched without English words
        self.assertEqual(self.ping(True, WIN_UNREACHABLE, rc=1), (None, None))

    def test_windows_failures_are_errors_not_loss(self):
        for name, out in (("general failure", WIN_GENERAL_FAILURE), ("unknown host", WIN_NO_HOST), ("empty output", "")):
            with self.subTest(name):
                value, reason = self.ping(True, out, rc=1)
                self.assertEqual(value, c.ERR)
                self.assertTrue(reason)

    def test_reply_with_unreadable_time_is_an_error_not_loss(self):
        value, reason = self.ping(True, WIN_RU_NO_TIME)
        self.assertEqual(value, c.ERR)
        self.assertTrue(reason.startswith(c.UNSUPPORTED), reason)  # non-English letters: reported as an unsupported language

    def test_process_problems_are_errors(self):
        self.assertEqual(self.ping(True, error="ping timed out after 5s"), (c.ERR, "ping timed out after 5s"))
        self.assertEqual(self.ping(False, error="could not start ping: x")[0], c.ERR)

    def test_mac(self):
        self.assertEqual(self.ping(False, MAC_OK), (18.5, None))
        self.assertEqual(self.ping(False, MAC_LOST, rc=2), (None, None))
        self.assertEqual(self.ping(False, "", rc=71, err="ping: socket: Operation not permitted\n")[0], c.ERR)

    def test_no_target_means_no_reply_and_no_command(self):
        with platform(True, None):
            self.assertEqual(c.ping(None), (None, None))
            c.run_cmd.assert_not_called()


class GatewayTests(unittest.TestCase):
    ROUTE = ("IPv4 Route Table\nActive Routes:\nNetwork Destination        Netmask          Gateway       Interface  Metric\n"
             "          0.0.0.0          0.0.0.0      192.168.1.1   192.168.1.20     35\n"
             "          0.0.0.0          0.0.0.0      10.0.0.1      10.0.0.5        900\n")

    def test_windows_picks_lowest_metric(self):
        with platform(True, Cmd(0, self.ROUTE)):
            self.assertEqual(c.default_gateway(), ("192.168.1.1", None))

    def test_windows_no_default_route_is_real(self):
        with platform(True, Cmd(0, "IPv4 Route Table\n")):
            self.assertEqual(c.default_gateway(), (None, None))

    def test_windows_failed_lookup_is_an_error(self):
        with platform(True, Cmd(1, "", "denied")):
            self.assertEqual(c.default_gateway()[0], c.ERR)
        with platform(True, Cmd(error="could not start route")):
            self.assertEqual(c.default_gateway(), (c.ERR, "could not start route"))

    def test_mac(self):
        with platform(False, Cmd(0, "   route to: default\n    gateway: 192.168.100.1\n  interface: en0\n")):
            self.assertEqual(c.default_gateway(), ("192.168.100.1", None))
        with platform(False, Cmd(1, "route: writing to routing socket: not in table\n")):
            self.assertEqual(c.default_gateway(), (None, None))
        with platform(False, Cmd(error="could not start route")):
            self.assertEqual(c.default_gateway()[0], c.ERR)


class WindowsWifiTests(unittest.TestCase):
    def wifi(self, out="", rc=0, error=None):
        with platform(True, Cmd(rc, out, "", error)):
            return c.win_wifi()

    def test_connected(self):
        w = self.wifi(NETSH_OK)
        self.assertEqual((w["assoc"], w["rssi"], w["ch"], w["band"], w["tx"], w["phy"]), (True, -50, 13, "2.4", "72", "n"))

    def test_real_disconnect(self):
        self.assertEqual(self.wifi(NETSH_DISCONNECTED), {"assoc": False})

    def test_unreadable_output_is_an_error_never_a_disconnect(self):
        for name, out in (("location permission", NETSH_LOCATION), ("no adapter", NETSH_NO_ADAPTER), ("empty", "")):
            with self.subTest(name):
                w = self.wifi(out)
                self.assertIn("error", w)
                self.assertNotIn("assoc", w)
                self.assertFalse(w["error"].startswith(c.UNSUPPORTED))  # English text: not a language problem

    def test_connected_without_signal_is_an_error(self):
        self.assertIn("error", self.wifi("    Name : Wi-Fi\n    State : connected\n"))

    def test_command_failure_is_an_error(self):
        self.assertIn("error", self.wifi(error="could not start netsh: x"))

    def test_non_english_output_reports_unsupported_language(self):
        for name, out in (("German", NETSH_DE), ("Russian", NETSH_RU)):
            with self.subTest(name):
                w = self.wifi(out)
                self.assertTrue(w["error"].startswith(c.UNSUPPORTED), w)
                self.assertIn("not supported yet", w["error"])


class MacWifiTests(unittest.TestCase):
    def wifi(self, out="", rc=0, error=None):
        with platform(False, Cmd(rc, out, "", error)):
            return c.mac_wifi()

    def test_connected(self):
        w = self.wifi("-70 -89 149 5 80 351 5\n")
        self.assertEqual((w["assoc"], w["rssi"], w["noise"], w["ch"], w["band"], w["width"], w["tx"], w["phy"]),
                         (True, -70, -89, 149, "5", "80", "351", "ac"))

    def test_not_associated(self):
        self.assertEqual(self.wifi("NA\n"), {"assoc": False})

    def test_garbage_and_failures_are_errors(self):
        self.assertIn("error", self.wifi("what is this"))
        self.assertIn("error", self.wifi(error="could not start wifi-info"))

    def test_macos_does_not_depend_on_the_system_language(self):
        with mock.patch.object(c, "IS_WIN", False), mock.patch.object(c, "IS_MAC", True):
            self.assertIsNone(c.display_language())
            self.assertTrue(c.system_info().startswith("macOS"))


class LanguageTests(unittest.TestCase):
    def test_non_english_detection(self):
        self.assertFalse(c.non_english("State : connected 100%"))
        self.assertFalse(c.non_english("SSID : My Network 123"))
        self.assertTrue(c.non_english("Состояние : подключено"))
        self.assertTrue(c.non_english("Verbunden: größer"))
        self.assertFalse(c.non_english(""))

    def test_display_language_on_windows(self):
        fake = mock.Mock()
        for langid, want in ((0x0409, ("en_US", True)), (0x0419, ("ru_RU", False)), (0x0407, ("de_DE", False))):
            fake.windll.kernel32.GetUserDefaultUILanguage.return_value = langid
            with mock.patch.object(c, "IS_WIN", True), mock.patch.dict(sys.modules, {"ctypes": fake}):
                self.assertEqual(c.display_language(), want)

    def test_display_language_unknown_when_the_call_is_unavailable(self):
        with mock.patch.object(c, "IS_WIN", True):  # on this system ctypes has no windll, or the call fails
            fake = mock.Mock()
            fake.windll.kernel32.GetUserDefaultUILanguage.side_effect = OSError
            with mock.patch.dict(sys.modules, {"ctypes": fake}):
                self.assertIsNone(c.display_language())

    def test_language_error_names_the_language(self):
        with mock.patch.object(c, "display_language", return_value=("ru_RU", False)):
            msg = c.language_error("Состояние : подключено")
        self.assertIn("ru_RU", msg)
        self.assertIn("not supported yet", msg)
        self.assertTrue(msg.startswith(c.UNSUPPORTED))


class FormatTests(unittest.TestCase):
    """Normal samples must keep exactly the log format the dashboard and the existing logs use."""

    def test_mac_sample(self):
        w = {"assoc": True, "rssi": -70, "noise": -89, "ch": 149, "band": "5", "width": "80", "tx": "351", "phy": "ac"}
        self.assertEqual(c.format_wifi(w), " rssi=-70 noise=-89 snr=19 ch=149 band=5GHz width=80MHz tx=351Mbps phy=11ac")

    def test_windows_sample(self):
        w = {"assoc": True, "rssi": -50, "ch": 13, "band": "2.4", "tx": "72", "phy": "n"}
        self.assertEqual(c.format_wifi(w), " rssi=-50 ch=13 band=2.4GHz tx=72Mbps phy=11n")

    def test_other_states(self):
        self.assertEqual(c.format_wifi(None), "")
        self.assertEqual(c.format_wifi({"assoc": False}), " rssi=NA (not associated)")
        self.assertEqual(c.format_wifi({"error": "anything"}), " wifi=ERR")

    def test_ping_values(self):
        self.assertEqual((c.ms(2.0), c.ms(None), c.ms(c.ERR)), ("2.000", "LOST", "ERR"))


class EffectiveSettingsTests(unittest.TestCase):
    ARGS = type("Args", (), {"interval": 5.0, "scan_every": 900})()

    def effective(self, control):
        with mock.patch.object(c.logstore, "read_control", return_value={"scan_paused": False, "interval": None, "scan_every": None, **control}):
            return c.effective(self.ARGS)

    def test_command_line_values_apply_by_default(self):
        _, interval, scan_every = self.effective({})
        self.assertEqual((interval, scan_every), (5.0, 900))

    def test_dashboard_values_override(self):
        _, interval, scan_every = self.effective({"interval": 10, "scan_every": 300})
        self.assertEqual((interval, scan_every), (10, 300))

    def test_zero_scan_period_means_off_not_default(self):
        self.assertEqual(self.effective({"scan_every": 0})[2], 0)

    def test_pause_waits_for_the_current_interval_and_notices_a_change(self):
        clock = {"now": 100.0}
        control = {"interval": 15}

        def fake_sleep(sec):
            self.assertLessEqual(sec, 1.0)  # short steps, so a change is noticed quickly
            clock["now"] += sec
            if clock["now"] >= 103.0:
                control["interval"] = 2  # the user picks a shorter interval while the collector waits

        with mock.patch.object(c.time, "time", side_effect=lambda: clock["now"]), mock.patch.object(c.time, "sleep", side_effect=fake_sleep), \
                mock.patch.object(c.logstore, "read_control", side_effect=lambda: {"scan_paused": False, "interval": control["interval"], "scan_every": None}):
            c.pause(100.0, self.ARGS)
        self.assertAlmostEqual(clock["now"], 103.0)  # due at 100 + 2 s once the shorter interval applies, so it returns at the first check after that

    def test_pause_returns_at_once_when_the_sample_took_longer_than_the_interval(self):
        with mock.patch.object(c.time, "time", return_value=200.0), mock.patch.object(c.time, "sleep") as sleep, \
                mock.patch.object(c.logstore, "read_control", return_value={"scan_paused": False, "interval": None, "scan_every": None}):
            c.pause(100.0, self.ARGS)
        sleep.assert_not_called()


class ErrorTrackerTests(unittest.TestCase):
    def test_announces_once_after_three_failures_then_recovery(self):
        t = c.ErrorTracker()
        events = [t.update("router ping", "boom") for _ in range(5)]
        self.assertEqual([e is not None for e in events], [False, False, True, False, False])
        self.assertIn("router ping could not be measured (boom)", events[2])
        self.assertIn("Not counted as packet loss", events[2])
        self.assertTrue(t.update("router ping", None).startswith("Measurement recovered"))
        self.assertIsNone(t.update("router ping", None))

    def test_short_blips_are_silent(self):
        t = c.ErrorTracker()
        self.assertEqual([t.update("x", r) for r in ("e", None, "e", "e", None)], [None] * 5)

    def test_language_problem_is_announced_at_once(self):
        t = c.ErrorTracker()
        self.assertIn("not supported", t.update("Wi-Fi status", c.UNSUPPORTED + ": not English, which is not supported yet"))
        self.assertIsNone(t.update("Wi-Fi status", c.UNSUPPORTED + ": again"))

    def test_sources_are_tracked_separately(self):
        t = c.ErrorTracker()
        for _ in range(3):
            t.update("router ping", "x")
        self.assertIsNone(t.update("internet ping", None))


class MainLoopTests(unittest.TestCase):
    """Runs collector.main() for a few samples with simulated measurements and reads back what it logged."""

    def run_main(self, gw_results, net_results, wifi_results, gateway=("192.168.1.1", None), win=True, control=None, scan_every=0):
        logged, sleeps = [], []
        self.info_writes = []
        gws, nets, wifis = iter(gw_results), iter(net_results), iter(wifi_results)

        def fake_ping(host):
            return next(gws) if host == "192.168.1.1" else next(nets)

        def fake_sleep(_):
            sleeps.append(1)
            if len(sleeps) >= len(gw_results):
                raise KeyboardInterrupt

        with mock.patch.object(c, "IS_WIN", win), mock.patch.object(c, "IS_MAC", not win), \
                mock.patch.object(c, "default_gateway", **({"side_effect": gateway} if isinstance(gateway, list) else {"return_value": gateway})) as gw_lookup, \
                mock.patch.object(c.logstore, "write_info", side_effect=lambda info: self.info_writes.append(dict(info))), \
                mock.patch.object(c, "ping", side_effect=fake_ping), mock.patch.object(c, "get_wifi", side_effect=lambda h: next(wifis)), \
                mock.patch.object(c, "ensure_mac_helper", return_value=False), \
                mock.patch.object(c, "pause", side_effect=lambda *a: fake_sleep(None)), \
                mock.patch.object(c.logstore, "append", side_effect=lambda kind, line, when=None: logged.append(line)), \
                mock.patch.object(c.logstore, "maintain"), mock.patch.object(c.logstore, "read_control", return_value={"scan_paused": False, "interval": None, "scan_every": None, **(control or {})}), \
                mock.patch.object(c, "do_scan") as do_scan, \
                mock.patch.object(sys, "argv", ["collector.py", "--scan-every", str(scan_every)]), contextlib.redirect_stdout(io.StringIO()):
            c.main()
        self.scans = do_scan.call_count
        return [re.sub(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d ", "", line) for line in logged], gw_lookup

    GOOD_WIFI = {"assoc": True, "rssi": -50, "ch": 13, "band": "2.4", "tx": "72", "phy": "n"}

    def test_normal_samples_keep_the_existing_format(self):
        lines, _ = self.run_main([(3.0, None)] * 2, [(20.0, None)] * 2, [self.GOOD_WIFI] * 2)
        self.assertEqual(lines, ["gateway_ms=3.000 internet_ms=20.000 rssi=-50 ch=13 band=2.4GHz tx=72Mbps phy=11n"] * 2)

    def test_real_loss_is_logged_as_lost_without_events(self):
        lines, _ = self.run_main([(None, None)] * 4, [(None, None)] * 4, [{"assoc": False}] * 4)
        self.assertEqual(lines, ["gateway_ms=LOST internet_ms=LOST rssi=NA (not associated)"] * 4)

    def test_measurement_errors_are_logged_as_err_with_one_event_then_recovery(self):
        err = (c.ERR, "ping exit 1 without a result: no output")
        ok = (3.0, None)
        lines, _ = self.run_main([err, err, err, err, ok], [(20.0, None)] * 5, [self.GOOD_WIFI] * 5)
        samples = [l for l in lines if "EVENT" not in l]
        events = [l for l in lines if "EVENT" in l]
        self.assertEqual([l.split()[0] for l in samples], ["gateway_ms=ERR"] * 4 + ["gateway_ms=3.000"])
        self.assertFalse(any("LOST" in l for l in samples))
        self.assertEqual(len(events), 2)
        self.assertIn("EVENT Measurement error: router ping could not be measured", events[0])
        self.assertIn("EVENT Measurement recovered: router ping", events[1])
        self.assertEqual(lines.index(events[0]), 3 + 0)  # right after the third failed sample

    def test_unsupported_language_is_reported_on_the_first_sample(self):
        wifi = {"error": c.UNSUPPORTED + " (de_DE): not English, which is not supported yet. Output seen: Status : verbunden"}
        lines, _ = self.run_main([(3.0, None)] * 2, [(20.0, None)] * 2, [wifi, wifi])
        self.assertTrue(lines[0].startswith("gateway_ms=3.000 internet_ms=20.000 wifi=ERR"))
        self.assertIn("EVENT Measurement error: Wi-Fi status could not be measured", lines[1])
        self.assertIn("not supported yet", lines[1])
        self.assertEqual(sum("EVENT" in l for l in lines), 1)

    def test_failed_gateway_lookup_is_retried_and_only_the_router_is_marked(self):
        lines, lookup = self.run_main([(None, None)] * 3, [(20.0, None)] * 3, [self.GOOD_WIFI] * 3, gateway=(c.ERR, "route failed"))
        self.assertTrue(all(l.startswith("gateway_ms=ERR internet_ms=20.000") for l in lines if "EVENT" not in l))
        self.assertEqual(lookup.call_count, 3)  # looked up again on every sample until it works

    def test_collector_publishes_its_settings_for_the_dashboard(self):
        self.run_main([(3.0, None)] * 2, [(20.0, None)] * 2, [self.GOOD_WIFI] * 2)
        first = self.info_writes[0]
        self.assertEqual((first["interval"], first["scan_every"], first["host"]), (5.0, 0, "1.1.1.1"))
        self.assertIn("pid", first)
        self.assertTrue(first["system"].startswith("Windows"))
        self.assertEqual(self.info_writes[-1]["gateway"], "192.168.1.1")

    def test_settings_are_rewritten_only_when_the_gateway_changes(self):
        self.run_main([(3.0, None)] * 3, [(20.0, None)] * 3, [self.GOOD_WIFI] * 3,
                      gateway=[(c.ERR, "route failed"), ("192.168.1.1", None), ("192.168.1.1", None)])
        gateways = [w["gateway"] for w in self.info_writes]
        self.assertEqual(gateways, [None, "192.168.1.1"])  # start-up write, then one update; no write while it stays the same

    def test_scan_period_from_the_command_line_scans_at_the_first_sample(self):
        self.run_main([(3.0, None)] * 2, [(20.0, None)] * 2, [self.GOOD_WIFI] * 2, scan_every=900)
        self.assertEqual(self.scans, 1)

    def test_dashboard_can_turn_scanning_off(self):
        self.run_main([(3.0, None)] * 3, [(20.0, None)] * 3, [self.GOOD_WIFI] * 3, scan_every=900, control={"scan_every": 0})
        self.assertEqual(self.scans, 0)

    def test_scan_switch_pauses_scans(self):
        self.run_main([(3.0, None)] * 3, [(20.0, None)] * 3, [self.GOOD_WIFI] * 3, scan_every=900, control={"scan_paused": True})
        self.assertEqual(self.scans, 0)

    def test_mac_run_does_not_use_windows_paths(self):
        with mock.patch.object(c, "win_wifi", side_effect=AssertionError("Windows code ran on macOS")):
            lines, _ = self.run_main([(3.0, None)], [(20.0, None)], [None], win=False)
        self.assertEqual(lines, ["gateway_ms=3.000 internet_ms=20.000"])


# ---------------------------------------------------------------- internet download speed

class Clock:
    """A monotonic clock that only moves when the fake download reads, so speeds can be checked exactly."""
    def __init__(self):
        self.now = 5000.0

    def __call__(self):
        return self.now


class FakeResponse:
    def __init__(self, clock, ttfb=0.084, step=0.01, status=200, size=None):
        self.clock, self.ttfb, self.step, self.status, self.left, self.reads = clock, ttfb, step, status, size, 0

    def read1(self, n):
        return self.read(n)

    def read(self, n):
        self.clock.now += self.ttfb if self.reads == 0 else self.step
        self.reads += 1
        k = min(n, self.left) if self.left is not None else n
        if self.left is not None:
            self.left -= k
        return b"x" * k

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, resp):
        self.resp, self.requests = resp, []

    def open(self, req, timeout=None):
        self.requests.append((req, timeout))
        return self.resp


class SpeedRequestTests(unittest.TestCase):
    def test_default_server_takes_the_size_in_the_address_and_gets_no_range(self):
        for url in (None, c.logstore.SPEED_DEFAULT_URL):
            req = c.speed_request(url, 25)
            self.assertEqual(req.full_url, "https://speed.cloudflare.com/__down?bytes=25000000")
            self.assertFalse(req.has_header("Range"))

    def test_custom_server_gets_a_range_for_exactly_the_size(self):
        req = c.speed_request("https://example.com/big.bin", 10)
        self.assertEqual((req.full_url, req.get_header("Range")), ("https://example.com/big.bin", "bytes=0-9999999"))

    def test_headers_ask_for_the_plain_uncached_bytes(self):
        req = c.speed_request(None, 10)
        self.assertEqual((req.get_header("Accept-encoding"), req.get_header("Cache-control"), req.get_header("User-agent")),
                         ("identity", "no-cache", "WiFiMonitor/1"))

    def test_host_only(self):
        self.assertEqual(c.speed_host("https://user.example.com:8443/a?token=1"), "user.example.com")
        self.assertEqual(c.speed_host(None), "speed.cloudflare.com")

    def test_opener_speaks_only_http_and_https(self):
        handlers = c.speed_opener().handlers
        self.assertFalse(any(isinstance(h, (urllib.request.FileHandler, urllib.request.FTPHandler, urllib.request.DataHandler)) for h in handlers))
        for url in ("file:///etc/hosts", "ftp://example.com/x", "data:text/plain,hi"):
            with self.subTest(url), self.assertRaises(urllib.error.URLError):
                c.speed_opener().open(url, timeout=1)

    def test_redirect_handler_limits(self):
        self.assertEqual(c.SafeRedirect.max_redirections, 3)
        h = c.SafeRedirect()
        req = urllib.request.Request("http://example.com/a")
        for target in ("file:///etc/passwd", "ftp://example.com/x", "http://user:pw@example.com/x", "http:///x"):
            with self.subTest(target), self.assertRaises(urllib.error.URLError):
                h.redirect_request(req, None, 302, "Found", {}, target)
        new = h.redirect_request(req, None, 302, "Found", {}, "https://cdn.example.com/a")
        self.assertEqual(new.full_url, "https://cdn.example.com/a")


class RedirectRuleTests(unittest.TestCase):
    def follow(self, old, new):
        return c.SafeRedirect().redirect_request(urllib.request.Request(old), None, 302, "Found", {}, new)

    def refused(self, old, new):
        with self.assertRaises(urllib.error.URLError):
            self.follow(old, new)

    def test_https_is_never_downgraded_to_http(self):
        self.refused("https://speed.example.com/a", "http://speed.example.com/a")
        self.refused("https://127.0.0.1:8443/a", "http://127.0.0.1:8443/a")  # not even locally
        self.assertEqual(self.follow("http://speed.example.com/a", "https://speed.example.com/a").full_url, "https://speed.example.com/a")

    def test_the_internet_cannot_send_the_download_to_this_computer_or_its_local_network(self):
        for target in ("http://127.0.0.1/x", "http://127.1.2.3:8080/x", "https://localhost/x", "http://[::1]/x", "http://169.254.169.254/latest/meta-data",
                       "http://[fe80::1]/x", "http://[::ffff:127.0.0.1]/x", "http://app.localhost/x"):
            with self.subTest(target):
                self.refused("https://speed.example.com/a", target)
                self.refused("http://203.0.113.7/a", target)

    def test_a_local_server_may_redirect_locally_and_anyone_may_redirect_to_other_public_hosts(self):
        self.assertEqual(self.follow("http://127.0.0.1:8000/a", "http://127.0.0.1:8000/b").full_url, "http://127.0.0.1:8000/b")
        self.assertEqual(self.follow("http://localhost:8000/a", "http://127.0.0.1:9000/b").full_url, "http://127.0.0.1:9000/b")
        self.assertEqual(self.follow("http://127.0.0.1:8000/a", "https://cdn.example.com/b").full_url, "https://cdn.example.com/b")
        self.assertEqual(self.follow("https://speed.example.com/a", "https://cdn.example.net/b").full_url, "https://cdn.example.net/b")
        self.assertEqual(self.follow("https://speed.example.com/a", "https://192.168.1.1/b").full_url, "https://192.168.1.1/b")  # a home router address is not blocked

    def test_the_three_hop_limit_is_kept(self):
        self.assertEqual(c.SafeRedirect.max_redirections, 3)

    def test_local_host(self):
        self.assertEqual([c.local_host(h) for h in ("localhost", "LOCALHOST.", "127.0.0.1", "::1", "169.254.1.1", "fe80::1%en0", "8.8.8.8", "example.com", "192.168.1.1", None, "")],
                         [True, True, True, True, True, True, False, False, False, False, False])


class SpeedErrorTests(unittest.TestCase):
    def kind(self, exc, host="speed.example.com", mac=False):
        with mock.patch.object(c, "IS_MAC", mac):
            return c.speed_error(exc, host)

    def test_http_errors_name_the_host_and_never_the_address(self):
        kind, reason = self.kind(urllib.error.HTTPError("https://speed.example.com/secret?token=abc", 403, "Forbidden", {}, None))
        self.assertEqual(kind, "http")
        self.assertIn("HTTP 403 Forbidden from speed.example.com", reason)
        self.assertIn("different address", reason)  # 4xx: suggest another server
        self.assertNotIn("token", reason)
        kind, reason = self.kind(urllib.error.HTTPError("https://speed.example.com/x", 503, "Service Unavailable", {}, None))
        self.assertEqual(kind, "http")
        self.assertNotIn("different address", reason)

    def test_redirects_that_urllib_refuses(self):
        kind, reason = self.kind(urllib.error.HTTPError("file:///etc/passwd", 302, "Found - Redirection to url 'file:///etc/passwd' is not allowed", {}, None))
        self.assertEqual(kind, "http")
        self.assertNotIn("passwd", reason)

    def test_dns_timeout_and_connect(self):
        self.assertEqual(self.kind(urllib.error.URLError(socket.gaierror(-2, "Name or service not known")))[0], "dns")
        self.assertIn("speed.example.com", self.kind(urllib.error.URLError(socket.gaierror(-2, "x")))[1])
        self.assertEqual(self.kind(urllib.error.URLError(socket.timeout("timed out")))[0], "timeout")
        self.assertEqual(self.kind(socket.timeout("timed out"))[0], "timeout")
        self.assertEqual(self.kind(TimeoutError())[0], "timeout")
        self.assertEqual(self.kind(urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")))[0], "connect")
        self.assertEqual(self.kind(ConnectionResetError(54, "reset"))[0], "connect")
        self.assertEqual(self.kind(OSError("network is unreachable"))[0], "connect")

    def test_anything_else_is_other_with_a_reason(self):
        self.assertEqual(self.kind(ValueError("odd"))[0], "other")
        self.assertEqual(self.kind(urllib.error.URLError("unknown url type: file")), ("other", "unknown url type: file"))
        self.assertTrue(self.kind(RuntimeError())[1])

    def test_tls_leads_with_the_raw_cause_and_macos_gets_the_certificate_hint(self):
        cause = ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate (_ssl.c:1000)")
        kind, reason = self.kind(urllib.error.URLError(cause), mac=True)
        self.assertEqual(kind, "tls")
        self.assertTrue(reason.startswith("[SSL: CERTIFICATE_VERIFY_FAILED]"), reason)
        self.assertIn("Install Certificates.command", reason)
        kind, reason = self.kind(urllib.error.URLError(cause), mac=False)
        self.assertEqual(kind, "tls")
        self.assertTrue(reason.startswith("[SSL: CERTIFICATE_VERIFY_FAILED]"), reason)
        self.assertNotIn("Install Certificates", reason)
        self.assertEqual(self.kind(ssl.SSLError(1, "[SSL] oops"), mac=False)[0], "tls")  # raised while reading, not wrapped in URLError


class GateTests(unittest.TestCase):
    def test_a_scan_blocks_a_speed_test_and_the_other_way_round(self):
        g = c.Gate()
        self.assertTrue(g.try_begin_scan(now=100))
        self.assertFalse(g.try_begin_scan(now=101))
        self.assertFalse(g.try_begin_speed(now=101))
        g.end_scan(now=110)
        self.assertFalse(g.try_begin_speed(now=110 + c.SPEED_SETTLE_S - 0.1))  # too soon after the scan
        self.assertTrue(g.try_begin_speed(now=110 + c.SPEED_SETTLE_S))
        self.assertFalse(g.try_begin_speed(now=116))
        self.assertFalse(g.try_begin_scan(now=116))
        g.end_speed(now=130)
        self.assertFalse(g.try_begin_scan(now=130 + c.SPEED_SETTLE_S - 0.1))
        self.assertTrue(g.try_begin_scan(now=130 + c.SPEED_SETTLE_S))

    def test_idle_gate_lets_either_start(self):
        self.assertTrue(c.Gate().try_begin_speed(now=0))
        self.assertTrue(c.Gate().try_begin_scan(now=0))

    def test_samples_overlapping_a_test_are_recognised(self):
        g = c.Gate()
        self.assertFalse(g.speed_overlaps(0))
        g.try_begin_speed(now=100)
        self.assertTrue(g.speed_overlaps(500))  # still running
        g.end_speed(now=130)
        self.assertTrue(g.speed_overlaps(125))  # the sample began before the test ended
        self.assertTrue(g.speed_overlaps(130))
        self.assertFalse(g.speed_overlaps(130.1))

    def test_run_scan_always_releases_the_gate(self):
        g = c.Gate()
        g.try_begin_scan(now=0)
        with mock.patch.object(c, "do_scan", side_effect=RuntimeError("boom")), self.assertRaises(RuntimeError):
            c.run_scan(g, False)
        self.assertFalse(g.scanning)


class DoSpeedtestTests(unittest.TestCase):
    """The measurement itself, with a fake clock and a fake download."""

    def run_fake(self, n_mb=10, **kw):
        clock = Clock()
        resp = FakeResponse(clock, size=n_mb * 1000000, **kw)
        opener = FakeOpener(resp)
        self.progress = []
        with mock.patch.object(c, "speed_opener", return_value=opener), mock.patch.object(c.time, "monotonic", clock), \
                mock.patch.object(c.logstore, "write_speed_progress", side_effect=lambda d, tries=1: self.progress.append(d)):
            res = c.do_speedtest(None, n_mb, "manual", {"rssi": -52, "snr": 38, "ch": 149, "band": "5", "tx": 866})
        return res, resp, opener

    KEYS = {"v", "start", "trigger", "ok", "mbps", "ttfb_ms", "bytes", "dur_s", "total_s", "status", "host", "mb", "range", "capped", "kind", "reason", "wifi"}

    def test_speed_excludes_connect_and_the_first_chunk(self):
        res, resp, opener = self.run_fake(10)
        n = 10000000
        later = resp.reads - 1  # every read after the first one takes 10 ms
        self.assertTrue(res["ok"])
        self.assertEqual(res["bytes"], n)
        self.assertAlmostEqual(res["mbps"], round((n - 65536) * 8 / (later * 0.01) / 1e6, 2), places=1)
        self.assertEqual((res["ttfb_ms"], res["status"], res["host"], res["mb"], res["range"], res["capped"]), (84.0, 200, "speed.cloudflare.com", 10, False, False))
        self.assertAlmostEqual(res["dur_s"], later * 0.01, places=1)
        self.assertAlmostEqual(res["total_s"], res["dur_s"] + 0.084, places=1)
        self.assertEqual((res["kind"], res["reason"], res["trigger"]), (None, None, "manual"))
        self.assertEqual(res["wifi"], {"rssi": -52, "snr": 38, "ch": 149, "band": "5", "tx": 866})
        self.assertEqual(set(res), self.KEYS)
        json.dumps(res)
        self.assertEqual(opener.requests[0][1], c.SPEED_CONNECT_TIMEOUT)

    def test_reads_never_go_past_the_requested_size(self):
        class Strict(FakeResponse):
            def read(self, n):
                assert n <= c.SPEED_CHUNK and n > 0
                return super().read(n)
        clock = Clock()
        resp = Strict(clock, size=10 * 1000000 + 12345)  # the server has more than was asked for (it ignored the range)
        with mock.patch.object(c, "speed_opener", return_value=FakeOpener(resp)), mock.patch.object(c.time, "monotonic", clock), \
                mock.patch.object(c.logstore, "write_speed_progress"):
            res = c.do_speedtest("https://example.com/big", 10, "scheduled")
        self.assertEqual((res["ok"], res["bytes"], res["range"], res["wifi"]), (True, 10000000, True, None))
        self.assertEqual(resp.left, 12345)

    def test_progress_is_written_twice_a_second_with_a_rolling_and_an_average_rate(self):
        res, resp, _ = self.run_fake(10)
        self.assertEqual(self.progress[0]["phase"], "connecting")
        running = [p for p in self.progress if p["phase"] == "downloading"]
        self.assertGreaterEqual(len(running), 2)
        self.assertTrue(all(p["running"] and p["pid"] == os.getpid() and p["target_bytes"] == 10000000 and p["host"] == "speed.cloudflare.com" for p in self.progress))
        self.assertEqual(running[0]["ttfb_ms"], 84.0)
        self.assertEqual(running[0]["id"], res["start"])
        self.assertTrue(all(b["bytes"] >= a["bytes"] for a, b in zip(running, running[1:])))
        for p in running:
            self.assertAlmostEqual(p["mbps"], 65536 * 8 / 0.01 / 1e6, delta=1)  # about one second of steady 52 Mbps reads
            self.assertAlmostEqual(p["avg_mbps"], 65536 * 8 / 0.01 / 1e6, delta=1)
        gaps = [b["elapsed_s"] - a["elapsed_s"] for a, b in zip(running, running[1:])]
        self.assertTrue(all(g >= c.SPEED_PROGRESS_S - 1e-6 for g in gaps))

    def test_a_download_too_quick_to_measure_is_short_not_fast(self):
        res, _, _ = self.run_fake(10, step=0.0001)  # 1.5 ms of measured download
        self.assertEqual((res["ok"], res["kind"], res["mbps"]), (False, "short", None))
        self.assertIn("too short to measure", res["reason"])
        self.assertIn("choose a larger file or MB", res["reason"])

    def test_a_download_that_ended_early_under_one_mb_is_short(self):
        clock = Clock()
        resp = FakeResponse(clock, size=300000)  # EOF after 300 KB
        with mock.patch.object(c, "speed_opener", return_value=FakeOpener(resp)), mock.patch.object(c.time, "monotonic", clock), \
                mock.patch.object(c.logstore, "write_speed_progress"):
            res = c.do_speedtest("https://example.com/small", 10, "manual")
        self.assertEqual((res["ok"], res["kind"], res["mbps"], res["bytes"]), (False, "short", None, 300000))
        self.assertIn("choose a larger file or MB", res["reason"])

    def test_an_early_end_with_a_usable_amount_still_counts(self):
        res, _, _ = self.run_fake(10)  # sanity: complete
        clock = Clock()
        resp = FakeResponse(clock, size=4000000)  # the file is 4 MB although 10 MB were asked for
        with mock.patch.object(c, "speed_opener", return_value=FakeOpener(resp)), mock.patch.object(c.time, "monotonic", clock), \
                mock.patch.object(c.logstore, "write_speed_progress"):
            res = c.do_speedtest("https://example.com/four", 10, "manual")
        self.assertEqual((res["ok"], res["bytes"]), (True, 4000000))

    def test_the_cap_stops_a_slow_download_and_still_counts_it(self):
        with mock.patch.object(c, "SPEED_CAP_S", 1.0):
            res, resp, _ = self.run_fake(100, step=0.1)  # would take 15 s
        self.assertEqual((res["ok"], res["capped"]), (True, True))
        self.assertLess(res["bytes"], 100000000)
        self.assertGreater(res["mbps"], 0)

    def test_a_cap_reached_with_nothing_after_the_first_chunk_is_a_stall_not_a_speed(self):
        clock = Clock()
        resp = FakeResponse(clock, size=10000000, ttfb=0.084)
        with mock.patch.object(c, "SPEED_CAP_S", 0.05), mock.patch.object(c, "speed_opener", return_value=FakeOpener(resp)), \
                mock.patch.object(c.time, "monotonic", clock), mock.patch.object(c.logstore, "write_speed_progress"):
            res = c.do_speedtest(None, 10, "manual")
        self.assertEqual((res["ok"], res["capped"], res["kind"], res["mbps"], res["bytes"]), (False, True, "timeout", None, 65536))
        self.assertIn("stalled", res["reason"])

    def test_a_clock_jump_means_the_computer_slept(self):
        clock = Clock()
        resp = FakeResponse(clock, size=10000000)
        walls = itertools.chain([1000.0], itertools.repeat(1100.0))  # 100 s of wall time against ~1.5 s of monotonic time
        with mock.patch.object(c, "speed_opener", return_value=FakeOpener(resp)), mock.patch.object(c.time, "monotonic", clock), \
                mock.patch.object(c.time, "time", side_effect=lambda: next(walls)), mock.patch.object(c.logstore, "write_speed_progress"):
            res = c.do_speedtest("https://example.com/x", 10, "scheduled")
        self.assertEqual((res["ok"], res["mbps"], res["reason"]), (False, None, "interrupted (computer slept)"))

    def test_an_error_while_downloading_is_err_with_the_partial_bytes(self):
        class Dies(FakeResponse):
            def read(self, n):
                if self.reads == 3:
                    raise ConnectionResetError(54, "Connection reset by peer")
                return super().read(n)
        clock = Clock()
        with mock.patch.object(c, "speed_opener", return_value=FakeOpener(Dies(clock, size=10000000))), mock.patch.object(c.time, "monotonic", clock), \
                mock.patch.object(c.logstore, "write_speed_progress"):
            res = c.do_speedtest(None, 10, "manual")
        self.assertEqual((res["ok"], res["kind"], res["mbps"], res["bytes"]), (False, "connect", None, 3 * 65536))
        self.assertEqual(set(res), self.KEYS)
        self.assertIsNone(res["dur_s"])


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
        try:
            self.server.routes[self.path.split("?")[0]](self)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client stopped reading: that is what a capped or complete test does

    def log_message(self, *args):
        pass


def send_body(h, nbytes, code=200, headers=(), delay=0.0, length=None):
    h.send_response(code)
    for k, v in headers:
        h.send_header(k, v)
    h.send_header("Content-Length", str(nbytes if length is None else length))
    h.end_headers()
    sent = 0
    while sent < nbytes:
        k = min(65536, nbytes - sent)
        h.wfile.write(b"x" * k)
        sent += k
        if delay:
            time.sleep(delay)


class LocalServerCase(unittest.TestCase):
    """A real HTTP server on an ephemeral local port; proxies are bypassed so nothing leaves the machine."""

    progress_file = False  # True: let the test write logs/speed.json for real (in a throwaway directory)

    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.seen, self.server.routes = [], {}
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        env = mock.patch.dict(os.environ, {"no_proxy": "*", "NO_PROXY": "*"})
        env.start()
        self.addCleanup(env.stop)
        if not self.progress_file:
            progress = mock.patch.object(c.logstore, "write_speed_progress")
            progress.start()
            self.addCleanup(progress.stop)
        quick = mock.patch.object(c, "SPEED_MIN_S", 0.0)  # loopback is far too fast for the real 0.2 s floor
        quick.start()
        self.addCleanup(quick.stop)
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()


class LocalDownloadTests(LocalServerCase):
    def test_a_server_that_honours_the_range_is_read_to_the_end(self):
        def ranged(h):
            lo, hi = re.match(r"bytes=(\d+)-(\d+)", h.headers["Range"]).groups()
            send_body(h, int(hi) - int(lo) + 1, 206, [("Content-Range", "bytes %s-%s/99999999" % (lo, hi))])
        self.server.routes["/file"] = ranged
        res = c.do_speedtest(self.url + "/file", 2, "manual")
        self.assertEqual((res["ok"], res["status"], res["bytes"], res["range"], res["host"]), (True, 206, 2000000, True, "127.0.0.1"))
        self.assertGreater(res["mbps"], 0)
        path, headers = self.server.seen[0]
        self.assertEqual((headers["range"], headers["accept-encoding"], headers["user-agent"]), ("bytes=0-1999999", "identity", "WiFiMonitor/1"))

    def test_a_server_that_ignores_the_range_is_read_for_exactly_the_size_then_closed(self):
        self.server.routes["/file"] = lambda h: send_body(h, 3000000)  # 200 with more than was asked for
        res = c.do_speedtest(self.url + "/file", 2, "manual")
        self.assertEqual((res["ok"], res["status"], res["bytes"]), (True, 200, 2000000))

    def test_the_default_style_address_is_used_as_given_with_the_size_filled_in(self):
        self.server.routes["/__down"] = lambda h: send_body(h, int(h.path.split("bytes=")[1]))
        res = c.do_speedtest(self.url + "/__down?bytes={bytes}", 2, "manual")
        self.assertEqual((res["ok"], res["bytes"]), (True, 2000000))
        self.assertEqual(self.server.seen[0][0], "/__down?bytes=2000000")

    def test_a_slow_server_is_stopped_at_the_cap(self):
        self.server.routes["/slow"] = lambda h: send_body(h, 5000000, delay=0.05)
        with mock.patch.object(c, "SPEED_CAP_S", 0.4):
            res = c.do_speedtest(self.url + "/slow", 5, "manual")
        self.assertEqual((res["ok"], res["capped"]), (True, True))
        self.assertLess(res["bytes"], 5000000)
        self.assertLess(res["total_s"], 3)

    def test_ttfb_is_when_the_first_bytes_arrive_not_when_a_full_buffer_does(self):
        def late(h):
            h.send_response(200)
            h.send_header("Content-Length", "2000000")
            h.end_headers()
            h.wfile.write(b"x" * 1000)  # a first few bytes, then the server goes quiet for a while
            time.sleep(0.7)
            h.wfile.write(b"x" * 1999000)
        self.server.routes["/late"] = late
        res = c.do_speedtest(self.url + "/late", 2, "manual")
        self.assertTrue(res["ok"])
        self.assertLess(res["ttfb_ms"], 400)  # reading a whole 64 KB chunk first would have waited for the 0.7 s pause
        self.assertGreater(res["dur_s"], 0.6)

    def test_a_slow_trickle_stops_near_the_cap_and_still_counts(self):
        def trickle(h):
            h.send_response(200)
            h.send_header("Content-Length", "5000000")
            h.end_headers()
            for _ in range(60):
                h.wfile.write(b"x" * 1024)  # about 10 KB/s
                time.sleep(0.1)
        self.server.routes["/trickle"] = trickle
        with mock.patch.object(c, "SPEED_CAP_S", 1.5):
            res = c.do_speedtest(self.url + "/trickle", 5, "scheduled")
        # a capped test that did receive data after the first chunk is a (very slow) measurement, not a "too short" error
        self.assertEqual((res["ok"], res["capped"], res["kind"]), (True, True, None))
        self.assertGreater(res["mbps"], 0)
        self.assertLess(res["mbps"], 0.5)
        self.assertGreater(res["total_s"], 1.4)
        self.assertLess(res["total_s"], 2.0)  # near the cap, not at the end of the 6 s the server would take
        self.assertLess(res["bytes"], 30000)

    def test_progress_is_refreshed_while_nothing_arrives(self):
        self.server.routes["/slowstart"] = lambda h: (time.sleep(1.0), send_body(h, 3000000))
        writes = []
        with mock.patch.object(c, "SPEED_HEARTBEAT_S", 0.1), \
                mock.patch.object(c.logstore, "write_speed_progress", side_effect=lambda d, tries=1: writes.append(dict(d)) or True):
            res = c.do_speedtest(self.url + "/slowstart", 2, "manual")
        self.assertTrue(res["ok"])
        waiting = [w for w in writes if w["phase"] == "connecting"]
        self.assertGreaterEqual(len(waiting), 5)  # one at the start plus a refresh every 0.1 s of the 1 s wait
        stamps = [w["updated"] for w in waiting]
        self.assertEqual(stamps, sorted(stamps))
        self.assertGreater(stamps[-1] - stamps[0], 0.7)
        self.assertTrue(all(w["running"] and w["id"] == res["start"] for w in writes))
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertLess(max(gaps), 1.0)  # never anywhere near the 5 s the dashboard needs
        self.assertFalse([t for t in threading.enumerate() if t.name.startswith("Thread") and "heartbeat" in str(t._target)])  # stopped again
        n = len(writes)
        time.sleep(0.4)
        self.assertEqual(len(writes), n)  # and it stays stopped

    def test_the_heartbeat_stops_when_the_test_fails_too(self):
        writes = []
        with mock.patch.object(c, "SPEED_HEARTBEAT_S", 0.05), mock.patch.object(socket, "getaddrinfo", side_effect=socket.gaierror(-2, "x")), \
                mock.patch.object(c.logstore, "write_speed_progress", side_effect=lambda d, tries=1: writes.append(d) or True):
            res = c.do_speedtest("http://speed.invalid/f", 2, "manual")
        self.assertEqual(res["kind"], "dns")
        n = len(writes)
        time.sleep(0.3)
        self.assertEqual(len(writes), n)

    def test_a_small_file_is_short(self):
        self.server.routes["/small"] = lambda h: send_body(h, 300000)
        res = c.do_speedtest(self.url + "/small", 2, "manual")
        self.assertEqual((res["ok"], res["kind"], res["mbps"]), (False, "short", None))

    def test_a_truncated_download_is_an_error_not_a_speed(self):
        self.server.routes["/cut"] = lambda h: send_body(h, 1500000, length=4000000)  # promises 4 MB, sends 1.5 MB, closes
        res = c.do_speedtest(self.url + "/cut", 4, "manual")
        self.assertEqual((res["ok"], res["mbps"]), (False, None))
        self.assertTrue(res["reason"])

    def test_http_errors_are_err_with_the_status(self):
        self.server.routes["/403"] = lambda h: send_body(h, 0, 403)
        self.server.routes["/404"] = lambda h: send_body(h, 0, 404)
        self.server.routes["/500"] = lambda h: send_body(h, 0, 500)
        for code in (403, 404, 500):
            with self.subTest(code):
                res = c.do_speedtest("%s/%d" % (self.url, code), 2, "manual")
                self.assertEqual((res["ok"], res["kind"], res["mbps"]), (False, "http", None))
                self.assertIn("HTTP %d" % code, res["reason"])
                self.assertIn("127.0.0.1", res["reason"])

    def test_a_range_the_file_cannot_satisfy_is_an_http_error(self):
        self.server.routes["/tiny"] = lambda h: send_body(h, 0, 416)
        res = c.do_speedtest(self.url + "/tiny", 2, "manual")
        self.assertEqual((res["kind"], res["ok"]), ("http", False))

    def test_a_redirect_to_a_local_file_is_refused(self):
        self.server.routes["/go"] = lambda h: send_body(h, 0, 302, [("Location", "file:///etc/hosts")])
        res = c.do_speedtest(self.url + "/go", 2, "manual")
        self.assertEqual((res["ok"], res["kind"], res["bytes"]), (False, "http", 0))
        self.assertNotIn("/etc/hosts", res["reason"])

    def test_a_redirect_to_another_http_address_is_followed_and_keeps_the_range(self):
        self.server.routes["/go"] = lambda h: send_body(h, 0, 302, [("Location", "/real")])
        self.server.routes["/real"] = lambda h: send_body(h, 3000000)
        res = c.do_speedtest(self.url + "/go", 2, "manual")
        self.assertEqual((res["ok"], res["bytes"]), (True, 2000000))
        self.assertEqual(self.server.seen[1][1]["range"], "bytes=0-1999999")

    def test_at_most_three_redirects_are_followed(self):
        for i in range(6):
            self.server.routes["/r%d" % i] = (lambda i: lambda h: send_body(h, 0, 302, [("Location", "/r%d" % (i + 1))]))(i)
        self.server.routes["/r6"] = lambda h: send_body(h, 3000000)
        res = c.do_speedtest(self.url + "/r0", 2, "manual")
        self.assertEqual((res["ok"], res["kind"]), (False, "http"))
        self.assertLessEqual(len(self.server.seen), 5)

    def test_silence_is_a_timeout(self):
        self.server.routes["/hang"] = lambda h: time.sleep(1.0)
        with mock.patch.object(c, "SPEED_CONNECT_TIMEOUT", 0.3):
            res = c.do_speedtest(self.url + "/hang", 2, "manual")
        self.assertEqual((res["ok"], res["kind"], res["mbps"]), (False, "timeout", None))

    def test_a_closed_port_is_a_connect_error(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        res = c.do_speedtest("http://127.0.0.1:%d/x" % port, 2, "manual")
        self.assertEqual((res["ok"], res["kind"]), (False, "connect"))

    def test_an_unknown_host_is_a_dns_error(self):
        with mock.patch.object(socket, "getaddrinfo", side_effect=socket.gaierror(-2, "Name or service not known")):
            res = c.do_speedtest("http://speed.invalid/file", 2, "manual")
        self.assertEqual((res["ok"], res["kind"], res["mbps"], res["host"]), (False, "dns", None, "speed.invalid"))
        self.assertIn("speed.invalid", res["reason"])


class RunSpeedtestThreadTests(LocalServerCase):
    """One test from start to finish on a real log directory: events, the speed log line, the final progress file."""

    progress_file = True

    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp(prefix="wifimonitor-speed-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        for p in (mock.patch.object(c.logstore, "LOG_DIR", self.dir), contextlib.redirect_stdout(io.StringIO())):
            p.__enter__()
            self.addCleanup(p.__exit__, None, None, None)
        self.gate, self.tracker = c.Gate(), c.ErrorTracker()

    def run_one(self, route, trigger="manual", mb=2):
        self.server.routes["/f"] = route
        self.assertTrue(self.gate.try_begin_speed())
        c.run_speedtest_thread(self.gate, self.tracker, self.url + "/f", mb, trigger, {"rssi": -50, "snr": None, "ch": 6, "band": "2.4", "tx": 72})
        day = time.strftime("%Y-%m-%d")
        events = [l.split(" EVENT ", 1)[1] for l in c.logstore.read_day("monitor", day) if " EVENT " in l]
        return events, c.logstore.read_day("speed", day)

    def test_a_manual_test_logs_started_finished_a_speed_line_and_final_progress(self):
        events, speed = self.run_one(lambda h: send_body(h, 3000000))
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0], "Speed test started (manual, 2 MB from 127.0.0.1)")
        self.assertRegex(events[1], r"^Speed test finished: [\d.]+ Mbps \(2\.0 MB in [\d.]+ s\)$")
        self.assertNotIn("/f", " ".join(events))  # the host, never the address
        self.assertEqual(len(speed), 1)
        stamp, payload = speed[0].split(" ", 2)[0:2], speed[0].split(" ", 2)[2]
        rec = json.loads(payload)
        self.assertEqual((rec["ok"], rec["bytes"], rec["trigger"], rec["host"], rec["wifi"]["ch"]), (True, 2000000, "manual", "127.0.0.1", 6))
        self.assertEqual(speed[0], "%s %s" % (" ".join(stamp), json.dumps(rec, separators=(",", ":"))))  # compact, stamped at completion
        prog = c.logstore.read_speed_progress()
        self.assertEqual((prog["running"], prog["phase"], prog["id"]), (False, "done", rec["start"]))
        self.assertEqual((prog["result"]["t"], prog["result"]["mbps"], prog["pid"], prog["trigger"]), (" ".join(stamp), rec["mbps"], os.getpid(), "manual"))
        self.assertFalse(self.gate.speeding)

    def test_the_final_progress_says_when_the_next_scheduled_test_is_due(self):
        self.server.routes["/f"] = lambda h: send_body(h, 3000000)
        for every, expect in ((1800, True), (0, False)):
            self.assertTrue(self.gate.try_begin_speed(now=0))
            c.run_speedtest_thread(self.gate, self.tracker, self.url + "/f", 2, "scheduled", None, every)
            prog = c.logstore.read_speed_progress()
            if expect:
                due = dt.datetime.strptime(prog["next_at"], "%Y-%m-%d %H:%M:%S")
                self.assertAlmostEqual((due - dt.datetime.strptime(prog["result"]["t"], "%Y-%m-%d %H:%M:%S")).total_seconds(), 1800, delta=1)
            else:
                self.assertIsNone(prog["next_at"])

    def test_a_scheduled_test_logs_no_started_event(self):
        events, speed = self.run_one(lambda h: send_body(h, 3000000), trigger="scheduled")
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].startswith("Speed test finished:"))
        self.assertEqual(json.loads(speed[0].split(" ", 2)[2])["trigger"], "scheduled")

    def test_a_failure_is_logged_as_err_and_the_gate_is_released(self):
        events, speed = self.run_one(lambda h: send_body(h, 0, 403), trigger="scheduled")
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].startswith("Speed test failed: HTTP 403"), events)
        rec = json.loads(speed[0].split(" ", 2)[2])
        self.assertEqual((rec["ok"], rec["mbps"], rec["kind"]), (False, None, "http"))
        prog = c.logstore.read_speed_progress()
        self.assertEqual((prog["running"], prog["phase"], prog["result"]["ok"]), (False, "error", False))
        self.assertFalse(self.gate.speeding)

    def test_three_failures_in_a_row_raise_one_measurement_error_and_a_success_recovers(self):
        events = []
        for _ in range(3):
            events, _ = self.run_one(lambda h: send_body(h, 0, 403), trigger="scheduled")
            self.gate.end_speed(now=0)
        self.assertTrue(any(e.startswith("Measurement error: speed test could not be measured") for e in events), events)
        events, _ = self.run_one(lambda h: send_body(h, 3000000), trigger="scheduled")
        self.assertTrue(any(e.startswith("Measurement recovered: speed test") for e in events), events)

    def test_the_gate_is_released_even_when_the_test_blows_up(self):
        self.assertTrue(self.gate.try_begin_speed())
        with mock.patch.object(c, "do_speedtest", side_effect=RuntimeError("boom")), self.assertRaises(RuntimeError):
            c.run_speedtest_thread(self.gate, self.tracker, self.url, 2, "scheduled", None)
        self.assertFalse(self.gate.speeding)


class WifiBriefTests(unittest.TestCase):
    def test_mac_and_windows(self):
        self.assertEqual(c.wifi_brief({"assoc": True, "rssi": -52, "noise": -90, "ch": 149, "band": "5", "tx": "866"}),
                         {"rssi": -52, "snr": 38, "ch": 149, "band": "5", "tx": 866})
        self.assertEqual(c.wifi_brief({"assoc": True, "rssi": -50, "ch": 13, "band": "2.4", "tx": "72.5"}),
                         {"rssi": -50, "snr": None, "ch": 13, "band": "2.4", "tx": 72})
        self.assertEqual(c.wifi_brief({"assoc": True, "rssi": -50, "ch": 13, "band": "2.4", "tx": None})["tx"], None)

    def test_no_link_to_describe(self):
        for w in (None, {"assoc": False}, {"error": "x"}):
            self.assertIsNone(c.wifi_brief(w))


class SyncThread:
    """threading.Thread that runs its target at once, so a main-loop test is deterministic."""
    def __init__(self, target=None, args=(), daemon=None):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


class SpeedMainLoopTests(unittest.TestCase):
    """collector.main() with scripted control files and a fake speed test: when it starts one, and what it logs."""
    GOOD_WIFI = {"assoc": True, "rssi": -50, "noise": -90, "ch": 13, "band": "2.4", "tx": "72", "phy": "n"}

    FAIL = "the control file could not be read"

    def run_main(self, controls, samples=4, step=5.0, scan_every=0, hook=None, keep_running=False, gate=None, end_ago=0, progress_ok=()):
        """controls[i] is what the control file says at sample i (the last one repeats), or FAIL for an unreadable file (defaults, ok=False).
        `hook(i, gate)` runs between samples. A fake test ends `end_ago` seconds before the main loop notices."""
        self.logged, self.runs, self.progress, self.scans = [], [], [], []
        self.state = {"i": 0, "now": 1000000.0}
        self.gate = gate or c.Gate()
        ok_writes = iter(progress_ok)
        gws = iter([(3.0, None)] * samples)

        def fake_pause(*a):
            self.state["i"] += 1
            self.state["now"] += step
            if hook:
                hook(self.state["i"], self.gate)
            if self.state["i"] >= samples:
                raise KeyboardInterrupt

        def fake_speed(gate, tracker, url, mb, trigger, wifi, every=0):
            self.runs.append({"i": self.state["i"], "url": url, "mb": mb, "trigger": trigger, "wifi": wifi, "every": every})
            if not keep_running:
                gate.end_speed(now=time.monotonic() - end_ago)

        def control():
            ctl = controls[min(self.state["i"], len(controls) - 1)]
            if ctl == self.FAIL:
                out = c.logstore.Control(c.logstore.CONTROL_DEFAULTS)
                out.ok = False
                return out
            return {"scan_paused": False, "interval": None, "scan_every": None, **ctl}

        with mock.patch.object(c, "IS_WIN", True), mock.patch.object(c, "IS_MAC", False), \
                mock.patch.object(c, "default_gateway", return_value=("192.168.1.1", None)), \
                mock.patch.object(c.logstore, "write_info", side_effect=lambda info: self.info_writes.append(dict(info))), \
                mock.patch.object(c, "ping", side_effect=lambda host: next(gws) if host == "192.168.1.1" else (20.0, None)), \
                mock.patch.object(c, "get_wifi", return_value=self.GOOD_WIFI), mock.patch.object(c, "ensure_mac_helper", return_value=False), \
                mock.patch.object(c, "pause", side_effect=fake_pause), mock.patch.object(c, "Gate", return_value=self.gate), \
                mock.patch.object(c.logstore, "append", side_effect=lambda kind, line, when=None: self.logged.append(line)), \
                mock.patch.object(c.logstore, "maintain"), mock.patch.object(c.logstore, "read_control", side_effect=control), \
                mock.patch.object(c.logstore, "write_speed_progress", side_effect=lambda d, tries=1: self.progress.append(d) or next(ok_writes, True)), \
                mock.patch.object(c, "do_scan", side_effect=lambda h: self.scans.append(self.state["i"])), \
                mock.patch.object(c, "run_speedtest_thread", side_effect=fake_speed), mock.patch.object(c, "threading", types.SimpleNamespace(Thread=SyncThread, Lock=threading.Lock, Event=threading.Event)), \
                mock.patch.object(c.time, "time", side_effect=lambda: self.state["now"]), \
                mock.patch.object(sys, "argv", ["collector.py", "--scan-every", str(scan_every)]), contextlib.redirect_stdout(io.StringIO()):
            self.info_writes = []
            c.main()
        return [re.sub(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d ", "", line) for line in self.logged]

    def test_nothing_runs_by_default_and_no_new_cli_flags_are_needed(self):
        lines = self.run_main([{}])
        self.assertEqual(self.runs, [])
        self.assertEqual(lines, ["gateway_ms=3.000 internet_ms=20.000 rssi=-50 noise=-90 snr=40 ch=13 band=2.4GHz tx=72Mbps phy=11n"] * 4)

    def test_the_collector_says_it_can_run_speed_tests(self):
        self.run_main([{}])
        self.assertIs(self.info_writes[0]["speed"], True)

    def test_run_now_starts_one_manual_test_with_the_dashboard_settings(self):
        self.run_main([{}, {"speed_run": 111, "speed_url": "https://example.com/f.bin", "speed_mb": 40}])
        self.assertEqual(len(self.runs), 1)
        r = self.runs[0]
        self.assertEqual((r["i"], r["trigger"], r["url"], r["mb"]), (1, "manual", "https://example.com/f.bin", 40))
        self.assertEqual(r["wifi"], {"rssi": -50, "snr": 40, "ch": 13, "band": "2.4", "tx": 72})  # the sample the test started on

    def test_the_default_server_is_used_when_no_address_is_set(self):
        self.run_main([{}, {"speed_run": 111}])
        self.assertEqual((self.runs[0]["url"], self.runs[0]["mb"]), (c.logstore.SPEED_DEFAULT_URL, 25))

    def test_a_stamp_already_there_at_start_up_is_never_replayed(self):
        self.run_main([{"speed_run": 555}])
        self.assertEqual(self.runs, [])

    def test_the_same_stamp_runs_once_and_a_new_one_runs_again(self):
        self.run_main([{}, {"speed_run": 1}, {"speed_run": 1}, {"speed_run": 2}], samples=5)
        self.assertEqual([r["i"] for r in self.runs], [1, 3])

    def test_a_request_while_a_test_is_running_is_consumed_and_noted(self):
        lines = self.run_main([{}, {"speed_run": 1}, {"speed_run": 2}, {"speed_run": 2}], keep_running=True)
        self.assertEqual(len(self.runs), 1)
        self.assertEqual([l for l in lines if "EVENT" in l], ["EVENT Speed test already running, request ignored"])

    def test_a_request_is_kept_while_a_scan_runs_and_started_when_it_is_over(self):
        gate = c.Gate()
        gate.try_begin_scan()  # a scan is in progress as the request arrives

        def hook(i, g):
            if i == 2:
                g.end_scan(now=time.monotonic() - 100)

        self.run_main([{}, {"speed_run": 1}], samples=4, hook=hook, gate=gate)
        self.assertEqual([r["i"] for r in self.runs], [2])  # refused at sample 1, tried again at sample 2
        self.assertEqual(self.runs[0]["trigger"], "manual")

    def test_scan_pause_does_not_stop_speed_tests(self):
        self.run_main([{"scan_paused": True}, {"scan_paused": True, "speed_run": 1}])
        self.assertEqual(len(self.runs), 1)

    def test_a_schedule_waits_a_full_period_at_start_up_then_repeats_after_each_test(self):
        self.run_main([{"speed_every": 600}], samples=6, step=400.0)  # samples at +0, 400, 800, 1200, 1600, 2000 s
        # the first test starts at +800 s and ends at once; the next is due a full 600 s after that, so at +2000 s (sample 5), not +1200 or +1600
        self.assertEqual([(r["i"], r["trigger"]) for r in self.runs], [(2, "scheduled"), (5, "scheduled")])

    def test_turning_the_schedule_on_counts_from_that_moment_and_never_runs_at_once(self):
        self.run_main([{"speed_every": 0}, {"speed_every": 600}], samples=5, step=400.0)
        self.assertEqual([r["i"] for r in self.runs], [3])  # enabled at +400: due at +1000, first sample after that is +1200

    def test_turning_the_schedule_off_cancels_a_waiting_scheduled_test(self):
        gate = c.Gate()
        gate.try_begin_scan()

        def hook(i, g):
            if i == 3:
                g.end_scan(now=time.monotonic() - 100)

        self.run_main([{"speed_every": 600}, {"speed_every": 600}, {"speed_every": 600}, {"speed_every": 0}], samples=5, step=400.0, hook=hook, gate=gate)
        self.assertEqual(self.runs, [])

    def test_a_scheduled_run_and_a_request_are_one_test(self):
        self.run_main([{"speed_every": 600}, {"speed_every": 600}, {"speed_every": 600, "speed_run": 9}], samples=4, step=400.0)
        self.assertEqual([(r["i"], r["trigger"]) for r in self.runs], [(2, "manual")])

    def test_the_next_scheduled_test_is_a_period_after_the_finish_not_after_the_start(self):
        def hook(i, g):
            if i == 7:
                self.state["now"] += 300.0  # the loop only notices at +1000 s that the +600 s test ended at +750 s

        self.run_main([{"speed_every": 600}], samples=13, step=100.0, hook=hook, end_ago=250)
        # 1st test at +600 s. It finished at +750 s, so the next is due at +1350 s: the first sample after that is +1400 s (sample 11).
        # Counting from the start it would have run at +1200 s (sample 9), and from the moment it was noticed at +1600 s (sample 13).
        self.assertEqual([r["i"] for r in self.runs], [6, 11])
        self.assertEqual({r["every"] for r in self.runs}, {600})

    def test_an_unreadable_control_file_at_start_up_does_not_replay_an_old_run_now(self):
        self.run_main([self.FAIL, {"speed_run": 555}, {"speed_run": 555}], samples=4)
        self.assertEqual(self.runs, [])  # the first good read is the baseline
        self.run_main([self.FAIL, {"speed_run": 555}, {"speed_run": 556}], samples=4)
        self.assertEqual([r["i"] for r in self.runs], [2])  # a stamp after that baseline still runs

    def test_a_failed_read_in_between_neither_replays_nor_forgets_a_stamp(self):
        self.run_main([{"speed_run": 5}, {"speed_run": 5}, self.FAIL, {"speed_run": 5}, {"speed_run": 6}], samples=6)
        self.assertEqual([r["i"] for r in self.runs], [4])
        self.run_main([{}, {"speed_run": 5}, self.FAIL, {"speed_run": 5}], samples=5)
        self.assertEqual([r["i"] for r in self.runs], [1])  # run once, not again when the file comes back

    def test_an_older_stamp_is_not_a_new_request(self):
        self.run_main([{"speed_run": 10}, {"speed_run": 5}, {"speed_run": 10}], samples=4)
        self.assertEqual(self.runs, [])

    def test_an_unreadable_control_file_does_not_reset_the_schedule(self):
        self.run_main([{"speed_every": 600}, self.FAIL, {"speed_every": 600}, {"speed_every": 600}], samples=4, step=400.0)
        self.assertEqual([r["i"] for r in self.runs], [2])  # still due at +600 s, as if the unreadable sample had not happened
        self.run_main([{"speed_every": 0}, {"speed_every": 600}, self.FAIL, {"speed_every": 600}], samples=4, step=400.0)
        self.assertEqual([r["i"] for r in self.runs], [3])  # enabled at +400 s, due at +1000 s

    def test_an_unreadable_control_file_keeps_the_other_settings_too(self):
        self.run_main([{"scan_paused": True}, self.FAIL, self.FAIL], samples=3, scan_every=900)
        self.assertEqual(self.scans, [])  # paused scanning stays paused instead of falling back to the default
        gate = c.Gate()
        gate.try_begin_scan()  # a scan holds the request back while the control file turns unreadable

        def hook(i, g):
            if i == 3:
                g.end_scan(now=time.monotonic() - 100)

        mine = {"speed_url": "https://example.com/f", "speed_mb": 40}
        self.run_main([mine, {**mine, "speed_run": 1}, self.FAIL, self.FAIL], samples=5, hook=hook, gate=gate)
        self.assertEqual([(r["i"], r["url"], r["mb"]) for r in self.runs], [(3, "https://example.com/f", 40)])

    def test_samples_during_a_test_are_flagged_and_the_ones_after_are_not(self):
        def hook(i, g):
            if i == 3:
                g.end_speed()  # the test ends between samples 2 and 3

        lines = self.run_main([{}, {"speed_run": 1}], samples=5, hook=hook, keep_running=True)
        self.assertEqual([l.endswith(" speed=1") for l in lines], [False, False, True, False, False])  # sample 1 is logged before its test starts
        self.assertTrue(all(l.startswith("gateway_ms=3.000 internet_ms=20.000 rssi=-50") for l in lines))

    def test_a_sample_that_began_before_a_test_ended_is_flagged(self):
        def hook(i, g):
            if i == 2:
                g.end_speed(now=time.monotonic() + 1000)  # a test that ended "after" the next sample began

        lines = self.run_main([{}, {"speed_run": 1}], samples=4, hook=hook, keep_running=True)
        self.assertTrue(lines[2].endswith(" speed=1"))

    def test_scans_wait_for_a_running_test_and_are_retried_at_the_next_sample(self):
        gate = c.Gate()
        gate.try_begin_speed()

        def hook(i, g):
            if i == 2:
                g.end_speed(now=time.monotonic() - 100)

        self.run_main([{}], samples=5, scan_every=900, hook=hook, gate=gate)
        self.assertEqual(self.scans, [2])  # not 900 s later: the period only restarts once a scan really starts

    def test_a_scan_blocks_nothing_forever(self):
        self.run_main([{}], samples=3, scan_every=900)
        self.assertEqual(self.scans, [0])
        self.assertFalse(self.gate.scanning)  # the scan thread released the gate

    def test_idle_progress_is_written_once_and_again_only_when_it_changes(self):
        self.run_main([{}], samples=4)
        self.assertEqual([(p["running"], p["phase"], p["next_at"]) for p in self.progress], [(False, "idle", None)])
        self.run_main([{}, {"speed_every": 600}], samples=4)
        self.assertEqual([p["next_at"] is None for p in self.progress], [True, False])
        self.assertRegex(self.progress[1]["next_at"], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")

    def test_idle_progress_is_retried_until_it_is_written(self):
        self.run_main([{}], samples=6, progress_ok=[False, False, True])
        self.assertEqual(len(self.progress), 3)  # two failed writes are tried again at the next samples, then it is done
        self.run_main([{}], samples=6)
        self.assertEqual(len(self.progress), 1)

    def test_a_finished_tests_result_is_not_overwritten_by_idle(self):
        self.run_main([{}, {"speed_run": 1}], samples=6)
        self.assertEqual([p["phase"] for p in self.progress], ["idle"])  # only the start-up write: the test's own "done" record stays

    def test_changing_the_schedule_after_a_test_writes_idle_again(self):
        self.run_main([{}, {"speed_run": 1}, {"speed_run": 1}, {"speed_run": 1, "speed_every": 600}], samples=5)
        self.assertEqual([p["next_at"] is None for p in self.progress], [True, False])

    def test_a_new_test_hands_the_file_back_to_the_test(self):
        self.run_main([{}, {"speed_run": 1}, {"speed_run": 2}, {"speed_run": 2}], samples=5)
        self.assertEqual(len(self.runs), 2)
        self.assertEqual(len(self.progress), 1)

    def test_idle_progress_is_not_written_over_a_running_test(self):
        self.run_main([{}, {"speed_run": 1}], samples=4, keep_running=True)
        self.assertEqual(len(self.progress), 1)  # the start-up write; the test itself owns the file afterwards


if __name__ == "__main__":
    unittest.main()
