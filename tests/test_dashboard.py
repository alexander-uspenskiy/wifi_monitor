"""Dashboard page logic (availability, loss, events, markers), run with Node on the real functions from src/dashboard.html.
Skipped when Node.js is not installed."""
import json
import os
import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser

import context

HTML = os.path.join(context.SRC, "dashboard.html")
README = os.path.join(context.ROOT, "README.md")
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


def simulate_probes(scenarios):
    """Runs the page's setProbesOpen against a fake DOM and storage; one list of states per scenario."""
    payload = {"rows": [], "notes": [], "merrs": [], "scans": [], "xmin": 0, "xmax": 1, "probesSim": scenarios}
    r = subprocess.run([NODE, HARNESS, HTML], input=json.dumps(payload), capture_output=True, text=True)
    if r.returncode:
        raise AssertionError(r.stderr)
    return json.loads(r.stdout)["probesSim"]


class Markup(HTMLParser):
    """Collects every element of the page with its attributes, ancestors (as (tag, id) pairs) and text."""
    VOID = {"meta", "link", "input", "br", "hr", "img", "path", "circle", "rect", "line", "source", "col"}

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.els, self.stack, self.open_ttl = [], [], []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        el = {"tag": tag, "attrs": dict(attrs), "up": [(t, a.get("id")) for t, a in self.stack], "text": ""}
        self.els.append(el)
        if tag not in self.VOID:
            self.stack.append((tag, el["attrs"]))
            self.open_ttl.append(el)

    def handle_startendtag(self, tag, attrs):
        self.els.append({"tag": tag, "attrs": dict(attrs), "up": [(t, a.get("id")) for t, a in self.stack], "text": ""})

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:], self.open_ttl[i:]
                return

    def handle_data(self, data):
        for el in self.open_ttl:
            el["text"] += data

    def by_id(self, ident):
        return [e for e in self.els if e["attrs"].get("id") == ident]


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


class CompactTableTests(unittest.TestCase):
    """The Daily summary and Probes settings tables are compact so they fit without a sideways scroller on normal screens."""

    def setUp(self):
        with open(HTML, encoding="utf-8") as f:
            self.html = f.read()

    def test_only_the_two_tables_are_compact(self):
        tables = re.findall(r'<table class="([^"]*)"', self.html)
        compact = [c for c in tables if "compact" in c.split()]
        self.assertEqual(sorted(compact), ["days compact", "days probes compact"])
        # the "likely cause" analysis table keeps its own look
        self.assertIn('<table class="days spikes">', self.html)  # built in JS, so it is not in the `tables` list above

    def test_compact_rules_let_headers_and_notes_wrap(self):
        self.assertRegex(self.html, r"table\.compact th \{[^}]*white-space: normal")
        self.assertRegex(self.html, r"table\.compact td \{[^}]*padding: 5px 5px")
        # the Every column holds a note inside a nowrap cell; without this it forces the table about 370 px wide
        self.assertRegex(self.html, r"table\.probes\.compact \.note \{[^}]*white-space: normal")
        self.assertRegex(self.html, r"table\.probes\.compact td:nth-child\(4\) \{ max-width: 150px; \}")

    def test_narrow_screens_get_an_even_tighter_table(self):
        self.assertRegex(self.html, r"@media \(max-width: 800px\) \{ table\.compact \{ font-size: 11\.5px; \}")

    def test_the_scroll_fallback_is_kept_for_very_narrow_screens(self):
        self.assertRegex(self.html, r"\.tablewrap \{ overflow-x: auto; \}")


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


def mk_speed(mbps, ok=True, **kw):
    """One entry of the "tests" list of /api/speed."""
    t = {"v": 1, "start": "2026-10-09 12:00:00", "t": "2026-10-09 12:00:06", "trigger": "manual", "ok": ok, "mbps": mbps if ok else None, "ttfb_ms": 80.0 if ok else None,
         "bytes": 25_000_000 if ok else None, "dur_s": 5.0, "status": 200 if ok else None, "host": "speed.cloudflare.com", "mb": 25, "capped": False,
         "kind": None if ok else "dns", "reason": None if ok else "could not resolve", "wifi": {"rssi": -50, "snr": 38, "ch": 149, "band": "5", "tx": 866}}
    t.update(kw)
    return t


