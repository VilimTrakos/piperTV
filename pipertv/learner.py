"""Coordinate local IR capture and saved button mappings for the Flask UI.

This layer is independent of HTTP so a future remote-controlled app UI can reuse
the receiver and recordings without running another service.
"""

import copy
import threading
import uuid

from .buttons import BUTTON_IDS
from .lirc import CaptureOptions
from .receiver import CaptureManager

ACTIVE = {"arming", "listening", "cancelling"}
TERMINAL = {"captured", "timeout", "cancelled", "error"}


class Workbench:
    def __init__(self, store, device="/dev/lirc0", demo=False, backend=None):
        self.store = store
        self.backend = backend if backend is not None else CaptureManager(device=device, demo=demo)
        self.demo = demo
        self.lock = threading.RLock()
        self.jobs = {}
        self.active_id = None
        self._thread = None

    def state(self):
        with self.lock:
            return {
                "mode": "demo" if self.demo else "hardware",
                "buttons": self.store.buttons(),
                "recordings": self.store.snapshot()["recordings"],
                "data_file": str(self.store.path),
                "active_capture": self._public(self.jobs[self.active_id]) if self.active_id else None,
            }

    def health(self):
        return self.backend.health()

    @staticmethod
    def _public(job):
        return copy.deepcopy({key: value for key, value in job.items() if not key.startswith("_")})

    def start(self, options):
        button_id = options.get("button_id")
        if not isinstance(button_id, str) or button_id not in BUTTON_IDS:
            raise ValueError("Select a remote button before recording.")
        capture_options = {key: options[key] for key in ("timeout_s", "gap_us", "max_duration_s") if key in options}
        CaptureOptions.parse(capture_options)
        with self.lock:
            if self.active_id:
                raise RuntimeError("A capture is already in progress. Finish or cancel it first.")
            while len(self.jobs) >= 100:
                self.jobs.pop(next(iter(self.jobs)))
            job_id = uuid.uuid4().hex
            job = {"id": job_id, "button_id": button_id, "status": "arming", "_cancel": threading.Event()}
            self.jobs[job_id] = job
            self.active_id = job_id
            self._thread = threading.Thread(target=self._capture, args=(job, capture_options), daemon=True)
            self._thread.start()
            return self._public(job)

    def _capture(self, job, options):
        capture_id = None
        outcome = {"status": "cancelled"}
        try:
            capture = self.backend.start(options)
            capture_id = capture["id"]
            while not job["_cancel"].is_set():
                status = capture["status"]
                if status in TERMINAL:
                    outcome = {"status": status}
                    if capture.get("error"):
                        outcome["error"] = str(capture["error"])
                    if status == "captured":
                        outcome["signal"] = capture["signal"]
                    break
                with self.lock:
                    if not job["_cancel"].is_set():
                        job["status"] = status
                if job["_cancel"].wait(0.05):
                    break
                capture = self.backend.get(capture_id)
        except Exception as exc:
            outcome = {"status": "error", "error": f"Capture was not saved: {exc}"}
        finally:
            if capture_id and (job["_cancel"].is_set() or outcome["status"] == "error"):
                self.backend.cancel(capture_id)
                # Local device cleanup must finish before the next recording can start.
                while self.backend.get(capture_id)["status"] not in TERMINAL:
                    threading.Event().wait(0.025)
            with self.lock:
                if job["_cancel"].is_set():
                    outcome = {"status": "cancelled"}
                elif outcome["status"] == "captured":
                    try:
                        # Commit and publish together, so Cancel cannot race a saved sample.
                        self.store.append(job["button_id"], outcome["signal"])
                    except Exception as exc:
                        outcome = {"status": "error", "error": f"Capture was not saved: {exc}"}
                job.update(outcome)
                self.active_id = None

    def get(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError("Unknown capture")
            return self._public(self.jobs[job_id])

    def cancel(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError("Unknown capture")
            job = self.jobs[job_id]
            if job["status"] in ACTIVE:
                job["_cancel"].set()
                job["status"] = "cancelling"
            return self._public(job)

    def close(self):
        with self.lock:
            if self.active_id:
                self.cancel(self.active_id)
            thread = self._thread
        if thread is not None:
            thread.join(timeout=2)
        if hasattr(self.backend, "close"):
            self.backend.close()
