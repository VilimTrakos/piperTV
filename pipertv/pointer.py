"""Move the Raspberry Pi's own desktop cursor through a virtual input device.

uinput is a kernel interface, so the same code drives an X11 or a Wayland
session without talking to either one:
https://docs.kernel.org/input/uinput.html
https://docs.kernel.org/input/event-codes.html

The device reports absolute positions. A relative device cannot be told to jump
to a target, and reading the cursor back is a display-server question this layer
deliberately does not ask; tracking the position here keeps Pointer and Snapping
modes working identically on both session types.

Creating this device moves the real cursor, so nothing here runs at import. An
active control session opens it, and closing it removes the device again.
"""

from __future__ import annotations

import os
import re
import struct
import threading
import time

# asm-generic ioctl encoding. Every size encoded below (uinput_setup 92,
# uinput_abs_setup 28, int 4) is the same on 32- and 64-bit Linux, so these
# numbers are identical on Raspberry Pi ARM and on x86.
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
UI_DEV_SETUP = 0x405C5503
UI_ABS_SETUP = 0x401C5504
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_RELBIT = 0x40045566
UI_SET_ABSBIT = 0x40045567

EV_SYN, EV_KEY, EV_REL, EV_ABS = 0x00, 0x01, 0x02, 0x03
# The wheel, so a page can be scrolled where it sits rather than by dragging a
# scrollbar with the cursor. Positive is up, as on a real mouse.
REL_WHEEL = 0x08
SYN_REPORT = 0
ABS_X, ABS_Y = 0x00, 0x01
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE = 0x110, 0x111, 0x112
BUS_VIRTUAL = 0x06

BUTTONS = {"left": BTN_LEFT, "right": BTN_RIGHT, "middle": BTN_MIDDLE}

# struct input_event carries two longs, so it is 16 bytes on 32-bit ARM and 24
# on 64-bit. Native sizes and alignment give the running kernel's layout.
EVENT = "@llHHi"
EVENT_SIZE = struct.calcsize(EVENT)
SETUP = "=HHHH80sI"          # struct uinput_setup
ABS_SETUP = "=HH6i"          # struct uinput_abs_setup

# An absolute axis is mapped onto the whole screen by both X11 and Wayland, so
# the device range is a resolution-independent grid rather than a pixel count.
AXIS_MAX = 32767
DEVICE = "/dev/uinput"
SCREEN_SIZE = "/sys/class/graphics/fb0/virtual_size"


def to_axis(pixel: float, extent: int) -> int:
    """Convert a pixel coordinate on a screen `extent` wide into axis units."""
    if not isinstance(extent, int) or isinstance(extent, bool) or extent < 2:
        raise ValueError("Screen size must be at least 2 pixels.")
    position = round(float(pixel) / (extent - 1) * AXIS_MAX)
    return max(0, min(AXIS_MAX, position))


def to_pixel(axis: int, extent: int) -> int:
    if not isinstance(extent, int) or isinstance(extent, bool) or extent < 2:
        raise ValueError("Screen size must be at least 2 pixels.")
    return max(0, min(extent - 1, round(axis / AXIS_MAX * (extent - 1))))


def read_screen_size(path: str = SCREEN_SIZE) -> tuple[int, int] | None:
    """Read the framebuffer size, for when no accessibility bus reports one."""
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


def health(device: str = DEVICE) -> dict:
    """Describe whether this Pi can present a virtual pointer, without opening one."""
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