@unittest.skipUnless(NODE, "Node.js is not installed")
class SpeedHelperTests(unittest.TestCase):
    def call(self, name, *args):
        return helpers([[name, list(args)]])[0]

    def test_scale_picks_the_smallest_that_fits(self):
        cases = [(None, 100), (0, 100), (45.7, 100), (100, 100), (100.1, 250), (250, 250), (251, 500), (500, 500), (501, 1000), (940, 1000), (5000, 1000)]
        self.assertEqual(helpers([["speedScale", [v]] for v, _ in cases]), [want for _, want in cases])

    def test_needle_angle_is_clamped_to_the_dial(self):
        out = helpers([["needleAngle", [v, sc]] for v, sc in [(None, 100), (-5, 100), (0, 100), (50, 100), (100, 100), (400, 100), (125, 250)]])
        self.assertEqual(out, [0, 0, 0, 90, 180, 180, 90])

    def test_mbps_text(self):
        out = helpers([["fmtMbps", [v]] for v in (None, 312.4, 100, 99.94, 45.7, 5, 0)])
        self.assertEqual(out, ["–", "312 Mbps", "100 Mbps", "99.9 Mbps", "45.7 Mbps", "5.0 Mbps", "0.0 Mbps"])

    def test_median_ignores_failed_tests(self):
        tests = [mk_speed(100), mk_speed(0, ok=False), mk_speed(300), mk_speed(200)]
        self.assertEqual(self.call("speedMedian", tests), 200)
        self.assertEqual(self.call("speedMedian", [tests[0], tests[2]]), 200)  # even count: mean of the middle two
        self.assertIsNone(self.call("speedMedian", [mk_speed(0, ok=False)]))
        self.assertIsNone(self.call("speedMedian", []))

    def test_ok_needs_a_real_number(self):
        out = helpers([["speedOk", [t]] for t in (mk_speed(10), mk_speed(0, ok=False), mk_speed(None), {"ok": True, "mbps": None}, None)])
        self.assertEqual(out, [True, False, False, False, False])

    def test_bar_colours_follow_the_median(self):
        out = helpers([["speedBarColor", [v, 100]] for v in (100, 80, 79.9, 50, 49.9, 0)] + [["speedBarColor", [None, 100]], ["speedBarColor", [30, None]]])
        self.assertEqual(out, ["good", "good", "warn", "warn", "bad", "bad", "muted", "good"])

    def test_usage_estimate_and_metered_warning(self):
        one, hourly, half, tenmin = (self.call("speedUsage", *a) for a in ((25, 0), (25, 3600), (25, 1800), (25, 600)))
        self.assertEqual((one["perDayMB"], one["perMonthGB"], one["warn"]), (0, 0, False))
        self.assertIn("25 MB", one["text"])
        self.assertIn("only when you press Run now", one["text"])
        self.assertNotIn("per day", one["text"])  # one-time mode: no "about 0 MB per day"
        self.assertEqual((hourly["perDayMB"], hourly["warn"]), (600, False))
        self.assertAlmostEqual(hourly["perMonthGB"], 18)
        self.assertEqual((half["perDayMB"], half["warn"]), (1200, True))
        self.assertAlmostEqual(tenmin["perDayMB"], 3600)
        self.assertAlmostEqual(tenmin["perMonthGB"], 108)
        self.assertIn("108 GB", tenmin["text"])
        for u in (one, hourly):
            self.assertNotIn("metered", u["text"])
        for u in (half, tenmin):
            self.assertIn("metered", u["text"])

    def test_stats_leave_failed_tests_out_of_the_numbers(self):
        st = self.call("speedStats", [mk_speed(100), mk_speed(0, ok=False), mk_speed(300)])
        self.assertEqual((st["n"], st["okN"], st["last"], st["median"], st["min"], st["max"], st["avg"]), (3, 2, 300, 200, 100, 300, 200))
        self.assertAlmostEqual(st["cv"], 0.5)
        empty = self.call("speedStats", [])
        self.assertEqual((empty["n"], empty["okN"], empty["last"], empty["median"], empty["cv"]), (0, 0, None, None, None))
        only_err = self.call("speedStats", [mk_speed(0, ok=False)])
        self.assertEqual((only_err["n"], only_err["okN"], only_err["last"], only_err["min"]), (1, 0, None, None))


