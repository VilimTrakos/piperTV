"""The remote control as a whole: detection, the gate, and what a press does.

A press acts only while the gate is open (see session.py): the TV shows the
Pi, a mode was chosen for this visit, and nothing is being recorded. A
supervisor thread re-checks detection a few times a second, because control
must stop when the TV switches away even if nobody presses anything.

Where a press goes:
  * exit/home close what is open, even with the gate shut (see _handle_exit);
  * with a service open, it goes to the service (keys, or the cursor on a web page);
  * in a desktop mode, it moves the cursor;
  * in "piper" mode it is only logged, and the TV page polls the log.
"""

from __future__ import annotations

import logging
import threading
import time

from .backdrop import Backdrop
from .cec import CecMonitor
from .desktop import DesktopControl, validate_pointer
from .focus import TEXT_ROLES, FocusWatcher
from .interface import Interface
from .ir_control import IRController
from .keyboard import PAGE_BACK, OnScreenKeyboard, ServiceKeys
from .launcher import SNAP, ServiceLauncher
from .pointer import health as pointer_health
from .pointer import read_screen_size
from .roles import DIRECTIONS, RoleMap
from .session import DESKTOP_MODES, ControlSession
from .targets import AtspiTargets
from .tv import ButtonLog
from .window import validate_window

LOG = logging.getLogger(__name__)
DEFAULT_SCREEN = (1920, 1080)
POLL_S = 0.4
RECORDING = "Recording a remote button, so the remote is not controlling the desktop."

# Keys that close an open service. With nothing open, exit pressed twice
# closes Piper. Back isn't one of them: it belongs to the service (it's how
# you leave a video).
CLOSE_KEYS = ("exit", "home")
EXIT_CONFIRM_S = 6.0
# On a web page back (= previous page) takes two presses within this time;
# the first press often just closes the keyboard.
BACK_CONFIRM_S = 3.0
# Pages focus their own search box as they load. Only focus that follows an
# OK within this time counts as someone wanting the keyboard.
KEYBOARD_AFTER_CLICK_S = 5.0
# How long after OK to ask the page what it focused.
AFTER_CLICK_S = 0.45
# A service takes a few seconds to show its window on a Pi 3B+. After this the
# interface is closed to leave the memory to the service.
HIDE_INTERFACE_AFTER_S = 6.0


def run_later(delay, action):
    """Run action once, delay seconds from now, on a daemon thread."""
    timer = threading.Timer(delay, action)
    timer.daemon = True
    timer.start()
    return timer


class DoublePress:
    """press() returns True for the second press within window_s of the first."""

    def __init__(self, window_s: float, clock=time.monotonic):
        self.window_s = float(window_s)
        self.clock = clock
        self._first_at = -float("inf")
        self._lock = threading.Lock()

    def press(self) -> bool:
        with self._lock:
            now = self.clock()
            if now - self._first_at <= self.window_s:
                self._first_at = -float("inf")
                return True
            self._first_at = now
            return False

    def armed(self) -> bool:
        with self._lock:
            return self.clock() - self._first_at <= self.window_s

    def remaining(self) -> float:
        with self._lock:
            left = self.window_s - (self.clock() - self._first_at)
            return round(left, 1) if left > 0 else 0.0

    def reset(self) -> None:
        with self._lock:
            self._first_at = -float("inf")


