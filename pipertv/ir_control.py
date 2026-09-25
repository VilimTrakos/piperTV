"""Recognise learned remote buttons in the live IR stream.

RC5 frames are decoded properly (address, command and toggle bit). Anything
else is matched against every learned sample by comparing the timings.
Protocols: https://docs.kernel.org/userspace-api/media/rc/rc-protos.html
"""

from __future__ import annotations

import logging
import statistics
import struct
import threading
import time
from dataclasses import dataclass

from .gpio_ir import describe, open_receiver
from .lirc import CaptureError, CaptureOptions, Mode2Capture
from .roles import DIRECTIONS

LOG = logging.getLogger(__name__)
FRAME_GAP_US = 10_000


def split_frames(durations, gap_us=FRAME_GAP_US):
    """One learned press may hold several frames and repeat codes; yield each."""
    start = 0
    for index in range(1, len(durations), 2):
        if durations[index] >= gap_us:
            if index - start >= 3:
                yield tuple(durations[start:index])
            start = index + 1
    if len(durations) - start >= 3:
        yield tuple(durations[start:])


def decode_rc5(frame):
    """((address, command), toggle) for a valid 14-bit RC5 frame, else None.

    The receiver drops the idle half-bit at each end. Every mark/space must
    be one or two half-bit units long, and the result must be valid
    Manchester code.
    """
    if not 13 <= len(frame) <= 27 or len(frame) % 2 == 0:
        return None
    short = [value for value in frame if 550 <= value <= 1150]
    # Alternating bits can merge almost every half-bit into double-length
    # intervals, so there may be no short interval at all.
    unit = statistics.median(short) if short else statistics.median(frame) / 2
    if not 680 <= unit <= 1050:
        return None
    halves = [0]
    for index, duration in enumerate(frame):
        count = round(duration / unit)
        if count not in (1, 2) or abs(duration - count * unit) > count * unit * .25:
            return None
        halves.extend([1 - index % 2] * count)
    if len(halves) == 27:
        halves.append(0)
    if len(halves) != 28:
        return None
    bits = []
    for index in range(0, 28, 2):
        pair = halves[index:index + 2]
        if pair == [0, 1]:
            bits.append(1)
        elif pair == [1, 0]:
            bits.append(0)
        else:
            return None
    if bits[0] != 1:
        return None
    address = int("".join(map(str, bits[3:8])), 2)
    command = int("".join(map(str, bits[8:14])), 2) + (0 if bits[1] else 64)
    return (address, command), bits[2]


def timing_distance(frame, reference):
    """Mean relative error after correcting for a small clock difference, or None."""
    if len(frame) != len(reference) or len(frame) < 7:
        return None
    scale = statistics.median(a / b for a, b in zip(frame, reference))
    if not .75 <= scale <= 1.30:
        return None
    errors = [abs(a - b * scale) / (b * scale) for a, b in zip(frame, reference)]
    if max(errors) > .28 or statistics.mean(errors) > .12:
        return None
    return statistics.mean(errors)


def is_nec_repeat(frame):
    return (len(frame) == 3 and 7000 <= frame[0] <= 11000
            and 1700 <= frame[1] <= 2900 and 350 <= frame[2] <= 800)


def is_nec_frame(frame):
    return len(frame) == 67 and 7000 <= frame[0] <= 11000 and 3300 <= frame[1] <= 5700


@dataclass(frozen=True)
class Match:
    button_id: str | None
    protocol: str
    toggle: int | None = None
    repeat: bool = False


