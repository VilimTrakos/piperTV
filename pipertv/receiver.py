"""Local Raspberry Pi capture manager shared by the Flask application."""

from __future__ import annotations

import copy
import os
import threading
import uuid
from dataclasses import asdict

from .lirc import (CaptureCancelled, CaptureOptions, CaptureTimeout, LircDevice,
                   capture_stream, demo_signal, utc_now)

TERMINAL = {"captured", "timeout", "cancelled", "error"}


class CaptureManager:
    """Own one active acquisition and retain at most 100 recent jobs in memory."""

    def __init__(self, device: str = "/dev/lirc0", demo: bool = False):
        self.device = device
        self.demo = demo
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._active: str | None = None
        self._cancel: threading.Event | None = None
        self._thread: threading.Thread | None = None

    def health(self) -> dict:
        with self._lock:
            return {"ok": True, "mode": "demo" if self.demo else "hardware",
                    "device": None if self.demo else self.device,
                    "device_exists": self.demo or os.path.exists(self.device),
                    "device_readable": self.demo or os.access(self.device, os.R_OK),
                    "active_capture_id": self._active}

    def start(self, options: dict) -> dict:
        parsed = CaptureOptions.parse(options)
        with self._lock:
            if self._active is not None:
                raise RuntimeError("A capture is already active. Finish or cancel it first.")
            while len(self._jobs) >= 100:
                del self._jobs[next(iter(self._jobs))]
            job_id = uuid.uuid4().hex
            job = {"id": job_id, "status": "arming", "created_at": utc_now(),
                   "options": asdict(parsed)}
            self._jobs[job_id] = job
            self._active = job_id
            cancelled = threading.Event()
            self._cancel = cancelled
            self._thread = threading.Thread(target=self._run, args=(job_id, parsed, cancelled),
                                            name="ir-capture", daemon=True)
            self._thread.start()
            return copy.deepcopy(job)

    def _listening(self, job_id: str, cancelled: threading.Event) -> None:
        with self._lock:
            if cancelled.is_set():
                raise CaptureCancelled()
            self._jobs[job_id]["status"] = "listening"

    def _run(self, job_id: str, options: CaptureOptions, cancelled: threading.Event) -> None:
        result: dict = {}
        try:
            if self.demo:
                self._listening(job_id, cancelled)
                if cancelled.wait(min(1.0, options.timeout_s)):
                    raise CaptureCancelled()
                if options.timeout_s < 1.0:
                    raise CaptureTimeout()
                result = {"status": "captured", "signal": demo_signal(options.gap_us)}
            else:
                with LircDevice(self.device, options.gap_us) as device:
                    self._listening(job_id, cancelled)
                    result = {"status": "captured",
                              "signal": capture_stream(device, options, cancelled)}
        except CaptureCancelled:
            result = {"status": "cancelled"}
        except CaptureTimeout:
            result = {"status": "timeout"}
        except Exception as exc:
            result = {"status": "error", "error": str(exc) or type(exc).__name__}
        finally:
            with self._lock:
                if cancelled.is_set():
                    result = {"status": "cancelled"}
                self._jobs[job_id].update(result, finished_at=utc_now())
                self._active = None
                self._cancel = None

    def get(self, job_id: str) -> dict:
        with self._lock:
            return copy.deepcopy(self._jobs[job_id])

    def cancel(self, job_id: str) -> dict:
        thread = None
        with self._lock:
            self._jobs[job_id]  # Raise KeyError before changing cancellation state.
            if self._active == job_id and self._cancel is not None:
                self._cancel.set()
                thread = self._thread
        # Publish the terminal status only after _run closes the device and
        # releases the capture slot. A UI can immediately re-record then.
        if thread is not None:
            thread.join(timeout=0.25)
        return self.get(job_id)

    def close(self) -> None:
        with self._lock:
            if self._cancel is not None:
                self._cancel.set()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=2)

