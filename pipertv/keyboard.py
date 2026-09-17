"""Type into whatever is on the Pi's screen, so a remote can drive it.

Piper opens a service and then has nothing to say to it: YouTube's television
app is built for a four-way pad and an OK button, and until this existed the
first screen it shows -- a Get started button -- was the end of the road.

The same kernel interface as the virtual pointer, for the same reason: uinput
events arrive as if from a keyboard plugged into the Pi, so whatever holds
focus receives them on Wayland and X11 alike, with nothing to ask the display
server and nothing to install in the browser.

Only the keys a remote actually has are declared. A device that could type
anything would be a keylogger's mirror image sitting on a machine whose remote
is also the television's; this one can press arrows, enter, escape and back,
and nothing else. It exists only while a service is open and is destroyed with
it, so no device is left behind that could type into the desktop afterwards.
"""

from __future__ import annotations

import logging
import os
import re
import struct
import threading
import time

from .pointer import (BUS_VIRTUAL, DEVICE, EV_KEY, EV_SYN, EVENT, SETUP, SYN_REPORT,
                      UI_DEV_CREATE, UI_DEV_DESTROY, UI_DEV_SETUP, UI_SET_EVBIT,
                      UI_SET_KEYBIT)

# Linux input event codes; see include/uapi/linux/input-event-codes.h.
KEY_ENTER, KEY_ESC, KEY_BACKSPACE = 28, 1, 14
KEY_LEFT, KEY_RIGHT, KEY_UP, KEY_DOWN = 105, 106, 103, 108
KEY_HOME, KEY_SPACE = 102, 57

LOG = logging.getLogger(__name__)

# What Piper can perform, named after the role rather than the key cap.
KEY_LEFTSHIFT = 42
# Letters and digits in the order the kernel numbers them.
_ROWS = (("q w e r t y u i o p", 16), ("a s d f g h j k l", 30),
         ("z x c v b n m", 44), ("1 2 3 4 5 6 7 8 9 0", 2))
LETTERS = {key: first + offset
           for keys, first in _ROWS
           for offset, key in enumerate(keys.split())}
# What a search box needs beyond letters. Anything else is out of reach on
# purpose: this types what a person picked from a keyboard on the screen.
PUNCTUATION = {"-": 12, "=": 13, ".": 52, ",": 51, "/": 53, ";": 39, "'": 40}
SHIFTED = {"_": "-", "+": "=", ":": ";", "?": "/", '"': "'", "<": ",", ">": "."}

KEYS = {"up": KEY_UP, "down": KEY_DOWN, "left": KEY_LEFT, "right": KEY_RIGHT,
        "ok": KEY_ENTER, "back": KEY_ESC, "home": KEY_HOME, "space": KEY_SPACE,
        "backspace": KEY_BACKSPACE, "enter": KEY_ENTER, "shift": KEY_LEFTSHIFT,
        **LETTERS, **PUNCTUATION}


