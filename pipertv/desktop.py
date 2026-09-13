"""Turn recognised remote buttons into movements of this Pi's real cursor.

Pointer mode nudges the cursor and accelerates while a direction is held.
Snapping mode jumps it straight to the next target in that direction, so the
desktop mouse really moves rather than a highlight moving on its own.

Targets come from a provider rather than from this module: the desktop exposes
its icons through accessibility, and a later PiperTV interface can offer its own
targets to the same gate. A provider only has to return screen positions.

This runs on the IR reader thread, so desktop queries must stay bounded and a
press must not raise into the receiver. The gate is checked after slow queries.
"""

from __future__ import annotations

import logging
import threading
import time

from .ir_control import DIRECTIONS
from .pointer import VirtualPointer

LOG = logging.getLogger(__name__)

CLICKS = {"ok": "left", "menu": "right"}
# How far off the straight line a target may sit and still count as "that way".
SPREAD = 2.0
MARGIN_PX = 40


def choose_target(position, targets, direction):
    """Pick the nearest target in one direction, preferring aligned ones.

    A target directly ahead beats a nearer one far off to the side, which is
    what makes repeated presses walk a row of icons instead of wandering.
    """
    if direction not in DIRECTIONS:
        return None
    x, y = position
    best = None
    for target in targets:
        try:
            tx, ty = int(target["x"]), int(target["y"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        dx, dy = tx - x, ty - y
        if direction == "left":
            ahead, aside = -dx, abs(dy)
        elif direction == "right":
            ahead, aside = dx, abs(dy)
        elif direction == "up":
            ahead, aside = -dy, abs(dx)
        else:
            ahead, aside = dy, abs(dx)
        if ahead <= 0 or aside > ahead * SPREAD + MARGIN_PX:
            continue
        score = ahead + aside * SPREAD
        if best is None or score < best[0]:
            best = (score, target)
    return best[1] if best else None


class DesktopControl:
    """Apply button presses to the cursor for the session's chosen mode.

    The virtual pointer exists only while control is on: it opens on the first
    press of a session and is removed as soon as the session ends, so no device
    is left behind that could move the cursor after the TV switches away.
    """

    def __init__(self, session, screen, pointer_factory=VirtualPointer, targets=None,
                 step_px=24, max_step_px=180, accelerate_within_s=0.25,
                 clock=time.monotonic, enabled=None):
        width, height = screen
        if not 2 <= step_px <= max_step_px:
            raise ValueError("Pointer step sizes must grow from at least 2 pixels.")
        self.session = session
        self.enabled = session.enabled if enabled is None else enabled
        self.screen = (int(width), int(height))
        self.pointer_factory = pointer_factory
        self.targets = targets
        self.step_px = int(step_px)
        self.max_step_px = int(max_step_px)
        self.accelerate_within_s = float(accelerate_within_s)
        self.clock = clock
        self._lock = threading.RLock()
        self._pointer = None
        self._error = None
        self._last_button = None
        self._last_at = -float("inf")
        self._streak = 0

    def _step(self, button, now):
        """Grow the step while one direction is held, and reset when released."""
        if button == self._last_button and now - self._last_at <= self.accelerate_within_s:
            self._streak += 1
        else:
            self._streak = 0
        self._last_button, self._last_at = button, now
        return min(self.max_step_px, round(self.step_px * (1 + self._streak * 0.35)))

    def _open(self):
        if self._pointer is None:
            self._pointer = self.pointer_factory(self.screen)
            self._pointer.open()
        return self._pointer

    def _current(self, original):
        """A slow device/desktop query must not act in a different TV visit."""
        # The predicate may refresh CEC and replace the visit. Read the session
        # after it, not before, or an old identity could validate a new visit.
        allowed = self.enabled()
        current = self.session.snapshot()
        return (allowed and current.get("mode") == original.get("mode")
                and (current.get("session") or {}).get("id")
                == (original.get("session") or {}).get("id"))

    def _snap(self, pointer, button, original):
        if self.targets is None:
            raise RuntimeError("Snapping has no source of targets on this desktop.")
        found = choose_target(pointer.position, self.targets.targets(), button)
        if found is not None and hasattr(self.targets, "resolve"):
            found = self.targets.resolve(found)
            # Revalidation can return a moved window. A RIGHT press must never
            # jump left just because its cached target used to be on the right.
            if found is not None:
                found = choose_target(pointer.position, [found], button)
        if found is None:
            return None  # Nothing that way; the cursor stays where the user left it.
        if not self._current(original):
            self.release()
            return None
        return pointer.move_to(int(found["x"]), int(found["y"]))

    def press(self, button, mode=None):
        """Act on one recognised button. Returns the cursor position, or None.

        `mode` overrides the visit's choice, for a service that Piper opened:
        a page built for a mouse is driven by snapping whatever the browser was
        asked to do with the desktop. The gate is unchanged -- an override
        decides how the cursor moves, never whether it may.
        """
        with self._lock:
            try:
                if not self.enabled():
                    # The TV switched away, or recording started: remove the device.
                    self.release()
                    return None
                if button not in DIRECTIONS and button not in CLICKS:
                    return None  # TV volume/power/source must not even open a pointer.
                original = self.session.snapshot()
                mode = mode or original.get("mode")
                if mode not in ("pointer", "snapping"):
                    self.release()
                    return None
                pointer = self._open()
                if not self._current(original):
                    self.release()
                    return None
                if button in DIRECTIONS:
                    if mode == "snapping":
                        position = self._snap(pointer, button, original)
                        self._error = None
                        return position
                    step = self._step(button, self.clock())
                    dx = step if button == "right" else -step if button == "left" else 0
                    dy = step if button == "down" else -step if button == "up" else 0
                    position = pointer.move_by(dx, dy)
                    self._error = None
                    return position
                if button in CLICKS:
                    pointer.click(CLICKS[button])
                    self._error = None
                    self._last_button, self._streak = None, 0
                    if self.targets is not None and hasattr(self.targets, "invalidate"):
                        self.targets.invalidate()
                    return pointer.position
                return None
            except Exception as exc:
                # The reader thread must survive a desktop problem.
                self._error = str(exc) or type(exc).__name__
                LOG.warning("Desktop control: %s", exc)
                self.release()
                return None

    def release(self):
        """Remove the virtual pointer, ending any influence over the cursor."""
        with self._lock:
            pointer, self._pointer = self._pointer, None
            self._last_button, self._streak = None, 0
            if pointer is not None:
                try:
                    pointer.close()
                except Exception as exc:  # noqa: BLE001 - closing must not raise
                    LOG.warning("Closing the virtual pointer: %s", exc)

    def health(self):
        with self._lock:
            return {"ok": self._error is None, "active": self._pointer is not None,
                    "screen": list(self.screen), "step_px": self.step_px,
                    "targets": None if self.targets is None else self.targets.name,
                    "error": self._error}

    def close(self):
        self.release()
