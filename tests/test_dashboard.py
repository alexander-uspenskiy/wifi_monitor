"""Dashboard page logic (availability, loss, events, markers), run with Node on the real functions from src/dashboard.html.
Skipped when Node.js is not installed."""
import json
import os
import re
import shutil
import subprocess
import unittest

import context

HTML = os.path.join(context.SRC, "dashboard.html")
HARNESS = os.path.join(context.ROOT, "tests", "dashboard_harness.js")
NODE = shutil.which("node")
T0 = 1_760_000_000_000


def row(i, **kw):
    d = {"x": T0 + i * 5000, "t": "", "gw": 3.0, "net": 20.0, "gwLost": False, "netLost": False, "assoc": True,
         "rssi": -60, "snr": 22, "ch": 36, "band": "5"}
    d.update(kw)
    return d


def lost(i, which):
    kw = {}
    if "gw" in which:
        kw.update(gw=None, gwLost=True)
    if "net" in which:
        kw.update(net=None, netLost=True)
    return row(i, **kw)


def err(i, which, **kw):
    d = {}
    if "gw" in which:
        d.update(gw=None, gwErr=True)
    if "net" in which:
        d.update(net=None, netErr=True)
    d.update(kw)
    return row(i, **d)


def helpers(calls):
    r = subprocess.run([NODE, HARNESS, HTML], input=json.dumps({"rows": [], "notes": [], "merrs": [], "scans": [], "xmin": 0, "xmax": 1, "calls": calls}),
                       capture_output=True, text=True)
    if r.returncode:
        raise AssertionError(r.stderr)
    return json.loads(r.stdout)["calls"]


def analyse(rows, notes=(), merrs=(), scans=()):
    payload = {"rows": rows, "notes": list(notes), "merrs": list(merrs), "scans": list(scans),
               "xmin": rows[0]["x"] if rows else T0, "xmax": rows[-1]["x"] + 1 if rows else T0 + 1}
    r = subprocess.run([NODE, HARNESS, HTML], input=json.dumps(payload), capture_output=True, text=True)
    if r.returncode:
        raise AssertionError(r.stderr)
    return json.loads(r.stdout)


@unittest.skipUnless(NODE, "Node.js is not installed")
class NormalDataUnchangedTests(unittest.TestCase):
    """Numbers produced by the page logic before ERR support existed, for a fixed data set without any ERR."""

    def test_golden_dataset(self):
        rows = [row(i, ch=36 if i < 100 else 40) for i in range(120)]
        rows[10], rows[11] = lost(10, "net"), lost(11, "net")
        rows[40] = lost(40, "gw")
        rows[60] = row(60, gw=None, net=None, gwLost=True, netLost=True, assoc=False, ch=36)
        rows[61] = row(61, gw=None, net=None, gwLost=True, netLost=True, assoc=False, ch=36)
        rows[80] = row(80, gw=120.0)
        out = analyse(rows, notes=[{"x": T0 + 30000, "t": "", "text": "moved"}])
        st = out["stats"]
        self.assertEqual((st["outages"], st["changes"], st["notes"], st["errors"]), (1, 1, 1, 0))
        self.assertEqual((st["longest"], st["longestDown"], st["worst"]), (10000, 10000, 10000))
        self.assertAlmostEqual(st["avail"], 95.7983, places=3)
        self.assertAlmostEqual(st["availGw"], 97.479, places=3)
        self.assertAlmostEqual(st["availNet"], 96.6387, places=3)
        self.assertEqual((st["jitter"], st["call"]), (0, 95))
        self.assertEqual([m["type"] for m in out["markers"]], ["outages", "changes", "notes"])
        self.assertEqual([e["label"] for e in out["events"]], ["CHANNEL", "OUTAGE", "NOTE"])
        self.assertEqual([e["group"] for e in out["spikes"]], ["ISP or internet", "Wi-Fi", "Wi-Fi", "Wi-Fi"])

    def test_availability_counts_either_ping(self):
        rows = [row(i) for i in range(10)]
        rows[3] = lost(3, "net")
        st = analyse(rows)["stats"]
        self.assertAlmostEqual(st["avail"], 88.8889, places=3)  # 1 of 9 intervals down (observed time is counted once)
        self.assertAlmostEqual(st["availGw"], 100)
        self.assertAlmostEqual(st["availNet"], 88.8889, places=3)

    def test_empty_data(self):
        st = analyse([])["stats"]
        self.assertEqual((st["avail"], st["call"], st["outages"]), (None, None, 0))


