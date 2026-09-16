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
import time

from .cec import CecMonitor
from .desktop import POINTER_DEFAULTS, DesktopControl, validate_pointer
from .interface import Interface
from .ir_control import DIRECTIONS, IRController
from .keyboard import ServiceKeys
from .launcher import SNAP, ServiceLauncher
from .pointer import health as pointer_health
from .pointer import read_screen_size
from .roles import RoleMap
from .session import DESKTOP_MODES, ControlSession
from .targets import AtspiTargets
from .tv import ButtonLog

LOG = logging.getLogger(__name__)
DEFAULT_SCREEN = (1920, 1080)
POLL_S = 0.4
RECORDING = "Recording a remote button, so the remote is not controlling the desktop."
# The way out, which is the one thing that must work when nothing else does.
# Back is not among them any more: a service has its own back, and taking it
# away would make the service unusable to get out of a video. Exit and home
# close the service; exit alone, pressed twice with nothing open, closes the
# interface, because that is the door out of Piper itself.
RETURN_TO_PIPER = ("exit", "home")
LEAVE = "exit"
# Holding a direction while what it moves moves in whole steps -- the ring, a
# menu, the cursor jumping between controls. Slow enough that a press a shade
# too long does not skip past what it was aimed at, and still fast enough to
# run down a long list.
STEP_HOLD_DELAY_S = 0.65
STEP_HOLD_INTERVAL_S = 0.25
# Long enough to be a decision, short enough that a stray press expires.
LEAVE_CONFIRM_S = 6.0


class LeaveRequest:
    """One press of exit asks to leave Piper; a second one within the window acts.

    A single press cannot close the interface: on this remote the TV obeys the
    same code, so exit is pressed for other reasons all the time. Asking first
    also gives the screen somewhere to say what is about to happen.
    """

    def __init__(self, clock=time.monotonic, window_s: float = LEAVE_CONFIRM_S):
        self.clock = clock
        self.window_s = float(window_s)
        self._asked_at = -float("inf")
        self._lock = threading.RLock()

    def press(self) -> bool:
        """Record a press. True when it completes the confirmation."""
        with self._lock:
            now = self.clock()
            if now - self._asked_at <= self.window_s:
                self._asked_at = -float("inf")
                return True
            self._asked_at = now
            return False

    def armed(self) -> bool:
        with self._lock:
            return self.clock() - self._asked_at <= self.window_s

    def remaining(self) -> float:
        with self._lock:
            left = self.window_s - (self.clock() - self._asked_at)
            return round(left, 1) if left > 0 else 0.0

    def disarm(self) -> None:
        with self._lock:
            self._asked_at = -float("inf")


