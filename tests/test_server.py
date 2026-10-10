"""Dashboard server: parsing log lines (old and new formats), the daily summary, the control API and the download speed feeds."""
import datetime as dt
import json
import os
import re
import shutil
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import context  # noqa: F401
import dashboard_server as s

# lines copied from real logs written before ERR existed: they must keep parsing exactly the same
MAC_LINE = "2026-10-09 15:17:57 gateway_ms=3.057 internet_ms=21.925 rssi=-70 noise=-89 snr=19 ch=149 band=5GHz width=80MHz tx=351Mbps phy=11ac"
WIN_LINE = "2026-10-09 15:17:57 gateway_ms=2.000 internet_ms=20.000 rssi=-50 ch=13 band=2.4GHz tx=72Mbps phy=11n"
LOST_LINE = "2026-10-09 15:18:02 gateway_ms=LOST internet_ms=LOST rssi=NA (not associated)"
PINGS_ONLY = "2026-10-09 15:18:07 gateway_ms=2.5 internet_ms=LOST"
ERR_LINE = "2026-10-09 15:18:12 gateway_ms=ERR internet_ms=21.0 wifi=ERR"


class ParseTests(unittest.TestCase):
    def test_mac_line(self):
        r = s.parse(MAC_LINE)
        self.assertEqual((r["gw"], r["net"], r["rssi"], r["noise"], r["snr"], r["ch"], r["band"], r["width"], r["tx"], r["phy"]),
                         (3.057, 21.925, -70, -89, 19, 149, "5", 80, 351, "11ac"))
        self.assertEqual((r["gwLost"], r["netLost"], r["assoc"], r["gwErr"], r["netErr"], r["wifiErr"]), (False, False, True, False, False, False))

    def test_windows_line(self):
        r = s.parse(WIN_LINE)
        self.assertEqual((r["gw"], r["rssi"], r["noise"], r["ch"], r["band"], r["width"]), (2.0, -50, None, 13, "2.4", None))

    def test_loss_and_disconnect(self):
        r = s.parse(LOST_LINE)
        self.assertEqual((r["gw"], r["gwLost"], r["netLost"], r["assoc"], r["gwErr"], r["netErr"]), (None, True, True, False, False, False))
        r = s.parse(PINGS_ONLY)
        self.assertEqual((r["gwLost"], r["netLost"], r["assoc"]), (False, True, True))

    def test_err_is_neither_loss_nor_disconnect(self):
        r = s.parse(ERR_LINE)
        self.assertEqual((r["gw"], r["gwLost"], r["gwErr"], r["net"], r["netLost"], r["netErr"], r["wifiErr"], r["assoc"]),
                         (None, False, True, 21.0, False, False, True, True))

    def test_speed_flag(self):
        self.assertIs(s.parse(MAC_LINE)["speed"], False)
        self.assertIs(s.parse(MAC_LINE + " speed=1")["speed"], True)
        r = s.parse("2026-10-09 15:18:02 gateway_ms=LOST internet_ms=LOST rssi=NA (not associated) speed=1")
        self.assertEqual((r["speed"], r["assoc"], r["gwLost"]), (True, False, True))
        r = s.parse(MAC_LINE + " speed=1")
        self.assertEqual((r["phy"], r["tx"], r["gw"]), ("11ac", 351, 3.057))  # the flag does not disturb the other fields
        self.assertIs(s.parse(PINGS_ONLY + " speed=0")["speed"], False)

    def test_events_and_junk_are_not_samples(self):
        self.assertIsNone(s.parse("2026-10-09 15:18:12 EVENT Measurement error: router ping could not be measured (x)"))
        self.assertIsNone(s.parse("not a log line"))

    def test_measurement_events_are_read_as_events(self):
        m = s.EVENT.match("2026-10-09 15:18:12 EVENT Measurement error: Wi-Fi status could not be measured (x)")
        self.assertEqual(m.group(2), "Measurement error: Wi-Fi status could not be measured (x)")


def row(gw=3.0, net=20.0, t="2026-10-09 10:00:00", **kw):
    d = s.parse("%s gateway_ms=%s internet_ms=%s" % (t, "LOST" if gw is None else gw, "LOST" if net is None else net))
    d.update(kw)
    return d


