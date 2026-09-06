"""Prevent two app processes or external edits from silently losing recordings."""

import multiprocessing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from pipertv.lirc import demo_signal
from pipertv.storage import RecordingStore


def _write_together(path, ready, start, result):
    store = RecordingStore(path)
    ready.put(True)
    if not start.wait(5):
        result.put("barrier timeout")
        return
    try:
        store.append("power", demo_signal())
        result.put("saved")
    except RuntimeError as exc:
        result.put(str(exc))


class StorageConflictTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "recordings.json"

    def test_second_writer_cannot_overwrite_first_new_file(self):
        first = RecordingStore(self.path)
        second = RecordingStore(self.path)
        previous = second.snapshot()
        first.append("power", demo_signal())
        saved = self.path.read_bytes()

        with self.assertRaisesRegex(RuntimeError, "restart PiperTV"):
            second.append("power", demo_signal())

        self.assertEqual(self.path.read_bytes(), saved)
        self.assertEqual(second.snapshot(), previous)
        # Reopening for reading is legal and resuming after restart retains data.
        reopened = RecordingStore(self.path)
        reopened.append("power", demo_signal())
        self.assertEqual(len(reopened.snapshot()["recordings"]["power"]["samples"]), 2)

    def test_external_edit_blocks_rename_without_overwriting(self):
        store = RecordingStore(self.path)
        store.append("power", demo_signal())
        previous = store.snapshot()
        externally_saved = self.path.read_bytes() + b"\n"
        self.path.write_bytes(externally_saved)

        with self.assertRaisesRegex(RuntimeError, "Nothing was overwritten"):
            store.rename("power", "Changed name")

        self.assertEqual(self.path.read_bytes(), externally_saved)
        self.assertEqual(store.snapshot(), previous)

    def test_external_deletion_does_not_recreate_stale_file(self):
        store = RecordingStore(self.path)
        store.append("power", demo_signal())
        self.path.unlink()

        with self.assertRaisesRegex(RuntimeError, "deleted"):
            store.delete_sample("power", 0)

        self.assertFalse(self.path.exists())
        self.assertEqual(len(store.snapshot()["recordings"]["power"]["samples"]), 1)

    def test_failed_save_keeps_fingerprint_for_retry(self):
        store = RecordingStore(self.path)
        store.append("power", demo_signal())
        with patch("pipertv.storage.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                store.append("power", demo_signal())

        store.append("power", demo_signal())
        self.assertEqual(len(RecordingStore(self.path).snapshot()["recordings"]["power"]["samples"]), 2)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_simultaneous_processes_save_once_and_report_conflict(self):
        # Separate processes exercise the advisory file lock, not the thread lock.
        context = multiprocessing.get_context("spawn")
        ready = context.Queue()
        result = context.Queue()
        start = context.Event()
        processes = [context.Process(target=_write_together, args=(str(self.path), ready, start, result)) for _ in range(2)]
        try:
            for process in processes:
                process.start()
            for _ in processes:
                self.assertTrue(ready.get(timeout=5))
            start.set()
            outcomes = [result.get(timeout=5) for _ in processes]
            for process in processes:
                process.join(timeout=5)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(outcomes.count("saved"), 1)
            self.assertEqual(sum("restart PiperTV" in value for value in outcomes), 1)
            restored = RecordingStore(self.path)
            self.assertEqual(len(restored.snapshot()["recordings"]["power"]["samples"]), 1)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                if process.pid is not None:
                    process.join(timeout=5)
            ready.close()
            result.close()


if __name__ == "__main__":
    unittest.main()
