"""Virtual input devices through /dev/uinput.

uinput is a kernel interface, so this works the same under X11 and Wayland.
https://docs.kernel.org/input/uinput.html
https://docs.kernel.org/input/event-codes.html
"""

from __future__ import annotations

import fcntl
import os
import re
import struct
import threading
import time

# ioctl numbers (asm-generic). The struct sizes they encode are the same on
# 32- and 64-bit, so they are valid on the Pi and on x86 alike.
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
UI_DEV_SETUP = 0x405C5503
UI_ABS_SETUP = 0x401C5504
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_RELBIT = 0x40045566
UI_SET_ABSBIT = 0x40045567

EV_SYN, EV_KEY, EV_REL, EV_ABS = 0x00, 0x01, 0x02, 0x03
SYN_REPORT = 0
BUS_VIRTUAL = 0x06
VENDOR_ID = 0x1209  # pid.codes

# struct input_event holds two longs: 16 bytes on 32-bit ARM, 24 on 64-bit.
EVENT = "@llHHi"
EVENT_SIZE = struct.calcsize(EVENT)
SETUP = "=HHHH80sI"   # struct uinput_setup
ABS_SETUP = "=HH6i"   # struct uinput_abs_setup
DEVICE = "/dev/uinput"


class UinputDevice:
    """Base class: subclasses declare their events in _declare().

    A closed device can't be reopened. The kernel device is destroyed on
    close, so an old handle can never send input again.
    """

    kind = "device"   # used in messages
    product_id = 0

    def __init__(self, device: str = DEVICE, name: str = "PiperTV", settle_s: float = 0.12):
        if not isinstance(device, str) or not re.fullmatch(r"/dev/[a-z0-9_]{1,32}", device):
            raise ValueError("uinput device must look like /dev/uinput.")
        self.device = device
        self.name = name
        self.settle_s = settle_s
        self._lock = threading.RLock()
        self._fd: int | None = None
        self._created = False
        self._closed = False

    def _declare(self) -> None:
        raise NotImplementedError

    def _held_codes(self):
        """Keys/buttons to release when the device closes."""
        return ()

    def _ioctl(self, request: int, value) -> None:
        fcntl.ioctl(self._fd, request, value)

    def open(self):
        with self._lock:
            if self._closed:
                raise RuntimeError(f"A closed virtual {self.kind} cannot be reopened.")
            if self._fd is not None:
                raise RuntimeError(f"This virtual {self.kind} is already open.")
            self._fd = os.open(self.device, os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            try:
                self._declare()
                name = self.name.encode("ascii", "replace")[:79]
                self._ioctl(UI_DEV_SETUP, struct.pack(SETUP, BUS_VIRTUAL, VENDOR_ID,
                                                      self.product_id, 1, name, 0))
                self._ioctl(UI_DEV_CREATE, 0)
                self._created = True
                # udev needs a moment to set the device up; events sent before
                # that are lost.
                time.sleep(self.settle_s)
            except BaseException:
                self.close()
                raise
        return self

    def __enter__(self):
        return self.open()

    def __exit__(self, *_args) -> None:
        self.close()

    def _write(self, events) -> None:
        if self._fd is None:
            raise RuntimeError(f"The virtual {self.kind} is closed.")
        now = time.clock_gettime(time.CLOCK_MONOTONIC)
        seconds, microseconds = int(now), int(now % 1 * 1_000_000)
        payload = b"".join(struct.pack(EVENT, seconds, microseconds, kind, code, value)
                           for kind, code, value in (*events, (EV_SYN, SYN_REPORT, 0)))
        if os.write(self._fd, payload) != len(payload):
            raise OSError(f"The virtual {self.kind} accepted only part of an event batch.")

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._fd is None:
                return
            try:
                # A key left pressed would keep repeating on the desktop.
                for code in self._held_codes():
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
