"""Continuous raw-IR recognition for desktop control, separate from learning.

RC5's toggle is decoded, never guessed by ignoring arbitrary timing differences.
Other protocols use every learned sample with a conservative timing comparison.
Reference: https://docs.kernel.org/userspace-api/media/rc/rc-protos.html
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import statistics
import struct
import threading
import time

from .lirc import CaptureError, CaptureOptions, LircDevice, Mode2Capture

LOG = logging.getLogger(__name__)
FRAME_GAP_US = 10_000
DIRECTIONS = frozenset({"up", "down", "left", "right"})


def split_frames(durations, gap_us=FRAME_GAP_US):
    """A learned press can contain several full frames and short repeat frames."""
    start = 0
    for index in range(1, len(durations), 2):
        if durations[index] >= gap_us:
            if index - start >= 3:
                yield tuple(durations[start:index])
            start = index + 1
    if len(durations) - start >= 3:
        yield tuple(durations[start:])


def decode_rc5(frame):
    """Return ((address, command), toggle) for a valid 14-bit RC5 envelope.

    The receiver omits the leading idle half-bit and the final idle half-bit.
    A mark/space pair is one Manchester bit; all half-bits must be plausible.
    """
    if not 13 <= len(frame) <= 27 or len(frame) % 2 == 0:
        return None
    short = [value for value in frame if 550 <= value <= 1150]
    # Alternating data can merge almost every half-bit into double-length
    # intervals; valid extended RC5 can even contain no short interval at all.
    # Manchester validation below, rather than an arbitrary short-edge count,
    # decides whether the reconstructed message is a valid RC5 frame.
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
    """Normalize a modest clock difference; reject any substantially wrong edge."""
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
    if len(frame) != 3:
        return False
    return (7000 <= frame[0] <= 11000 and 1700 <= frame[1] <= 2900
            and 350 <= frame[2] <= 800)


def is_nec_frame(frame):
    return (len(frame) == 67 and 7000 <= frame[0] <= 11000
            and 3300 <= frame[1] <= 5700)


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
                if sample.get("source") != "lirc":
                    continue  # Simulated recordings must never control a desktop.
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
            return None  # Never select arbitrarily between two learned buttons.
        return Match(ranked[0][0], "nec" if is_nec_frame(frame) else "raw")

    @property
    def button_count(self):
        return len({button for buttons in self.rc5.values() for button in buttons}
                   | {button for button, _frame in self.templates})


class RepeatFilter:
    def __init__(self, release_s=.28, delay_s=.32, interval_s=.09):
        self.release_s, self.delay_s, self.interval_s = release_s, delay_s, interval_s
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
        if (match.button_id in DIRECTIONS and now - self.started_at >= self.delay_s
                and now - self.emitted_at >= self.interval_s):
            self.emitted_at = now
            return match.button_id
        return None


class FrameReader:
    """Preserve all MODE2 events when several IR frames arrive in one read."""
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
                frames.append(None)  # Also invalidate any previous repeat binding.
                continue
            if self.parser.complete:
                if len(self.parser.durations) >= 3:
                    frames.append(tuple(self.parser.durations))
                else:
                    frames.append(None)
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
    """Run only when both resumed and the activation/recording gate permits it.

    pause() waits for the LIRC descriptor to close before recording can begin.
    No input device is grabbed and all activity stops when the gate closes.
    """
    def __init__(self, recordingstore, on_button, enabled, device="/dev/lirc0",
                 device_factory=LircDevice, clock=time.monotonic):
        self.store, self.on_button, self.enabled = recordingstore, on_button, enabled
        self.device, self.device_factory, self.clock = device, device_factory, clock
        self.matcher = SignalMatcher(self.store.snapshot())
        self.repeat = RepeatFilter()
        self._stop, self._paused, self._released = threading.Event(), threading.Event(), threading.Event()
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
        """Apply an edited library without reopening a paused recording device."""
        with self._lock:
            # Serialize the snapshot as well as assignment: otherwise a slow
            # rebuild can overwrite a newer deletion with its older snapshot.
            self.matcher = SignalMatcher(self.store.snapshot())
            self.repeat.reset()

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
            if button and self.enabled() and not self._paused.is_set():
                self._last_button = button
            else:
                button = None
        # The desktop may need D-Bus calls. Never keep pause()/health()/reload
        # blocked behind those calls; the desktop checks the gate again just
        # before injecting an input event.
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
                # Re-check after clearing: pause() must never observe a stale
                # released event while a new device is being opened.
                if self._paused.is_set() or not self.enabled():
                    continue
                with self.device_factory(self.device, FRAME_GAP_US) as device:
                    with self._lock:
                        self._listening, self._error = True, None
                        self.repeat.reset()
                    reader = FrameReader()
                    while not self._stop.is_set() and not self._paused.is_set() and self.enabled():
                        data = device.read(.025)
                        now = self.clock()
                        frames = reader.feed(data, now) if data else reader.idle(now)
                        for frame in frames:
                            self.process_frame(frame, now)
            except Exception as exc:
                with self._lock:
                    self._error = str(exc)
                    self.repeat.reset()
                LOG.warning("Desktop IR receiver: %s", exc)
            finally:
                with self._lock:
                    self._listening = False
                    self.repeat.reset()
                self._released.set()
            if self._error:
                self._stop.wait(1)
