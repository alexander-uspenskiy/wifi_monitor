"""Collector: command running, ping and Wi-Fi classification (real loss vs measurement error), language checks, log line format."""
import contextlib
import io
import re
import subprocess
import sys
import unittest
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

    def run_main(self, gw_results, net_results, wifi_results, gateway=("192.168.1.1", None), win=True):
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
                mock.patch.object(c.time, "sleep", side_effect=fake_sleep), \
                mock.patch.object(c.logstore, "append", side_effect=lambda kind, line, when=None: logged.append(line)), \
                mock.patch.object(c.logstore, "maintain"), mock.patch.object(c.logstore, "read_control", return_value={"scan_paused": False}), \
                mock.patch.object(sys, "argv", ["collector.py", "--scan-every", "0"]), contextlib.redirect_stdout(io.StringIO()):
            c.main()
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

    def test_mac_run_does_not_use_windows_paths(self):
        with mock.patch.object(c, "win_wifi", side_effect=AssertionError("Windows code ran on macOS")):
            lines, _ = self.run_main([(3.0, None)], [(20.0, None)], [None], win=False)
        self.assertEqual(lines, ["gateway_ms=3.000 internet_ms=20.000"])


if __name__ == "__main__":
    unittest.main()