class SummaryTests(unittest.TestCase):
    def rows(self, spec):
        return [s.parse("2026-10-09 10:00:%02d %s" % (i * 5, line)) for i, line in enumerate(spec)]

    def test_summary_of_normal_data_is_unchanged(self):
        r = self.rows(["gateway_ms=3.0 internet_ms=20.0 rssi=-60 snr=22 ch=36 band=5GHz",
                       "gateway_ms=LOST internet_ms=20.0 rssi=-60 snr=22 ch=36 band=5GHz",
                       "gateway_ms=3.0 internet_ms=LOST rssi=-60 snr=22 ch=40 band=5GHz",
                       "gateway_ms=LOST internet_ms=LOST rssi=NA (not associated)",
                       "gateway_ms=3.0 internet_ms=20.0 rssi=-60 snr=22 ch=40 band=5GHz"])
        out = s.summarize(r, [])
        self.assertEqual((out["samples"], out["gwLoss"], out["netLoss"], out["changes"], out["errSamples"]), (5, 40.0, 40.0, 1, 0))
        self.assertEqual(out["outageMin"], 0.1)  # one 5 s down sample, reported in minutes
        self.assertEqual((out["gwP95"], out["netMedian"]), (3.0, 20.0))

    def test_err_samples_are_left_out_of_loss_and_outages(self):
        r = self.rows(["gateway_ms=ERR internet_ms=ERR wifi=ERR",
                       "gateway_ms=3.0 internet_ms=LOST rssi=-60 ch=36 band=5GHz",
                       "gateway_ms=3.0 internet_ms=20.0 rssi=-60 ch=36 band=5GHz",
                       "gateway_ms=LOST internet_ms=LOST rssi=-60 ch=36 band=5GHz"])
        out = s.summarize(r, [])
        self.assertEqual((out["gwLoss"], out["netLoss"]), (33.3, 66.7))  # of the 3 samples where each ping was actually taken
        self.assertEqual(out["errSamples"], 1)
        self.assertEqual(out["outageMin"], 0.0)  # the ERR sample is not an outage

    def test_all_err_has_no_loss_figure(self):
        out = s.summarize(self.rows(["gateway_ms=ERR internet_ms=ERR"] * 3), [])
        self.assertEqual((out["gwLoss"], out["netLoss"], out["errSamples"]), (None, None, 3))

    def test_empty(self):
        out = s.summarize([], [])
        self.assertEqual((out["samples"], out["gwLoss"], out["errSamples"]), (0, None, 0))

    def test_data_without_speed_flags_has_the_same_statistics_as_before(self):
        r = self.rows(["gateway_ms=3.0 internet_ms=20.0 rssi=-60 snr=22 ch=36 band=5GHz", "gateway_ms=LOST internet_ms=LOST rssi=NA (not associated)",
                       "gateway_ms=3.0 internet_ms=20.0 rssi=-60 snr=22 ch=36 band=5GHz"])
        out = s.summarize(r, [{"t": "x", "text": "Scanning paused"}])
        self.assertEqual(out, {"samples": 3, "speedSamples": 0, "gwLoss": 33.3, "netLoss": 33.3, "errSamples": 0, "gwP95": 3.0, "netP95": 20.0,
                               "netMedian": 20.0, "outageMin": 0.1, "changes": 0, "rssi": -60.0, "snr": 22.0, "notes": ["Scanning paused"]})

    def test_samples_taken_during_a_speed_test_are_left_out_of_the_statistics(self):
        r = self.rows(["gateway_ms=3.0 internet_ms=20.0 rssi=-60 snr=22 ch=36 band=5GHz",
                       "gateway_ms=40.0 internet_ms=900.0 rssi=-70 snr=12 ch=36 band=5GHz speed=1",
                       "gateway_ms=LOST internet_ms=LOST rssi=-70 snr=12 ch=40 band=5GHz speed=1",
                       "gateway_ms=ERR internet_ms=ERR wifi=ERR speed=1",
                       "gateway_ms=LOST internet_ms=LOST rssi=NA (not associated) speed=1",
                       "gateway_ms=3.0 internet_ms=22.0 rssi=-60 snr=22 ch=36 band=5GHz"])
        out = s.summarize(r, [])
        self.assertEqual((out["samples"], out["speedSamples"]), (6, 4))  # all rows are still counted as samples
        self.assertEqual((out["gwP95"], out["netP95"], out["netMedian"]), (3.0, 22.0, 22.0))
        self.assertEqual((out["gwLoss"], out["netLoss"], out["outageMin"], out["changes"]), (0.0, 0.0, 0.0, 0))
        self.assertEqual(out["errSamples"], 1)  # an ERR is an ERR whenever it happened
        self.assertEqual((out["rssi"], out["snr"]), (-65.0, 17.0))  # signal readings keep every sample

    def test_a_speed_test_does_not_stretch_an_outage_gap(self):
        r = self.rows(["gateway_ms=LOST internet_ms=LOST rssi=-60 ch=36 band=5GHz", "gateway_ms=3.0 internet_ms=20.0 speed=1",
                       "gateway_ms=3.0 internet_ms=20.0 rssi=-60 ch=36 band=5GHz"])
        self.assertEqual(s.summarize(r, [])["outageMin"], 0.2)  # 10 s to the next sample that counts

    def test_speed_test_events_are_not_daily_summary_notes(self):
        notes = [{"t": "a", "text": "Speed test started (manual, 25 MB from speed.cloudflare.com)"}, {"t": "b", "text": "Speed test finished: 1.0 Mbps (25.0 MB in 5.2 s)"},
                 {"t": "c", "text": "Speed test failed: x"}, {"t": "d", "text": "Speed test set to run one time (Run now only)"}, {"t": "e", "text": "Scanning paused"},
                 {"t": "f", "text": "Measurement error: speed test could not be measured (x). Not counted as packet loss or a disconnect."},
                 {"t": "g", "text": "Measurement recovered: speed test is being measured again"}]
        notes.append({"t": "h", "text": "Measurement error: router ping could not be measured (x)."})  # other measurement errors still are notes
        out = s.summarize([], notes)
        self.assertEqual(out["notes"], ["Scanning paused", notes[7]["text"]])  # the speed test's own measurement-error events stay out of the notes too


DEFAULT_CONTROL = {"scan_paused": False, "interval": None, "scan_every": None, "speed_url": None, "speed_mb": 25, "speed_every": 0, "speed_run": None}
NOW = dt.datetime(2026, 10, 9, 12, 0, 30)
INFO = {"pid": 4242, "started": "2026-10-09 11:00:00", "system": "macOS 26.0", "interval": 5.0, "scan_every": 900,
        "host": "1.1.1.1", "gateway": "192.168.1.1"}


def sample(line="gateway_ms=3.0 internet_ms=20.0 rssi=-60 snr=22 ch=36 band=5GHz", t="2026-10-09 12:00:25"):
    return s.parse("%s %s" % (t, line))


