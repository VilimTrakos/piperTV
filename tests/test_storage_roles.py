"""Saved role bindings travel with the signals, and never corrupt a library."""

import json
from pathlib import Path
import tempfile
import unittest

from pipertv.storage import RecordingStore

SIGNAL = {
    "durations_us": [9000, 4500, 560, 560, 560],
    "carrier_hz": 38000, "carrier_source": "assumed",
    "trailing_gap_us": 120000, "source": "demo",
    "captured_at": "2026-09-06T12:00:00+00:00",
}


class RoleStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "recordings.json"
        self.store = RecordingStore(self.path)

    def test_a_fresh_library_binds_nothing(self):
        self.assertEqual(self.store.roles(), {})

    def test_a_binding_survives_a_restart(self):
        self.store.set_role("up", "play")
        self.assertEqual(RecordingStore(self.path).roles(), {"up": "play"})

    def test_binding_returns_the_whole_map(self):
        self.store.set_role("up", "play")
        self.assertEqual(self.store.set_role("ok", "pause"),
                         {"up": "play", "ok": "pause"})

    def test_releasing_a_role_removes_only_that_binding(self):
        self.store.set_role("up", "play")
        self.store.set_role("ok", "pause")
        self.assertEqual(self.store.set_role("up", None), {"ok": "pause"})
        self.assertEqual(RecordingStore(self.path).roles(), {"ok": "pause"})

    def test_releasing_an_unbound_role_is_harmless(self):
        self.assertEqual(self.store.set_role("up", None), {})

    def test_an_unknown_role_is_refused(self):
        with self.assertRaisesRegex(ValueError, "Unknown role"):
            self.store.set_role("scroll", "play")

    def test_an_unknown_button_is_refused(self):
        with self.assertRaises(KeyError):
            self.store.set_role("up", "nonexistent")

    def test_a_colliding_binding_is_refused_and_changes_nothing(self):
        self.store.set_role("up", "play")
        saved = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "only one role"):
            self.store.set_role("down", "play")
        self.assertEqual(self.path.read_bytes(), saved)
        self.assertEqual(self.store.roles(), {"up": "play"})

    def test_bindings_and_recordings_share_one_file(self):
        self.store.append("power", SIGNAL)
        self.store.set_role("up", "play")
        restored = RecordingStore(self.path)
        self.assertEqual(restored.roles(), {"up": "play"})
        self.assertEqual(len(restored.snapshot()["recordings"]["power"]["samples"]), 1)

    def test_the_accessor_cannot_be_used_to_edit_the_map(self):
        self.store.set_role("up", "play")
        self.store.roles()["up"] = "tampered"
        self.assertEqual(self.store.roles(), {"up": "play"})


class BackwardCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "recordings.json"

    def _write(self, document):
        self.path.write_text(json.dumps(document), encoding="utf-8")

    def test_a_library_recorded_before_roles_existed_still_loads(self):
        self._write({"schema_version": 1, "recordings":
                     {"power": {"label": "Power", "samples": [SIGNAL]}}})
        store = RecordingStore(self.path)
        self.assertEqual(store.roles(), {})
        # And it can be given bindings afterwards.
        store.set_role("up", "play")
        self.assertEqual(RecordingStore(self.path).roles(), {"up": "play"})

    def test_a_corrupt_binding_refuses_to_load_and_leaves_the_file_alone(self):
        self._write({"schema_version": 1, "recordings": {},
                     "roles": {"up": "down"}})
        original = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "left untouched"):
            RecordingStore(self.path)
        self.assertEqual(self.path.read_bytes(), original)

    def test_a_binding_to_a_button_that_does_not_exist_refuses_to_load(self):
        self._write({"schema_version": 1, "recordings": {},
                     "roles": {"up": "nonexistent"}})
        with self.assertRaises(ValueError):
            RecordingStore(self.path)


if __name__ == "__main__":
    unittest.main()
