"""Read raw infrared from the kernel's LIRC device in MODE2 format.

https://docs.kernel.org/userspace-api/media/rc/lirc-dev-intro.html

The TSOP2238 demodulates the carrier, so 38 kHz is assumed, not measured.
"""

from __future__ import annotations

import fcntl
import os
import select
import stat
import struct
import threading
import time
from dataclasses import asdict, dataclass
from typing import Callable

from .util import utc_now

SPACE = 0x00000000
PULSE = 0x01000000
FREQUENCY = 0x02000000
TIMEOUT = 0x03000000
OVERFLOW = 0x04000000
VALUE_MASK = 0x00FFFFFF
MODE_MASK = 0xFF000000
MAX_EDGES = 19_999

# ioctl numbers (asm-generic encoding, same on ARM and x86)
LIRC_GET_FEATURES = 0x80046900
LIRC_GET_MIN_TIMEOUT = 0x80046908
LIRC_GET_MAX_TIMEOUT = 0x80046909
LIRC_GET_REC_TIMEOUT = 0x80046924
LIRC_SET_REC_MODE = 0x40046912
LIRC_SET_REC_TIMEOUT = 0x40046918
LIRC_SET_REC_TIMEOUT_REPORTS = 0x40046919
LIRC_MODE_MODE2 = 0x00000004
LIRC_CAN_REC_MODE2 = 0x00040000
LIRC_CAN_SET_REC_TIMEOUT = 0x10000000

OPTION_LIMITS = {"timeout_s": (0.5, 120), "gap_us": (10_000, 1_000_000),
                 "max_duration_s": (0.1, 30)}


class CaptureError(RuntimeError):
    """The input cannot be saved as a complete capture."""


class CaptureCancelled(Exception):
    pass


class CaptureTimeout(Exception):
    pass


@dataclass(frozen=True)
class CaptureOptions:
    timeout_s: float = 10.0
    gap_us: int = 120_000
    max_duration_s: float = 3.0

    @classmethod
    def parse(cls, values: dict) -> CaptureOptions:
        if not isinstance(values, dict):
            raise ValueError("Capture options must be a JSON object.")
        unknown = set(values) - set(OPTION_LIMITS)
        if unknown:
            raise ValueError("Unknown capture option: " + ", ".join(sorted(unknown)))
        options = {**asdict(cls()), **values}
        for name, (low, high) in OPTION_LIMITS.items():
            value = options[name]
            # NaN and infinity fail the range check as well.
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not low <= value <= high:
                raise ValueError(f"{name} must be a number between {low} and {high}.")
        if int(options["gap_us"]) != options["gap_us"]:
            raise ValueError("gap_us must be a whole number of microseconds.")
        if options["max_duration_s"] <= options["gap_us"] / 1_000_000:
            raise ValueError("max_duration_s must be longer than the ending gap.")
        return cls(float(options["timeout_s"]), int(options["gap_us"]),
                   float(options["max_duration_s"]))


class Mode2Capture:
    """Incremental MODE2 parser; handles split reads and repeat frames."""

    def __init__(self, options: CaptureOptions):
        self.options = options
        self.durations: list[int] = []
        self.pending_space = 0
        self.total_us = 0
        self.complete = False
        self.started_at: float | None = None
        self.timeout_at: float | None = None
        self._timeout_spaces: int | None = None
        self._buffer = bytearray()

    def feed(self, data: bytes, now: float) -> None:
        self._buffer.extend(data)
        offset = 0
        while len(self._buffer) - offset >= 4 and not self.complete:
            word = struct.unpack_from("=I", self._buffer, offset)[0]
            offset += 4
            self.event(word, now)
        del self._buffer[:offset]

    def event(self, word: int, now: float) -> None:
        if self.complete:
            return
        kind, value = word & MODE_MASK, word & VALUE_MASK
        if kind == OVERFLOW:
            raise CaptureError("IR receiver overflow: data was lost. Please record again.")
        if kind == FREQUENCY:
            return  # the TSOP2238 cannot measure the carrier
        if kind not in (PULSE, SPACE, TIMEOUT):
            raise CaptureError(f"Unexpected LIRC event type 0x{kind:08x}.")
        if value == 0:
            return
        if kind == PULSE:
            self._pulse(value, now)
        elif self.durations:
            self._space(kind, value, now)

    def _pulse(self, value: int, now: float) -> None:
        if self.started_at is None:
            self.started_at = now
        if self.pending_space and self.durations:
            self.durations.extend((self.pending_space, value))
            self.total_us += self.pending_space + value
        elif self.durations:
            self.durations[-1] += value
            self.total_us += value
        else:
            self.durations.append(value)
            self.total_us = value
        self.pending_space = 0
        self.timeout_at = None
        self._timeout_spaces = None
        if len(self.durations) > MAX_EDGES:
            raise CaptureError("Too many IR edges; capture was not saved. Try a short press.")
        if self.total_us > self.options.max_duration_s * 1_000_000:
            raise CaptureError("IR signal exceeds max_duration_s; capture was not saved.")

    def _space(self, kind: int, value: int, now: float) -> None:
        if kind == TIMEOUT:
            self.pending_space += value
            self.timeout_at = now
            self._timeout_spaces = 0
        elif self._timeout_spaces == 1:
            # After a timeout lirc_dev inserts a synthetic space, then gpio-ir
            # reports the real edge-to-edge space. Use the real one only, or
            # the gap between repeat frames is counted twice.
            self.pending_space = value
            self._timeout_spaces = 2
            self.timeout_at = None
        else:
            self.pending_space += value
            if self._timeout_spaces is not None:
                self._timeout_spaces += 1
            self.timeout_at = None
        if self.pending_space >= self.options.gap_us:
            self.complete = True

    def finish_idle(self, now: float) -> bool:
        # Only a kernel timeout proves the receiver is idle; no data at all
        # could just as well mean a stalled device. gpio-ir delivers edges in
        # ~15 ms batches, so allow 30 ms for a late repeat frame.
        if (self.timeout_at is not None and not self._buffer
                and now - self.timeout_at >=
                max(0, self.options.gap_us - self.pending_space) / 1_000_000 + 0.03):
            self.pending_space = max(self.pending_space, self.options.gap_us)
            self.complete = True
        return self.complete

    def result(self, source: str = "lirc") -> dict:
        if not self.complete or not self.durations:
            raise CaptureError("IR signal has not ended; an incomplete capture cannot be saved.")
        if len(self.durations) < 3:
            raise CaptureError("Only one IR pulse was received. Try recording the remote button again.")
        return {"durations_us": self.durations.copy(), "carrier_hz": 38_000,
                "carrier_source": "assumed", "trailing_gap_us": self.pending_space,
                "captured_at": utc_now(), "source": source}