@unittest.skipUnless(NODE, "Node.js is not installed")
class SpeedAdviceTests(unittest.TestCase):
    def advice(self, tests, *link):
        return helpers([["speedAdvice", [tests, *link]]])[0]

    def keys(self, tests, *link):
        return self.advice(tests, *link)["keys"]

    def test_no_data_gives_no_advice(self):
        self.assertEqual(self.advice([]), {"keys": [], "messages": []})

    def test_healthy_result_says_ok_and_always_adds_the_caveat(self):
        a = self.advice([mk_speed(400)])
        self.assertEqual(a["keys"], ["ok"])
        self.assertEqual(len(a["messages"]), 2)
        self.assertIn("single download", a["messages"][-1])
        self.assertIn("single download", self.advice([mk_speed(20, wifi={"rssi": -80, "snr": 10, "tx": 100})])["messages"][-1])

    def test_weak_wifi(self):
        for wifi in ({"rssi": -50, "snr": 20, "tx": 866}, {"rssi": -72, "snr": 38, "tx": 866}, {"rssi": -75, "snr": None, "tx": None}):
            with self.subTest(wifi):
                self.assertEqual(self.keys([mk_speed(30, wifi=wifi)]), ["wifi_weak"])

    def test_far_below_the_negotiated_link_with_a_good_link(self):
        self.assertEqual(self.keys([mk_speed(100)]), ["below_link"])   # 100 < 0.35 * 866
        self.assertEqual(self.keys([mk_speed(303)]), ["below_link"])   # 0.35 * 866 = 303.1
        self.assertEqual(self.keys([mk_speed(304)]), ["ok"])
        # Windows has no snr: the rssi alone decides
        self.assertEqual(self.keys([mk_speed(80, wifi={"rssi": -55, "snr": None, "tx": 300})]), ["below_link"])
        # a link that is not good (rssi -68) is not blamed on the internet
        self.assertEqual(self.keys([mk_speed(80, wifi={"rssi": -68, "snr": 30, "tx": 866})]), ["ok"])

    def test_good_link_but_slow_when_tx_is_unknown(self):
        self.assertEqual(self.keys([mk_speed(30, wifi={"rssi": -50, "snr": 40, "tx": None})]), ["good_link_slow"])
        self.assertEqual(self.keys([mk_speed(60, wifi={"rssi": -50, "snr": 40, "tx": None})]), ["ok"])

    def test_missing_wifi_block_is_not_an_error(self):
        self.assertEqual(self.keys([mk_speed(30, wifi=None)]), ["ok"])
        self.assertEqual(self.keys([mk_speed(30)], None), ["ok"])  # link given as null

    def test_link_argument_overrides_the_test_block(self):
        self.assertEqual(self.keys([mk_speed(400)], {"rssi": -80, "snr": 10, "tx": 100}), ["wifi_weak"])

    def test_variance_needs_five_good_tests(self):
        spread = [mk_speed(v) for v in (300, 100, 300, 120, 300)]
        self.assertIn("variance", self.keys(spread))
        self.assertNotIn("variance", self.keys(spread[:4]))
        steady = [mk_speed(v) for v in (300, 310, 290, 305, 295)]
        self.assertNotIn("variance", self.keys(steady))
        # a failed test is not a zero: it does not make the series look unsteady
        self.assertNotIn("variance", self.keys(steady + [mk_speed(0, ok=False)]))

    def test_high_first_byte_time(self):
        self.assertIn("ttfb_high", self.keys([mk_speed(400, ttfb_ms=501)]))
        self.assertNotIn("ttfb_high", self.keys([mk_speed(400, ttfb_ms=500)]))

    def test_slow_first_byte_is_not_reported_on_a_very_slow_link(self):
        self.assertNotIn("ttfb_high", self.keys([mk_speed(4.9, ttfb_ms=900)]))
        self.assertIn("ttfb_high", self.keys([mk_speed(5, ttfb_ms=900)]))

    def test_failures(self):
        bad = mk_speed(0, ok=False)
        self.assertEqual(self.keys([mk_speed(400), bad, bad, bad]), ["all_failed"])
        self.assertEqual(self.keys([mk_speed(400), bad, bad, mk_speed(400)]), ["ok"])
        self.assertEqual(self.keys([bad]), ["all_failed"])  # nothing has ever worked


