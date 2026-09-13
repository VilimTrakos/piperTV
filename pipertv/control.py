"""Compose HDMI detection, the control gate, and desktop control into one unit.

This layer is independent of HTTP, the same way the recording workbench is, so
the pieces can be driven from a future PiperTV interface without a web server.

It owns the one rule that ties Codex's detector to the cursor: the remote may
act only while the TV reports this Pi's input, a mode was chosen for that visit,
and no recording is in progress. A supervisor thread keeps that judgement fresh,
because control has to stop when the TV switches away even if nobody is asking.
"""

from __future__ import annotations

import logging
import threading

from .cec import CecMonitor
from .desktop import DesktopControl
from .ir_control import IRController
from .pointer import health as pointer_health
from .pointer import read_screen_size
from .session import DESKTOP_MODES, ControlSession
from .targets import AtspiTargets
from .tv import ButtonLog

LOG = logging.getLogger(__name__)
DEFAULT_SCREEN = (1920, 1080)
POLL_S = 0.4
RECORDING = "Recording a remote button, so the remote is not controlling the desktop."


class RemoteControl:
    """Everything needed to let a learned remote drive this Pi's desktop."""

    def __init__(self, store, screen=None, device="/dev/lirc0", cec_device="/dev/cec0",
                 poll_s=POLL_S, monitor=None, targets=None, controller=None, desktop=None,
                 buttons=None):
        self.store = store
        self.screen = tuple(screen or read_screen_size() or DEFAULT_SCREEN)
        self.session = ControlSession()
        self._source_lock = threading.RLock()
        self.targets = AtspiTargets(self.screen) if targets is None else targets
        self.desktop = (DesktopControl(self.session, self.screen, targets=self.targets,
                                       enabled=self._enabled)
                        if desktop is None else desktop)
        self.buttons = ButtonLog() if buttons is None else buttons
        self._input_context = None
        self.monitor = CecMonitor(device=cec_device) if monitor is None else monitor
        self.controller = (IRController(store, self._press, self._enabled,
                                        device=device)
                           if controller is None else controller)
        self.poll_s = poll_s
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None

    # --- lifecycle -------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self.monitor.start()
            self.controller.resume()
            self._thread = threading.Thread(target=self._run, name="piper-control", daemon=True)
            self._thread.start()

    def _refresh_source(self) -> dict:
        with self._source_lock:
            try:
                detection = self.monitor.snapshot()
                self.session.update(detection)
            except Exception as exc:
                # A failed read must withdraw permission, not leave the last
                # positive judgement in effect until the monitor recovers.
                LOG.warning("Reading HDMI selection: %s", exc)
                detection = {"state": "unknown", "reason": "HDMI detection is unavailable."}
                self.session.update(detection)
            self._sync_input_context()
            return detection

    def _sync_input_context(self) -> dict:
        """Discard queued actions whenever their visit, mode, or hold ends.

        Called under _source_lock so an IR callback cannot append an old
        session's action behind the clear.
        """
        state = self.session.snapshot()
        context = (state["session"]["id"] if state["session"] else None,
                   state["mode"], state["control"])
        if context != self._input_context:
            self.buttons.clear()
            self._input_context = context
        return state

    def _enabled(self) -> bool:
        # Consult current evidence before every IR action, including switches
        # that arrived between supervisor passes.
        self._refresh_source()
        return self.session.enabled()

    def _press(self, button: str) -> None:
        """Route one recognised press to whatever the chosen mode drives.

        Every press is recorded for the interface on the TV, which asks for
        them by number. The cursor moves only under a desktop mode, so the
        Piper interface never drags the mouse around behind itself.
        """
        with self._source_lock:
            if not self._enabled():
                return
            state = self.session.snapshot()
            mode = state["mode"]
            self.buttons.append(button, mode, state["session"]["id"])
        if mode in DESKTOP_MODES:
            self.desktop.press(button)

    def _tick(self) -> None:
        """One supervisor pass: refresh the judgement, act on a lost visit."""
        try:
            self._refresh_source()
            # Keyed off the pointer that actually exists rather than a
            # remembered verdict: a mode can be chosen between two passes, and
            # a visit can end in that same gap.
            state = self.session.snapshot()
            if ((state["control"] != "on" or state["mode"] not in DESKTOP_MODES)
                    and self.desktop.health().get("active")):
                self.desktop.release()
        except Exception as exc:  # noqa: BLE001 - the gate must keep running
            LOG.warning("Control supervisor: %s", exc)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._tick()
            self._stop.wait(self.poll_s)

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is not None and threading.current_thread() is not thread:
            thread.join(timeout=3)
        for part in (self.controller, self.monitor, self.desktop):
            try:
                part.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must finish
                LOG.warning("Closing control: %s", exc)
        self.session.stop()

    # --- the browser's actions ------------------------------------------

    def choose(self, mode, session_id) -> dict:
        with self._source_lock:
            self._refresh_source()
            self.session.choose(mode, session_id)
            result = self._sync_input_context()
        if self.desktop.health().get("active"):
            self.desktop.release()
        # A fresh visit should see the desktop as it is now, not as it was.
        if hasattr(self.targets, "invalidate"):
            self.targets.invalidate()
        return result

    def manual(self, confirmed) -> dict:
        with self._source_lock:
            self._refresh_source()
            self.session.start_manual(confirmed)
            result = self._sync_input_context()
        if self.desktop.health().get("active"):
            self.desktop.release()
        return result

    def stop(self) -> dict:
        with self._source_lock:
            self.session.stop()
            result = self._sync_input_context()
        self.desktop.release()
        return result

    # --- recording interlock --------------------------------------------

    def hold(self, reason: str = RECORDING) -> dict:
        """Stand down so a capture can own the receiver.

        The gate closes first, then the reader releases the LIRC device; a
        recording must never be answered by the desktop moving as well.
        """
        with self._source_lock:
            self.session.hold(reason)
            result = self._sync_input_context()
        self.desktop.release()
        self.controller.pause()
        return result

    def release(self) -> dict:
        with self._source_lock:
            self._refresh_source()
            self.session.release()
            result = self._sync_input_context()
        # Resuming makes the reader ready; its predicate still prevents it
        # opening LIRC until a visit has a mode. Otherwise learning before the
        # first visit leaves it paused permanently.
        self.controller.resume()
        return result

    def reload_recordings(self) -> None:
        self.controller.reload_recordings()

    # --- the interface on the TV -----------------------------------------

    def events(self, after: int = 0) -> dict:
        """Presses the TV interface has not seen yet, with the gate's verdict."""
        with self._source_lock:
            self._refresh_source()
            result = self.buttons.since(after)
            state = self.session.snapshot()
        result["mode"] = state["mode"]
        result["control"] = state["control"]
        result["session_id"] = state["session"]["id"] if state["session"] else None
        return result

    # --- reporting -------------------------------------------------------

    def snapshot(self) -> dict:
        detection = self._refresh_source()
        state = self.session.snapshot()
        state["detection"] = detection
        state["runtime"] = {"pointer": pointer_health(),
                            "receiver": self.controller.health(),
                            "desktop": self.desktop.health(),
                            "targets": self.targets.health() if hasattr(self.targets, "health") else None}
        return state

    def health(self) -> dict:
        targets = self.targets.health() if hasattr(self.targets, "health") else None
        return {"screen": list(self.screen), "pointer": pointer_health(),
                "receiver": self.controller.health(), "desktop": self.desktop.health(),
                "targets": targets, "detection": self.monitor.snapshot()}