class RemoteControl:
    """Everything needed to let a learned remote drive this Pi's desktop."""

    def __init__(self, store, screen=None, device="/dev/lirc0", cec_device="/dev/cec0",
                 poll_s=POLL_S, monitor=None, targets=None, controller=None, desktop=None,
                 buttons=None, launcher=None, browser=None, interface=None, port=8765,
                 keys=None):
        self.store = store
        self.screen = tuple(screen or read_screen_size() or DEFAULT_SCREEN)
        self.session = ControlSession()
        self._source_lock = threading.RLock()
        self.targets = AtspiTargets(self.screen) if targets is None else targets
        self.desktop = (DesktopControl(self.session, self.screen, targets=self.targets,
                                       enabled=self._enabled)
                        if desktop is None else desktop)
        self.buttons = ButtonLog() if buttons is None else buttons
        self.launcher = ServiceLauncher(browser=browser) if launcher is None else launcher
        self.interface = Interface(port=port) if interface is None else interface
        self.keys = ServiceKeys() if keys is None else keys
        self.leaving = LeaveRequest()
        self._input_context = None
        self.roles = self._load_roles()
        self.pointer = self._load_pointer()
        self.monitor = CecMonitor(device=cec_device) if monitor is None else monitor
        self.controller = (IRController(store, self._press, self._listening,
                                        device=device,
                                        is_direction=self._is_direction,
                                        pace=self._hold_pace)
                           if controller is None else controller)
        self.poll_s = poll_s
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None

    # --- roles -----------------------------------------------------------

    def _load_roles(self) -> RoleMap:
        """Read the saved bindings, falling back to plain keys if they are bad.

        A hand-edited library must not stop the remote working altogether; an
        unusable map means every key simply acts as itself again.
        """
        try:
            return RoleMap(self.store.snapshot().get("roles"))
        except Exception as exc:  # noqa: BLE001 - the remote must still start
            LOG.warning("Ignoring saved role bindings: %s", exc)
            return RoleMap()

    def _load_pointer(self) -> dict:
        """Read the saved cursor preference, falling back to the defaults.

        A hand-edited library must not stop the remote working; an unusable
        preference means the cursor behaves as it does out of the box.
        """
        try:
            settings = validate_pointer(self.store.snapshot().get("pointer") or {})
        except Exception as exc:  # noqa: BLE001 - the remote must still start
            LOG.warning("Ignoring the saved pointer settings: %s", exc)
            settings = dict(POINTER_DEFAULTS)
        try:
            self.desktop.configure(settings)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Applying the pointer settings: %s", exc)
        return settings

    def reload_pointer(self) -> dict:
        with self._source_lock:
            self.pointer = self._load_pointer()
            return dict(self.pointer)

    def _is_direction(self, button_id) -> bool:
        """Whether holding this key should repeat, judged by what it performs."""
        return self.roles.action(button_id) in DIRECTIONS

    def _hold_pace(self, button_id):
        """How fast holding this key should repeat, judged by what it moves.

        Almost everything a press moves here moves in whole steps: the ring
        turns by one service, a menu in Kodi or YouTube moves by one item,
        snapping goes from one control to the next. For those, a rate meant
        for pixels turns a press a shade too long into two steps, and the
        thing being aimed at is skipped entirely.

        One case is different, and it is the exception rather than the rule:
        nudging a cursor, where a fast repeat is exactly what makes it glide.
        """
        if self._cursor_service() == "pointer":
            return None
        if self.launcher.running() is None and self.session.snapshot()["mode"] == "pointer":
            return None
        # How long counts as "held" is a habit, not a constant: some people
        # press a television remote for a moment, some lean on it.
        return (self.pointer.get("hold_delay_s", STEP_HOLD_DELAY_S),
                self.pointer.get("hold_interval_s", STEP_HOLD_INTERVAL_S))

    def reload_roles(self) -> dict:
        with self._source_lock:
            self.roles = self._load_roles()
            return self.roles.bindings()

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

    def _listening(self) -> bool:
        """Whether to read the receiver at all, which is not permission to act.

        The gate decides what a press may do; it must not decide whether a
        press is heard. While the receiver was opened only under an open gate,
        the way out of a full-screen service could never arrive -- the one
        press that has to work when everything else is refused. Reading is not
        acting: a press heard with the gate shut moves no cursor, drives no
        interface, and is not even recorded. Only the way out is honoured.
        """
        return not self._stop.is_set()

    def _press(self, button: str) -> None:
        """Route one recognised press to whatever the chosen mode drives.

        Every press is recorded for the interface on the TV, which asks for
        them by number. The cursor moves only under a desktop mode, so the
        Piper interface never drags the mouse around behind itself.
        """
        # What the remote sent is recorded; what it performs is acted on. They
        # differ once a role has been moved to a key the TV ignores.
        action = self.roles.action(button)
        with self._source_lock:
            allowed = self._enabled()
            state = self.session.snapshot()
            mode = state["mode"] if allowed else None
            if allowed:
                self.buttons.append(button, mode, state["session"]["id"], action=action)
        if action is not None and self._way_out(action):
            return
        if self.launcher.running() is not None:
            # Something is on the screen and it is not Piper, so the remote
            # drives that instead of the ring.
            if allowed and action is not None:
                self._drive_service(action)
            return
        if allowed and action is not None and mode in DESKTOP_MODES:
            self.desktop.press(action)

    def _cursor_service(self):
        """How the open service is driven, when it is driven by the cursor.

        "snap" steps between the controls a page reports; "nudge" moves the
        cursor itself and gathers speed while a direction is held. Which one
        is a preference, because which works better is a property of the page
        rather than of Piper.
        """
        running = self.launcher.running()
        if running is None or self.launcher.policy(running["id"]) != SNAP:
            return None
        return "snapping" if self.pointer.get("drive") != "nudge" else "pointer"

    def _drive_service(self, action: str) -> None:
        """Send one press to the open service in the language it understands.

        A television app is typed at. A site built for a mouse is snapped
        through: its arrow keys do nothing, so the cursor jumps between the
        controls the page reports and OK clicks the one it landed on. Back is
        typed either way, because escape closes an overlay in both.
        """
        running = self.launcher.running()
        if running is None:
            return
        driving = self._cursor_service()
        if driving is not None and (action in DIRECTIONS or action == "ok"):
            self.desktop.press(action, mode=driving)
            return
        self.keys.send(action)

    def _way_out(self, action: str) -> bool:
        """Leave whatever is on the screen. The one thing the gate cannot veto.

        A service owns the screen while it runs, and the interface owns it the
        rest of the time; both are full screen with no keyboard in front of
        them. If this needed the gate, then losing the TV's report -- which
        happens on its own -- would trap whoever is watching, with nothing left
        to press. Ending something on the Pi's own screen is safe to allow
        either way: it moves no cursor and starts nothing.
        """
        if action not in RETURN_TO_PIPER:
            self.leaving.disarm()
            return False
        if self.launcher.running() is not None:
            self.leaving.disarm()
            self.launcher.stop()
            self.keys.release()
            self.desktop.release()
            return True
        if action != LEAVE:
            return False
        # Nothing is open, so this is about Piper itself. Ask, then act.
        if self.leaving.press():
            self.interface.close()
        return True

    def _tick(self) -> None:
        """One supervisor pass: refresh the judgement, act on a lost visit."""
        try:
            self._refresh_source()
            # Keyed off the pointer that actually exists rather than a
            # remembered verdict: a mode can be chosen between two passes, and
            # a visit can end in that same gap.
            state = self.session.snapshot()
            # A service driven by snapping owns the cursor even though the
            # visit chose the interface. Taking the pointer away between two
            # presses looked like snapping was random: each new device believes
            # it is at the centre of the screen, so every press started from
            # there instead of from the control the cursor was actually on.
            snapping_service = self._cursor_service() is not None
            if ((state["control"] != "on"
                 or (state["mode"] not in DESKTOP_MODES and not snapping_service))
                    and self.desktop.health().get("active")):
                self.desktop.release()
            # Someone closed the service from the desktop, or it crashed: the
            # keyboard must not outlive what it was typing into.
            if self.launcher.running() is None and self.keys.health().get("active"):
                self.keys.release()
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
        for part in (self.controller, self.monitor, self.desktop, self.launcher, self.keys):
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

    def launch(self, service, session_id) -> dict:
        """Open a service for the visit the interface was actually looking at.

        The press that chose the icon was only allowed because the TV is
        showing this Pi; the launch it leads to answers to the same gate, so a
        page left open on a PC cannot start something on a TV showing anything
        else.
        """
        with self._source_lock:
            self._refresh_source()
            state = self._sync_input_context()
            if state["hold"]:
                raise RuntimeError(state["hold"])
            if state["control"] != "on":
                raise RuntimeError("The TV is not showing the Pi, so Piper cannot open "
                                   "anything on it.")
            if state["mode"] != "piper":
                raise RuntimeError("Choose the Piper interface for this visit before opening "
                                   "a service.")
            if session_id != state["session"]["id"]:
                raise RuntimeError("That belongs to an earlier visit to the Pi's input. "
                                   "Reload the interface on the TV.")
        return self.launcher.launch(service)

    def stop_service(self) -> dict:
        """Give the screen back to the interface. Always allowed: it is the way out."""
        return self.launcher.stop()

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
        # A saved binding travels in the same file as the signals.
        self.reload_roles()

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
        result["services"] = self.launcher.snapshot()
        result["leaving"] = {"armed": self.leaving.armed(),
                             "seconds": self.leaving.remaining()}
        return result

    # --- reporting -------------------------------------------------------

    def snapshot(self) -> dict:
        detection = self._refresh_source()
        state = self.session.snapshot()
        state["detection"] = detection
        state["roles"] = self.roles.describe()
        state["services"] = self.launcher.snapshot()
        state["interface"] = self.interface.snapshot()
        state["keys"] = self.keys.health()
        state["pointer_settings"] = dict(self.pointer)
        state["runtime"] = {"pointer": pointer_health(),
                            "receiver": self.controller.health(),
                            "desktop": self.desktop.health(),
                            "targets": self.targets.health() if hasattr(self.targets, "health") else None}
        return state

    def health(self) -> dict:
        targets = self.targets.health() if hasattr(self.targets, "health") else None
        return {"screen": list(self.screen), "pointer": pointer_health(),
                "receiver": self.controller.health(), "desktop": self.desktop.health(),
                "targets": targets, "detection": self.monitor.snapshot(),
                "services": self.launcher.snapshot(),
                "interface": self.interface.snapshot(),
                "keys": self.keys.health()}