@unittest.skipUnless(NODE, "Node.js is not installed")
class SpeedSamplesAreNotCountedTests(unittest.TestCase):
    """Samples taken during a speed test (speed: true) are drawn, but never count as loss, outages, spikes or unavailability."""

    def noisy(self):
        rows = [row(i) for i in range(60)]
        for i in range(20, 30):  # a speed test fills the link: slow and lost pings
            rows[i] = row(i, gw=250.0, net=None, netLost=True, speed=True)
        rows[25] = row(25, gw=None, net=None, gwLost=True, netLost=True, speed=True)
        return rows

    def test_no_outage_spike_or_unavailability_from_a_test(self):
        out = analyse(self.noisy())
        st = out["stats"]
        self.assertEqual((st["outages"], st["longest"], st["longestDown"]), (0, 0, 0))
        self.assertEqual((st["avail"], st["availGw"], st["availNet"]), (100, 100, 100))
        self.assertEqual(out["spikes"], [])
        self.assertEqual(out["markers"], [])
        self.assertEqual(out["events"], [])

    def test_a_test_leaves_no_gap_and_jitter_ignores_it(self):
        rows = self.noisy()
        for i in range(20, 30):
            rows[i]["net"] = 400.0 + i
            rows[i]["netLost"] = False
        out = analyse(rows)
        self.assertEqual(out["markers"], [])
        self.assertEqual(out["stats"]["jitter"], 0)
        self.assertEqual(out["stats"]["call"], 100)

    def test_a_real_outage_around_a_test_is_still_found(self):
        rows = [row(i) for i in range(60)]
        for i in (10, 11, 12):
            rows[i] = row(i, gw=None, net=None, gwLost=True, netLost=True)
        for i in range(30, 40):
            rows[i] = row(i, net=500.0, speed=True)
        out = analyse(rows)
        self.assertEqual(out["stats"]["outages"], 1)
        self.assertEqual([e["label"] for e in out["events"]], ["OUTAGE"])
        self.assertEqual(len(out["spikes"]), 1)  # the lost pings at 10 to 12, not the test

    def test_an_outage_that_spans_a_test_is_not_cut_in_two(self):
        rows = [row(i) for i in range(40)]
        for i in range(5, 35):
            rows[i] = row(i, gw=None, net=None, gwLost=True, netLost=True, speed=15 <= i < 20)
        out = analyse(rows)
        self.assertEqual(out["stats"]["outages"], 1)

    def test_data_without_the_flag_is_unchanged(self):
        rows = [row(i) for i in range(30)]
        rows[5] = lost(5, "net")
        self.assertEqual(analyse(rows)["stats"], analyse([dict(r, speed=False) for r in rows])["stats"])


@unittest.skipUnless(NODE, "Node.js is not installed")
class SpeedSamplesInTilesAndSelectionTests(unittest.TestCase):
    """latencyFigures feeds the tiles (render) and the selection panel (renderSel): speed-test samples must not reach either."""

    def rows(self):
        rows = [row(i, gw=3.0 + i % 2, net=20.0) for i in range(10)]
        rows[2] = lost(2, "net")
        rows[3] = err(3, "gw")
        for i in (5, 6, 7):  # during a test: slow, lost and unmeasured pings
            rows[i] = row(i, gw=300.0, net=None, netLost=True, speed=True)
        rows[6] = row(6, gw=None, net=None, gwLost=True, netLost=True, speed=True)
        rows[7] = row(7, gw=None, net=None, gwErr=True, netErr=True, speed=True)
        return rows

    def test_speed_samples_are_left_out_of_every_figure(self):
        f = helpers([["latencyFigures", [self.rows()]]])[0]
        self.assertEqual(len(f["core"]), 7)
        self.assertEqual(f["gw"], [3.0, 4.0, 3.0, 3.0, 3.0, 4.0])  # the 300 ms pings are gone
        self.assertEqual(f["net"], [20.0] * 6)
        self.assertEqual((f["gwLost"], f["netLost"]), (0, 1))
        self.assertEqual((f["gwN"], f["netN"], f["errN"]), (6, 7, 1))

    def test_data_without_speed_samples_is_counted_in_full(self):
        rows = [row(i) for i in range(5)]
        rows[1] = lost(1, "gw net")
        f = helpers([["latencyFigures", [rows]]])[0]
        self.assertEqual((len(f["core"]), len(f["gw"]), len(f["net"]), f["gwLost"], f["netLost"], f["gwN"], f["netN"], f["errN"]), (5, 4, 4, 1, 1, 5, 5, 0))

    def test_tiles_and_selection_panel_use_the_helper(self):
        with open(HTML, encoding="utf-8") as f:
            script = re.search(r"<script>(.*)</script>", f.read(), re.S).group(1)
        render = re.search(r"\nfunction render\(\) \{.*?\n\}\n", script, re.S).group(0)
        sel = re.search(r"\nfunction renderSel\(\) \{.*?\n\}\n", script, re.S).group(0)
        self.assertIn("= latencyFigures(rows);", render)
        self.assertIn("= latencyFigures(rs);", sel)
        # no figure is taken straight from the unfiltered rows any more
        self.assertNotRegex(render, r"\b(gwOk|netOk|gwN|netN|errN|gwLost|netLost)\s*=\s*rows\.filter")
        self.assertNotRegex(sel, r"rs\.filter\(d => d\.(gw|net)")
        self.assertNotRegex(sel, r"rs\.length\) \* ?100|rs\.length\)\.toFixed")


