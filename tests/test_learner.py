import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from pipertv.learner import Workbench
from pipertv.storage import RecordingStore, validate_signal


SIGNAL = {
    "durations_us": [9000, 4500, 560, 560, 560],
    "carrier_hz": 38000, "carrier_source": "assumed",
    "trailing_gap_us": 120000, "source": "demo",
    "captured_at": "2026-09-06T12:00:00+00:00",
}


class ControlledReceiver:
    def __init__(self):
        self.release = threading.Event()
        self.started = threading.Event()
        self.cancelled = threading.Event()
        self.result = {"id": "remote1", "status": "captured", "signal": SIGNAL}

    def health(self):
        return {"ok": True, "mode": "demo"}

    def start(self, options):
        self.started.set()
        return {"id": "remote1", "status": "listening"}

    def get(self, job_id):
        self.release.wait(2)
        return copy.deepcopy(self.result)

    def cancel(self, job_id):
        self.cancelled.set()
        return {"id": job_id, "status": "cancelled"}


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RecordingStore(Path(self.temp.name) / "recordings.json")
        self.backend = ControlledReceiver()
        self.app = Workbench(self.store, backend=self.backend)
        self.addCleanup(self.backend.release.set)
        self.addCleanup(self.app.close)

    def wait_finished(self, job):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.app.active_id is None:
                return self.app.get(job["id"])
            time.sleep(0.01)
        self.fail("Capture worker did not finish")

    def test_capture_is_saved_once_to_original_button(self):
        job = self.app.start({"button_id": "power"})
        self.assertTrue(self.backend.started.wait(1))
        with self.assertRaises(RuntimeError):
            self.app.start({"button_id": "mute"})
        self.backend.release.set()
        self.assertEqual(self.wait_finished(job)["status"], "captured")
        for _ in range(3):
            self.app.get(job["id"])
        self.assertEqual(self.store.snapshot()["recordings"]["power"]["samples"], [SIGNAL])

    def test_cancel_discards_late_receiver_success(self):
        job = self.app.start({"button_id": "mute"})
        self.assertTrue(self.backend.started.wait(1))
        self.app.cancel(job["id"])
        self.backend.release.set()
        self.assertEqual(self.wait_finished(job)["status"], "cancelled")
        self.assertTrue(self.backend.cancelled.is_set())
        self.assertEqual(self.store.snapshot()["recordings"], {})

    def test_terminal_cancel_allows_immediate_next_capture(self):
        first = self.app.start({"button_id": "mute"})
        self.assertTrue(self.backend.started.wait(1))
        self.assertEqual(self.app.cancel(first["id"])["status"], "cancelling")
        self.backend.release.set()
        self.assertEqual(self.wait_finished(first)["status"], "cancelled")
        second = self.app.start({"button_id": "power"})
        self.assertEqual(self.wait_finished(second)["status"], "captured")
        self.assertNotIn("mute", self.store.snapshot()["recordings"])

    def test_failed_save_reports_error(self):
        with patch.object(self.store, "append", side_effect=OSError("disk full")):
            job = self.app.start({"button_id": "power"})
            self.backend.release.set()
            result = self.wait_finished(job)
        self.assertEqual(result["status"], "error")
        self.assertIn("not saved", result["error"])
        self.assertEqual(self.store.snapshot()["recordings"], {})

    def test_timeout_keeps_previous_sample(self):
        self.store.append("power", SIGNAL)
        self.backend.result = {"id": "remote1", "status": "timeout", "error": "No signal"}
        job = self.app.start({"button_id": "power"})
        self.backend.release.set()
        self.assertEqual(self.wait_finished(job)["status"], "timeout")
        self.assertEqual(len(self.store.snapshot()["recordings"]["power"]["samples"]), 1)

    def test_bad_options_do_not_start_receiver(self):
        for options in ({"button_id": "unknown"}, {"button_id": "power", "timeout_s": float("nan")}, {"button_id": "power", "gap_us": -1}):
            with self.assertRaises(ValueError):
                self.app.start(options)
        self.assertFalse(self.backend.started.is_set())


class RecordingGate:
    """Stands in for the desktop-control gate wrapped around a capture."""

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
        self.backend = ControlledReceiver()
        self.gate = RecordingGate()
        self.app = Workbench(self.store, backend=self.backend, gate=self.gate)
        self.addCleanup(self.backend.release.set)
        self.addCleanup(self.app.close)

    def wait_for(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_recording_stands_the_desktop_down_and_restores_it(self):
        job = self.app.start({"button_id": "power"})
        self.assertTrue(self.backend.started.wait(1))
        self.assertEqual(self.gate.held, 1)
        self.assertEqual(self.gate.released, 0, "control must stay down while recording")
        self.backend.release.set()
        self.assertTrue(self.wait_for(lambda: self.gate.released == 1))
        self.assertEqual(self.app.get(job["id"])["status"], "captured")

    def test_a_cancelled_recording_still_restores_control(self):
        job = self.app.start({"button_id": "mute"})
        self.assertTrue(self.backend.started.wait(1))
        self.app.cancel(job["id"])
        self.backend.release.set()
        self.assertTrue(self.wait_for(lambda: self.gate.released == 1))

    def test_capture_is_not_published_finished_until_control_release_finishes(self):
        entered, finish = threading.Event(), threading.Event()
        observed = []

        def slow_release():
            observed.append(self.app.active_id)
            entered.set()
            finish.wait(2)
            self.gate.released += 1

        self.gate.release = slow_release
        job = self.app.start({"button_id": "power"})
        self.backend.release.set()
        try:
            self.assertTrue(entered.wait(1))
            self.assertEqual(observed, [job["id"]])
            self.assertEqual(self.app.active_id, job["id"],
                             "the next recording must not race the old gate release")
        finally:
            finish.set()
        self.assertTrue(self.wait_for(lambda: self.app.active_id is None))
        self.assertEqual(self.app.get(job["id"])["status"], "captured")

    def test_a_gate_that_cannot_stand_down_prevents_the_capture(self):
        self.gate.fail = True
        with self.assertRaises(RuntimeError):
            self.app.start({"button_id": "power"})
        # The receiver must not be opened while the desktop is still listening.
        self.assertFalse(self.backend.started.is_set())
        self.assertIsNone(self.app.active_id)
        self.assertEqual(self.gate.held, 0)

    def test_a_failure_restoring_control_does_not_hide_the_recording(self):
        def broken():
            raise RuntimeError("the virtual pointer went away")

        self.gate.release = broken
        job = self.app.start({"button_id": "power"})
        self.backend.release.set()
        self.assertTrue(self.wait_for(lambda: self.app.active_id is None))
        self.assertEqual(self.app.get(job["id"])["status"], "captured")
        self.assertEqual(self.store.snapshot()["recordings"]["power"]["samples"], [SIGNAL])


if __name__ == "__main__":
    unittest.main()
