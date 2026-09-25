"""The virtual mouse that moves the Pi's cursor, and the screen size it moves in.

The device reports absolute positions: a relative mouse can't jump straight
to a target, and there's no portable way to read the cursor back. So the
position is tracked here, starting at the centre of the screen.

Creating the device doesn't move the cursor; only move_to() does.
"""

from __future__ import annotations

import os
import re
import struct
import subprocess
import threading
import time

from .uinput import (ABS_SETUP, DEVICE, EV_ABS, EV_KEY, EV_REL, EV_SYN, UI_ABS_SETUP,
                     UI_SET_ABSBIT, UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_RELBIT, UinputDevice)

ABS_X, ABS_Y = 0x00, 0x01
REL_WHEEL = 0x08  # positive scrolls up
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE = 0x110, 0x111, 0x112
BUTTONS = {"left": BTN_LEFT, "right": BTN_RIGHT, "middle": BTN_MIDDLE}
# The axis range is mapped onto the whole screen, whatever its resolution.
AXIS_MAX = 32767

# fb0 keeps whatever mode the kernel set at boot (1024x768 if the TV was off),
# so ask the compositor first and use fb0 only as a fallback.
SCREEN_SIZE = "/sys/class/graphics/fb0/virtual_size"
DISPLAY_TOOL = ("wlr-randr",)
ASK_TIMEOUT_S = 3.0
SIZE_CACHE_S = 5.0
_asked = {"at": -float("inf"), "size": None}
_ask_lock = threading.Lock()


def _check_extent(extent) -> None:
    if not isinstance(extent, int) or isinstance(extent, bool) or extent < 2:
        raise ValueError("Screen size must be at least 2 pixels.")


def to_axis(pixel: float, extent: int) -> int:
    _check_extent(extent)
    position = round(float(pixel) / (extent - 1) * AXIS_MAX)
    return max(0, min(AXIS_MAX, position))


def to_pixel(axis: int, extent: int) -> int:
    _check_extent(extent)
    return max(0, min(extent - 1, round(axis / AXIS_MAX * (extent - 1))))


def ask_compositor(command=DISPLAY_TOOL, run=subprocess.run,
                   clock=time.monotonic) -> tuple[int, int] | None:
    """The current mode as wlr-randr reports it, cached for a few seconds."""
    with _ask_lock:
        now = clock()
        if now - _asked["at"] < SIZE_CACHE_S:
            return _asked["size"]
        size = None
        try:
            done = run(list(command), capture_output=True, text=True,
                       timeout=ASK_TIMEOUT_S, check=False)
            for line in (done.stdout or "").splitlines():
                if "current" not in line:
                    continue
                found = re.search(r"(\d{2,6})\s*x\s*(\d{2,6})\s*px", line)
                if found:
                    size = (int(found.group(1)), int(found.group(2)))
                    break
        except Exception:
            size = None  # no wlr-randr, or not a wlroots desktop
        _asked["at"], _asked["size"] = now, size
        return size


def read_framebuffer_size(path: str = SCREEN_SIZE) -> tuple[int, int] | None:
    try:
        with open(path, "r", encoding="ascii") as handle:
            text = handle.read(64)
    except OSError:
        return None
    found = re.fullmatch(r"\s*(\d{1,6})\s*,\s*(\d{1,6})\s*", text)
    if not found:
        return None
    width, height = int(found.group(1)), int(found.group(2))
    return (width, height) if width >= 2 and height >= 2 else None


def read_screen_size(path: str = SCREEN_SIZE, ask=ask_compositor) -> tuple[int, int] | None:
    size = ask() if ask is not None else None
    return size if size is not None else read_framebuffer_size(path)


def health(device: str = DEVICE) -> dict:
    """Whether a virtual pointer could be created, without creating one."""
    exists = os.path.exists(device)
    writable = exists and os.access(device, os.W_OK)
    result = {"ok": bool(writable), "device": device, "device_exists": exists,
              "device_writable": writable, "screen_size": read_screen_size()}
    if not exists:
        result["error"] = (f"{device} was not found. Load the uinput module on the Pi "
                           "(sudo modprobe uinput) and make it load at boot.")
    elif not writable:
        result["error"] = (f"{device} is not writable. Grant your Pi account access with a "
                           "udev rule, then log in again.")
    return result


class VirtualPointer(UinputDevice):
    kind = "pointer"
    product_id = 0x7011

    def __init__(self, screen: tuple[int, int], device: str = DEVICE,
                 name: str = "PiperTV remote pointer", settle_s: float = 0.12):
        width, height = screen
        for extent in (width, height):
            if not isinstance(extent, int) or isinstance(extent, bool) or not 2 <= extent <= 65536:
                raise ValueError("Screen size must be between 2 and 65536 pixels.")
        super().__init__(device, name, settle_s)
        self.screen = (width, height)
        self._x, self._y = AXIS_MAX // 2, AXIS_MAX // 2

    def _declare(self) -> None:
        for kind in (EV_ABS, EV_KEY, EV_REL, EV_SYN):
            self._ioctl(UI_SET_EVBIT, kind)
        self._ioctl(UI_SET_RELBIT, REL_WHEEL)
        for axis in (ABS_X, ABS_Y):
            self._ioctl(UI_SET_ABSBIT, axis)
            # axis, padding, then value, min, max, fuzz, flat, resolution
            self._ioctl(UI_ABS_SETUP, struct.pack(ABS_SETUP, axis, 0, 0, 0, AXIS_MAX, 0, 0, 0))
        for code in BUTTONS.values():
            self._ioctl(UI_SET_KEYBIT, code)

    def _held_codes(self):
        return BUTTONS.values()

    @property
    def position(self) -> tuple[int, int]:
        """Where we last put the cursor, in screen pixels."""
        with self._lock:
            return (to_pixel(self._x, self.screen[0]), to_pixel(self._y, self.screen[1]))

    def move_to(self, x: int, y: int) -> tuple[int, int]:
        with self._lock:
            self._x = to_axis(x, self.screen[0])
            self._y = to_axis(y, self.screen[1])
            self._write(((EV_ABS, ABS_X, self._x), (EV_ABS, ABS_Y, self._y)))
            return self.position

    def move_by(self, dx: int, dy: int) -> tuple[int, int]:
        with self._lock:
            x, y = self.position
            return self.move_to(x + dx, y + dy)

    def scroll(self, clicks: int) -> int:
        """Turn the wheel: positive scrolls up, negative down."""
        if isinstance(clicks, bool) or not isinstance(clicks, int):
            raise ValueError("A wheel turns in whole clicks.")
        if not -32 <= clicks <= 32:
            raise ValueError("A single press must not turn the wheel more than 32 clicks.")
        if clicks == 0:
            return 0
        with self._lock:
            self._write(((EV_REL, REL_WHEEL, clicks),))
            return clicks

    def click(self, button: str = "left") -> None:
        code = BUTTONS.get(button)
        if code is None:
            raise ValueError("Button must be left, right, or middle.")
        with self._lock:
            self._write(((EV_KEY, code, 1),))
            self._write(((EV_KEY, code, 0),))