@unittest.skipUnless(NODE, "Node.js is not installed")
class SpeedMeasurementEventsTests(unittest.TestCase):
    """"Measurement error: speed test ..." events are about the speed test, not about the ping or Wi-Fi measurements."""
    SPEED_ERR = "Measurement error: speed test could not be measured (could not resolve). Not counted as packet loss or a disconnect."
    PING_ERR = "Measurement error: Wi-Fi status could not be measured (x). Not counted as packet loss or a disconnect."

    def test_which_events_are_speed_events(self):
        texts = [self.SPEED_ERR, "Measurement recovered: speed test is being measured again", self.PING_ERR, "Measurement recovered: router ping is being measured again",
                 "Measurement error: speed testing x", "Speed test failed: could not resolve"]
        out = helpers([["isSpeedMeasure", [{"text": t}]] for t in texts])
        self.assertEqual(out, [True, True, False, False, False, False])

    def test_speed_error_is_not_counted_as_a_ping_error(self):
        rows = [row(i) for i in range(10)]
        out = analyse(rows, merrs=[{"x": T0 + 15000, "t": "", "text": self.SPEED_ERR}])
        self.assertEqual(out["stats"]["errors"], 0)
        # it still shows up in the Events card and on the chart, with its own wording
        self.assertEqual([m["type"] for m in out["markers"]], ["errors"])
        self.assertIn("speed test", out["events"][0]["text"])
        both = analyse(rows, merrs=[{"x": T0 + 15000, "t": "", "text": self.SPEED_ERR}, {"x": T0 + 20000, "t": "", "text": self.PING_ERR}])
        self.assertEqual(both["stats"]["errors"], 1)

    def test_banner_reason_skips_speed_events(self):
        with open(HTML, encoding="utf-8") as f:
            script = re.search(r"<script>(.*)</script>", f.read(), re.S).group(1)
        why = re.search(r"const why = (.*);", script).group(1)
        self.assertIn("!isSpeedMeasure(n)", why)