class VirtualPointer:
    """One virtual absolute pointing device, owned by an active control session.

    Not reusable after close(): the kernel device is destroyed, and a new
    session creates a new one so a stale handle cannot move the cursor.
    """

    def __init__(self, screen: tuple[int, int], device: str = DEVICE,
                 name: str = "PiperTV remote pointer", settle_s: float = 0.12):
        width, height = screen
        for extent in (width, height):
            if not isinstance(extent, int) or isinstance(extent, bool) or not 2 <= extent <= 65536:
                raise ValueError("Screen size must be between 2 and 65536 pixels.")
        if not isinstance(device, str) or not re.fullmatch(r"/dev/[a-z0-9_]{1,32}", device):
            raise ValueError("uinput device must look like /dev/uinput.")
        self.screen = (width, height)
        self.device = device
        self.name = name
        self.settle_s = settle_s
        self._lock = threading.RLock()
        self._fd: int | None = None
        self._closed = False
        self._created = False
        # Start at the centre: the real cursor has not moved yet, so any first
        # press would otherwise jump it from a position this layer never knew.
        self._x, self._y = AXIS_MAX // 2, AXIS_MAX // 2

    @property
    def position(self) -> tuple[int, int]:
        """The cursor position this device last commanded, in screen pixels."""
        with self._lock:
            return (to_pixel(self._x, self.screen[0]), to_pixel(self._y, self.screen[1]))

    def _ioctl(self, request: int, value) -> None:
        import fcntl

        fcntl.ioctl(self._fd, request, value)

    def __enter__(self) -> "VirtualPointer":
        return self.open()

    def open(self) -> "VirtualPointer":
        if os.name != "posix":
            raise OSError("A virtual pointer requires Linux on the Raspberry Pi.")
        with self._lock:
            if self._closed:
                raise RuntimeError("A closed virtual pointer cannot be reopened.")
            if self._fd is not None:
                raise RuntimeError("This virtual pointer is already open.")
            self._fd = os.open(self.device, os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            try:
                self._ioctl(UI_SET_EVBIT, EV_ABS)
                self._ioctl(UI_SET_EVBIT, EV_KEY)
                self._ioctl(UI_SET_EVBIT, EV_REL)
                self._ioctl(UI_SET_EVBIT, EV_SYN)
                self._ioctl(UI_SET_RELBIT, REL_WHEEL)
                for axis in (ABS_X, ABS_Y):
                    self._ioctl(UI_SET_ABSBIT, axis)
                    # value, minimum, maximum, fuzz, flat, resolution
                    self._ioctl(UI_ABS_SETUP, struct.pack(ABS_SETUP, axis, 0,
                                                          0, 0, AXIS_MAX, 0, 0, 0))
                for code in BUTTONS.values():
                    self._ioctl(UI_SET_KEYBIT, code)
                name = self.name.encode("ascii", "replace")[:79]
                self._ioctl(UI_DEV_SETUP, struct.pack(SETUP, BUS_VIRTUAL, 0x1209,
                                                      0x7011, 1, name, 0))
                self._ioctl(UI_DEV_CREATE, 0)
                self._created = True
                # udev has to notice the device before the session routes events.
                time.sleep(self.settle_s)
                # Opening must not move the pointer: the control gate can close
                # while udev is settling. The caller checks it again before an
                # explicit move or click. Until then position is only an origin
                # for the first remote movement, not a measured cursor position.
            except BaseException:
                self.close()
                raise
        return self

    def _write(self, events) -> None:
        if self._fd is None:
            raise RuntimeError("The virtual pointer is closed.")
        now = time.clock_gettime(time.CLOCK_MONOTONIC)
        seconds, microseconds = int(now), int(now % 1 * 1_000_000)
        payload = b"".join(
            struct.pack(EVENT, seconds, microseconds, kind, code, value)
            for kind, code, value in (*events, (EV_SYN, SYN_REPORT, 0)))
        written = os.write(self._fd, payload)
        if written != len(payload):
            raise OSError("The virtual pointer accepted only part of an event batch.")

    def move_to(self, x: int, y: int) -> tuple[int, int]:
        """Place the cursor at a screen pixel; Snapping jumps straight to a target."""
        with self._lock:
            self._x = to_axis(x, self.screen[0])
            self._y = to_axis(y, self.screen[1])
            self._write(((EV_ABS, ABS_X, self._x), (EV_ABS, ABS_Y, self._y)))
            return self.position

    def move_by(self, dx: int, dy: int) -> tuple[int, int]:
        """Nudge the cursor; Pointer mode repeats this while a direction is held."""
        with self._lock:
            x, y = self.position
            return self.move_to(x + dx, y + dy)

    def scroll(self, clicks: int) -> int:
        """Turn the wheel: positive scrolls up, negative down.

        Whatever is under the cursor scrolls, which is the point -- a page
        scrolls where it sits, without the cursor having to leave it for a
        scrollbar at the edge of the screen.
        """
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

    def __exit__(self, *_args) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._fd is None:
                return
            try:
                # Release anything held, so a closed session cannot leave a
                # button down on the desktop.
                for code in BUTTONS.values():
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