class ProbesTests(unittest.TestCase):
    def probes(self, info=INFO, row="default", control=None, scan=("2026-10-09 11:50:00", 37), win=False, step=None, speed=None):
        row = sample() if row == "default" else row
        out = s.build_probes(info, control or {}, row, step, scan, now=NOW, win=win, speed=speed)
        return out["collector"], {p["id"]: p for p in out["probes"]}

    def test_every_probe_is_listed_with_its_real_interval(self):
        collector, p = self.probes()
        self.assertEqual(list(p), ["router", "internet", "wifi", "gateway", "scan", "speed", "maintain"])
        self.assertEqual((p["router"]["everyText"], p["internet"]["everyText"], p["wifi"]["everyText"]), ("5 s", "5 s", "5 s"))
        self.assertEqual((p["gateway"]["everyText"], p["scan"]["everyText"]), ("1 min", "15 min"))
        self.assertEqual((p["router"]["target"], p["internet"]["target"]), ("192.168.1.1", "1.1.1.1"))
        self.assertTrue(collector["running"] and collector["settingsKnown"])
        self.assertEqual((collector["pid"], collector["system"]), (4242, "macOS 26.0"))

    def test_custom_settings_are_shown(self):
        _, p = self.probes(dict(INFO, interval=10, scan_every=600, host="8.8.8.8"))
        self.assertEqual((p["router"]["everyText"], p["scan"]["everyText"], p["internet"]["target"]), ("10 s", "10 min", "8.8.8.8"))
        self.assertIn("ping -c 1 -W 1000 8.8.8.8", p["internet"]["method"])

    def test_scanning_off_or_paused(self):
        _, p = self.probes(dict(INFO, scan_every=0))
        self.assertEqual((p["scan"]["everyText"], p["scan"]["state"]), ("off", "off"))
        _, p = self.probes(control={"scan_paused": True})
        self.assertEqual(p["scan"]["everyText"], "15 min (paused)")
        self.assertEqual(p["scan"]["state"], "off")

    def test_platform_specific_commands(self):
        _, mac = self.probes(win=False)
        _, win = self.probes(win=True)
        self.assertIn("ping -c 1", mac["router"]["method"])
        self.assertIn("ping -n 1", win["router"]["method"])
        self.assertIn("CoreWLAN", mac["wifi"]["method"])
        self.assertIn("netsh wlan show interfaces", win["wifi"]["method"])
        self.assertIn("route -n get default", mac["gateway"]["method"])
        self.assertIn("route print", win["gateway"]["method"])
        self.assertIn("netsh wlan show networks", win["scan"]["method"])

    def test_results_and_states(self):
        _, p = self.probes()
        self.assertEqual((p["router"]["result"], p["router"]["state"], p["internet"]["result"]), ("3.0 ms", "ok", "20.0 ms"))
        self.assertEqual((p["wifi"]["result"], p["wifi"]["state"]), ("-60 dBm · SNR 22 dB · ch 36 (5 GHz)", "ok"))
        self.assertEqual(p["scan"]["result"], "37 networks")
        _, p = self.probes(row=sample("gateway_ms=LOST internet_ms=LOST rssi=NA (not associated)"))
        self.assertEqual((p["router"]["result"], p["router"]["state"], p["wifi"]["result"], p["wifi"]["state"]),
                         ("LOST", "bad", "not associated", "bad"))
        _, p = self.probes(row=sample("gateway_ms=ERR internet_ms=20.0 wifi=ERR"))
        self.assertEqual((p["router"]["state"], p["internet"]["state"], p["wifi"]["state"]), ("warn", "ok", "warn"))
        self.assertIn("ERR", p["router"]["result"])
        _, p = self.probes(row=sample("gateway_ms=3.0 internet_ms=20.0"))
        self.assertEqual(p["wifi"]["state"], "off")  # pings only, no Wi-Fi details logged

    def test_last_run_age_comes_from_the_latest_sample_and_scan(self):
        _, p = self.probes()
        self.assertEqual((p["router"]["ago"], p["scan"]["ago"]), (5, 630))

    def test_collector_not_running_when_samples_stop(self):
        collector, p = self.probes(row=sample(t="2026-10-09 11:50:00"))
        self.assertFalse(collector["running"])
        self.assertIsNone(collector["pid"])
        self.assertEqual(collector["lastSample"], "2026-10-09 11:50:00")

    def test_without_the_settings_file_it_falls_back_to_the_log_spacing(self):
        collector, p = self.probes(info={}, step=10)
        self.assertFalse(collector["settingsKnown"])
        self.assertEqual((p["router"]["everyText"], p["scan"]["everyText"], p["internet"]["target"]), ("10 s", "15 min", "1.1.1.1"))

    def test_no_data_at_all(self):
        collector, p = self.probes(info={}, row=None, scan=None)
        self.assertFalse(collector["running"])
        self.assertEqual((p["router"]["state"], p["wifi"]["state"], p["scan"]["result"]), ("off", "off", "no scan yet"))

    def test_retimeable_probes_carry_their_menu(self):
        _, p = self.probes(control={"interval": 10, "scan_every": None})
        for pid in ("router", "internet", "wifi"):
            c = p[pid]["control"]
            self.assertEqual((c["key"], c["override"], c["default"], c["value"]), ("interval", 10, 5.0, 10))
            self.assertEqual(c["choices"], [2, 5, 10, 15])
        self.assertEqual(p["router"]["everyText"], "10 s")  # the override wins over the collector's start-up value
        sc = p["scan"]["control"]
        self.assertEqual((sc["key"], sc["override"], sc["default"], sc["value"]), ("scan_every", None, 900, 900))
        self.assertEqual(sc["choices"], [0, 300, 900, 1800, 3600])
        for pid in ("gateway", "maintain"):
            self.assertNotIn("control", p[pid])

    def test_scan_override_wins_and_zero_means_off(self):
        _, p = self.probes(control={"scan_every": 1800})
        self.assertEqual(p["scan"]["everyText"], "30 min")
        _, p = self.probes(control={"scan_every": 0})
        self.assertEqual((p["scan"]["everyText"], p["scan"]["state"], p["scan"]["control"]["value"]), ("off", "off", 0))

    def test_speed_row_before_any_test(self):
        _, p = self.probes()
        sp = p["speed"]
        self.assertEqual((sp["name"], sp["target"], sp["everyText"], sp["every"], sp["result"], sp["state"], sp["last"], sp["ago"]),
                         ("Internet download speed", "speed.cloudflare.com", "one time (Run now only)", None, "no test yet", "off", None, None))
        self.assertIsNone(sp["control"])
        self.assertEqual(sp["note"], "set in the Internet download speed card")
        self.assertEqual(sp["what"], "Single-stream download from one server, to see how fast data reaches this computer")
        self.assertEqual(sp["method"], "HTTP GET of 25 MB (Range: bytes=0-24999999; the default server takes the size in the address)")
        self.assertEqual(sp["traffic"], "about 25 MB per test, only when you press Run now")

    def test_speed_row_with_a_schedule_a_custom_server_and_a_result(self):
        test = {"t": "2026-10-09 11:58:00", "ok": True, "mbps": 312.44, "mb": 40}
        _, p = self.probes(control={"speed_url": "https://files.example.org:8443/big.bin?token=abc", "speed_mb": 40, "speed_every": 1800}, speed=test)
        sp = p["speed"]
        self.assertEqual((sp["target"], sp["everyText"], sp["every"], sp["result"], sp["state"], sp["last"], sp["ago"]),
                         ("files.example.org", "30 min", 1800, "312.4 Mbps", "ok", "2026-10-09 11:58:00", 150))
        self.assertIn("about 40 MB per test, about 1920 MB per day at this setting", sp["traffic"])
        self.assertNotIn("token", json.dumps(sp))  # the host only, never the address

    def test_speed_row_failure_is_err_never_zero(self):
        _, p = self.probes(speed={"t": "2026-10-09 11:58:00", "ok": False, "mbps": None, "reason": "HTTP 403 Forbidden from example.com"})
        self.assertEqual((p["speed"]["result"], p["speed"]["state"]), ("ERR (HTTP 403 Forbidden from example.com)", "warn"))

    def test_speed_row_does_not_change_the_other_probes(self):
        _, with_speed = self.probes(control={"speed_every": 600}, speed={"t": "2026-10-09 11:58:00", "ok": True, "mbps": 1.0})
        _, plain = self.probes()
        for pid in ("router", "internet", "wifi", "gateway", "scan", "maintain"):
            self.assertEqual(with_speed[pid], plain[pid])

    def test_every_text(self):
        self.assertEqual([s.every_text(v) for v in (0, 2, 5.0, 59, 60, 900, 5400, 7200)], ["off", "2 s", "5 s", "59 s", "1 min", "15 min", "1.5 h", "2 h"])