class SpeedCardMarkupTests(unittest.TestCase):
    """Static checks of the Internet download speed card (no Node needed)."""

    @classmethod
    def setUpClass(cls):
        with open(HTML, encoding="utf-8") as f:
            cls.html = f.read()
        with open(README, encoding="utf-8") as f:
            cls.readme = f.read()
        cls.dom = Markup(cls.html)
        cls.script = re.search(r"<script>(.*)</script>", cls.html, re.S).group(1)

    def one(self, ident):
        found = self.dom.by_id(ident)
        self.assertEqual(len(found), 1, "id %s must exist exactly once" % ident)
        return found[0]

    def test_ids_exist_exactly_once_inside_the_card(self):
        for ident in ("spUrl", "spMb", "spEvery", "spRun", "spSave", "spUsage", "spGauge", "cSpeed", "spBody", "spAdvice"):
            with self.subTest(ident):
                self.assertIn(("div", "speedCard"), self.one(ident)["up"])
        self.assertEqual(self.one("speedCard")["attrs"].get("class", "").split(), ["card", "full"])

    def test_card_comes_right_after_the_least_crowded_channels_card(self):
        i_blocks, i_speed, i_tx = (self.html.index(n) for n in ('id="cBlocks"', 'id="speedCard"', '<div class="card"><h2>Transmit rate'))
        self.assertLess(i_blocks, i_speed)
        self.assertLess(i_speed, i_tx)
        # the only card opened between the chart and the end of the speed card's opening tag is the speed card itself
        self.assertEqual(self.html[i_blocks:i_speed].count('<div class="card'), 1)
        self.assertEqual(self.html[i_speed:i_tx].count('<div class="card'), 0)

    def test_title_hint_and_label(self):
        h2 = re.search(r'<div class="card full" id="speedCard"><h2>(.*?)</h2>', self.html, re.S).group(1)
        self.assertTrue(h2.startswith("Internet download speed"))
        self.assertIn('data-tip-key="speed"', h2)
        self.assertIn("Single download from one server. Not a full speed test: it can read lower than your plan on fast connections.", self.html)

    def test_form_controls(self):
        url = self.one("spUrl")["attrs"]
        self.assertEqual((url["type"], url["maxlength"]), ("url", "500"))
        self.assertEqual(url["placeholder"], "Default: Cloudflare (speed.cloudflare.com)")
        mb = self.one("spMb")["attrs"]
        self.assertEqual((mb["type"], mb["min"], mb["max"]), ("number", "10", "100"))
        sel = re.search(r'<select[^>]*id="spEvery".*?</select>', self.html, re.S).group(0)
        self.assertEqual(re.findall(r'<option value="(\d+)">([^<]*)<', sel), [("0", "One time"), ("600", "Every 10 min"), ("1800", "Every 30 min"), ("3600", "Every 1 hour")])
        self.assertNotIn("start-up", sel)
        self.assertNotIn("data-ctl", self.one("spEvery")["attrs"])  # not the probes table's menu mechanism
        self.assertEqual(self.one("spRun")["text"], "Run now")
        self.assertEqual(self.one("spSave")["text"], "Save")
        self.assertEqual(self.one("spGauge")["tag"], "svg")
        self.assertEqual(self.one("cSpeed")["tag"], "canvas")

    def test_tip_fix_and_export_entries(self):
        for needle in ('  speed: "', "FIX.speed = {", "speed: 'speed'", "speed: 'download-speed'", "  speed: r => ({"):
            with self.subTest(needle):
                self.assertIn(needle, self.script)
        fix = re.search(r"FIX\.speed = \{(.*?)\};", self.script, re.S).group(1)
        for part in ("you:", "router:", "isp:"):
            self.assertIn(part, fix)
        # the existing wiring turns the hint into the export button and the "How to improve" block
        self.assertIn("EXPORTS[key] && card", self.script)
        self.assertIn("FIX[key] && card", self.script)

    def test_probes_tip_names_the_one_exception(self):
        self.assertIn("about 100 bytes each. The one exception is the Internet download speed test, which downloads 10 to 100 MB from a speed server, "
                      "only when you press Run now or choose a schedule in its own card.", self.script)

    def test_behaviour_is_wired(self):
        for needle in ("fetch('/api/speed'", "speed_run: true", "body.speed_url", "speed_mb:", "speed_every:", "id: 'speedTests'", "globalAlpha = 0.10",
                       "The collector is not running, so nothing will happen. Start it first", "setTimeout(loadSpeed, 1000)", "document.hidden", "d.speed", "speedPlugin]"):
            with self.subTest(needle):
                self.assertIn(needle, self.script)

    def test_error_texts_and_masked_url_handling(self):
        self.assertNotIn(".slice(0, 300)", self.script)  # the server's 400 text (about 310 characters) must not be cut
        self.assertIn(".slice(0, 800)", self.script)
        self.assertIn("r.status === 403", self.script)
        self.assertIn("open the dashboard at http://127.0.0.1:", self.script)
        # speed_url is only sent when the user typed in the field, so a masked value is never written back
        self.assertIn("if (speedUrlEdited) body.speed_url", self.script)
        self.assertNotIn("speed_url: url", self.script)
        self.assertIn("this page was not opened from this computer", self.script)

    def test_stale_state_does_not_claim_the_collector_stopped_while_it_runs(self):
        text = re.search(r"else if \(noColl\) text = (.*)\n\s*else if \(st\.phase === 'stale'\) text = '([^']*)'", self.script)
        self.assertTrue(text)
        self.assertNotIn("collector", text.group(2))
        self.assertIn("collector is not running", text.group(1))

    def test_server_text_is_escaped_before_it_goes_into_html(self):
        fn = re.search(r"function renderSpeed\(\) \{.*?\n\}\n", self.script, re.S).group(0)
        uses = [m.start() for m in re.finditer(r"t\.reason", fn)]
        self.assertTrue(uses)
        for i in uses:
            self.assertTrue(fn[:i].endswith("escHtml("), fn[max(0, i - 30):i + 20])
        self.assertNotRegex(self.script, r"innerHTML[^\n]*s\.url")

    def test_readme_describes_the_feature(self):
        for needle in ("Internet download speed", "wifi-speed-", "speed.json", "speed_url", "speed_mb", "speed_every", "speed_run", "metered", "single stream",
                       "Install Certificates.command"):
            with self.subTest(needle):
                self.assertIn(needle, self.readme)
        self.assertNotIn("no internet service is involved", self.readme)
        self.assertNotIn("only thing the dashboard page fetches from outside", self.readme)


SUMMARY_NOW = {"id": "router", "every": 5, "everyText": "5 s"}


@unittest.skipUnless(NODE, "Node.js is not installed")
class ProbesSummaryTests(unittest.TestCase):
    def summary(self, d):
        return helpers([["probesSummary", [d]]])[0]

    def data(self, running=True, probes=(), last_sample="2026-10-09 12:00:00"):
        c = {"running": running}
        if last_sample is not None:
            c["lastSample"] = last_sample
        return {"collector": c, "probes": list(probes)}

    def test_not_loaded_yet(self):
        self.assertEqual(self.summary(None), "Loading…")

    def test_collector_not_running_shows_the_last_sample_time(self):
        out = self.summary(self.data(running=False))
        self.assertIn("Collector not running", out)
        self.assertIn("2026-10-09 12:00:00", out)

    def test_collector_not_running_and_no_sample_says_never(self):
        out = self.summary(self.data(running=False, last_sample=None))
        self.assertIn("Collector not running", out)
        self.assertIn("never", out)

    def test_running_summary_is_exact(self):
        probes = [SUMMARY_NOW, {"id": "scan", "every": 900, "everyText": "15 min"}]
        self.assertEqual(self.summary(self.data(probes=probes)),
                         "Collector running · router/internet/Wi-Fi every 5 s · scan every 15 min")

    def test_paused_scan_shows_the_paused_text(self):
        probes = [SUMMARY_NOW, {"id": "scan", "every": 900, "everyText": "15 min (paused)"}]
        self.assertIn("scan every 15 min (paused)", self.summary(self.data(probes=probes)))

    def test_scans_off(self):
        probes = [SUMMARY_NOW, {"id": "scan", "every": None, "everyText": "off"}]
        out = self.summary(self.data(probes=probes))
        self.assertEqual(out, "Collector running · router/internet/Wi-Fi every 5 s · scans off")

    def test_missing_pieces_do_not_throw(self):
        scan = {"id": "scan", "every": 900, "everyText": "15 min"}
        with self.subTest("no router probe"):
            out = self.summary(self.data(probes=[scan]))
            self.assertIn("?", out)
            self.assertIn("scan every 15 min", out)
        with self.subTest("empty probes"):
            out = self.summary(self.data(probes=[]))
            self.assertIn("Collector running", out)
        with self.subTest("no probes or collector keys"):
            self.assertIn("Collector not running", self.summary({}))


