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


if __name__ == "__main__":
    unittest.main()