class LircDevice:
    source = "lirc"

    def __init__(self, path: str, gap_us: int):
        self.path = path
        self.gap_us = gap_us
        self.fd: int | None = None
        self._previous_timeout: int | None = None
        self._configured_timeout: int | None = None

    def _ioctl(self, request: int, value: int = 0) -> int:
        buffer = bytearray(struct.pack("=I", value))
        fcntl.ioctl(self.fd, request, buffer, True)
        return struct.unpack("=I", buffer)[0]

    def __enter__(self) -> LircDevice:
        try:
            self.fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            if not stat.S_ISCHR(os.fstat(self.fd).st_mode):
                raise CaptureError(f"{self.path} is not a character device.")
            features = self._ioctl(LIRC_GET_FEATURES)
            if not features & LIRC_CAN_REC_MODE2:
                raise CaptureError(f"{self.path} does not support raw IR; select a gpio-ir receiver.")
            self._ioctl(LIRC_SET_REC_MODE, LIRC_MODE_MODE2)
            self._ioctl(LIRC_SET_REC_TIMEOUT_REPORTS, 1)
            if features & LIRC_CAN_SET_REC_TIMEOUT:
                lower = self._ioctl(LIRC_GET_MIN_TIMEOUT)
                upper = self._ioctl(LIRC_GET_MAX_TIMEOUT)
                self._previous_timeout = self._ioctl(LIRC_GET_REC_TIMEOUT)
                self._ioctl(LIRC_SET_REC_TIMEOUT, min(upper, max(lower, self.gap_us)))
                self._configured_timeout = self._ioctl(LIRC_GET_REC_TIMEOUT)
            # Throw away whatever arrived while we were configuring.
            for _ in range(64):
                try:
                    chunk = os.read(self.fd, 4096)
                except BlockingIOError:
                    break
                if not chunk:
                    raise CaptureError("IR receiver disconnected while arming.")
            else:
                raise CaptureError("IR input is already busy. Release the remote and try again.")
            return self
        except BaseException:
            self.__exit__()
            raise

    def read(self, timeout: float) -> bytes | None:
        readable, _, _ = select.select([self.fd], [], [], timeout)
        if not readable:
            return None
        try:
            data = os.read(self.fd, 4096)
        except BlockingIOError:
            return None
        if not data:
            raise CaptureError("IR receiver disconnected during capture.")
        return data

    def __exit__(self, *_args) -> None:
        if self.fd is None:
            return
        try:
            # Put the timeout back, unless someone else changed it meanwhile.
            if (self._previous_timeout is not None and self._configured_timeout is not None
                    and self._ioctl(LIRC_GET_REC_TIMEOUT) == self._configured_timeout):
                self._ioctl(LIRC_SET_REC_TIMEOUT, self._previous_timeout)
        except OSError:
            pass
        finally:
            os.close(self.fd)
            self.fd = None


def capture_stream(device, options: CaptureOptions, cancelled: threading.Event,
                   clock: Callable[[], float] = time.monotonic) -> dict:
    """Read one button press from an open device (anything with read(timeout))."""
    parser = Mode2Capture(options)
    armed_at = clock()
    while True:
        if cancelled.is_set():
            raise CaptureCancelled()
        data = device.read(0.025)
        now = clock()
        if cancelled.is_set():
            raise CaptureCancelled()
        if data is not None:
            if not data:
                raise CaptureError("IR receiver closed or returned an incomplete stream.")
            parser.feed(data, now)
        # Drain queued events first; only judge silence when a read got nothing.
        if parser.complete or (data is None and parser.finish_idle(now)):
            return parser.result(getattr(device, "source", "lirc"))
        if parser.started_at is None:
            if now - armed_at >= options.timeout_s:
                raise CaptureTimeout()
        elif now - parser.started_at >= options.max_duration_s:
            raise CaptureError("IR did not end before max_duration_s; capture was not saved. "
                               "Release the button sooner or increase the duration limit.")


def demo_signal(gap_us: int = 120_000) -> dict:
    """A made-up NEC frame plus one repeat, for demo mode."""
    durations = [9000, 4500]
    for bit in range(32):
        durations.extend((560, 1690 if (0x20DF10EF >> bit) & 1 else 560))
    durations.extend((560, 40_000, 9000, 2250, 560))
    return {"durations_us": durations, "carrier_hz": 38_000,
            "carrier_source": "assumed", "trailing_gap_us": gap_us,
            "captured_at": utc_now(), "source": "demo"}