@unittest.skipUnless(NODE, "Node.js is not installed")
class ProbesToggleLogicTests(unittest.TestCase):
    """setProbesOpen from the page, run against a minimal fake DOM and localStorage."""

    def test_open_then_close(self):
        init, opened, closed = simulate_probes([{"steps": [[True, True], [False, True]]}])[0]
        self.assertFalse(init["initialOpen"])
        self.assertEqual((opened["open"], opened["panelHidden"], opened["summaryHidden"], opened["expanded"], opened["collapsed"]),
                         (True, False, True, "true", False))
        self.assertEqual(opened["store"], {"probesOpen": "1"})
        self.assertEqual((closed["open"], closed["panelHidden"], closed["summaryHidden"], closed["expanded"], closed["collapsed"]),
                         (False, True, False, "false", True))
        self.assertEqual(closed["store"], {"probesOpen": "0"})

    def test_not_saving_leaves_storage_untouched(self):
        _, opened, closed = simulate_probes([{"store": {"other": "x"}, "steps": [[True, False], [False, False]]}])[0]
        self.assertEqual(opened["store"], {"other": "x"})
        self.assertEqual(closed["store"], {"other": "x"})
        self.assertFalse(opened["panelHidden"])  # but the state still changed

    def test_remembered_state_is_read_at_load(self):
        out = simulate_probes([{"store": {"probesOpen": "1"}}, {"store": {"probesOpen": "0"}}, {}])
        self.assertEqual([o[0]["initialOpen"] for o in out], [True, False, False])

    def test_blocked_storage_does_not_break_the_toggle(self):
        init, opened, closed = simulate_probes([{"throws": True, "steps": [[True, True], [False, True]]}])[0]
        self.assertFalse(init["initialOpen"])
        self.assertEqual((opened["open"], opened["panelHidden"], opened["expanded"]), (True, False, "true"))
        self.assertEqual((closed["open"], closed["panelHidden"], closed["expanded"]), (False, True, "false"))