class SignalMatcher:
    def __init__(self, document):
        self.templates = []
        self.rc5 = {}
        for button_id, record in document.get("recordings", {}).items():
            for sample in record.get("samples", []):
                if sample.get("source") not in ("lirc", "gpio"):
                    continue  # demo recordings must never drive the desktop
                for frame in split_frames(sample.get("durations_us", [])):
                    decoded = decode_rc5(frame)
                    if decoded:
                        self.rc5.setdefault(decoded[0], set()).add(button_id)
                    elif len(frame) >= 7:
                        self.templates.append((button_id, frame))

    def match(self, frame):
        if is_nec_repeat(frame):
            return Match(None, "nec", repeat=True)
        decoded = decode_rc5(frame)
        if decoded:
            buttons = self.rc5.get(decoded[0], set())
            if len(buttons) == 1:
                return Match(next(iter(buttons)), "rc5", decoded[1])
            return None
        candidates = {}
        for button_id, reference in self.templates:
            score = timing_distance(frame, reference)
            if score is not None:
                candidates[button_id] = min(score, candidates.get(button_id, 1))
        ranked = sorted(candidates.items(), key=lambda item: item[1])
        if not ranked:
            return None
        if len(ranked) > 1 and ranked[1][1] - ranked[0][1] < .065:
            return None  # too close to call between two buttons
        return Match(ranked[0][0], "nec" if is_nec_frame(frame) else "raw")

    @property
    def button_count(self):
        return len({button for buttons in self.rc5.values() for button in buttons}
                   | {button for button, _frame in self.templates})


class RepeatFilter:
    """Turn a stream of frames into presses; held directions repeat.

    is_direction(button) decides what repeats (by the role a button performs,
    so a play key bound to "up" repeats too). pace(button) may return
    (delay_s, interval_s) to slow the repeat down, e.g. when each press moves
    to the next item rather than nudging a cursor.
    """

    def __init__(self, release_s=.28, delay_s=.32, interval_s=.09, is_direction=None,
                 pace=None):
        self.release_s, self.delay_s, self.interval_s = release_s, delay_s, interval_s
        self.is_direction = is_direction or (lambda button: button in DIRECTIONS)
        self.pace = pace
        self.reset()

    def reset(self):
        self.last = None
        self.last_at = self.started_at = self.emitted_at = -float("inf")

    def accept(self, match, now):
        if match is None:
            self.reset()
            return None
        if match.repeat:
            if (not self.last or self.last.protocol != match.protocol
                    or now - self.last_at > self.release_s):
                self.reset()
                return None
            match = self.last
        fresh = (self.last is None or match.button_id != self.last.button_id
                 or match.toggle != self.last.toggle or now - self.last_at > self.release_s)
        self.last, self.last_at = match, now
        if fresh:
            self.started_at = self.emitted_at = now
            return match.button_id
        delay_s, interval_s = self.delay_s, self.interval_s
        if self.pace is not None:
            chosen = self.pace(match.button_id)
            if chosen is not None:
                delay_s, interval_s = chosen
        if (self.is_direction(match.button_id) and now - self.started_at >= delay_s
                and now - self.emitted_at >= interval_s):
            self.emitted_at = now
            return match.button_id
        return None


class FrameReader:
    """Split a MODE2 stream into frames, even when one read holds several."""

    def __init__(self):
        self.buffer = bytearray()
        self.options = CaptureOptions(timeout_s=120, gap_us=FRAME_GAP_US, max_duration_s=.5)
        self.parser = Mode2Capture(self.options)

    def feed(self, data, now):
        self.buffer.extend(data)
        frames = []
        while len(self.buffer) >= 4:
            word = struct.unpack_from("=I", self.buffer)[0]
            del self.buffer[:4]
            try:
                self.parser.event(word, now)
            except CaptureError:
                self.parser = Mode2Capture(self.options)
                frames.append(None)  # None also breaks any repeat in progress
                continue
            if self.parser.complete:
                frames.append(tuple(self.parser.durations) if len(self.parser.durations) >= 3 else None)
                self.parser = Mode2Capture(self.options)
        return frames

    def idle(self, now):
        if self.buffer:
            return []
        if self.parser.finish_idle(now):
            frame = tuple(self.parser.durations)
            self.parser = Mode2Capture(self.options)
            return [frame if len(frame) >= 3 else None]
        if self.parser.started_at is not None and now - self.parser.started_at > .6:
            self.parser = Mode2Capture(self.options)
            return [None]
        return []