class ControlApiTests(unittest.TestCase):
    """The real request handler on a throwaway port: what the dashboard may change, and what it may not."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), s.Handler)
        cls.port = cls.server.server_address[1]
        cls.port_patch = unittest.mock.patch.object(s, "PORT", cls.port)  # the speed test settings are only accepted for the page's own address
        cls.port_patch.start()
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.port_patch.stop()
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        for path in (s.logstore.CONTROL_FILE,):
            if os.path.exists(path):
                os.remove(path)
        self.events = []
        patcher = unittest.mock.patch.object(s, "note_event", side_effect=self.events.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, body, origin=None, ctype="application/json", host=None):
        headers = {"Content-Type": ctype}
        if origin:
            headers["Origin"] = origin
        if host:
            headers["Host"] = host
        req = urllib.request.Request("http://127.0.0.1:%d/api/control" % self.port, json.dumps(body).encode(), headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def get(self):
        with urllib.request.urlopen("http://127.0.0.1:%d/api/control" % self.port, timeout=5) as r:
            return json.loads(r.read())

    def test_interval_and_scan_period_can_be_set_and_are_noted(self):
        code, ctl = self.post({"interval": 10})
        self.assertEqual((code, ctl["interval"]), (200, 10))
        code, ctl = self.post({"scan_every": 0})
        self.assertEqual((code, ctl["scan_every"]), (200, 0))
        self.assertEqual(self.get(), {**DEFAULT_CONTROL, "interval": 10, "scan_every": 0})
        self.assertEqual(self.events, ["Probe interval set to 10 s from the dashboard", "Scan interval set to off from the dashboard"])

    def test_null_goes_back_to_the_start_up_value(self):
        self.post({"interval": 15})
        code, ctl = self.post({"interval": None})
        self.assertEqual((code, ctl["interval"]), (200, None))
        self.assertEqual(self.events[-1], "Probe interval back to the command-line value")

    def test_scan_period_null_is_noted_plainly(self):
        self.post({"scan_every": 300})
        self.post({"scan_every": None})
        self.assertEqual(self.events, ["Scan interval set to 5 min from the dashboard", "Scan interval back to the command-line value"])

    def test_scan_switch_still_works_and_only_notes_real_changes(self):
        self.post({"scan_paused": True})
        self.post({"scan_paused": True})
        self.post({"scan_paused": False})
        self.assertEqual(self.events, ["Scanning paused", "Scanning resumed"])

    def test_unsupported_values_are_refused(self):
        for body in ({"interval": 60}, {"interval": 1}, {"interval": "5"}, {"interval": True}, {"scan_every": 17}, {"scan_paused": "yes"},
                     {"nonsense": 1}, {}, {"interval": 5, "nonsense": 1}):
            with self.subTest(body):
                code, text = self.post(body)
                self.assertEqual(code, 400)
        self.assertEqual(self.get(), DEFAULT_CONTROL)  # nothing was applied
        self.assertEqual(self.events, [])

    SPEED_HELP = ("speed_url (an http or https address of up to 500 characters, or null for the default), speed_mb (10 to 100), "
                  "speed_every (one of [0, 600, 1800, 3600]) and speed_run (true)")

    def test_control_defaults_have_the_speed_keys(self):
        self.assertEqual(self.get(), DEFAULT_CONTROL)
        self.assertEqual(len(self.get()), 7)

    def test_speed_settings_can_be_set_alone_or_with_the_legacy_keys_and_are_noted_once_per_change(self):
        code, ctl = self.post({"speed_url": "https://files.example.org:8443/big.bin?token=secret"})
        self.assertEqual((code, ctl["speed_url"]), (200, "https://files.example.org:8443/big.bin?token=secret"))
        self.assertEqual(self.post({"speed_mb": 60, "speed_every": 1800, "interval": 10})[0], 200)
        self.assertEqual(self.post({"speed_mb": 60, "speed_every": 1800})[0], 200)  # no change: no event
        self.assertEqual(self.get(), {**DEFAULT_CONTROL, "speed_url": "https://files.example.org:8443/big.bin?token=secret", "speed_mb": 60, "speed_every": 1800, "interval": 10})
        self.post({"speed_url": None})
        self.post({"speed_every": 0})
        self.assertEqual(self.events, ["Speed test server set to files.example.org from the dashboard", "Probe interval set to 10 s from the dashboard",
                                       "Speed test size set to 60 MB", "Speed test set to run every 30 min",
                                       "Speed test server back to the default (speed.cloudflare.com)", "Speed test set to run one time (Run now only)"])
        self.assertFalse(any("token" in e or "big.bin" in e for e in self.events))  # events carry the host only

    def test_run_now_stores_a_fresh_stamp_and_is_noted_every_time(self):
        before = int(time.time() * 1000)
        code, ctl = self.post({"speed_run": True})
        self.assertEqual(code, 200)
        first = ctl["speed_run"]
        self.assertIsInstance(first, int)
        self.assertGreaterEqual(first, before)
        code, ctl = self.post({"speed_run": True})
        self.assertGreater(ctl["speed_run"], first)  # always a new value, which is what the collector acts on
        self.assertEqual(self.get()["speed_run"], ctl["speed_run"])
        self.assertEqual(self.events, ["Speed test requested from the dashboard"] * 2)

    def test_run_now_with_other_settings_in_one_post(self):
        code, ctl = self.post({"speed_mb": 10, "speed_run": True})
        self.assertEqual((code, ctl["speed_mb"], isinstance(ctl["speed_run"], int)), (200, 10, True))
        self.assertEqual(self.events, ["Speed test size set to 10 MB", "Speed test requested from the dashboard"])

    def test_speed_settings_are_validated_all_or_nothing(self):
        for body in ({"speed_run": False}, {"speed_run": None}, {"speed_run": 1}, {"speed_run": "true"}, {"speed_run": 1760000000000},
                     {"speed_url": "ftp://example.com/f"}, {"speed_url": "file:///etc/passwd"}, {"speed_url": "https://u:p@example.com/f"}, {"speed_url": "http://"},
                     {"speed_url": "https://example.com/" + "a" * 500}, {"speed_url": "https://exa mple.com"}, {"speed_url": 5}, {"speed_url": ""},
                     {"speed_mb": 9}, {"speed_mb": 101}, {"speed_mb": "25"}, {"speed_mb": 25.5}, {"speed_mb": True}, {"speed_mb": None},
                     {"speed_every": 17}, {"speed_every": None}, {"speed_every": "600"}, {"speed_every": True},
                     {"interval": 10, "speed_mb": 500}, {"speed_mb": 30, "scan_every": 17}, {"speed_mb": 30, "speed_run": False}, {"speed_mb": 30, "nonsense": 1}):
            with self.subTest(body):
                code, text = self.post(body)
                self.assertEqual(code, 400)
                self.assertTrue(text.startswith("expected one or more of scan_paused (true or false), interval (one of [2, 5, 10, 15]) and scan_every (one of "), text)
                self.assertTrue(text.endswith("; " + self.SPEED_HELP), text)
        self.assertEqual(self.get(), DEFAULT_CONTROL)
        self.assertEqual(self.events, [])

    def test_speed_settings_are_refused_for_any_other_host_header(self):
        for host in ("evil.example", "evil.example:%d" % self.port, "127.0.0.1", "127.0.0.1:1", "localhost", "192.168.1.5:%d" % self.port):
            for body in ({"speed_run": True}, {"speed_mb": 30}, {"speed_url": None}, {"speed_every": 600}, {"interval": 10, "speed_mb": 30}, {"speed_run": False}):
                with self.subTest(host=host, body=body):
                    code, text = self.post(body, host=host)
                    self.assertEqual(code, 403)
                    self.assertEqual(text, "forbidden: open the dashboard at http://127.0.0.1:%d or http://localhost:%d to change speed settings" % (self.port, self.port))
        self.assertEqual(self.get(), DEFAULT_CONTROL)
        self.assertEqual(self.events, [])

    def test_the_page_may_use_either_spelling_of_its_own_address(self):
        self.assertEqual(self.post({"speed_mb": 30}, host="127.0.0.1:%d" % self.port)[0], 200)
        self.assertEqual(self.post({"speed_mb": 40}, host="localhost:%d" % self.port)[0], 200)

    def get_as(self, host):
        req = urllib.request.Request("http://127.0.0.1:%d/api/control" % self.port, headers={"Host": host})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())

    def test_a_custom_address_is_shown_in_full_only_to_the_pages_own_address(self):
        url = "https://files.example.org:8443/big.bin?token=secret"
        self.post({"speed_url": url})
        self.assertEqual(self.get()["speed_url"], url)  # urllib sends Host 127.0.0.1:PORT
        self.assertEqual(self.get_as("localhost:%d" % self.port)["speed_url"], url)
        for host in ("evil.example", "evil.example:%d" % self.port, "127.0.0.1", "192.168.1.5:%d" % self.port):
            with self.subTest(host):
                ctl = self.get_as(host)
                self.assertEqual(ctl["speed_url"], "files.example.org")  # the host only: a token never leaves
                self.assertNotIn("secret", json.dumps(ctl))
                self.assertEqual(ctl["speed_mb"], 25)
        code, ctl = self.post({"interval": 10}, host="evil.example")  # the answer to a legacy post is masked as well
        self.assertEqual((code, ctl["speed_url"]), (200, "files.example.org"))
        self.assertEqual(self.get()["speed_url"], url)  # nothing was altered by looking
        self.post({"speed_url": None})
        self.assertIsNone(self.get_as("evil.example")["speed_url"])

    def test_legacy_settings_do_not_need_the_host_check(self):
        self.assertEqual(self.post({"interval": 10}, host="localhost")[0], 200)  # unchanged behaviour

    def test_foreign_origins_and_wrong_content_types_are_refused(self):
        self.assertEqual(self.post({"interval": 10}, origin="http://evil.example")[0], 403)
        self.assertEqual(self.post({"interval": 10}, ctype="text/plain")[0], 403)
        self.assertEqual(self.post({"speed_run": True}, origin="http://evil.example")[0], 403)
        self.assertEqual(self.get()["interval"], None)
        self.assertIsNone(self.get()["speed_run"])


def write_lines(kind, day, lines):
    os.makedirs(s.logstore.LOG_DIR, exist_ok=True)
    with open(os.path.join(s.logstore.LOG_DIR, "wifi-%s-%s.log" % (kind, day)), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def speed_line(t, mbps=300.0, nbytes=25000000, ok=True, **kw):
    rec = {"v": 1, "start": t, "trigger": "manual", "ok": ok, "mbps": mbps if ok else None, "ttfb_ms": 80.0, "bytes": nbytes, "dur_s": 5.0, "total_s": 5.1,
           "status": 200, "host": "speed.cloudflare.com", "mb": 25, "range": False, "capped": False, "kind": None if ok else "http",
           "reason": None if ok else "HTTP 403 Forbidden from speed.cloudflare.com", "wifi": {"rssi": -52, "snr": 38, "ch": 149, "band": "5", "tx": 866}}
    rec.update(kw)
    return "%s %s" % (t, json.dumps(rec, separators=(",", ":")))


class SpeedFeedCase(unittest.TestCase):
    """Runs against a throwaway log directory with its own control, info and progress files."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wifimonitor-speedfeed-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        for name, value in (("LOG_DIR", self.dir), ("CONTROL_FILE", os.path.join(self.dir, "control.json")), ("INFO_FILE", os.path.join(self.dir, "collector.json"))):
            patcher = unittest.mock.patch.object(s.logstore, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_progress(self, data):
        with open(os.path.join(self.dir, "speed.json"), "w") as f:
            json.dump(data, f)


class ReadSpeedTests(SpeedFeedCase):
    def test_tests_come_oldest_first_with_the_completion_stamp(self):
        write_lines("speed", "2026-10-09", [speed_line("2026-10-09 09:00:07", 100.0, start="2026-10-09 09:00:00"), speed_line("2026-10-09 10:00:07", 200.0)])
        out = s.read_speed()
        self.assertEqual([(t["t"], t["mbps"]) for t in out], [("2026-10-09 09:00:07", 100.0), ("2026-10-09 10:00:07", 200.0)])
        self.assertEqual(out[0]["start"], "2026-10-09 09:00:00")
        self.assertEqual(out[0]["wifi"]["ch"], 149)

    def test_bad_lines_are_skipped(self):
        write_lines("speed", "2026-10-09", ["garbage", "2026-10-09 09:00:07 {not json", "2026-10-09 09:00:08 [1, 2]", "2026-10-09 09:00:09 5",
                                            speed_line("2026-10-09 10:00:07"), ""])
        self.assertEqual([t["t"] for t in s.read_speed()], ["2026-10-09 10:00:07"])

    def test_limit_takes_the_newest_and_is_clamped(self):
        write_lines("speed", "2026-10-09", [speed_line("2026-10-09 %02d:00:00" % h) for h in range(1, 21)])
        self.assertEqual([t["t"][11:13] for t in s.read_speed(limit=3)], ["18", "19", "20"])
        self.assertEqual(len(s.read_speed()), 20)
        self.assertEqual(len(s.read_speed(limit=0)), 1)
        self.assertEqual(len(s.read_speed(limit=-5)), 1)
        self.assertEqual(len(s.read_speed(limit=100000)), 20)

    def test_the_limit_spans_days_but_a_day_ignores_it(self):
        write_lines("speed", "2026-10-08", [speed_line("2026-10-08 %02d:00:00" % h) for h in (1, 2, 3)])
        write_lines("speed", "2026-10-09", [speed_line("2026-10-09 %02d:00:00" % h) for h in (1, 2)])
        self.assertEqual([t["t"] for t in s.read_speed(limit=3)], ["2026-10-08 03:00:00", "2026-10-09 01:00:00", "2026-10-09 02:00:00"])
        self.assertEqual(len(s.read_speed("2026-10-08", limit=1)), 3)
        self.assertEqual(s.read_speed("2026-01-01"), [])

    def test_no_logs_is_an_empty_list(self):
        self.assertEqual(s.read_speed(), [])


class SpeedStateTests(SpeedFeedCase):
    def test_no_file_is_idle(self):
        st = s.read_speed_state(NOW)
        self.assertEqual(st, {"running": False, "phase": "idle", "id": None, "trigger": None, "host": None, "mb": None, "target_bytes": None, "bytes": 0,
                              "elapsed_s": None, "mbps": None, "avg_mbps": None, "ttfb_ms": None, "next_at": None, "updated": None})

    def test_a_running_test_is_passed_through(self):
        self.write_progress({"running": True, "phase": "downloading", "pid": 1, "id": "2026-10-09 12:00:00", "trigger": "manual", "host": "h", "mb": 25,
                             "target_bytes": 25000000, "bytes": 8000000, "elapsed_s": 2.1, "mbps": 301.2, "avg_mbps": 295.0, "ttfb_ms": 84.2,
                             "next_at": None, "updated": NOW.timestamp() - 1})
        st = s.read_speed_state(NOW)
        self.assertEqual((st["running"], st["phase"], st["bytes"], st["mbps"], st["avg_mbps"], st["id"]), (True, "downloading", 8000000, 301.2, 295.0, "2026-10-09 12:00:00"))
        self.assertNotIn("pid", st)

    def test_a_running_test_that_stopped_reporting_is_stale(self):
        self.write_progress({"running": True, "phase": "downloading", "updated": NOW.timestamp() - 6, "bytes": 5})
        st = s.read_speed_state(NOW)
        self.assertEqual((st["running"], st["phase"], st["bytes"]), (False, "stale", 5))
        self.write_progress({"running": True, "phase": "connecting"})  # never stamped
        self.assertEqual(s.read_speed_state(NOW)["phase"], "stale")

    def test_a_finished_test_is_not_stale_however_old(self):
        self.write_progress({"running": False, "phase": "done", "updated": NOW.timestamp() - 3600, "mbps": 50.0})
        st = s.read_speed_state(NOW)
        self.assertEqual((st["running"], st["phase"], st["mbps"]), (False, "done", 50.0))

    def test_damaged_or_odd_files_read_as_idle(self):
        for text in ("{nope", "[1]", '{"running": "yes", "phase": "weird"}'):
            with open(os.path.join(self.dir, "speed.json"), "w") as f:
                f.write(text)
            st = s.read_speed_state(NOW)
            self.assertEqual((st["running"], st["phase"]), (False, "idle"), text)


class SpeedApiTests(SpeedFeedCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), s.Handler)
        cls.port = cls.server.server_address[1]
        cls.port_patch = unittest.mock.patch.object(s, "PORT", cls.port)
        cls.port_patch.start()
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.port_patch.stop()
        cls.server.shutdown()
        cls.server.server_close()

    def get(self, query="", host=None):
        try:
            req = urllib.request.Request("http://127.0.0.1:%d/api/speed%s" % (self.port, query), headers={"Host": host} if host else {})
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_the_shape_with_nothing_logged_yet(self):
        code, d = self.get()
        self.assertEqual(code, 200)
        self.assertEqual(list(d), ["now", "day", "settings", "collector", "state", "tests", "usage"])
        self.assertRegex(d["now"], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
        self.assertIsNone(d["day"])
        self.assertEqual(d["settings"], {"url": None, "defaultUrl": "https://speed.cloudflare.com/__down?bytes={bytes}", "mb": 25, "every": 0, "run": None,
                                         "limits": {"mbMin": 10, "mbMax": 100, "every": [0, 600, 1800, 3600], "urlMax": 500}})
        self.assertEqual(d["collector"], {"running": False, "ago": None})
        self.assertEqual(d["state"]["phase"], "idle")
        self.assertEqual(d["tests"], [])
        self.assertEqual(d["usage"], {"estimatedMbPerDay": 0, "estimatedGbPerMonth": 0, "actualMb": 0.0, "actualTests": 0})

    def test_settings_tests_collector_and_usage_come_together(self):
        today = dt.date.today().isoformat()
        stamp = (dt.datetime.now() - dt.timedelta(seconds=3)).strftime("%Y-%m-%d %H:%M:%S")
        write_lines("monitor", today, ["%s gateway_ms=3.0 internet_ms=20.0" % stamp])
        write_lines("speed", today, [speed_line(today + " 00:00:01", 100.0), speed_line(today + " 00:00:02", 200.0), speed_line(today + " 00:00:03", None, ok=False, nbytes=0)])
        s.logstore.write_control(speed_url="https://example.com/f.bin", speed_mb=25, speed_every=600, speed_run=1760000000000)
        code, d = self.get()
        self.assertEqual(code, 200)
        self.assertEqual((d["settings"]["url"], d["settings"]["every"], d["settings"]["run"]), ("https://example.com/f.bin", 600, 1760000000000))
        self.assertTrue(d["collector"]["running"])
        self.assertLessEqual(d["collector"]["ago"], 10)
        self.assertEqual([t["mbps"] for t in d["tests"]], [100.0, 200.0, None])
        self.assertEqual(d["tests"][2]["ok"], False)
        self.assertEqual(d["usage"], {"estimatedMbPerDay": 3600.0, "estimatedGbPerMonth": 108.0, "actualMb": 50.0, "actualTests": 3})

    def test_the_custom_address_is_masked_for_a_foreign_host_header(self):
        url = "https://files.example.org:8443/big.bin?token=secret"
        s.logstore.write_control(speed_url=url)
        self.assertEqual(self.get()[1]["settings"]["url"], url)
        self.assertEqual(self.get(host="localhost:%d" % self.port)[1]["settings"]["url"], url)
        for host in ("evil.example", "evil.example:%d" % self.port, "127.0.0.1"):
            with self.subTest(host):
                d = self.get(host=host)[1]
                self.assertEqual(d["settings"]["url"], "files.example.org")
                self.assertNotIn("secret", json.dumps(d))
        s.logstore.write_control(speed_url=None)
        self.assertIsNone(self.get(host="evil.example")[1]["settings"]["url"])

    def test_the_collector_counts_as_stopped_when_samples_stop(self):
        today = dt.date.today().isoformat()
        stamp = (dt.datetime.now() - dt.timedelta(seconds=120)).strftime("%Y-%m-%d %H:%M:%S")
        write_lines("monitor", today, ["%s gateway_ms=3.0 internet_ms=20.0" % stamp])
        d = self.get()[1]
        self.assertEqual(d["collector"]["running"], False)
        self.assertGreaterEqual(d["collector"]["ago"], 120)

    def test_a_slower_interval_gives_the_collector_more_time(self):
        s.logstore.write_control(interval=15)  # allowed gap is max(30, 4 x 15) = 60 s
        write_lines("monitor", dt.date.today().isoformat(), ["%s gateway_ms=3.0 internet_ms=20.0" % (dt.datetime.now() - dt.timedelta(seconds=50)).strftime("%Y-%m-%d %H:%M:%S")])
        self.assertTrue(self.get()[1]["collector"]["running"])

    def test_events_between_samples_do_not_count_as_samples(self):
        today = dt.date.today().isoformat()
        old = (dt.datetime.now() - dt.timedelta(seconds=300)).strftime("%Y-%m-%d %H:%M:%S")
        new = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        write_lines("monitor", today, ["%s gateway_ms=3.0 internet_ms=20.0" % old, "%s EVENT Speed test started (manual, 25 MB from x)" % new])
        self.assertFalse(self.get()[1]["collector"]["running"])

    def test_a_day_returns_all_of_it_and_its_own_usage(self):
        write_lines("speed", "2026-10-08", [speed_line("2026-10-08 %02d:00:00" % h, nbytes=10000000) for h in range(1, 61)])
        code, d = self.get("?day=2026-10-08&limit=5")
        self.assertEqual((code, d["day"], len(d["tests"])), (200, "2026-10-08", 60))
        self.assertEqual((d["usage"]["actualMb"], d["usage"]["actualTests"]), (600.0, 60))
        code, d = self.get("?day=2026-01-01")
        self.assertEqual((code, d["tests"], d["usage"]["actualTests"]), (200, [], 0))

    def test_a_bad_day_is_refused_like_the_other_feeds(self):
        self.assertEqual(self.get("?day=yesterday"), (400, "bad day"))
        self.assertEqual(self.get("?day=2026-10-08;x")[0], 400)

    def test_limit_is_clamped_and_junk_falls_back(self):
        today = dt.date.today().isoformat()
        write_lines("speed", today, [speed_line(today + " %02d:00:00" % h) for h in range(0, 24)] * 1)
        self.assertEqual(len(self.get("?limit=5")[1]["tests"]), 5)
        self.assertEqual(len(self.get("?limit=0")[1]["tests"]), 1)
        self.assertEqual(len(self.get("?limit=-3")[1]["tests"]), 1)
        self.assertEqual(len(self.get("?limit=abc")[1]["tests"]), 24)
        self.assertEqual(len(self.get("?limit=9999")[1]["tests"]), 24)
        self.assertEqual(self.get("?limit=5")[1]["usage"]["actualTests"], 24)  # usage covers the whole day, not just the newest few

    def test_running_and_stale_state(self):
        self.write_progress({"running": True, "phase": "downloading", "bytes": 1, "mbps": 10.0, "updated": time.time()})
        st = self.get()[1]["state"]
        self.assertEqual((st["running"], st["phase"], st["mbps"]), (True, "downloading", 10.0))
        self.write_progress({"running": True, "phase": "downloading", "bytes": 1, "mbps": 10.0, "updated": time.time() - 30})
        st = self.get()[1]["state"]
        self.assertEqual((st["running"], st["phase"]), (False, "stale"))

    def test_the_data_feed_carries_the_flag_and_the_probes_feed_the_speed_row(self):
        today = dt.date.today().isoformat()
        write_lines("monitor", today, ["%s 10:00:00 gateway_ms=3.0 internet_ms=20.0 speed=1" % today, "%s 10:00:05 gateway_ms=3.0 internet_ms=20.0" % today])
        with urllib.request.urlopen("http://127.0.0.1:%d/api/data" % self.port, timeout=5) as r:
            rows = json.loads(r.read())
        self.assertEqual([r["speed"] for r in rows], [True, False])
        write_lines("speed", today, [speed_line(today + " 09:00:00", 312.44)])
        with urllib.request.urlopen("http://127.0.0.1:%d/api/probes" % self.port, timeout=5) as r:
            probes = {p["id"]: p for p in json.loads(r.read())["probes"]}
        self.assertEqual((probes["speed"]["result"], probes["speed"]["state"], probes["speed"]["last"]), ("312.4 Mbps", "ok", today + " 09:00:00"))


if __name__ == "__main__":
    unittest.main()