class ProbesCardMarkupTests(unittest.TestCase):
    """Static checks of the collapsible Probes settings card and the centred header (no Node needed)."""

    @classmethod
    def setUpClass(cls):
        with open(HTML, encoding="utf-8") as f:
            cls.html = f.read()
        with open(README, encoding="utf-8") as f:
            cls.readme = f.read()
        cls.dom = Markup(cls.html)
        cls.script = re.search(r"<script>(.*)</script>", cls.html, re.S).group(1)
        cls.style = re.search(r"<style>(.*?)</style>", cls.html, re.S).group(1)

    def one(self, ident):
        found = self.dom.by_id(ident)
        self.assertEqual(len(found), 1, "id %s must exist exactly once" % ident)
        return found[0]

    def test_title_is_probes_settings_and_the_old_wording_is_gone(self):
        ttl = [e for e in self.dom.els if "ttl" in e["attrs"].get("class", "").split() and ("h2", "probesHead") in [(t, i) for t, i in e["up"]]]
        self.assertEqual([e["text"] for e in ttl], ["Probes settings"])
        for old in ("What this app measures", "probes and how often"):
            with self.subTest(old):
                self.assertNotIn(old, self.html)
                self.assertNotIn(old, self.readme)
        self.assertIn("Probes settings", self.readme)

    def test_card_starts_collapsed_with_a_hidden_panel(self):
        card = self.one("probesCard")
        self.assertIn("collapsed", card["attrs"].get("class", "").split())
        panel = self.one("probesPanel")
        self.assertIn("hidden", panel["attrs"])
        toggle = self.one("probesToggle")
        self.assertEqual(toggle["tag"], "button")
        self.assertEqual(toggle["attrs"].get("aria-expanded"), "false")
        self.assertEqual(toggle["attrs"].get("aria-controls"), "probesPanel")
        self.assertEqual(toggle["attrs"].get("type"), "button")
        # named by the visible title, so a screen reader does not hear "Probes settings" twice
        self.assertEqual(toggle["attrs"].get("aria-labelledby"), "probesTitle")
        self.assertNotIn("aria-label", toggle["attrs"])
        self.assertEqual(self.one("probesTitle")["tag"], "span")
        self.assertIn('id="probesTitle">Probes settings<', self.html)

    def test_summary_and_status_lines_are_not_chart_range_labels(self):
        # updateRangeLabels() writes the chart range into EVERY .rangeline, which would overwrite these lines
        for ident in ("probesSummary", "probestatus"):
            with self.subTest(ident):
                classes = self.one(ident)["attrs"].get("class", "").split()
                self.assertNotIn("rangeline", classes)
                self.assertIn("probeline", classes)
        self.assertRegex(self.html, r"\.rangeline, \.probeline \{")  # same look as the other range lines

    def test_summary_shows_settings_only_not_transient_messages(self):
        self.assertIn("$('#probesSummary').textContent = probesSummary(probeData);", self.html)

    def test_ids_exist_exactly_once_and_are_placed_correctly(self):
        for ident in ("probesCard", "probesHead", "probesToggle", "probesSummary", "probesPanel", "probestatus", "probesbody"):
            with self.subTest(ident):
                self.one(ident)
        for ident in ("probestatus", "probesbody"):
            with self.subTest("inside panel: " + ident):
                self.assertIn(("div", "probesPanel"), self.one(ident)["up"])
        self.assertNotIn(("div", "probesPanel"), self.one("probesSummary")["up"])  # the summary must stay visible while collapsed
        self.assertIn(("div", "probesCard"), self.one("probesSummary")["up"])
        self.assertIn(("h2", "probesHead"), self.one("probesToggle")["up"])

    def test_probes_hint_is_in_the_heading_but_not_inside_the_button(self):
        hints = [e for e in self.dom.els if e["attrs"].get("data-tip-key") == "probes"]
        self.assertEqual(len(hints), 1)
        up = hints[0]["up"]
        self.assertIn(("h2", "probesHead"), up)
        self.assertNotIn("button", [t for t, _ in up])
        self.assertIn("hint", hints[0]["attrs"].get("class", "").split())

    def test_header_is_centred_wrapping_and_has_no_spacer(self):
        rule = re.search(r"^\s*header\s*\{([^}]*)\}", self.style, re.M).group(1)
        self.assertRegex(rule, r"display:\s*flex")
        self.assertRegex(rule, r"justify-content:\s*center")
        self.assertRegex(rule, r"flex-wrap:\s*wrap")
        self.assertNotIn("spacer", self.html)
        self.assertRegex(self.style, r"--ctlh:\s*\d+px")
        self.assertIn("var(--ctlh)", self.style)

    def test_hidden_attribute_beats_display_rules(self):
        sel, body = re.search(r"([^{}]*\.ctl\[hidden\][^{}]*)\{([^}]*)\}", self.style).groups()
        self.assertRegex(body, r"display:\s*none")
        for needle in (".collbody[hidden]", ".probessum[hidden]"):
            with self.subTest(needle):
                self.assertIn(needle, sel)
        stop = self.one("stopctl")  # the Stop control is a .ctl that starts hidden
        self.assertIn("ctl", stop["attrs"].get("class", "").split())
        self.assertIn("hidden", stop["attrs"])

    def test_a_click_toggles_exactly_once(self):
        heads = re.findall(r"\$\('#probesHead'\)\.addEventListener\('click'", self.script)
        self.assertEqual(len(heads), 1)
        self.assertNotRegex(self.script, r"#probesToggle'\)\s*\.addEventListener|getElementById\('probesToggle'\)\s*\.addEventListener")
        self.assertNotIn("onclick", self.html.lower())
        self.assertEqual(len(re.findall(r"setProbesOpen\(!probesOpen", self.script)), 1)
        listener = re.search(r"\$\('#probesHead'\)\.addEventListener.*", self.script).group(0)
        self.assertIn("e.target.closest('.hint')) return", listener)  # the (i) icon opens its tooltip, it does not toggle the card

    def test_rendering_does_not_close_a_menu_the_user_is_using(self):
        fn = re.search(r"function renderProbes\(\) \{.*?\n\}\n", self.script, re.S).group(0)
        guard, rows, summary = (fn.find(s) for s in ("body.contains(document.activeElement)", "body.innerHTML = probeRows()", "$('#probesSummary').textContent ="))
        self.assertTrue(0 <= summary < guard < rows, (summary, guard, rows))


if __name__ == "__main__":
    unittest.main()
