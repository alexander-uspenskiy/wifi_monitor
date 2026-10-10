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
