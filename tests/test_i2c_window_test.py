# SPDX-License-Identifier: Apache-2.0
"""Check observation decisions without a receiver or the native extension."""

import errno
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "i2c_window_test", Path(__file__).resolve().parents[1] / "examples/i2c_window_test.py"
)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class Clock:
    def __init__(self):
        self.now = 0.0

    def sleep(self, delay):
        self.now += delay


class Receiver:
    def __init__(self, clock, live=True, fix=True):
        self.clock = clock
        self.live = live
        self.fix = fix

    def close(self):
        pass

    def peek(self):
        return 0

    def __getattr__(self, name):
        values = dict.fromkeys(probe.FIELDS)
        # Coordinates are synthetic values, not a recorded receiver position.
        values.update(
            timestamp=1000 + (int(self.clock.now) if self.live else 0),
            lat=0.0, lon=0.0, mode=3 if self.fix else 1,
            status=1 if self.fix else 0, used_sats=6, total_sats=12,
        )
        return values[name]


class ObservationTests(unittest.TestCase):
    def observe(self, factory, duration=3):
        clock = Clock()
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(probe.time, "monotonic", lambda: clock.now), patch.object(probe.time, "sleep", clock.sleep), patch("builtins.print"):
                summary = probe.observe(lambda: factory(clock), Path(directory), duration, 0.25, "synthetic")
            self.assertTrue((Path(directory) / "samples.jsonl").exists())
            self.assertTrue((Path(directory) / "summary.json").exists())
        return summary

    def test_live_fix_and_update_intervals(self):
        summary = self.observe(Receiver)
        self.assertTrue(summary["completed"])
        self.assertTrue(summary["communication_success"])
        self.assertTrue(summary["fix_success"])
        self.assertEqual(summary["timestamp_updates"], 3)
        self.assertEqual(summary["successful_peeks"], 12)
        self.assertEqual(summary["first_fix_s"], 0)
        self.assertEqual(summary["device_update_interval_s"]["median"], 1)

    def test_communication_without_fix_is_distinct(self):
        summary = self.observe(lambda clock: Receiver(clock, fix=False))
        self.assertTrue(summary["communication_success"])
        self.assertTrue(summary["live_updates_observed"])
        self.assertFalse(summary["fix_success"])
        self.assertIsNone(summary["first_fix_s"])

    def test_cached_snapshot_does_not_count_as_live_updates(self):
        summary = self.observe(lambda clock: Receiver(clock, live=False))
        self.assertEqual(summary["timestamp_updates"], 1)
        self.assertFalse(summary["live_updates_observed"])
        self.assertIsNone(summary["device_update_interval_s"])

    def test_stale_snapshot_cannot_establish_a_late_fix(self):
        class LateFix(Receiver):
            def peek(self):
                self.fix = self.clock.now >= 6
                return 0

        summary = self.observe(lambda clock: LateFix(clock, live=False), duration=8)
        self.assertFalse(summary["fix_success"])
        self.assertGreater(summary["last_timestamp_update_age_s"], 5)

    def test_waiting_is_not_communication_success(self):
        class Waiting:
            def peek(self):
                return -errno.EAGAIN

        summary = self.observe(lambda clock: Waiting())
        self.assertFalse(summary["communication_success"])
        self.assertEqual(summary["waiting_peeks"], 12)
        self.assertEqual(summary["read_errors"], 0)

    def test_stop_failure_is_recorded(self):
        class BadClose(Receiver):
            def close(self):
                raise OSError(errno.ETIMEDOUT, "STOP acknowledgement missing")

        summary = self.observe(BadClose)
        self.assertFalse(summary["close_success"])
        self.assertEqual(summary["errors"][0]["phase"], "close")
        self.assertEqual(summary["errors"][0]["type"], "TimeoutError")

    def test_initialization_exception_is_recorded(self):
        def fail(clock):
            raise OSError(errno.EBUSY, "synthetic busy device")

        summary = self.observe(fail)
        self.assertFalse(summary["completed"])
        self.assertFalse(summary["communication_success"])
        self.assertEqual(summary["errors"][0]["type"], "OSError")

    def test_out_of_range_coordinates_do_not_establish_fix(self):
        self.assertFalse(probe.has_fix({"mode": 3, "status": 1, "lat": 91, "lon": 0}))
        self.assertFalse(probe.has_fix({"mode": 3, "status": 1, "lat": None, "lon": 0}))

    def test_2d_differential_fix_is_valid(self):
        self.assertTrue(probe.has_fix({"mode": 2, "status": 2, "lat": 0, "lon": 0}))

    def test_dead_reckoning_is_not_a_gnss_fix(self):
        self.assertFalse(probe.has_fix({"mode": 3, "status": 3, "lat": 0, "lon": 0}))

    def test_logs_inside_git_checkout_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").write_text("gitdir: elsewhere\n")
            with self.assertRaises(ValueError):
                probe.create_log_dir(root / "logs")


if __name__ == "__main__":
    unittest.main()
