"""Read an IR receiver on any GPIO pin through the GPIO character device.

The kernel's gpio-ir overlay (/dev/lirc0) is fixed to one pin in config.txt
and needs root and a reboot to move. The GPIO chardev lets a user in the gpio
group watch any free line, and the kernel timestamps each edge in the
interrupt handler, so timing is as good as gpio-ir's. Edges are converted to
the same MODE2 words /dev/lirc0 produces, so the rest of Piper can read
either source.

https://docs.kernel.org/userspace-api/gpio/chardev.html (uAPI v2)
"""

from __future__ import annotations

import errno
import fcntl
import os
import select
import stat
import struct
import time

from .lirc import OVERFLOW, PULSE, SPACE, TIMEOUT, VALUE_MASK, CaptureError, LircDevice

CHIP = "/dev/gpiochip0"
LIRC = "/dev/lirc0"
CONSUMER = "piper-ir"

# linux/gpio.h, uAPI v2
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
# Kernel-side event queue. A long press is a few hundred edges.
EVENT_BUFFER = 1024

# GPIO number -> physical pin on the 40-pin header.
HEADER = {2: 3, 3: 5, 4: 7, 5: 29, 6: 31, 7: 26, 8: 24, 9: 21, 10: 19, 11: 23,
          12: 32, 13: 33, 14: 8, 15: 10, 16: 36, 17: 11, 18: 12, 19: 35, 20: 38,
          21: 40, 22: 15, 23: 16, 24: 18, 25: 22, 26: 37, 27: 13}
# Pins that have an alternate function. They work as an IR input while that
# function is off, but a plain pin is the safer choice.
FUNCTIONS = {2: ("SDA", "I2C"), 3: ("SCL", "I2C"),
             4: ("GPCLK0", "1-Wire and the clock output"),
             7: ("CE1", "SPI"), 8: ("CE0", "SPI"), 9: ("MISO", "SPI"),
             10: ("MOSI", "SPI"), 11: ("SCLK", "SPI"),
             12: ("PWM0", "PWM audio and fans"), 13: ("PWM1", "PWM audio and fans"),
             14: ("TXD", "the serial port"), 15: ("RXD", "the serial port"),
             18: ("PCM_CLK", "I2S audio, such as a sound card hat"),
             19: ("PCM_FS", "I2S audio, such as a sound card hat"),
             20: ("PCM_DIN", "I2S audio, such as a sound card hat"),
             21: ("PCM_DOUT", "I2S audio, such as a sound card hat")}

# "auto": the kernel receiver if config.txt sets one up, otherwise GPIO17
# (the pin used in the wiring guide).
AUTO = "auto"
DEFAULT_PIN = 17
# Consumer name of a line held by the gpio-ir overlay, e.g. "ir-receiver@11".
KERNEL_RECEIVER = "ir-receiver"


def validate_pin(value) -> str | int:
    """Accepts auto, 18, "18" or "GPIO18"."""
    if isinstance(value, str):
        text = value.strip().lower()
        if text == AUTO:
            return AUTO
        text = text.removeprefix("gpio")
        value = int(text) if text.isdigit() else value
    if isinstance(value, bool) or not isinstance(value, int) or value not in HEADER:
        raise ValueError(f"The IR pin is auto or a GPIO number on the header, "
                         f"{min(HEADER)} to {max(HEADER)}; {value!r} is neither.")
    return value


def kernel_line(lines) -> dict | None:
    """The line held by the kernel's IR receiver, if there is one."""
    return next((line for line in lines
                 if (line.get("consumer") or "").startswith(KERNEL_RECEIVER)), None)


def resolve(pin, lines, lirc: str = LIRC, exists=os.path.exists):
    """What to open for a pin setting: the LIRC path or {"kind": "gpio", "pin": n}.

    A pin the kernel receiver already holds can't be requested again, so it
    is read through /dev/lirc0 instead.
    """
    held = kernel_line(lines)
    if pin == AUTO:
        if held is not None or exists(lirc):
            return lirc
        return {"kind": "gpio", "pin": DEFAULT_PIN}
    if held is not None and held["gpio"] == pin:
        return lirc
    return {"kind": "gpio", "pin": pin}