class VirtualKeyboard:
    """One virtual keyboard, owned by whatever service is on the screen.

    Not reusable after close(): the kernel device is destroyed, and anything
    that wants to type again creates a new one, so a stale handle cannot reach
    the desktop later.
    """

    def __init__(self, device: str = DEVICE, name: str = "PiperTV remote keyboard",
                 settle_s: float = 0.12):
        if not isinstance(device, str) or not re.fullmatch(r"/dev/[a-z0-9_]{1,32}", device):
            raise ValueError("uinput device must look like /dev/uinput.")
        self.device = device
        self.name = name
        self.settle_s = settle_s
        self._lock = threading.RLock()
        self._fd: int | None = None
        self._created = False
        self._closed = False

    def _ioctl(self, request: int, value) -> None:
        import fcntl

        fcntl.ioctl(self._fd, request, value)

    def __enter__(self) -> "VirtualKeyboard":
        return self.open()

    def open(self) -> "VirtualKeyboard":
        if os.name != "posix":
            raise OSError("A virtual keyboard requires Linux on the Raspberry Pi.")
        with self._lock:
            if self._closed:
                raise RuntimeError("A closed virtual keyboard cannot be reopened.")
            if self._fd is not None:
                raise RuntimeError("This virtual keyboard is already open.")
            self._fd = os.open(self.device, os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            try:
                self._ioctl(UI_SET_EVBIT, EV_KEY)
                self._ioctl(UI_SET_EVBIT, EV_SYN)
                for code in sorted(set(KEYS.values())):
                    self._ioctl(UI_SET_KEYBIT, code)
                name = self.name.encode("ascii", "replace")[:79]
                self._ioctl(UI_DEV_SETUP, struct.pack(SETUP, BUS_VIRTUAL, 0x1209,
                                                      0x7012, 1, name, 0))
                self._ioctl(UI_DEV_CREATE, 0)
                self._created = True
                # udev has to notice the device before the session routes keys;
                # without this the first press of a session is swallowed.
                time.sleep(self.settle_s)
            except BaseException:
                self.close()
                raise
        return self

    def _write(self, events) -> None:
        if self._fd is None:
            raise RuntimeError("The virtual keyboard is closed.")
        now = time.clock_gettime(time.CLOCK_MONOTONIC)
        seconds, microseconds = int(now), int(now % 1 * 1_000_000)
        payload = b"".join(
            struct.pack(EVENT, seconds, microseconds, kind, code, value)
            for kind, code, value in (*events, (EV_SYN, SYN_REPORT, 0)))
        written = os.write(self._fd, payload)
        if written != len(payload):
            raise OSError("The virtual keyboard accepted only part of an event batch.")

    def tap(self, key: str) -> str:
        """Press and release one key, named by what Piper calls it."""
        code = KEYS.get(key)
        if code is None:
            raise ValueError(f"A remote cannot type {key!r}.")
        with self._lock:
            self._write(((EV_KEY, code, 1),))
            self._write(((EV_KEY, code, 0),))
            return key

    def write(self, text: str) -> int:
        """Type a line of text, as if someone had typed it on a keyboard.

        Only what a search box needs: letters, digits and a little
        punctuation, with shift held for the capitals. A character this
        keyboard has no key for is skipped rather than mistyped as something
        else.
        """
        if not isinstance(text, str):
            raise ValueError("Text to type must be a string.")
        typed = 0
        with self._lock:
            for character in text:
                key, shift = self._key_for(character)
                if key is None:
                    continue
                code = KEYS[key]
                if shift:
                    self._write(((EV_KEY, KEY_LEFTSHIFT, 1),))
                self._write(((EV_KEY, code, 1),))
                self._write(((EV_KEY, code, 0),))
                if shift:
                    self._write(((EV_KEY, KEY_LEFTSHIFT, 0),))
                typed += 1
        return typed

    @staticmethod
    def _key_for(character: str):
        """Which key types this character, and whether shift is held for it."""
        if character == " ":
            return "space", False
        lowered = character.lower()
        if lowered in KEYS and lowered != character:
            return lowered, True          # a capital letter
        if character in KEYS:
            return character, False
        if character in SHIFTED:
            return SHIFTED[character], True
        return None, False

    def __exit__(self, *_args) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._fd is None:
                return
            try:
                # Release everything: a closed session must not leave a key
                # held down, which would repeat into the desktop forever.
                for code in sorted(set(KEYS.values())):
                    self._write(((EV_KEY, code, 0),))
            except OSError:
                pass
            finally:
                try:
                    if self._created:
                        self._ioctl(UI_DEV_DESTROY, 0)
                except OSError:
                    pass
                finally:
                    os.close(self._fd)
                    self._fd = None
                    self._created = False


class ServiceKeys:
    """The keyboard Piper types with while a service is on the screen.

    It comes into being with the first key sent to a service and is destroyed
    when that service closes, so nothing is left behind that could type into
    the desktop afterwards. A failure here must never reach the IR reader: the
    device is dropped, the reason is kept, and the next press tries again.
    """

    def __init__(self, factory=VirtualKeyboard):
        self.factory = factory
        self._lock = threading.RLock()
        self._keyboard = None
        self._error: str | None = None

    def send(self, key: str):
        """Type one key into whatever holds focus. Returns what was sent."""
        with self._lock:
            if key not in KEYS:
                return None  # volume, power, digits: not ours to forward
            try:
                if self._keyboard is None:
                    self._keyboard = self.factory()
                    self._keyboard.open()
                sent = self._keyboard.tap(key)
                self._error = None
                return sent
            except Exception as exc:  # noqa: BLE001 - the receiver must survive
                self._error = str(exc) or type(exc).__name__
                LOG.warning("Typing into the open service: %s", exc)
                self.release()
                return None

    def write(self, text: str) -> int:
        """Type a line into whatever holds focus. Returns how much was typed."""
        with self._lock:
            try:
                if self._keyboard is None:
                    self._keyboard = self.factory()
                    self._keyboard.open()
                typed = self._keyboard.write(text)
                self._error = None
                return typed
            except Exception as exc:  # noqa: BLE001 - the receiver must survive
                self._error = str(exc) or type(exc).__name__
                LOG.warning("Typing into the open service: %s", exc)
                self.release()
                return 0

    def release(self) -> None:
        with self._lock:
            keyboard, self._keyboard = self._keyboard, None
            if keyboard is not None:
                try:
                    keyboard.close()
                except Exception as exc:  # noqa: BLE001 - closing must not raise
                    LOG.warning("Closing the virtual keyboard: %s", exc)

    def health(self) -> dict:
        with self._lock:
            return {"ok": self._error is None, "active": self._keyboard is not None,
                    "keys": sorted(KEYS), "error": self._error}

    def close(self) -> None:
        self.release()
