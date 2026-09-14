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
# A target that shares no band with where the cursor is may still be the only
# thing that way. It is considered, but only inside a narrow cone and always
# after anything that does share a band.
SPREAD = 1.0
MARGIN_PX = 24
# Centres this close along the axis of travel are the same row or column, not
# a step in that direction.
SAME_LINE_PX = 4


def _box(target, fallback_point=None):
    """The target's rectangle, or its point when it reports no extent."""
    if fallback_point is not None:
        x, y = fallback_point
        return (x, y, x, y)
    try:
        left, top = int(target["left"]), int(target["top"])
        right, bottom = int(target["right"]), int(target["bottom"])
    except (KeyError, TypeError, ValueError, OverflowError):
        try:
            x, y = int(target["x"]), int(target["y"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        return (x, y, x, y)
    if right < left or bottom < top:
        return None
    return (left, top, right, bottom)


def _overlap(low, high, other_low, other_high) -> int:
    """How much two spans share along one axis; zero when they merely touch."""
    return max(0, min(high, other_high) - max(low, other_low))


def choose_target(position, targets, direction, box=None):
    """Pick the control that is really next in one direction.

    Which one a person means by "down" is not the nearest thing in a wide cone
    from the cursor -- that picks diagonals, and a row of buttons is then
    walked in an order nobody can predict. It is the nearest control whose
    extent still lies in the band the cursor occupies: directly below, in the
    same column of the layout. Only when nothing shares that band does a
    target off to the side become the answer, and then within a narrow cone.

    `box` is the rectangle the cursor is currently on, so that a wide element
    hands over to whatever sits under any part of it, not only under its
    centre.
    """
    if direction not in DIRECTIONS:
        return None
    x, y = position
    origin = box if box is not None else (x, y, x, y)
    left, top, right, bottom = origin
    vertical = direction in ("up", "down")
    best = None
    for target in targets:
        extent = _box(target)
        if extent is None:
            continue
        t_left, t_top, t_right, t_bottom = extent
        t_x, t_y = (t_left + t_right) // 2, (t_top + t_bottom) // 2
        if vertical:
            ahead = (t_y - y) if direction == "down" else (y - t_y)
            aside = abs(t_x - x)
            # The band is the cursor's own width: anything under any part of a
            # wide element is "below" it.
            shared = _overlap(left, right, t_left, t_right)
            edge = (t_top - bottom) if direction == "down" else (top - t_bottom)
        else:
            ahead = (t_x - x) if direction == "right" else (x - t_x)
            aside = abs(t_y - y)
            shared = _overlap(top, bottom, t_top, t_bottom)
            edge = (t_left - right) if direction == "right" else (left - t_right)
        if ahead <= SAME_LINE_PX:
            continue  # beside the cursor, or behind it: not a step that way
        if shared > 0:
            # The same column or row: order by how far along it is, and let the
            # sideways offset only break ties.
            rank = (0, max(edge, 0), aside)
        elif aside <= ahead * SPREAD + MARGIN_PX:
            # Nothing shares the band; a target off to the side will do, but
            # only within a narrow cone and never ahead of one that does.
            rank = (1, ahead + aside * SPREAD, aside)
        else:
            continue
        if best is None or rank < best[0]:
            best = (rank, target)
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
        self._standing = None
        self._standing_at = None

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
        # Where the cursor stands is a control, not a point, whenever the last
        # press put it on one: a wide button hands over to whatever sits under
        # any part of it, which is what makes a row walk in order.
        standing = self._standing if self._standing_at == pointer.position else None
        found = choose_target(pointer.position, self.targets.targets(), button,
                              box=standing)
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
        position = pointer.move_to(int(found["x"]), int(found["y"]))
        self._standing = _box(found)
        self._standing_at = position
        return position

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
            self._standing, self._standing_at = None, None
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
