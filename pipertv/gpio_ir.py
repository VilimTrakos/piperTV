"""Read an IR receiver wired to any GPIO pin, chosen while Piper runs.

The kernel's own receiver -- the gpio-ir overlay behind /dev/lirc0 -- is bound
to one pin in /boot/firmware/config.txt, and moving it takes root and a
restart. Linux's GPIO character device lets an ordinary user in the gpio group
watch any free line instead, and it timestamps every edge in the interrupt
itself: a pulse measured here is as exact as the kernel's receiver measures
it, however late Python gets round to reading it.

The edges are turned into the MODE2 words /dev/lirc0 produces, so the learner
and the remote's recogniser read either source without knowing which. Both
are kept: the kernel receiver is what a Pi set up by the book already has,
and a pin it holds cannot be watched from here at all.

API: https://docs.kernel.org/userspace-api/gpio/chardev.html (uAPI v2)
"""

from __future__ import annotations

import os
import select
import stat
import struct
import time

from .lirc import (OVERFLOW, PULSE, SPACE, TIMEOUT, VALUE_MASK, CaptureError,
                   LircDevice)

CHIP = "/dev/gpiochip0"
LIRC = "/dev/lirc0"
CONSUMER = b"piper-ir"

# linux/gpio.h, uAPI v2, with the asm-generic ioctl encoding the Pi uses.
GPIO_GET_CHIPINFO_IOCTL = 0x8044B401
GPIO_V2_GET_LINEINFO_IOCTL = 0xC100B405
GPIO_V2_GET_LINE_IOCTL = 0xC250B407
CHIP_INFO = struct.Struct("=32s32sI")                    # 68 bytes
LINE_INFO = struct.Struct("=32s32sIIQ160s16s")           # 256 bytes
LINE_REQUEST = struct.Struct("=64I32sQI20s240sII20si")   # 592 bytes
LINE_EVENT = struct.Struct("=QIIII24s")                  # 48 bytes
FLAG_USED = 1 << 0
FLAG_INPUT = 1 << 2
FLAG_EDGE_RISING = 1 << 4
FLAG_EDGE_FALLING = 1 << 5
FLAG_BIAS_PULL_UP = 1 << 8
EVENT_RISING = 1
EVENT_FALLING = 2
# The kernel keeps up to this many edges for a slow reader. One long press of
# a remote is a few hundred; a full buffer is reported rather than guessed at.
EVENT_BUFFER = 1024

# Where each GPIO comes out on the Pi's 40-pin header, so a choice can be
# wired without a diagram. Only these are offered: the rest of the chip's
# lines are inside the board.
HEADER = {2: 3, 3: 5, 4: 7, 5: 29, 6: 31, 7: 26, 8: 24, 9: 21, 10: 19, 11: 23,
          12: 32, 13: 33, 14: 8, 15: 10, 16: 36, 17: 11, 18: 12, 19: 35, 20: 38,
          21: 40, 22: 15, 23: 16, 24: 18, 25: 22, 26: 37, 27: 13}

# One question matters to whoever wires the receiver: which pin its OUT is on.
# "auto" answers it for them -- the kernel's receiver when config.txt sets one
# up, and otherwise GPIO17, the pin the wiring guide uses.
AUTO = "auto"
DEFAULT_PIN = 17
# What the GPIO chip calls a line lent to the kernel's receiver: the gpio-ir
# overlay's device-tree node, "ir-receiver@11" for GPIO17.
KERNEL_RECEIVER = "ir-receiver"


def validate_pin(value) -> str | int:
    """A receiver pin as a person might write it: auto, 18, "18" or "GPIO18"."""
    if isinstance(value, str):
        text = value.strip().lower()
        if text == AUTO:
            return AUTO
        text = text[4:] if text.startswith("gpio") else text
        value = int(text) if text.isdigit() else value
    if isinstance(value, bool) or not isinstance(value, int) or value not in HEADER:
        raise ValueError(f"The IR pin is auto or a GPIO number on the header, "
                         f"{min(HEADER)} to {max(HEADER)}; {value!r} is neither.")
    return value


