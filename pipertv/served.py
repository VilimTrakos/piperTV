"""Piper with its interface in a browser elsewhere, and nothing on this screen.

Normally the Pi shows the interface itself: a chromium window on its HDMI
output, a cursor it moves, services it starts. That is most of a Pi 3B+'s
memory, and it means being in front of the television to use any of it. Served
mode is the other arrangement. The Pi keeps the two things only it can do --
the receiver and the recordings -- and the interface is opened from a laptop
on the same network, which opens the services in its own tabs.

The gate the desktop mode is built around has nothing to guard here. It exists
because a press could move this Pi's cursor while the television was showing
something else; a press in served mode reaches a web page and nothing else, so
there is no visit to establish, no HDMI report to wait for, and no mode to
choose. What remains is what the page actually needs: presses, numbered, with
what each one performs.

Nothing in this module touches the desktop, so it runs over SSH on a Pi with no
screen attached at all.
"""

from __future__ import annotations

import logging
import threading
import uuid

from .desktop import POINTER_DEFAULTS, validate_pointer
from .ir_control import DIRECTIONS, IRController
from .launcher import SERVICES
from .roles import RoleMap
from .tv import ButtonLog

LOG = logging.getLogger(__name__)
RECORDING = "Recording a remote button, so the remote is not driving the interface."


def pages(services=SERVICES) -> list[dict]:
    """The services a browser somewhere else can open, with where they live.

    A page and nothing more: Kodi is left out because it plays video in this
    Pi's own hardware, which is the whole reason it has a tile, and there is
    nothing about it a laptop could show. The interface says so on the tile
    rather than opening something that only looks like Kodi.
    """
    return [{"id": key, "name": service["name"], "url": service["url"]}
            for key, service in services.items() if service.get("url")]


def _saved(read, fallback, what: str):
    """A saved setting, or the default when the library says something unusable.

    The same forgiveness the desktop's control has: a hand-edited recordings
    file must not be the reason a remote stops working, because the remote is
    what people have in their hand when they find out.
    """
    try:
        return read()
    except Exception as exc:  # noqa: BLE001 - the remote must still start
        LOG.warning("Ignoring saved %s: %s", what, exc)
        return fallback()