class RemoteControl:
    """Lets the learned remote drive this Pi. Every part can be replaced in tests."""

    def __init__(self, store, screen=None, device="/dev/lirc0", cec_device="/dev/cec0",
                 poll_s=POLL_S, monitor=None, targets=None, controller=None, desktop=None,
                 buttons=None, launcher=None, browser=None, interface=None, port=8765,
                 keys=None, watcher=None, onscreen=None, later=None, backdrop=None):
        self.store = store
        self.screen = tuple(screen or read_screen_size() or DEFAULT_SCREEN)
        self.poll_s = poll_s
        self.session = ControlSession()
        self.monitor = monitor or CecMonitor(device=cec_device)
        self.targets = targets or AtspiTargets(self.screen)
        self.desktop = desktop or DesktopControl(self.session, self.screen, targets=self.targets,
                                                 enabled=self._enabled)
        self.buttons = buttons or ButtonLog()
        self.launcher = launcher or ServiceLauncher(browser=browser, screen=self.screen)
        self.interface = interface or Interface(port=port, screen=self.screen)
        self.backdrop = backdrop or Backdrop()
        self.keys = keys or ServiceKeys()
        self.onscreen = onscreen or OnScreenKeyboard()
        self.watcher = watcher or FocusWatcher(on_text_field=self._text_field_focused)
        self.later = later or run_later
        self.exit_twice = DoublePress(EXIT_CONFIRM_S)
        self.back_twice = DoublePress(BACK_CONFIRM_S)

        # _gate_lock: the session and the button log. _screen_lock: what is shown.
        self._gate_lock = threading.RLock()
        self._screen_lock = threading.RLock()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._context = None
        self._clicked_at = -float("inf")
        # The service the interface was closed for, while it is closed.
        # We only bring back an interface we closed ourselves.
        self._hidden_for: str | None = None

        self.roles = self._saved("roles", RoleMap)
        self.pointer = self._saved("pointer", validate_pointer)
        self.desktop.configure(self.pointer)
        self.window = self._saved("window", validate_window)
        self.launcher.configure(self.window)
        self.controller = controller or IRController(
            store, self._press, self._listening, device=device,
            is_direction=self._is_direction, pace=self._hold_pace)

    # --- settings ------------------------------------------------------------

    def _saved(self, key, parse):
        """A setting from the library. A broken one is ignored so the remote still works."""
        try:
            return parse(self.store.snapshot().get(key) or {})
        except Exception as exc:
            LOG.warning("Ignoring the saved %s settings: %s", key, exc)
            return parse({})

    def reload_roles(self) -> dict:
        with self._gate_lock:
            self.roles = self._saved("roles", RoleMap)
            return self.roles.bindings()

    def reload_pointer(self) -> dict:
        with self._gate_lock:
            self.pointer = self._saved("pointer", validate_pointer)
            self.desktop.configure(self.pointer)
            return dict(self.pointer)

    def reload_window(self) -> dict:
        """Apply new window settings; the interface is reopened in the new shape."""
        with self._gate_lock:
            self.window = self._saved("window", validate_window)
            self.launcher.configure(self.window)
        with self._screen_lock:
            if self._hidden_for is None:  # otherwise it comes back later anyway
                self.show_interface()
        return dict(self.window)

    def reload_recordings(self) -> None:
        self.controller.reload_recordings()
        self.reload_roles()  # the bindings are in the same file

    def use_receiver(self, device) -> dict:
        self.controller.use(device)
        return self.controller.health()

    def _is_direction(self, button_id) -> bool:
        """Whether holding this button repeats, judged by the role it performs."""
        return self.roles.action(button_id) in DIRECTIONS

    def _hold_pace(self, button_id):
        """(delay_s, interval_s) for a held direction, or None for the fast default.

        Almost every press moves one whole step (the dial, a menu item, the
        next control), where a fast repeat skips past the target. Only a
        nudged cursor wants the fast one.
        """
        if self._cursor_mode() == "pointer":
            return None
        if self.launcher.running() is None and self.session.snapshot()["mode"] == "pointer":
            return None
        return self.pointer["hold_delay_s"], self.pointer["hold_interval_s"]

    # --- the screen ------------------------------------------------------------

    def show_interface(self, focus=None) -> dict:
        """Open the interface in the configured shape.

        If a window doesn't appear, fall back to full screen: nobody at the TV
        can undo a setting that left the screen black.
        """
        with self._screen_lock:
            self._hidden_for = None
            self._update_backdrop()
            result = self.interface.open(self.window, focus)
            if not result.get("showing") and self.window.get("windowed"):
                LOG.warning("A window did not appear; filling the screen instead")
                self.window = validate_window({})
                self._update_backdrop()
                result = self.interface.open(self.window, focus)
            return result

    def _update_backdrop(self) -> None:
        # Black behind a full-screen interface, the desktop around a window.
        # Called before the interface opens, so the interface lands on top.
        if self.window.get("windowed"):
            self.backdrop.close()
        else:
            self.backdrop.show()

    def _hide_interface(self, opened) -> None:
        """Close the interface once the service opened from it covers the screen."""
        try:
            with self._screen_lock:
                running = self.launcher.running()
                if (running is None or running.get("id") != opened.get("id")
                        or running.get("started_at") != opened.get("started_at")):
                    return  # already closed, or replaced by another service
                if self._hidden_for is not None:
                    self._hidden_for = running["id"]
                    return
                if not self.interface.showing():
                    return  # someone else closed it; not ours to bring back
                if not self.window.get("windowed") and not self.backdrop.showing():
                    return  # without the backdrop the desktop panel would show
                if not self.interface.close().get("showing"):
                    self._hidden_for = running["id"]
                    LOG.info("%s covers the interface; closed it to free memory",
                             running.get("name", running["id"]))
        except Exception as exc:
            LOG.warning("Closing the interface behind a service: %s", exc)

    def _restore_interface(self) -> None:
        """Reopen the interface we closed, once no service covers it."""
        with self._screen_lock:
            if self._hidden_for is None or self.launcher.running() is not None:
                return
            LOG.info("Nothing covers the interface any more; opening it again")
            self.show_interface(focus=self._hidden_for)

    def _interface_state(self) -> dict:
        return dict(self.interface.snapshot(), closed_for=self._hidden_for,
                    backdrop=self.backdrop.snapshot())

    # --- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self.monitor.start()
            self.watcher.start()
            self.controller.resume()
            self._thread = threading.Thread(target=self._run, name="piper-control", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._tick()
            self._stop.wait(self.poll_s)

    def _tick(self) -> None:
        """One supervisor pass."""
        try:
            self._refresh()
            state = self.session.snapshot()
            # A web page is driven with the cursor even in "piper" mode. Keep the
            # pointer between presses there, or every snap restarts from the centre.
            cursor_wanted = state["control"] == "on" and (
                state["mode"] in DESKTOP_MODES or self._cursor_mode() is not None)
            if not cursor_wanted and self.desktop.health().get("active"):
                self.desktop.release()
            if self.launcher.running() is None:
                # The service is gone (closed from the desktop, crashed, or by
                # us): drop what belonged to it and bring the interface back.
                if self.keys.health().get("active"):
                    self.keys.release()
                if self.onscreen.health().get("ready"):
                    self.onscreen.close()
                if self._hidden_for is not None:
                    self._restore_interface()
        except Exception as exc:
            LOG.warning("Control supervisor: %s", exc)

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            thread, self._thread = self._thread, None
        if thread is not None and threading.current_thread() is not thread:
            thread.join(timeout=3)
        for part in (self.controller, self.monitor, self.desktop, self.launcher,
                     self.keys, self.watcher, self.onscreen):
            try:
                part.close()
            except Exception as exc:
                LOG.warning("Closing control: %s", exc)
        try:
            # The service closed with us; bring back the interface it covered,
            # or a restart leaves the TV on the desktop.
            self._restore_interface()
        except Exception as exc:
            LOG.warning("Putting the interface back: %s", exc)
        self.session.stop()

    # --- the gate --------------------------------------------------------------

    def _refresh(self) -> dict:
        """Feed the latest HDMI detection to the session. Returns the detection."""
        with self._gate_lock:
            try:
                detection = self.monitor.snapshot()
                self.session.update(detection)
            except Exception as exc:
                # A failed read must close the gate, not keep the last good answer.
                LOG.warning("Reading HDMI selection: %s", exc)
                detection = {"state": "unknown", "reason": "HDMI detection is unavailable."}
                self.session.update(detection)
            self._sync_context()
            return detection

    def _sync_context(self) -> dict:
        """Drop logged presses whenever the visit, the mode or the gate changes.

        Called with _gate_lock held, so a press from the old context can't be
        appended after the clear.
        """
        state = self.session.snapshot()
        context = (state["session"]["id"] if state["session"] else None,
                   state["mode"], state["control"])
        if context != self._context:
            self.buttons.clear()
            self._context = context
        return state

    def _enabled(self) -> bool:
        # Check fresh evidence before every action: the TV may have switched
        # since the last supervisor pass.
        self._refresh()
        return self.session.enabled()

    def _listening(self) -> bool:
        """Whether the IR reader reads at all. It keeps reading with the gate
        shut, so that exit can still close a full-screen service."""
        return not self._stop.is_set()

    # --- presses ---------------------------------------------------------------

    def _press(self, button: str) -> None:
        """Handle one recognised button (called on the IR reader thread)."""
        # `button` is the key that was pressed, `action` the role it performs.
        action = self.roles.action(button)
        with self._gate_lock:
            allowed = self._enabled()
            state = self.session.snapshot()
            if allowed:
                self.buttons.append(button, state["mode"], state["session"]["id"], action=action)
        if action is None:
            return
        if self._handle_exit(action):
            return
        if not allowed:
            return
        if self.launcher.running() is not None:
            self._drive_service(action)
        elif state["mode"] in DESKTOP_MODES:
            self.desktop.press(action)

    def _handle_exit(self, action: str) -> bool:
        """Close the keyboard, the service or Piper. True if the press was used.

        This works even with the gate shut. The TV sometimes stops reporting
        over CEC, and that must not leave the user stuck in a full-screen
        service. It only ever closes things, so it's safe.
        """
        if self.onscreen.showing() and action in ("back", "exit"):
            # The keyboard goes first, and this press is spent on it.
            self.exit_twice.reset()
            self.back_twice.reset()
            self.close_keyboard()
            return True
        if action not in CLOSE_KEYS:
            self.exit_twice.reset()
            return False
        if self.launcher.running() is not None:
            self.exit_twice.reset()
            self._close_service()
            return True
        if action != "exit":
            return False
        if self.exit_twice.press():
            self.leave()
        return True

    def _close_service(self) -> None:
        self.launcher.stop()
        self.onscreen.close()
        self.keys.release()
        self.desktop.release()

    def _cursor_mode(self):
        """"snapping" or "pointer" if the open service is driven with the cursor, else None."""
        running = self.launcher.running()
        if running is None or self.launcher.policy(running["id"]) != SNAP:
            return None
        return "pointer" if self.pointer.get("drive") == "nudge" else "snapping"

    def _drive_service(self, action: str) -> None:
        """Send a press to the open service.

        TV apps get key presses. Web pages ignore arrow keys, so there the
        cursor moves and OK clicks. Back on a page means the previous page,
        on the second press (the first one usually closes the keyboard).
        """
        if self.launcher.running() is None:
            return
        if action != "back":
            self.back_twice.reset()
        mode = self._cursor_mode()
        if mode is None:
            self.keys.send(action)
        elif action in DIRECTIONS or action == "ok":
            if action == "ok":
                self._clicked_at = time.monotonic()
            position = self.desktop.press(action, mode=mode)
            if action == "ok":
                self._check_click(position)
        elif action == "back":
            if self.back_twice.press():
                self.keys.send(PAGE_BACK)
        else:
            self.keys.send(action)

    # --- the on-screen keyboard ------------------------------------------------

    def _text_field_focused(self, field: dict) -> None:
        """FocusWatcher callback (on the bus thread): a page focused a text field."""
        if self._cursor_mode() is None or self.onscreen.showing():
            return  # TV apps (YouTube, Kodi) have their own keyboard
        if time.monotonic() - self._clicked_at > KEYBOARD_AFTER_CLICK_S:
            return  # the page focused it by itself
        LOG.info("A page is using %s; showing the keyboard", field.get("role"))
        self.open_keyboard()

    def _check_click(self, position) -> None:
        """After OK on a page, show the keyboard if it landed in a text field.

        Two checks, because neither catches everything: what is under the
        cursor (answers at once, and works for a box that was already
        focused and so announces nothing), and what the page focused a
        moment later (a button that opens a search bar elsewhere).
        """
        if position is not None:
            threading.Thread(target=self._look_under_cursor, args=(position,),
                             name="piper-click", daemon=True).start()
        self.later(AFTER_CLICK_S, self._check_focus_after_click)

    def _look_under_cursor(self, position) -> None:
        """Show the keyboard for a text field under the cursor, hide it for
        anything else (a click on a link or a search button)."""
        try:
            if self._cursor_mode() is None:
                return
            found = self.targets.at_point(*position)
            role = None if found is None else found.get("role")
            if role in TEXT_ROLES:
                if not self.onscreen.showing():
                    LOG.info("OK clicked a %s; showing the keyboard", role)
                    self.open_keyboard()
            elif role is not None and self.onscreen.showing() and self._outside_keyboard(position):
                self.close_keyboard()
        except Exception as exc:
            LOG.warning("Asking what the cursor is on: %s", exc)

    def _outside_keyboard(self, position) -> bool:
        # The page doesn't know the keyboard is drawn over it: a click on a
        # key reports whatever is underneath, and must not close the keyboard.
        height = self.onscreen.height()
        return height <= 0 or position[1] < self.screen[1] - height

    def _check_focus_after_click(self) -> None:
        """Show the keyboard if the page focused a text field after the click.

        Only ever shows it: the page saying nothing is no reason to take the
        keyboard away from someone typing.
        """
        try:
            if self._cursor_mode() is None or self.onscreen.showing():
                return
            field = self.watcher.typing_into()
            if field is not None:
                LOG.info("A click landed in %s; showing the keyboard", field.get("role"))
                self.open_keyboard()
        except Exception as exc:
            LOG.warning("Looking at what the click landed on: %s", exc)

    def open_keyboard(self) -> dict:
        with self._gate_lock:
            allowed = self.session.enabled()
        if allowed:
            self.onscreen.show()
        return self.onscreen.health()

    def close_keyboard(self) -> dict:
        self.onscreen.hide()
        self.watcher.forget()
        return self.onscreen.health()

    # --- requests from the web pages -------------------------------------------

    def choose(self, mode, session_id) -> dict:
        with self._gate_lock:
            self._refresh()
            self.session.choose(mode, session_id)
            state = self._sync_context()
        if self.desktop.health().get("active"):
            self.desktop.release()
        self.targets.invalidate()  # start the new visit from what is on screen now
        return state

    def manual(self, confirmed) -> dict:
        with self._gate_lock:
            self._refresh()
            self.session.start_manual(confirmed)
            state = self._sync_context()
        if self.desktop.health().get("active"):
            self.desktop.release()
        return state

    def stop(self) -> dict:
        with self._gate_lock:
            self.session.stop()
            state = self._sync_context()
        self.desktop.release()
        return state

    def launch(self, service, session_id) -> dict:
        """Open a service for the visit the TV page is showing.

        It answers to the same gate as the remote, so a page left open on a
        laptop can't start something on a TV that shows another input.
        """
        with self._gate_lock:
            self._refresh()
            state = self._sync_context()
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
        with self._screen_lock:
            if not self.window.get("windowed") and self.interface.showing():
                # The backdrop has to be there before the service, or it ends up on top.
                self.backdrop.show()
        state = self.launcher.launch(service)
        opened = state.get("running")
        if opened:
            self.later(HIDE_INTERFACE_AFTER_S, lambda: self._hide_interface(opened))
        if self.launcher.policy(service) == SNAP:
            self.onscreen.prepare()  # it takes seconds to start, so have it ready
        return state

    def stop_service(self) -> dict:
        return self.launcher.stop()

    def leave(self) -> dict:
        """Take Piper off the screen. The remote then moves the desktop mouse,
        and a double OK on the PiperTV icon brings Piper back."""
        if self.launcher.running() is not None:
            self._close_service()
        with self._screen_lock:
            self._hidden_for = None
            self.interface.close()
            self.backdrop.close()
        with self._gate_lock:
            state = self.session.snapshot()
            if state["session"] and state["mode"] != "pointer":
                self.session.choose("pointer", state["session"]["id"])
            state = self._sync_context()
        LOG.info("Left Piper; the remote moves the desktop's mouse")
        return dict(state, interface=self._interface_state())

    # --- used by the recorder --------------------------------------------------

    def hold(self, reason: str = RECORDING) -> dict:
        """Close the gate and let go of the receiver so a capture can use it."""
        with self._gate_lock:
            self.session.hold(reason)
            state = self._sync_context()
        self.desktop.release()
        self.controller.pause()
        return state

    def release(self) -> dict:
        with self._gate_lock:
            self._refresh()
            self.session.release()
            state = self._sync_context()
        # The reader waits for the gate by itself, so resuming is always safe.
        self.controller.resume()
        return state

    # --- reporting -------------------------------------------------------------

    def events(self, after: int = 0) -> dict:
        """Presses the TV page hasn't seen yet, and what it needs to draw itself."""
        with self._gate_lock:
            self._refresh()
            result = self.buttons.since(after)
            state = self.session.snapshot()
        result.update(mode=state["mode"], control=state["control"],
                      session_id=state["session"]["id"] if state["session"] else None,
                      services=self.launcher.snapshot(),
                      typing=self.watcher.typing_into(),
                      keyboard=self.onscreen.health(),
                      leaving={"armed": self.exit_twice.armed(),
                               "seconds": self.exit_twice.remaining()})
        return result

    def snapshot(self) -> dict:
        detection = self._refresh()
        state = self.session.snapshot()
        state.update(detection=detection, roles=self.roles.describe(),
                     services=self.launcher.snapshot(), interface=self._interface_state(),
                     keys=self.keys.health(), focus=self.watcher.health(),
                     keyboard=self.onscreen.health(),
                     pointer_settings=dict(self.pointer), window_settings=dict(self.window),
                     runtime=self._runtime())
        return state

    def health(self) -> dict:
        return dict(self._runtime(), screen=list(self.screen),
                    detection=self.monitor.snapshot(), services=self.launcher.snapshot(),
                    interface=self._interface_state(), keys=self.keys.health())

    def _runtime(self) -> dict:
        return {"pointer": pointer_health(), "receiver": self.controller.health(),
                "desktop": self.desktop.health(), "targets": self.targets.health()}