def is_gpio(receiver) -> bool:
    return isinstance(receiver, dict) and receiver.get("kind") == "gpio"


def describe(receiver) -> str:
    if is_gpio(receiver):
        return f"GPIO{receiver['pin']} (pin {HEADER.get(receiver['pin'], '?')})"
    return f"the kernel receiver ({receiver if isinstance(receiver, str) else LIRC})"


def open_receiver(receiver, gap_us: int):
    if isinstance(receiver, str):
        return LircDevice(receiver, gap_us)
    if is_gpio(receiver):
        return GpioIrDevice(CHIP, receiver["pin"], gap_us)
    return LircDevice(LIRC, gap_us)


def _text(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("utf-8", "replace")


def list_lines(chip: str = CHIP) -> list[dict]:
    """The header's GPIO lines, whether each is in use, and by whom."""
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
            function, purpose = FUNCTIONS.get(offset, (None, None))
            lines.append({"gpio": offset, "header_pin": HEADER[offset],
                          "name": _text(name) or f"GPIO{offset}",
                          "used": bool(flags & FLAG_USED),
                          "consumer": _text(consumer) or None,
                          "function": function, "purpose": purpose})
        return lines
    finally:
        os.close(fd)


class EdgeTimeline:
    """Turn timestamped edges into MODE2 words.

    The receiver's output idles high and goes low while it sees the carrier,
    so a falling edge starts a pulse and a rising edge ends it. The first edge
    after a quiet period only marks the start. Silence longer than the gap is
    reported once as a TIMEOUT, like the kernel does.
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
        # A line left low means the carrier is still there.
        if self.quiet or self.last_ns is None or not self.last_rising:
            return []
        waited = now_ns - self.last_ns
        if waited < self.gap_ns:
            return []
        self.quiet = True
        return [TIMEOUT | min(VALUE_MASK, waited // 1000)]


class GpioIrDevice:
    """A GPIO line read as an IR receiver, with the same interface as LircDevice."""

    source = "gpio"

    def __init__(self, chip: str, pin: int, gap_us: int, clock_ns=time.monotonic_ns):
        self.chip, self.pin = chip, int(pin)
        self.timeline = EdgeTimeline(gap_us)
        self.clock_ns = clock_ns
        self.fd: int | None = None
        self._seqno = 0
        self._buffer = bytearray()

    def _request(self, chip_fd: int, flags: int) -> int:
        offsets = [self.pin] + [0] * 63
        request = bytearray(LINE_REQUEST.pack(
            *offsets, CONSUMER.encode(), flags, 0, b"", b"", 1, EVENT_BUFFER, b"", 0))
        fcntl.ioctl(chip_fd, GPIO_V2_GET_LINE_IOCTL, request, True)
        return LINE_REQUEST.unpack(request)[-1]

    def __enter__(self) -> GpioIrDevice:
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
            flags = FLAG_INPUT | FLAG_EDGE_RISING | FLAG_EDGE_FALLING
            try:
                # Most receivers have their own pull-up; this covers those that don't.
                self.fd = self._request(chip_fd, flags | FLAG_BIAS_PULL_UP)
            except OSError as exc:
                if exc.errno == errno.EBUSY:
                    raise CaptureError(f"GPIO{self.pin} is in use by another driver; "
                                       "choose a free pin.") from exc
                self.fd = self._request(chip_fd, flags)
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
            timestamp, kind, _offset, seqno, _line_seqno, _pad = \
                LINE_EVENT.unpack_from(self._buffer)
            del self._buffer[:LINE_EVENT.size]
            if self._seqno and seqno != self._seqno + 1:
                # The kernel queue overflowed and edges were lost.
                words.append(OVERFLOW)
            self._seqno = seqno
            words.extend(self.timeline.edge(timestamp, kind == EVENT_RISING))
        return words

    def __exit__(self, *_args) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
