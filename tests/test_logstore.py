"""Collector info file, runtime controls, and the speed test's log kind and progress file."""
import datetime as dt
import gzip
import os
import shutil
import tempfile
import unittest
from unittest import mock

import context  # noqa: F401
import logstore


class InfoFileTests(unittest.TestCase):
    def tearDown(self):
        if os.path.exists(logstore.INFO_FILE):
            os.remove(logstore.INFO_FILE)

    def test_round_trip(self):
        info = {"pid": 1, "interval": 5.0, "scan_every": 900, "host": "1.1.1.1", "gateway": None}
        logstore.write_info(info)
        self.assertEqual(logstore.read_info(), info)

    def test_missing_or_damaged_file_reads_as_empty(self):
        self.assertEqual(logstore.read_info(), {})
        with open(logstore.INFO_FILE, "w") as f:
            f.write("{not json")
        self.assertEqual(logstore.read_info(), {})
        with open(logstore.INFO_FILE, "w") as f:
            f.write("[1, 2]")
        self.assertEqual(logstore.read_info(), {})

    def test_write_replaces_atomically(self):
        logstore.write_info({"a": 1})
        logstore.write_info({"b": 2})
        self.assertEqual(logstore.read_info(), {"b": 2})
        self.assertFalse(os.path.exists(logstore.INFO_FILE + ".tmp"))


DEFAULT_CONTROL = {"scan_paused": False, "interval": None, "scan_every": None, "speed_url": None, "speed_mb": 25, "speed_every": 0, "speed_run": None}


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.path = logstore.CONTROL_FILE
        if os.path.exists(self.path):
            os.remove(self.path)
        self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))

    def test_defaults(self):
        self.assertEqual(logstore.read_control(), DEFAULT_CONTROL)

    def test_valid_values_round_trip(self):
        logstore.write_control(interval=10, scan_every=0, scan_paused=True)
        self.assertEqual(logstore.read_control(), {**DEFAULT_CONTROL, "scan_paused": True, "interval": 10, "scan_every": 0})

    def test_none_returns_to_the_command_line_value(self):
        logstore.write_control(interval=10)
        logstore.write_control(interval=None)
        self.assertIsNone(logstore.read_control()["interval"])

    def test_invalid_values_are_rejected_on_write_and_ignored_on_read(self):
        logstore.write_control(interval=7, scan_every=123, scan_paused="yes", nonsense=1)
        self.assertEqual(logstore.read_control(), DEFAULT_CONTROL)
        with open(self.path, "w") as f:
            f.write('{"interval": 600, "scan_every": -1, "scan_paused": 1}')  # a hand-edited file cannot slow sampling beyond what the page handles
        self.assertEqual(logstore.read_control(), DEFAULT_CONTROL)
        with open(self.path, "w") as f:
            f.write("[1]")
        self.assertEqual(logstore.read_control()["interval"], None)

    def test_booleans_are_not_numbers(self):
        self.assertFalse(logstore.valid_control("interval", True))
        self.assertFalse(logstore.valid_control("scan_every", False))

    def test_choices(self):
        self.assertEqual(logstore.INTERVAL_CHOICES, (2, 5, 10, 15))
        self.assertIn(0, logstore.SCAN_CHOICES)
        self.assertTrue(all(logstore.valid_control("interval", v) for v in logstore.INTERVAL_CHOICES))


