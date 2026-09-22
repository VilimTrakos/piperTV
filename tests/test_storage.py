import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from pipertv.storage import RecordingStore, validate_signal


SIGNAL = {
    "durations_us": [9000, 4500, 560, 560, 560],
    "carrier_hz": 38000, "carrier_source": "assumed",
    "trailing_gap_us": 120000, "source": "demo",
    "captured_at": "2026-09-06T12:00:00+00:00",
}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "recordings.json"
        self.store = RecordingStore(self.path)

    def test_a_capture_from_a_gpio_pin_is_hardware(self):
        self.assertEqual(validate_signal(dict(SIGNAL, source="gpio"))["source"], "gpio")

    def test_samples_labels_reload_and_export(self):
        self.store.rename("power", "TV on / off")
        self.store.append("power", SIGNAL)
        second = dict(SIGNAL, durations_us=[8000, 4000, 500])
        self.store.append("power", second)
        restored = RecordingStore(self.path)
        record = restored.snapshot()["recordings"]["power"]
        self.assertEqual(record["label"], "TV on / off")
        self.assertEqual(len(record["samples"]), 2)
        self.assertIn("pulse 9000\nspace 4500\npulse 560", restored.irctl("power", 0))
        self.assertIn("carrier 38000", restored.irctl("power", 1))
        restored.delete_sample("power", 0)
        self.assertEqual(RecordingStore(self.path).snapshot()["recordings"]["power"]["samples"], [second])

    def test_failed_replace_preserves_disk_and_memory(self):
        self.store.append("power", SIGNAL)
        before = self.store.snapshot()
        original_bytes = self.path.read_bytes()
        with patch("pipertv.storage.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.append("power", SIGNAL)
        self.assertEqual(before, self.store.snapshot())
        self.assertEqual(original_bytes, self.path.read_bytes())
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_invalid_existing_file_is_untouched(self):
        self.path.write_text("{broken")
        with self.assertRaisesRegex(ValueError, "left untouched"):
            RecordingStore(self.path)
        self.assertEqual(self.path.read_text(), "{broken")

    def test_invalid_signal_cannot_be_saved(self):
        for durations in ([], [1, 2], [1, True, 1], [1, -2, 1], [1, 0, 1]):
            with self.subTest(durations=durations), self.assertRaises(ValueError):
                validate_signal(dict(SIGNAL, durations_us=durations))
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
