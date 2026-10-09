"""Dashboard server: parsing log lines (old and new formats) and the daily summary."""
import unittest

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


if __name__ == "__main__":
    unittest.main()