class ServedRemote:
    """The receiver, the roles, and a feed of presses for a browser elsewhere.

    It stands where RemoteControl stands in the application and answers what
    the interface asks, but it owns nothing on this Pi's screen: no cursor, no
    windows, no accessibility bus. Anything that only means something on the
    Pi's own screen says so instead of pretending, and the application turns
    that into the same answer a server started without desktop control gives.
    """

    def __init__(self, store, device: str = "/dev/lirc0", controller=None,
                 buttons=None, services=None, session_id: str | None = None):
        self.store = store
        self.buttons = ButtonLog() if buttons is None else buttons
        self.services = pages(SERVICES if services is None else services)
        # The page asks for presses since a number and wants to know it is
        # still talking to the same run. One visit for as long as this process
        # lives: there is no television input here to come and go.
        self.session_id = session_id or uuid.uuid4().hex
        self.roles = self._load_roles()
        self.pointer = self._load_pointer()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        # Why the remote is standing down, while it is. Recording owns the
        # receiver, and a page must not act on the press being learned.
        self._holding: str | None = None
        self.controller = (IRController(store, self._press, self._listening, device=device,
                                        is_direction=self._is_direction, pace=self._hold_pace)
                           if controller is None else controller)

    # --- settings --------------------------------------------------------

    def _load_roles(self) -> RoleMap:
        return _saved(lambda: RoleMap(self.store.snapshot().get("roles")), RoleMap,
                      "role bindings")

    def _load_pointer(self) -> dict:
        return _saved(lambda: validate_pointer(self.store.pointer()),
                      lambda: dict(POINTER_DEFAULTS), "cursor settings")

    def reload_roles(self) -> dict:
        with self._lock:
            self.roles = self._load_roles()
            return self.roles.bindings()

    def reload_pointer(self) -> dict:
        with self._lock:
            self.pointer = self._load_pointer()
            return dict(self.pointer)

    def reload_window(self) -> dict:
        """How Piper fills the television is the Pi's own screen's business."""
        return {}

    def reload_recordings(self) -> None:
        self.controller.reload_recordings()
        # A saved binding travels in the same file as the signals.
        self.reload_roles()

    def use_receiver(self, device) -> dict:
        """Listen for the remote on another receiver, from this moment on."""
        self.controller.use(device)
        return self.controller.health()

    # --- hearing the remote ----------------------------------------------

    def _listening(self) -> bool:
        return not self._stop.is_set()

    def _is_direction(self, button_id) -> bool:
        """Whether holding this key should repeat, judged by what it performs."""
        return self.roles.action(button_id) in DIRECTIONS

    def _hold_pace(self, button_id):
        """How fast holding a direction repeats.

        Everything a press moves here moves in whole steps -- the ring turns by
        one service, a list by one row -- so this is the paced repeat rather
        than the fast one meant for a cursor gliding across pixels. How long
        counts as "held" is still the setting people tune when one press of
        their own remote arrives as two.
        """
        return (self.pointer.get("hold_delay_s", POINTER_DEFAULTS["hold_delay_s"]),
                self.pointer.get("hold_interval_s", POINTER_DEFAULTS["hold_interval_s"]))

    def _press(self, button: str) -> None:
        """Record one recognised press for whichever browser is showing the page.

        What the remote sent is recorded; what it performs is recorded beside
        it, because they differ once a role has been moved to a key the
        television ignores. Nothing else happens here: the page decides what a
        press means, and it is the only thing that can.
        """
        self.buttons.append(button, "piper", self.session_id,
                            action=self.roles.action(button))

    # --- lifecycle --------------------------------------------------------

    def start(self) -> None:
        self._stop.clear()
        self.controller.resume()

    def close(self) -> None:
        self._stop.set()
        try:
            self.controller.close()
        except Exception as exc:  # noqa: BLE001 - shutdown must finish
            LOG.warning("Closing the receiver: %s", exc)

    # --- recording interlock ---------------------------------------------

    def hold(self, reason: str = RECORDING) -> dict:
        """Stand down so a capture can own the receiver.

        The page is told as well as the reader: a press being learned must not
        also turn the ring behind the recording that is waiting for it.
        """
        with self._lock:
            self._holding = reason
        self.controller.pause()
        return self.state()

    def release(self) -> dict:
        with self._lock:
            self._holding = None
        self.controller.resume()
        return self.state()

    # --- what the interface asks ------------------------------------------

    def state(self) -> dict:
        with self._lock:
            holding = self._holding
        return {"served": True, "mode": "piper", "session_id": self.session_id,
                "control": "off" if holding else "on", "hold": holding}

    def events(self, after: int = 0) -> dict:
        """Presses the interface has not seen yet, with what it may act on.

        The shape is the one the television's interface already reads, so the
        same page serves both arrangements. Everything that belongs to a screen
        this server does not have is absent rather than invented: no service is
        running here, because opening one happens in the browser.
        """
        result = self.buttons.since(after)
        result.update(self.state())
        result["services"] = {"available": True, "reason": None, "browser": None,
                              "services": [dict(service) for service in self.services],
                              "running": None, "error": None, "history": []}
        result["typing"] = None
        result["keyboard"] = None
        result["leaving"] = {"armed": False, "seconds": 0.0}
        return result

    def health(self) -> dict:
        return dict(self.state(), receiver=self.controller.health(),
                    presses=self.buttons.health(),
                    services=[service["id"] for service in self.services])

    # --- the Pi's own screen, which this server does not have --------------

    def _elsewhere(self, what: str):
        raise KeyError(f"{what} belongs to Piper on the Pi's own screen. This server is "
                       "serving the interface to a browser instead.")

    def snapshot(self) -> dict:
        # The studio asks this to draw its control panel, and already knows what
        # to do when a server has no desktop control to report.
        self._elsewhere("Desktop control")

    def choose(self, mode, session_id) -> dict:
        self._elsewhere("Choosing how the remote drives this desktop")

    def manual(self, confirmed) -> dict:
        self._elsewhere("Confirming what the television is showing")

    def stop(self) -> dict:
        self._elsewhere("Stopping desktop control")

    def launch(self, service, session_id) -> dict:
        self._elsewhere("Opening a service on the television")

    def stop_service(self) -> dict:
        self._elsewhere("Closing a service on the television")

    def leave(self) -> dict:
        self._elsewhere("Leaving Piper for the Pi's desktop")

    def open_keyboard(self) -> dict:
        self._elsewhere("The on-screen keyboard")

    def close_keyboard(self, text=None) -> dict:
        self._elsewhere("The on-screen keyboard")

    def show_interface(self, focus=None) -> dict:
        self._elsewhere("The interface on the television")