def kernel_line(lines) -> dict | None:
    """The pin the kernel's own receiver holds, if config.txt set one up."""
    return next((line for line in lines
                 if (line.get("consumer") or "").startswith(KERNEL_RECEIVER)), None)


def resolve(pin, lines, lirc: str = LIRC, exists=os.path.exists):
    """How to read the pin the receiver is on: through the kernel, or here.

    A pin the kernel's receiver already holds cannot be watched from here,
    and has no need to be: the kernel is reading that very wire, so asking it
    is the same thing. Every other pin is read directly.
    """
    held = kernel_line(lines)
    if pin == AUTO:
        if held is not None or exists(lirc):
            return lirc
        return {"kind": "gpio", "pin": DEFAULT_PIN}
    if held is not None and held["gpio"] == pin:
        return lirc
    return {"kind": "gpio", "pin": pin}


def describe(settings) -> str:
    """How a receiver is named to a person: which pin, not which API."""
    if isinstance(settings, dict) and settings.get("kind") == "gpio":
        return f"GPIO{settings['pin']} (pin {HEADER.get(settings['pin'], '?')})"
    return f"the kernel receiver ({settings if isinstance(settings, str) else LIRC})"


def open_receiver(settings, gap_us: int):
    """The device to read MODE2 from, for a receiver choice or a LIRC path."""
    if isinstance(settings, str):
        return LircDevice(settings, gap_us)
    if isinstance(settings, dict) and settings.get("kind") == "gpio":
        return GpioIrDevice(CHIP, settings["pin"], gap_us)
    return LircDevice(LIRC, gap_us)