class SpeedControlTests(unittest.TestCase):
    def setUp(self):
        self.path = logstore.CONTROL_FILE
        if os.path.exists(self.path):
            os.remove(self.path)
        self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))

    def test_defaults_are_a_one_time_25_mb_test_on_the_default_server(self):
        ctl = logstore.read_control()
        self.assertEqual((ctl["speed_url"], ctl["speed_mb"], ctl["speed_every"], ctl["speed_run"]), (None, 25, 0, None))
        self.assertEqual(len(logstore.CONTROL_DEFAULTS), 7)
        self.assertIn("{bytes}", logstore.SPEED_DEFAULT_URL)

    def test_valid_values_round_trip(self):
        logstore.write_control(speed_url="https://example.com/file.bin", speed_mb=100, speed_every=3600, speed_run=1760000000000)
        ctl = logstore.read_control()
        self.assertEqual((ctl["speed_url"], ctl["speed_mb"], ctl["speed_every"], ctl["speed_run"]),
                         ("https://example.com/file.bin", 100, 3600, 1760000000000))
        logstore.write_control(speed_url=None)
        self.assertIsNone(logstore.read_control()["speed_url"])

    def test_url_rules(self):
        ok = ["http://example.com/a", "https://example.com:8443/a?b=1", "https://192.168.1.5/f", "https://[::1]:9/f", "https://x.test/" + "a" * 400]
        bad = ["", "example.com/a", "ftp://example.com/a", "file:///etc/passwd", "javascript:alert(1)", "http://", "https:///path", "https://user:pw@example.com/a",
               "https://exa mple.com/a", "https://example.com/a b", "https://example.com/\nx", "https://example.com/\x00", "https://[::1/f",
               "https://x.test/" + "a" * 500, 5, True, ["https://example.com"]]
        for url in ok:
            with self.subTest(url):
                self.assertTrue(logstore.valid_control("speed_url", url))
        for url in bad:
            with self.subTest(url):
                self.assertFalse(logstore.valid_control("speed_url", url))
        self.assertTrue(logstore.valid_control("speed_url", None))

    def test_url_length_boundary(self):
        base = "https://x.test/"
        self.assertTrue(logstore.valid_speed_url(base + "a" * (500 - len(base))))  # exactly 500 characters
        self.assertFalse(logstore.valid_speed_url(base + "a" * (501 - len(base))))
        self.assertEqual(logstore.SPEED_URL_MAX, 500)

    def test_url_ports_ascii_and_characters_that_never_work(self):
        for url in ("http://example.com:8080/f", "https://example.com:443/{bytes}", "http://example.com/f?a=1&b=2#x"):
            with self.subTest(url):
                self.assertTrue(logstore.valid_speed_url(url))
        for url in ("http://example.com:abc/f", "http://example.com:99999/f", "http://example.com:-1/f", "http://example.com:/f" + "\x7f",
                    "http://exämple.com/f", "http://example.com/f\u00e9", "http://example.com/\"x\"", "http://example.com/<x>", "http://example.com/a\\b",
                    "http://example.com/a^b", "http://example.com/a`b"):
            with self.subTest(url):
                self.assertFalse(logstore.valid_speed_url(url))

    def test_size_choices(self):
        for v in (10, 25, 100):
            self.assertTrue(logstore.valid_control("speed_mb", v))
        for v in (9, 101, 0, -1, 25.0, "25", None, True):
            with self.subTest(v):
                self.assertFalse(logstore.valid_control("speed_mb", v))

    def test_schedule_choices(self):
        self.assertEqual(logstore.SPEED_EVERY_CHOICES, (0, 600, 1800, 3600))
        for v in logstore.SPEED_EVERY_CHOICES:
            self.assertTrue(logstore.valid_control("speed_every", v))
        for v in (None, 1, 300, 7200, 600.0, "600", True, False):
            with self.subTest(v):
                self.assertFalse(logstore.valid_control("speed_every", v))  # None is not "default": one time is 0

    def test_run_stamp(self):
        for v in (None, 0, 1760000000000):
            self.assertTrue(logstore.valid_control("speed_run", v))
        for v in (-1, 1.5, "1", True, False):
            with self.subTest(v):
                self.assertFalse(logstore.valid_control("speed_run", v))

    def test_invalid_values_are_rejected_on_write_and_ignored_on_read(self):
        logstore.write_control(speed_url="ftp://x", speed_mb=5, speed_every=None, speed_run=True)
        self.assertEqual(logstore.read_control(), DEFAULT_CONTROL)
        with open(self.path, "w") as f:
            f.write('{"speed_url": "file:///x", "speed_mb": 500, "speed_every": 5, "speed_run": "now", "interval": 5}')
        self.assertEqual(logstore.read_control(), {**DEFAULT_CONTROL, "interval": 5})  # a hand-edited file cannot make it download without limit

    def test_an_old_control_file_without_speed_keys_still_reads(self):
        with open(self.path, "w") as f:
            f.write('{"scan_paused": true, "interval": 10, "scan_every": 300}')
        self.assertEqual(logstore.read_control(), {**DEFAULT_CONTROL, "scan_paused": True, "interval": 10, "scan_every": 300})