class IRController:
    """Reads the receiver on a thread of its own and reports recognised buttons.

    It reads only while resumed and while enabled() is true. pause() returns
    once the device is closed, so a recording can open it.
    """

    def __init__(self, store, on_button, enabled, device="/dev/lirc0",
                 device_factory=open_receiver, clock=time.monotonic, is_direction=None,
                 pace=None):
        self.store = store
        self.on_button = on_button
        self.enabled = enabled
        self.device = device
        self.device_factory = device_factory
        self.clock = clock
        self.matcher = SignalMatcher(self.store.snapshot())
        self.repeat = RepeatFilter(is_direction=is_direction, pace=pace)
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._released = threading.Event()  # set while no device is open
        self._reopen = threading.Event()
        self._paused.set()
        self._released.set()
        self._thread = None
        self._lock = threading.RLock()
        self._error = None
        self._listening = False
        self._last_button = None

    def start(self):
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="piper-ir-control", daemon=True)
                self._thread.start()

    def resume(self):
        with self._lock:
            self.reload_recordings()
            self._paused.clear()
        self.start()

    def reload_recordings(self):
        # Take the snapshot under the lock too, or a slow rebuild could replace
        # a newer one (and bring back a deleted button).
        with self._lock:
            self.matcher = SignalMatcher(self.store.snapshot())
            self.repeat.reset()

    def use(self, device):
        """Switch to another receiver; the reader reopens on its next read."""
        with self._lock:
            self.device = device
            self._error = None
        self._reopen.set()

    def pause(self):
        self._paused.set()
        with self._lock:
            self.repeat.reset()
        if threading.current_thread() is not self._thread and not self._released.wait(2):
            raise RuntimeError("Desktop IR receiver is still closing; recording must wait.")

    def close(self):
        self._paused.set()
        self._stop.set()
        if self._thread and threading.current_thread() is not self._thread:
            self._thread.join(timeout=3)

    def health(self):
        with self._lock:
            return {"ok": self._error is None, "listening": self._listening,
                    "paused": self._paused.is_set(), "device": self.device,
                    "receiver": describe(self.device),
                    "learned_buttons": self.matcher.button_count,
                    "last_button": self._last_button, "error": self._error}

    def process_frame(self, frame, now=None):
        now = self.clock() if now is None else now
        with self._lock:
            if self._paused.is_set() or self._stop.is_set() or not self.enabled():
                self.repeat.reset()
                return None
            match = self.matcher.match(frame) if frame else None
            button = self.repeat.accept(match, now)
            if button:
                self._last_button = button
        # The callback may be slow (D-Bus, uinput), so it runs without the
        # lock; pause() and health() must not wait for it.
        if button and self.enabled() and not self._paused.is_set():
            self.on_button(button)
            return button
        return None

    def _run(self):
        while not self._stop.is_set():
            if self._paused.is_set() or not self.enabled():
                self._released.set()
                self._stop.wait(.05)
                continue
            self._released.clear()
            try:
                # Check again after clearing _released, so pause() can't miss
                # a device that is about to be opened.
                if self._paused.is_set() or not self.enabled():
                    continue
                self._reopen.clear()
                with self.device_factory(self.device, FRAME_GAP_US) as device:
                    with self._lock:
                        self._listening, self._error = True, None
                        self.repeat.reset()
                    reader = FrameReader()
                    while (not self._stop.is_set() and not self._paused.is_set()
                           and not self._reopen.is_set() and self.enabled()):
                        data = device.read(.025)
                        now = self.clock()
                        for frame in reader.feed(data, now) if data else reader.idle(now):
                            self.process_frame(frame, now)
            except Exception as exc:
                with self._lock:
                    self._error = str(exc)
                LOG.warning("Desktop IR receiver: %s", exc)
            finally:
                with self._lock:
                    self._listening = False
                    self.repeat.reset()
                self._released.set()
            if self._error:
                self._stop.wait(1)