@unittest.skipUnless(NODE, "Node.js is not installed")
class ErrHandlingTests(unittest.TestCase):
    def test_err_samples_do_not_lower_availability(self):
        rows = [row(i) for i in range(20)]
        for i in range(5, 12):
            rows[i] = err(i, "gw net", rssi=None, snr=None)
        st = analyse(rows)["stats"]
        self.assertEqual((st["avail"], st["availGw"], st["availNet"]), (100, 100, 100))
        self.assertEqual(st["longestDown"], 0)

    def test_err_is_unknown_so_known_loss_still_counts(self):
        rows = [row(i) for i in range(10)]
        rows[4] = err(4, "gw", net=None, netLost=True)  # router unknown, internet lost: still unavailable
        rows[6] = err(6, "gw")                          # router unknown, internet fine: not counted either way
        st = analyse(rows)["stats"]
        self.assertAlmostEqual(st["avail"], 100 * (1 - 1 / 8), places=3)  # 9 intervals, 1 unknown, 1 down
        self.assertAlmostEqual(st["availNet"], 100 * (1 - 1 / 9), places=3)
        self.assertAlmostEqual(st["availGw"], 100 * (1 - 0 / 7), places=3)  # router unknown for 2 intervals

    def test_disconnect_is_known_even_when_the_pings_could_not_be_measured(self):
        rows = [row(i) for i in range(6)]
        rows[2] = err(2, "gw net", assoc=False)
        st = analyse(rows)["stats"]
        self.assertAlmostEqual(st["avail"], 80, places=3)

    def test_err_samples_are_not_loss_outages_or_spikes(self):
        rows = [row(i) for i in range(30)]
        for i in (10, 11, 12):
            rows[i] = err(i, "gw net")
        out = analyse(rows)
        self.assertEqual(out["stats"]["outages"], 0)
        self.assertEqual(out["spikes"], [])
        self.assertEqual([e for e in out["events"] if e["label"] == "OUTAGE"], [])

    def test_call_ready_ignores_err_samples(self):
        rows = [row(i) for i in range(10)]
        for i in (3, 4):
            rows[i] = err(i, "net", rssi=None, snr=None)
        st = analyse(rows)["stats"]
        self.assertEqual((st["call"], st["worst"]), (100, 0))

    def test_measurement_events_become_markers_and_events_but_not_notes(self):
        rows = [row(i) for i in range(10)]
        merrs = [{"x": T0 + 15000, "t": "", "text": "Measurement error: Wi-Fi status could not be measured (x). Not counted as packet loss or a disconnect."},
                 {"x": T0 + 35000, "t": "", "text": "Measurement recovered: Wi-Fi status is being measured again"}]
        out = analyse(rows, notes=[{"x": T0 + 5000, "t": "", "text": "my note"}], merrs=merrs)
        self.assertEqual(sorted(m["type"] for m in out["markers"]), ["errors", "errors", "notes"])
        self.assertEqual((out["stats"]["errors"], out["stats"]["notes"]), (1, 1))  # only "error" counts as an error episode
        labels = {e["label"]: e["tag"] for e in out["events"]}
        self.assertEqual(labels, {"ERROR": "warn", "OK": "info", "NOTE": "info"})


@unittest.skipUnless(NODE, "Node.js is not installed")
class PageIntegrityTests(unittest.TestCase):
    def setUp(self):
        with open(HTML, encoding="utf-8") as f:
            self.html = f.read()

    def test_script_has_valid_syntax(self):
        script = re.search(r"<script>(.*)</script>", self.html, re.S).group(1)
        path = os.path.join(os.environ["WIFI_LOG_DIR"], "page.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(script)
        r = subprocess.run([NODE, "--check", path], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_ids_the_script_uses_exist(self):
        script = re.search(r"<script>(.*)</script>", self.html, re.S).group(1)
        for ident in set(re.findall(r"\$\('#(\w+)'\)", script)):
            with self.subTest(ident):
                self.assertIn('id="%s"' % ident, self.html)

    def test_new_pieces_are_wired(self):
        for needle in ('id="mbanner"', "errors: 'Measurement errors'", "isMeasure", "'router_error'", "measurement_error_samples",
                       'data-tip-key="stop"', 'id="stopBtn"'):
            with self.subTest(needle):
                self.assertIn(needle, self.html)


@unittest.skipUnless(NODE, "Node.js is not installed")
class RefreshAndProbesTests(unittest.TestCase):
    def setUp(self):
        with open(HTML, encoding="utf-8") as f:
            self.html = f.read()

    def test_refresh_selector_offers_the_expected_choices_and_defaults_to_5s(self):
        sel = re.search(r'<select[^>]*id="refreshSel".*?</select>', self.html, re.S).group(0)
        self.assertEqual(re.findall(r'<option value="(\d+)"', sel), ["2", "5", "10", "30", "60", "0"])
        self.assertIn("let refreshSec = 5;", self.html)

    def test_refresh_uses_the_selected_interval_not_a_fixed_one(self):
        self.assertIn("refreshSec * 1000", self.html)
        self.assertNotIn("}, 5000);", self.html)
        self.assertIn("localStorage.setItem('refreshSec'", self.html)
        self.assertIn("window.stopped", self.html)  # still stops after the service is stopped

    def test_probes_card_is_wired(self):
        for needle in ('id="probesbody"', 'id="probestatus"', 'data-tip-key="probes"', "/api/probes", "Dashboard refresh", "probes: () =>"):
            with self.subTest(needle):
                self.assertIn(needle, self.html)

    def test_probe_menus_are_wired(self):
        for needle in ("function everySelect", 'data-ctl="', "start-up value", "'/api/control'", "body.contains(document.activeElement)"):
            with self.subTest(needle):
                self.assertIn(needle, self.html)

    def test_menu_labels(self):
        out = helpers([["everyLabel", ["scan_every", 0]], ["everyLabel", ["scan_every", 900]], ["everyLabel", ["interval", 5]], ["everyLabel", ["scan_every", 3600]]])
        self.assertEqual(out, ["off", "15 min", "5 s", "1 h"])

    def test_formatting_helpers(self):
        out = helpers([["fmtEvery", [0]], ["fmtEvery", [5]], ["fmtEvery", [60]], ["fmtEvery", [900]], ["fmtEvery", [5400]],
                       ["fmtAgo", [None]], ["fmtAgo", [0]], ["fmtAgo", [12]], ["fmtAgo", [150]], ["fmtAgo", [7300]],
                       ["refreshLabel", [2]], ["refreshLabel", [60]], ["escHtml", ['<b a="1">&']]])
        self.assertEqual(out, ["off", "5 s", "1 min", "15 min", "1.5 h", "–", "just now", "12 s ago", "2 min ago", "2 h ago",
                               "2s", "1 min", "&lt;b a=&quot;1&quot;&gt;&amp;"])


if __name__ == "__main__":
    unittest.main()