class ControlReadTests(unittest.TestCase):
    """A control file that could not be read is told apart from one that is simply not there, so the collector can keep what it had."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wifimonitor-control-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = os.path.join(self.dir, "control.json")
        patcher = mock.patch.object(logstore, "CONTROL_FILE", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_missing_file_is_fine(self):
        ctl = logstore.read_control()
        self.assertTrue(ctl.ok)
        self.assertEqual(ctl, DEFAULT_CONTROL)

    def test_a_good_file_is_ok_and_a_plain_dict(self):
        logstore.write_control(speed_mb=40)
        ctl = logstore.read_control()
        self.assertTrue(ctl.ok)
        self.assertEqual(ctl["speed_mb"], 40)
        self.assertIsInstance(ctl, dict)

    def test_unreadable_damaged_or_odd_files_report_failure_with_defaults(self):
        for text in ("{not json", "[1, 2]", ""):
            with open(self.path, "w") as f:
                f.write(text)
            ctl = logstore.read_control()
            self.assertFalse(ctl.ok, text)
            self.assertEqual(ctl, DEFAULT_CONTROL)
        logstore.write_control(speed_mb=40)
        with mock.patch("builtins.open", side_effect=PermissionError("held by another process")):
            ctl = logstore.read_control()
        self.assertFalse(ctl.ok)
        self.assertEqual(ctl, DEFAULT_CONTROL)

    def test_a_file_with_only_invalid_values_is_still_a_successful_read(self):
        with open(self.path, "w") as f:
            f.write('{"speed_mb": 500}')
        self.assertTrue(logstore.read_control().ok)


class SpeedLogTests(unittest.TestCase):
    """The speed kind rotates like the other logs: today plain, older compressed, past the retention period deleted."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wifimonitor-speed-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        patcher = mock.patch.object(logstore, "LOG_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_kind_is_known_and_named_like_the_others(self):
        self.assertIn("speed", logstore.KINDS)
        self.assertEqual(os.path.basename(logstore.path_for("speed", dt.date(2026, 10, 9))), "wifi-speed-2026-10-09.log")

    def test_append_and_read_back(self):
        when = dt.datetime(2026, 10, 9, 12, 0, 0)
        logstore.append("speed", '2026-10-09 12:00:00 {"v":1}', when)
        logstore.append("speed", '2026-10-09 12:30:00 {"v":2}', when)
        self.assertEqual(logstore.read_day("speed", "2026-10-09"), ['2026-10-09 12:00:00 {"v":1}', '2026-10-09 12:30:00 {"v":2}'])
        self.assertEqual(logstore.list_files("monitor"), [])  # separate from the monitor files

    def test_rotation_compresses_old_days_and_deletes_expired_ones(self):
        today = dt.date(2026, 10, 9)
        for age in (0, 1, logstore.KEEP_DAYS - 1, logstore.KEEP_DAYS):
            day = today - dt.timedelta(days=age)
            logstore.append("speed", "%s 12:00:00 {}" % day, dt.datetime.combine(day, dt.time(12)))
        logstore.maintain(today)
        files = sorted(os.listdir(self.dir))
        names = ["wifi-speed-%s.log" % (today - dt.timedelta(days=a)) for a in (0, 1, logstore.KEEP_DAYS - 1)]
        self.assertEqual(files, sorted([names[0], names[1] + ".gz", names[2] + ".gz"]))
        with gzip.open(os.path.join(self.dir, names[1] + ".gz"), "rt") as f:
            self.assertIn(str(today - dt.timedelta(days=1)), f.read())
        self.assertEqual(len(logstore.read_day("speed", str(today - dt.timedelta(days=1)))), 1)  # readers handle .gz


class SpeedProgressTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wifimonitor-speed-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        patcher = mock.patch.object(logstore, "LOG_DIR", self.dir)  # the progress path is worked out at call time
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_round_trip_in_the_log_dir(self):
        data = {"running": True, "phase": "downloading", "bytes": 5, "updated": 1.5}
        self.assertTrue(logstore.write_speed_progress(data))
        self.assertEqual(logstore.read_speed_progress(), data)
        self.assertEqual(os.listdir(self.dir), ["speed.json"])  # no temp file left behind

    def test_missing_damaged_or_odd_file_reads_as_empty(self):
        self.assertEqual(logstore.read_speed_progress(), {})
        for text in ("{not json", "[1, 2]", '"x"'):
            with open(os.path.join(self.dir, "speed.json"), "w") as f:
                f.write(text)
            self.assertEqual(logstore.read_speed_progress(), {})

    def test_write_replaces_the_whole_file(self):
        logstore.write_speed_progress({"a": 1})
        logstore.write_speed_progress({"b": 2})
        self.assertEqual(logstore.read_speed_progress(), {"b": 2})

    def test_a_failed_write_is_swallowed_and_the_final_one_retries(self):
        with mock.patch.object(logstore.os, "replace", side_effect=OSError("busy")):
            self.assertFalse(logstore.write_speed_progress({"a": 1}))
        calls = []

        def flaky(src, dst):
            calls.append(src)
            if len(calls) < 3:
                raise PermissionError("held open by a reader")
            os.rename(src, dst)

        with mock.patch.object(logstore.os, "replace", side_effect=flaky), mock.patch.object(logstore.time, "sleep"):
            self.assertTrue(logstore.write_speed_progress({"done": True}, tries=5))
        self.assertEqual((len(calls), logstore.read_speed_progress()), (3, {"done": True}))


if __name__ == "__main__":
    unittest.main()
