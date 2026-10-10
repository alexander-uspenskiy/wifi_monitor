"""Collector info file shared with the dashboard."""
import os
import unittest

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


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.path = logstore.CONTROL_FILE
        if os.path.exists(self.path):
            os.remove(self.path)
        self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))

    def test_defaults(self):
        self.assertEqual(logstore.read_control(), {"scan_paused": False, "interval": None, "scan_every": None})

    def test_valid_values_round_trip(self):
        logstore.write_control(interval=10, scan_every=0, scan_paused=True)
        self.assertEqual(logstore.read_control(), {"scan_paused": True, "interval": 10, "scan_every": 0})

    def test_none_returns_to_the_command_line_value(self):
        logstore.write_control(interval=10)
        logstore.write_control(interval=None)
        self.assertIsNone(logstore.read_control()["interval"])

    def test_invalid_values_are_rejected_on_write_and_ignored_on_read(self):
        logstore.write_control(interval=7, scan_every=123, scan_paused="yes", nonsense=1)
        self.assertEqual(logstore.read_control(), {"scan_paused": False, "interval": None, "scan_every": None})
        with open(self.path, "w") as f:
            f.write('{"interval": 600, "scan_every": -1, "scan_paused": 1}')  # a hand-edited file cannot slow sampling beyond what the page handles
        self.assertEqual(logstore.read_control(), {"scan_paused": False, "interval": None, "scan_every": None})
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


if __name__ == "__main__":
    unittest.main()
