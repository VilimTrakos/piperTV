import stat
import struct
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pipertv import lirc
from pipertv.lirc import (CaptureCancelled, CaptureError, CaptureOptions,
                         CaptureTimeout, Mode2Capture, capture_stream)
from pipertv.receiver import CaptureManager


def words(*events):
    return b"".join(struct.pack("=I", word) for word in events)


def pulse(duration):
    return lirc.PULSE | duration


def timeout(duration):
    return lirc.TIMEOUT | duration


class FakeDevice:
    def __init__(self, events):
        self.events = iter(events)
        self.now = 0.0

    def read(self, wait):
        delay, data = next(self.events, (wait, None))
        self.now += delay
        return data


class Mode2Tests(unittest.TestCase):
    def test_leading_spaces_and_partial_reads_are_safe(self):
        parser = Mode2Capture(CaptureOptions())
        stream = words(1_000_000, timeout(300_000), pulse(9000), 4500,
                       pulse(560), timeout(120_000))
        for byte in stream:
            parser.feed(bytes([byte]), 1.0)
        signal = parser.result()
        self.assertEqual(signal["durations_us"], [9000, 4500, 560])
        self.assertEqual(signal["trailing_gap_us"], 120_000)
        self.assertEqual(signal["carrier_source"], "assumed")
        self.assertEqual(signal["source"], "lirc")

    def test_preserves_multiple_frames(self):
        parser = Mode2Capture(CaptureOptions())
        parser.feed(words(pulse(9000), 4500, pulse(560), 40_000,
                          pulse(9000), 2250, pulse(560), timeout(120_000)), 0)
        self.assertEqual(parser.result()["durations_us"],
                         [9000, 4500, 560, 40_000, 9000, 2250, 560])

    def test_short_timeout_does_not_end_capture_and_adds_remaining_gap(self):
        parser = Mode2Capture(CaptureOptions())
        parser.feed(words(pulse(560), timeout(20_000)), 0.02)
        self.assertFalse(parser.complete)
        parser.feed(words(30_000, pulse(9000), 2250, pulse(560), timeout(120_000)), .06)
        self.assertEqual(parser.result()["durations_us"], [560, 50_000, 9000, 2250, 560])

    def test_gpio_full_gap_after_timeout_does_not_double_count(self):
        parser = Mode2Capture(CaptureOptions())
        # lirc_dev inserts an extra 30ms SPACE after timeout; gpio-ir then
        # reports the actual 50ms edge-to-edge SPACE before the next PULSE.
        parser.feed(words(pulse(560), timeout(20_000), 30_000, 50_000,
                          pulse(9000), timeout(120_000)), .08)
        self.assertEqual(parser.result()["durations_us"], [560, 50_000, 9000])

    def test_short_timeout_waits_remaining_silence_and_dispatch_allowance(self):
        parser = Mode2Capture(CaptureOptions())
        parser.feed(words(pulse(9000), 4500, pulse(560), timeout(20_000)), 1.0)
        self.assertFalse(parser.finish_idle(1.05))
        self.assertFalse(parser.finish_idle(1.12))
        self.assertTrue(parser.finish_idle(1.131))
        self.assertEqual(parser.result()["durations_us"], [9000, 4500, 560])

    def test_single_noise_pulse_is_not_saved_as_remote_code(self):
        parser = Mode2Capture(CaptureOptions())
        parser.feed(words(pulse(100), timeout(120_000)), 0)
        with self.assertRaisesRegex(CaptureError, "one IR pulse"):
            parser.result()

    def test_no_timeout_marker_cannot_silently_finish_incomplete_signal(self):
        parser = Mode2Capture(CaptureOptions())
        parser.feed(words(pulse(560)), 0)
        self.assertFalse(parser.finish_idle(50))
        with self.assertRaises(CaptureError):
            parser.result()

    def test_ordinary_consecutive_durations_are_merged(self):
        parser = Mode2Capture(CaptureOptions())
        parser.feed(words(pulse(300), pulse(260), 200, 360, pulse(560),
                          lirc.FREQUENCY | 36_000, timeout(120_000)), 0)
        self.assertEqual(parser.result()["durations_us"], [560, 560, 560])
        self.assertEqual(parser.result()["carrier_hz"], 38_000)

    def test_overflow_rejects_capture(self):
        parser = Mode2Capture(CaptureOptions())
        with self.assertRaisesRegex(CaptureError, "overflow"):
            parser.feed(words(pulse(560), lirc.OVERFLOW | lirc.VALUE_MASK), 0)

    def test_raw_duration_limit_is_not_saved(self):
        parser = Mode2Capture(CaptureOptions(max_duration_s=.2))
        with self.assertRaisesRegex(CaptureError, "max_duration_s"):
            parser.feed(words(pulse(150_000), 80_000, pulse(560)), 0)
        self.assertFalse(parser.complete)

    def test_edge_limit_rejects_capture(self):
        with patch.object(lirc, "MAX_EDGES", 3):
            parser = Mode2Capture(CaptureOptions())
            with self.assertRaisesRegex(CaptureError, "Too many"):
                parser.feed(words(pulse(560), 560, pulse(560), 560, pulse(560)), 0)

    def test_stream_timeout_without_input(self):
        device = FakeDevice([])
        with self.assertRaises(CaptureTimeout):
            capture_stream(device, CaptureOptions(timeout_s=.5), threading.Event(),
                           clock=lambda: device.now)

    def test_stream_continues_after_short_timeout_until_repeat(self):
        device = FakeDevice([
            (.01, words(pulse(560))),
            (.02, words(timeout(20_000))),
            (.03, words(30_000, pulse(9000), 2250, pulse(560))),
            (.12, words(timeout(120_000))),
        ])
        result = capture_stream(device, CaptureOptions(), threading.Event(), clock=lambda: device.now)
        self.assertEqual(result["durations_us"], [560, 50_000, 9000, 2250, 560])

    def test_stream_max_duration_never_returns_partial_signal(self):
        device = FakeDevice([(.01, words(pulse(560)))])
        with self.assertRaisesRegex(CaptureError, "not saved"):
            capture_stream(device, CaptureOptions(max_duration_s=.3), threading.Event(),
                           clock=lambda: device.now)

    def test_stream_cancelled(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(CaptureCancelled):
            capture_stream(FakeDevice([]), CaptureOptions(), event)

    def test_stream_eof_rejects_partial_word(self):
        device = FakeDevice([(.01, words(pulse(560))[:2]), (.01, b"")])
        with self.assertRaises(CaptureError):
            capture_stream(device, CaptureOptions(), threading.Event(), clock=lambda: device.now)

    def test_invalid_options_are_rejected(self):
        for value in ({"timeout_s": True}, {"gap_us": -1}, {"gap_us": 20_000.5},
                      {"max_duration_s": float("nan")}, {"timeout_s": float("inf")},
                      {"unexpected": 1}, {"max_duration_s": .1}, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                CaptureOptions.parse(value)

    def test_device_configures_mode_and_restores_previous_timeout(self):
        current_timeout = [125_000]
        requests = []

        def ioctl(device, request, value=0):
            requests.append((request, value))
            if request == lirc.LIRC_GET_FEATURES:
                return lirc.LIRC_CAN_REC_MODE2 | lirc.LIRC_CAN_SET_REC_TIMEOUT
            if request == lirc.LIRC_GET_MIN_TIMEOUT:
                return 1
            if request == lirc.LIRC_GET_MAX_TIMEOUT:
                return 1_000_000
            if request == lirc.LIRC_GET_REC_TIMEOUT:
                return current_timeout[0]
            if request == lirc.LIRC_SET_REC_TIMEOUT:
                current_timeout[0] = value
            return 0

        with (patch.object(lirc.os, "open", return_value=42),
              patch.object(lirc.os, "fstat", return_value=SimpleNamespace(st_mode=stat.S_IFCHR)),
              patch.object(lirc.os, "read", side_effect=BlockingIOError),
              patch.object(lirc.os, "close") as close,
              patch.object(lirc.LircDevice, "_ioctl", new=ioctl)):
            with lirc.LircDevice("/dev/lirc0", 120_000):
                self.assertEqual(current_timeout[0], 120_000)
            self.assertEqual(current_timeout[0], 125_000)
            close.assert_called_once_with(42)
        self.assertIn((lirc.LIRC_SET_REC_MODE, lirc.LIRC_MODE_MODE2), requests)
        self.assertIn((lirc.LIRC_SET_REC_TIMEOUT_REPORTS, 1), requests)


class ManagerTests(unittest.TestCase):
    def test_demo_lifecycle_and_defensive_copies(self):
        manager = CaptureManager(demo=True)
        self.addCleanup(manager.close)
        job = manager.start({})
        self.assertEqual(job["status"], "arming")
        with self.assertRaises(RuntimeError):
            manager.start({})
        manager._thread.join(2)
        result = manager.get(job["id"])
        self.assertEqual(result["status"], "captured")
        self.assertEqual(result["signal"]["source"], "demo")
        result["signal"]["durations_us"].clear()
        self.assertTrue(manager.get(job["id"])["signal"]["durations_us"])
        self.assertIsNone(manager.health()["active_capture_id"])

    def test_cancel_is_fast_and_can_start_again(self):
        manager = CaptureManager(demo=True)
        self.addCleanup(manager.close)
        job = manager.start({})
        start = time.monotonic()
        self.assertEqual(manager.cancel(job["id"])["status"], "cancelled")
        self.assertLess(time.monotonic() - start, .5)
        self.assertEqual(manager.get(job["id"])["status"], "cancelled")
        self.assertIsNone(manager.health()["active_capture_id"])
        manager.start({})

    def test_device_failure_is_error_and_never_announces_listening(self):
        manager = CaptureManager(device="/missing/ir/device")
        self.addCleanup(manager.close)
        with patch.object(manager, "_listening", wraps=manager._listening) as listening:
            job = manager.start({})
            manager._thread.join(1)
            listening.assert_not_called()
        self.assertEqual(manager.get(job["id"])["status"], "error")
        self.assertIn("/missing/ir/device", manager.get(job["id"])["error"])

    def test_completed_jobs_are_bounded(self):
        manager = CaptureManager(demo=True)
        self.addCleanup(manager.close)
        manager._jobs = {str(i): {"id": str(i), "status": "timeout"} for i in range(100)}
        job = manager.start({})
        self.assertEqual(len(manager._jobs), 100)
        self.assertNotIn("0", manager._jobs)
        manager.cancel(job["id"])



if __name__ == "__main__":
    unittest.main()
