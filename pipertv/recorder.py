"""Record remote buttons into the library, one capture at a time."""

from __future__ import annotations

import copy
import logging
import os
import threading
import uuid

from .buttons import BUTTON_IDS
from .gpio_ir import CHIP, LIRC, describe, is_gpio, open_receiver
from .lirc import (OPTION_LIMITS, CaptureCancelled, CaptureOptions, CaptureTimeout,
                   capture_stream, demo_signal)

LOG = logging.getLogger(__name__)

ACTIVE = {"arming", "listening", "cancelling"}
FINISHED = {"captured", "timeout", "cancelled", "error"}
MAX_JOBS = 100


class Recorder:
    """Captures a press from the IR receiver and appends it to the store.

    `gate` is the desktop control (RemoteControl): it is held for the length
    of a capture, so the press being learned isn't also acted on and the
    receiver is free for us. `capture` replaces the hardware in tests.
    """

    def __init__(self, store, device=LIRC, demo=False, gate=None, capture=None):
        self.store = store
        self.device = device
        self.demo = demo
        self.gate = gate
        self._capture = capture or (self._capture_demo if demo else self._capture_hardware)
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._cancel: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self.active_id: str | None = None

    def use(self, device) -> None:
        """Read from another receiver, starting with the next capture."""
        self.device = device

    def health(self) -> dict:
        if self.demo:
            return {"ok": True, "mode": "demo", "device": None, "receiver": None,
                    "device_exists": True, "device_readable": True}
        path = CHIP if is_gpio(self.device) else self.device if isinstance(self.device, str) else LIRC
        result = {"ok": True, "mode": "hardware", "device": path,
                  "receiver": describe(self.device),
                  "device_exists": os.path.exists(path),
                  "device_readable": os.access(path, os.R_OK)}
        if not result["device_exists"]:
            result["ok"] = False
            result["error"] = (f"{path} was not found. This is not a Raspberry Pi, or its "
                               "GPIO driver is missing." if is_gpio(self.device) else
                               f"IR receiver {path} was not found. Connect the receiver, enable "
                               "the gpio-ir overlay, and reboot the Pi.")
        elif not result["device_readable"]:
            result["ok"] = False
            result["error"] = (f"{path} is not readable. Add your Pi user to the gpio group, "
                               "then log in again." if is_gpio(self.device) else
                               "The IR receiver exists but is not readable. Grant your Pi user "
                               "access to the LIRC device, then restart PiperTV.")
        return result

    def state(self) -> dict:
        with self._lock:
            active = copy.deepcopy(self._jobs[self.active_id]) if self.active_id else None
        return {"mode": "demo" if self.demo else "hardware",
                "buttons": self.store.buttons(),
                "recordings": self.store.snapshot()["recordings"],
                "data_file": str(self.store.path),
                "active_capture": active}

    def start(self, options: dict) -> dict:
        button_id = options.get("button_id")
        if not isinstance(button_id, str) or button_id not in BUTTON_IDS:
            raise ValueError("Select a remote button before recording.")
        parsed = CaptureOptions.parse({key: options[key] for key in OPTION_LIMITS if key in options})
        with self._lock:
            if self.active_id is not None:
                raise RuntimeError("A capture is already in progress. Finish or cancel it first.")
            if self.gate is not None:
                self.gate.hold()  # raises if the remote can't let go of the receiver
            while len(self._jobs) >= MAX_JOBS:
                del self._jobs[next(iter(self._jobs))]
            job = {"id": uuid.uuid4().hex, "button_id": button_id, "status": "arming"}
            self._jobs[job["id"]] = job
            self.active_id = job["id"]
            self._cancel = threading.Event()
            self._thread = threading.Thread(target=self._run, args=(job, parsed, self._cancel),
                                            name="ir-capture", daemon=True)
            self._thread.start()
            return dict(job)

    def get(self, job_id: str) -> dict:
        with self._lock:
            if job_id not in self._jobs:
                raise KeyError("Unknown capture")
            return copy.deepcopy(self._jobs[job_id])

    def cancel(self, job_id: str) -> dict:
        with self._lock:
            if job_id not in self._jobs:
                raise KeyError("Unknown capture")
            job = self._jobs[job_id]
            if job_id == self.active_id and job["status"] in ACTIVE:
                self._cancel.set()
                job["status"] = "cancelling"
            return copy.deepcopy(job)

    def close(self) -> None:
        with self._lock:
            if self.active_id is not None:
                self._cancel.set()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=2)

    def _run(self, job: dict, options: CaptureOptions, cancelled: threading.Event) -> None:
        try:
            signal = self._capture(options, cancelled, lambda: self._listening(job, cancelled))
            outcome = {"status": "captured", "signal": signal}
        except CaptureCancelled:
            outcome = {"status": "cancelled"}
        except CaptureTimeout:
            outcome = {"status": "timeout"}
        except Exception as exc:
            outcome = {"status": "error", "error": str(exc) or type(exc).__name__}
        with self._lock:
            if cancelled.is_set():
                outcome = {"status": "cancelled"}
            elif outcome["status"] == "captured":
                try:
                    self.store.append(job["button_id"], outcome["signal"])
                except Exception as exc:
                    outcome = {"status": "error", "error": f"Capture was not saved: {exc}"}
            # Give the receiver back before the job is marked finished: once
            # active_id is cleared a new capture may start and hold it again.
            if self.gate is not None:
                try:
                    self.gate.release()
                except Exception as exc:
                    LOG.warning("Resuming remote control after a capture: %s", exc)
            job.update(outcome)
            self.active_id = None

    def _listening(self, job: dict, cancelled: threading.Event) -> None:
        with self._lock:
            if cancelled.is_set():
                raise CaptureCancelled()
            job["status"] = "listening"

    def _capture_hardware(self, options, cancelled, listening) -> dict:
        with open_receiver(self.device, options.gap_us) as device:
            listening()
            return capture_stream(device, options, cancelled)

    def _capture_demo(self, options, cancelled, listening) -> dict:
        listening()
        if cancelled.wait(min(1.0, options.timeout_s)):
            raise CaptureCancelled()
        if options.timeout_s < 1.0:
            raise CaptureTimeout()
        return demo_signal(options.gap_us)
