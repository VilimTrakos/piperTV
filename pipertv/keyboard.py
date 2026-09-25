"""Keys for the open service, and the on-screen keyboard for search boxes.

VirtualKeyboard is a uinput keyboard that can press only the keys a remote
has (plus Alt, for Alt+Left). It can't type text; the on-screen keyboard
(wvkbd) does that itself. It exists only while a service is open.
"""

from __future__ import annotations

import logging
import shutil
import signal
import subprocess
import threading
import time

from .uinput import DEVICE, EV_KEY, EV_SYN, UI_SET_EVBIT, UI_SET_KEYBIT, UinputDevice

LOG = logging.getLogger(__name__)

# linux/input-event-codes.h
KEY_ESC = 1
KEY_ENTER = 28
KEY_LEFTALT = 56
KEY_HOME = 102
KEY_UP = 103
KEY_LEFT = 105
KEY_RIGHT = 106
KEY_DOWN = 108

KEYS = {"up": KEY_UP, "down": KEY_DOWN, "left": KEY_LEFT, "right": KEY_RIGHT,
        "ok": KEY_ENTER, "back": KEY_ESC, "home": KEY_HOME}
# On a web page escape does nothing; "back" there means the previous page.
PAGE_BACK = "page back"
CHORDS = {PAGE_BACK: (KEY_LEFTALT, KEY_LEFT)}
CODES = sorted(set(KEYS.values()) | {code for chord in CHORDS.values() for code in chord})


class VirtualKeyboard(UinputDevice):
    kind = "keyboard"
    product_id = 0x7012

    def __init__(self, device: str = DEVICE, name: str = "PiperTV remote keyboard",
                 settle_s: float = 0.12):
        super().__init__(device, name, settle_s)

    def _declare(self) -> None:
        self._ioctl(UI_SET_EVBIT, EV_KEY)
        self._ioctl(UI_SET_EVBIT, EV_SYN)
        for code in CODES:
            self._ioctl(UI_SET_KEYBIT, code)

    def _held_codes(self):
        return CODES

    def tap(self, key: str) -> str:
        """Press and release a key or a chord (modifier down first, up last)."""
        codes = CHORDS.get(key) or ((KEYS[key],) if key in KEYS else None)
        if codes is None:
            raise ValueError(f"A remote cannot type {key!r}.")
        with self._lock:
            for code in codes:
                self._write(((EV_KEY, code, 1),))
            for code in reversed(codes):
                self._write(((EV_KEY, code, 0),))
        return key


class ServiceKeys:
    """The keyboard used while a service is open, created on the first key.

    Errors are logged and kept for health() but never raised: this runs on
    the IR reader thread. The next key simply tries again.
    """

    def __init__(self, factory=VirtualKeyboard):
        self.factory = factory
        self._lock = threading.RLock()
        self._keyboard = None
        self._error: str | None = None

    def send(self, key: str):
        """Press one key. Returns it, or None if it isn't ours to send or failed."""
        if key not in KEYS and key not in CHORDS:
            return None  # volume, power, digits...
        with self._lock:
            try:
                if self._keyboard is None:
                    self._keyboard = self.factory()
                    self._keyboard.open()
                sent = self._keyboard.tap(key)
                self._error = None
                return sent
            except Exception as exc:
                self._error = str(exc) or type(exc).__name__
                LOG.warning("Typing into the open service: %s", exc)
                self.release()
                return None

    def release(self) -> None:
        with self._lock:
            keyboard, self._keyboard = self._keyboard, None
            if keyboard is not None:
                try:
                    keyboard.close()
                except Exception as exc:
                    LOG.warning("Closing the virtual keyboard: %s", exc)

    def health(self) -> dict:
        with self._lock:
            return {"ok": self._error is None, "active": self._keyboard is not None,
                    "keys": sorted(KEYS), "error": self._error}

    def close(self) -> None:
        self.release()


class OnScreenKeyboard:
    """wvkbd across the bottom of the screen, for typing into a page.

    It types into whatever has keyboard focus, so the letters land straight
    in the page's own field. Its keys are clicked with the cursor like
    anything else on the page. It's started hidden when a page opens (it
    takes seconds to start on a Pi) and shown or hidden with signals.
    """

    def __init__(self, command=("wvkbd-mobintl", "-L", "320", "--fn", "Sans 20", "--hidden"),
                 spawn=subprocess.Popen, show_signal=signal.SIGUSR2,
                 hide_signal=signal.SIGUSR1, settle_s: float = 0.5):
        self.command = list(command)
        self.spawn = spawn
        self.show_signal = show_signal
        self.hide_signal = hide_signal
        self.settle_s = settle_s
        self._lock = threading.RLock()
        self._process = None
        self._showing = False
        self._error: str | None = None

    def available(self) -> bool:
        return shutil.which(self.command[0]) is not None

    def _running(self):
        if self._process is not None and self._process.poll() is None:
            return self._process
        self._process = None
        self._showing = False
        return None

    def prepare(self) -> bool:
        """Start it hidden, if it isn't running yet."""
        with self._lock:
            if self._running() is not None:
                return True
            if not self.available():
                self._error = (f"{self.command[0]} is not installed on this Pi. "
                               "Install it with: sudo apt install wvkbd")
                return False
            try:
                self._process = self.spawn(
                    self.command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
                self._error = None
                return True
            except Exception as exc:
                self._process = None
                self._error = str(exc) or type(exc).__name__
                LOG.warning("Starting the on-screen keyboard: %s", exc)
                return False

    def show(self) -> bool:
        with self._lock:
            if self._running() is None:
                if not self.prepare():
                    return False
                time.sleep(self.settle_s)  # let it map its surface first
            try:
                self._process.send_signal(self.show_signal)
                self._showing = True
                self._error = None
                return True
            except Exception as exc:
                self._error = str(exc) or type(exc).__name__
                LOG.warning("Showing the on-screen keyboard: %s", exc)
                return False

    def hide(self) -> bool:
        with self._lock:
            process = self._running()
            self._showing = False
            if process is None:
                return False
            try:
                process.send_signal(self.hide_signal)
                return True
            except Exception as exc:
                self._error = str(exc) or type(exc).__name__
                LOG.warning("Hiding the on-screen keyboard: %s", exc)
                return False

    def showing(self) -> bool:
        with self._lock:
            return self._showing and self._running() is not None

    def height(self) -> int:
        """Pixels it covers at the bottom of the screen (its -L option), or 0."""
        try:
            return int(self.command[self.command.index("-L") + 1])
        except (ValueError, IndexError):
            return 0

    def health(self) -> dict:
        with self._lock:
            return {"ok": self._error is None, "available": self.available(),
                    "ready": self._running() is not None, "showing": self.showing(),
                    "error": self._error}

    def close(self) -> None:
        with self._lock:
            process, self._process = self._process, None
            self._showing = False
            if process is None or process.poll() is not None:
                return
            try:
                process.terminate()
                process.wait(timeout=2)
            except Exception as exc:
                LOG.warning("Closing the on-screen keyboard: %s", exc)