def _text(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("utf-8", "replace")


def list_lines(chip: str = CHIP) -> list[dict]:
    """The header's GPIO lines: which are free, and what holds the others."""
    import fcntl

    fd = os.open(chip, os.O_RDONLY | os.O_CLOEXEC)
    try:
        info = bytearray(CHIP_INFO.size)
        fcntl.ioctl(fd, GPIO_GET_CHIPINFO_IOCTL, info, True)
        count = CHIP_INFO.unpack(info)[2]
        lines = []
        for offset in sorted(HEADER):
            if offset >= count:
                continue
            buffer = bytearray(LINE_INFO.pack(b"", b"", offset, 0, 0, b"", b""))
            fcntl.ioctl(fd, GPIO_V2_GET_LINEINFO_IOCTL, buffer, True)
            name, consumer, _offset, _attrs, flags, _values, _pad = LINE_INFO.unpack(buffer)
            lines.append({"gpio": offset, "header_pin": HEADER[offset],
                          "name": _text(name) or f"GPIO{offset}",
                          "used": bool(flags & FLAG_USED),
                          "consumer": _text(consumer) or None})
        return lines
    finally:
        os.close(fd)


class EdgeTimeline:
    """Turn timestamped edges of a receiver's output into MODE2 words.

    A demodulating receiver idles high and pulls its output low for as long
    as it sees the carrier, so a falling edge starts a pulse and a rising edge
    ends it. The first edge after a quiet spell only marks a start. Quiet for
    the configured gap is reported once, as the kernel's timeout is.
    """

    def __init__(self, gap_us: int):
        self.gap_ns = int(gap_us) * 1000
        self.last_ns: int | None = None
        self.last_rising = True
        self.quiet = True

    def edge(self, timestamp_ns: int, rising: bool) -> list[int]:
        words = []
        if not self.quiet and self.last_ns is not None:
            duration = max(1, min(VALUE_MASK, (timestamp_ns - self.last_ns) // 1000))
            words.append((PULSE if rising else SPACE) | duration)
        self.last_ns, self.last_rising, self.quiet = timestamp_ns, rising, False
        return words

    def idle(self, now_ns: int) -> list[int]:
        # A line left low is a receiver still seeing carrier, not a quiet one.
        if self.quiet or self.last_ns is None or not self.last_rising:
            return []
        waited = now_ns - self.last_ns
        if waited < self.gap_ns:
            return []
        self.quiet = True
        return [TIMEOUT | min(VALUE_MASK, waited // 1000)]


class GpioIrDevice:
    """One GPIO line read as an IR receiver, in LircDevice's shape."""

    source = "gpio"

    def __init__(self, chip: str, pin: int, gap_us: int, clock_ns=time.monotonic_ns):
        self.chip, self.pin = chip, int(pin)
        self.timeline = EdgeTimeline(gap_us)
        self.clock_ns = clock_ns
        self.fd: int | None = None
        self._seqno = 0
        self._buffer = bytearray()

    def _request(self, chip_fd: int, flags: int) -> int:
        import fcntl

        offsets = [self.pin] + [0] * 63
        request = bytearray(LINE_REQUEST.pack(
            *offsets, CONSUMER, flags, 0, b"", b"", 1, EVENT_BUFFER, b"", 0))
        fcntl.ioctl(chip_fd, GPIO_V2_GET_LINE_IOCTL, request, True)
        return LINE_REQUEST.unpack(request)[-1]

    def __enter__(self) -> "GpioIrDevice":
        if os.name != "posix":
            raise CaptureError("Hardware reception requires Linux on the Raspberry Pi.")
        try:
            chip_fd = os.open(self.chip, os.O_RDONLY | os.O_CLOEXEC)
        except PermissionError as exc:
            raise CaptureError(f"{self.chip} is not readable; add this user to the gpio "
                               "group.") from exc
        except FileNotFoundError as exc:
            raise CaptureError(f"{self.chip} does not exist on this machine.") from exc
        try:
            if not stat.S_ISCHR(os.fstat(chip_fd).st_mode):
                raise CaptureError(f"{self.chip} is not a character device.")
            edges = FLAG_INPUT | FLAG_EDGE_RISING | FLAG_EDGE_FALLING
            try:
                # Most receivers have a pull-up of their own; one that does not
                # floats without this, and a floating line is noise.
                self.fd = self._request(chip_fd, edges | FLAG_BIAS_PULL_UP)
            except OSError as exc:
                if exc.errno == 16:  # EBUSY: something else holds the line
                    raise CaptureError(f"GPIO{self.pin} is in use by another driver; "
                                       "choose a free pin.") from exc
                self.fd = self._request(chip_fd, edges)
        finally:
            os.close(chip_fd)
        os.set_blocking(self.fd, False)
        return self

    def read(self, timeout: float) -> bytes | None:
        readable, _, _ = select.select([self.fd], [], [], timeout)
        words: list[int] = []
        if readable:
            try:
                data = os.read(self.fd, LINE_EVENT.size * 64)
            except BlockingIOError:
                data = None
            if data == b"":
                raise CaptureError(f"GPIO{self.pin} stopped delivering edges.")
            if data:
                words = self._events(data)
        if not words:
            words = self.timeline.idle(self.clock_ns())
        return b"".join(struct.pack("=I", word) for word in words) or None

    def _events(self, data: bytes) -> list[int]:
        self._buffer.extend(data)
        words = []
        while len(self._buffer) >= LINE_EVENT.size:
            timestamp, kind, _offset, seqno, _line_seqno, _pad = LINE_EVENT.unpack_from(self._buffer)
            del self._buffer[:LINE_EVENT.size]
            if self._seqno and seqno != self._seqno + 1:
                # The kernel's buffer filled and edges were dropped: the timing
                # after this point cannot be trusted, and saying so is better.
                words.append(OVERFLOW)
            self._seqno = seqno
            words.extend(self.timeline.edge(timestamp, kind == EVENT_RISING))
        return words

    def __exit__(self, *_args) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
