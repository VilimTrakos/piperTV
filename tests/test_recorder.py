import copy
import logging
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from pipertv import lirc
from pipertv.lirc import CaptureTimeout
from pipertv.recorder import Recorder
from pipertv.storage import RecordingStore

logging.getLogger("pipertv.recorder").addHandler(logging.NullHandler())

SIGNAL = {
    "durations_us": [9000, 4500, 560, 560, 560],
    "carrier_hz": 38000, "carrier_source": "assumed",
    "trailing_gap_us": 120000, "source": "demo",
    "captured_at": "2026-09-06T12:00:00+00:00",
}


class ControlledCapture:
    """Stands in for the receiver: the press arrives when the test says so."""

    def __init__(self):
        self.release = threading.Event()
        self.started = threading.Event()
        self.cancelled = None
        self.result = SIGNAL

    def __call__(self, options, cancelled, listening):
        self.cancelled = cancelled
        self.started.set()
        listening()
        self.release.wait(2)
        if isinstance(self.result, BaseException):
            raise self.result
        return copy.deepcopy(self.result)


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RecordingStore(Path(self.temp.name) / "recordings.json")
        self.capture = ControlledCapture()
        self.recorder = Recorder(self.store, capture=self.capture)
        self.addCleanup(self.capture.release.set)
        self.addCleanup(self.recorder.close)

    def finished(self, job):
        self.assertTrue(wait_for(lambda: self.recorder.active_id is None),
                        "the capture did not finish")
        return self.recorder.get(job["id"])

    def test_capture_is_saved_once_to_original_button(self):
        job = self.recorder.start({"button_id": "power"})
        self.assertTrue(self.capture.started.wait(1))
        with self.assertRaises(RuntimeError):
            self.recorder.start({"button_id": "mute"})
        self.capture.release.set()
        self.assertEqual(self.finished(job)["status"], "captured")
        for _ in range(3):
            self.recorder.get(job["id"])
        self.assertEqual(self.store.snapshot()["recordings"]["power"]["samples"], [SIGNAL])

    def test_cancel_discards_late_receiver_success(self):
        job = self.recorder.start({"button_id": "mute"})
        self.assertTrue(self.capture.started.wait(1))
        self.recorder.cancel(job["id"])
        self.capture.release.set()
        self.assertEqual(self.finished(job)["status"], "cancelled")
        self.assertTrue(self.capture.cancelled.is_set())
        self.assertEqual(self.store.snapshot()["recordings"], {})

    def test_terminal_cancel_allows_immediate_next_capture(self):
        first = self.recorder.start({"button_id": "mute"})
        self.assertTrue(self.capture.started.wait(1))
        self.assertEqual(self.recorder.cancel(first["id"])["status"], "cancelling")
        self.capture.release.set()
        self.assertEqual(self.finished(first)["status"], "cancelled")
        second = self.recorder.start({"button_id": "power"})
        self.assertEqual(self.finished(second)["status"], "captured")
        self.assertNotIn("mute", self.store.snapshot()["recordings"])

    def test_failed_save_reports_error(self):
        with patch.object(self.store, "append", side_effect=OSError("disk full")):
            job = self.recorder.start({"button_id": "power"})
            self.capture.release.set()
            result = self.finished(job)
        self.assertEqual(result["status"], "error")
        self.assertIn("not saved", result["error"])
        self.assertEqual(self.store.snapshot()["recordings"], {})

    def test_timeout_keeps_previous_sample(self):
        self.store.append("power", SIGNAL)
        self.capture.result = CaptureTimeout()
        job = self.recorder.start({"button_id": "power"})
        self.capture.release.set()
        self.assertEqual(self.finished(job)["status"], "timeout")
        self.assertEqual(len(self.store.snapshot()["recordings"]["power"]["samples"]), 1)

    def test_bad_options_do_not_start_receiver(self):
        for options in ({"button_id": "unknown"},
                        {"button_id": "power", "timeout_s": float("nan")},
                        {"button_id": "power", "gap_us": -1}):
            with self.assertRaises(ValueError):
                self.recorder.start(options)
        self.assertFalse(self.capture.started.is_set())

    def test_completed_jobs_are_bounded(self):
        self.recorder._jobs = {str(i): {"id": str(i), "status": "timeout"} for i in range(100)}
        job = self.recorder.start({"button_id": "power"})
        self.assertEqual(len(self.recorder._jobs), 100)
        self.assertNotIn("0", self.recorder._jobs)
        self.recorder.cancel(job["id"])


class RecorderDeviceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RecordingStore(Path(self.temp.name) / "recordings.json")

    def test_demo_lifecycle_and_defensive_copies(self):
        recorder = Recorder(self.store, demo=True)
        self.addCleanup(recorder.close)
        job = recorder.start({"button_id": "power"})
        self.assertEqual(job["status"], "arming")
        with self.assertRaises(RuntimeError):
            recorder.start({"button_id": "power"})
        recorder._thread.join(2)
        result = recorder.get(job["id"])
        self.assertEqual(result["status"], "captured")
        self.assertEqual(result["signal"]["source"], "demo")
        result["signal"]["durations_us"].clear()
        self.assertTrue(recorder.get(job["id"])["signal"]["durations_us"])

    def test_cancel_is_fast_and_can_start_again(self):
        recorder = Recorder(self.store, demo=True)
        self.addCleanup(recorder.close)
        job = recorder.start({"button_id": "power"})
        start = time.monotonic()
        recorder.cancel(job["id"])
        self.assertTrue(wait_for(lambda: recorder.active_id is None, timeout=.5))
        self.assertLess(time.monotonic() - start, .5)
        self.assertEqual(recorder.get(job["id"])["status"], "cancelled")
        recorder.start({"button_id": "power"})

    def test_device_failure_is_error_and_never_announces_listening(self):
        recorder = Recorder(self.store, device="/missing/ir/device")
        self.addCleanup(recorder.close)
        with patch.object(recorder, "_listening", wraps=recorder._listening) as listening:
            job = recorder.start({"button_id": "power"})
            recorder._thread.join(1)
            listening.assert_not_called()
        self.assertEqual(recorder.get(job["id"])["status"], "error")
        self.assertIn("/missing/ir/device", recorder.get(job["id"])["error"])

    def test_the_next_capture_listens_on_the_chosen_receiver(self):
        recorder = Recorder(self.store, device="/dev/lirc0")
        self.addCleanup(recorder.close)
        recorder.use({"kind": "gpio", "pin": 18})
        health = recorder.health()
        self.assertEqual(health["receiver"], "GPIO18 (pin 12)")
        self.assertEqual(health["device"], "/dev/gpiochip0")
        opened = []

        def refuse(source, _gap):
            opened.append(source)
            raise lirc.CaptureError("no receiver in a test")

        with patch("pipertv.recorder.open_receiver", side_effect=refuse):
            job = recorder.start({"button_id": "power"})
            recorder._thread.join(1)
        self.assertEqual(opened, [{"kind": "gpio", "pin": 18}])
        self.assertEqual(recorder.get(job["id"])["status"], "error")

    def test_a_missing_receiver_explains_the_setup(self):
        health = Recorder(self.store, device="/missing/ir/device").health()
        self.assertFalse(health["ok"])
        self.assertFalse(health["device_exists"])
        self.assertIn("gpio-ir", health["error"])


class RecordingGate:
    """Stands in for the desktop control that is held during a capture."""

    def __init__(self):
        self.held = self.released = 0
        self.fail = False

    def hold(self):
        if self.fail:
            raise RuntimeError("The receiver is still closing; recording must wait.")
        self.held += 1

    def release(self):
        self.released += 1


class RecordingGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RecordingStore(Path(self.temp.name) / "recordings.json")
        self.capture = ControlledCapture()
        self.gate = RecordingGate()
        self.recorder = Recorder(self.store, capture=self.capture, gate=self.gate)
        self.addCleanup(self.capture.release.set)
        self.addCleanup(self.recorder.close)

    def test_recording_stands_the_desktop_down_and_restores_it(self):
        job = self.recorder.start({"button_id": "power"})
        self.assertTrue(self.capture.started.wait(1))
        self.assertEqual(self.gate.held, 1)
        self.assertEqual(self.gate.released, 0, "control must stay down while recording")
        self.capture.release.set()
        self.assertTrue(wait_for(lambda: self.gate.released == 1))
        self.assertEqual(self.recorder.get(job["id"])["status"], "captured")

    def test_a_cancelled_recording_still_restores_control(self):
        job = self.recorder.start({"button_id": "mute"})
        self.assertTrue(self.capture.started.wait(1))
        self.recorder.cancel(job["id"])
        self.capture.release.set()
        self.assertTrue(wait_for(lambda: self.gate.released == 1))

    def test_capture_is_not_published_finished_until_control_release_finishes(self):
        entered, finish = threading.Event(), threading.Event()
        observed = []

        def slow_release():
            observed.append(self.recorder.active_id)
            entered.set()
            finish.wait(2)
            self.gate.released += 1

        self.gate.release = slow_release
        job = self.recorder.start({"button_id": "power"})
        self.capture.release.set()
        try:
            self.assertTrue(entered.wait(1))
            self.assertEqual(observed, [job["id"]])
            self.assertEqual(self.recorder.active_id, job["id"],
                             "the next recording must not race the old gate release")
        finally:
            finish.set()
        self.assertTrue(wait_for(lambda: self.recorder.active_id is None))
        self.assertEqual(self.recorder.get(job["id"])["status"], "captured")

    def test_a_gate_that_cannot_stand_down_prevents_the_capture(self):
        self.gate.fail = True
        with self.assertRaises(RuntimeError):
            self.recorder.start({"button_id": "power"})
        # The receiver must not be opened while the remote is still reading it.
        self.assertFalse(self.capture.started.is_set())
        self.assertIsNone(self.recorder.active_id)
        self.assertEqual(self.gate.held, 0)

    def test_a_failure_restoring_control_does_not_hide_the_recording(self):
        def broken():
            raise RuntimeError("the virtual pointer went away")

        self.gate.release = broken
        job = self.recorder.start({"button_id": "power"})
        self.capture.release.set()
        self.assertTrue(wait_for(lambda: self.recorder.active_id is None))
        self.assertEqual(self.recorder.get(job["id"])["status"], "captured")
        self.assertEqual(self.store.snapshot()["recordings"]["power"]["samples"], [SIGNAL])


if __name__ == "__main__":
    unittest.main()
