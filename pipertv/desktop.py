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

# How a service built for a mouse is driven, and how the cursor behaves while
# it is. Snapping jumps between the controls a page reports; nudging moves the
# cursor itself and gathers speed while a direction is held, which is steadier
# on a page whose controls Piper cannot see cleanly.
DRIVES = ("snap", "nudge")
POINTER_DEFAULTS = {"drive": "snap", "step_px": 24, "max_step_px": 180,
                    "accelerate_within_s": 0.25, "scroll_clicks": 2}
POINTER_LIMITS = {"step_px": (2, 200), "max_step_px": (8, 600),
                  "accelerate_within_s": (0.05, 2.0), "scroll_clicks": (1, 10)}
# The cursor is at an edge when a step would not move it any further. A page
# then scrolls instead, which is what a hand would do with the wheel rather
# than carry the cursor off to a scrollbar.
EDGE_PX = 2


def validate_pointer(values) -> dict:
    """Check a pointer preference, returning a complete, plain copy of it.

    Anything absent keeps its default, so the studio can send one field. A
    value outside its range is refused rather than clamped: a step of a
    thousand pixels is a mistake worth seeing, not a preference to honour
    quietly.
    """
    if not isinstance(values, dict):
        raise ValueError("Pointer settings must be a JSON object.")
    settings = dict(POINTER_DEFAULTS)
    for name, value in values.items():
        if name not in settings:
            raise ValueError(f"Unknown pointer setting {name!r}. "
                             f"Settings are: {', '.join(sorted(settings))}.")
        if name == "drive":
            if value not in DRIVES:
                raise ValueError("A service is driven by snapping or by nudging.")
            settings[name] = value
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a number.")
        low, high = POINTER_LIMITS[name]
        if not low <= value <= high:
            raise ValueError(f"{name} must be between {low} and {high}.")
        settings[name] = round(float(value), 3) if name == "accelerate_within_s" else int(value)
    if settings["step_px"] > settings["max_step_px"]:
        raise ValueError("The first step cannot be larger than the fastest one.")
    return settings
# What a sideways offset costs when nothing shares the cursor's band.
SIDE_WEIGHT = 1.5
# How far off the line of travel a target may sit and still be a step that way
# rather than a jump across the layout. Generous on purpose: the menu at the
# top of a page is a long way to the side of a cursor halfway down it.
SPREAD = 3.0
MARGIN_PX = 200
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
    centre. When none is given, the control the cursor is standing in serves
    as one, so the first press of a session behaves like every later press.
    """
    if direction not in DIRECTIONS:
        return None
    x, y = position
    origin = box if box is not None else standing_box(position, targets)
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
            # Nothing shares the band, but this is still recognisably that way:
            # the menu at the top of the screen, from halfway down it.
            rank = (1, ahead + aside * SIDE_WEIGHT, aside)
        else:
            # Barely ahead and wildly off to the side. Better than nothing --
            # a press that does nothing at all leaves the remote stuck -- but
            # only when the screen offers nothing better.
            rank = (2, ahead + aside * SIDE_WEIGHT, aside)
        if best is None or rank < best[0]:
            best = (rank, target)
    return best[1] if best else None


def standing_box(position, targets):
    """The smallest control the cursor is inside, as a rectangle.

    Snapping from a bare point treats the cursor as infinitely thin, so a wide
    control under it offers no band to travel in and the first press of a
    session behaves unlike all the ones after it. The innermost control wins:
    a page nests a link inside a row inside a panel, and the link is the thing
    a person would say they are on.
    """
    x, y = position
    smallest = None
    for target in targets:
        extent = _box(target)
        if extent is None:
            continue
        left, top, right, bottom = extent
        if not (left <= x <= right and top <= y <= bottom):
            continue
        area = (right - left) * (bottom - top)
        if smallest is None or area < smallest[0]:
            smallest = (area, extent)
    return smallest[1] if smallest else (x, y, x, y)


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
        self.scroll_clicks = POINTER_DEFAULTS["scroll_clicks"]

    def configure(self, settings: dict) -> dict:
        """Apply a checked pointer preference to the live cursor."""
        checked = validate_pointer(settings)
        with self._lock:
            self.step_px = checked["step_px"]
            self.max_step_px = checked["max_step_px"]
            self.accelerate_within_s = checked["accelerate_within_s"]
            self.scroll_clicks = checked["scroll_clicks"]
            self._streak = 0
            return self.settings()

    def settings(self) -> dict:
        with self._lock:
            return {"step_px": self.step_px, "max_step_px": self.max_step_px,
                    "accelerate_within_s": self.accelerate_within_s,
                    "scroll_clicks": self.scroll_clicks}

    def _scroll(self, pointer, button) -> tuple:
        """Turn the wheel in the direction the cursor cannot go any further."""
        pointer.scroll(self.scroll_clicks if button == "up" else -self.scroll_clicks)
        return pointer.position

    def _at_edge(self, pointer, button) -> bool:
        x, y = pointer.position
        if button == "up":
            return y <= EDGE_PX
        if button == "down":
            return y >= self.screen[1] - 1 - EDGE_PX
        return False

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
            # Nothing that way on this screenful. Below the fold there may be
            # plenty, so scroll rather than leaving the press to do nothing.
            if button in ("up", "down"):
                return self._scroll(pointer, button)
            return None  # Sideways, the cursor stays where the user left it.
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
                    if self._at_edge(pointer, button):
                        # The cursor has nowhere further to go that way, so the
                        # page moves under it instead.
                        position = self._scroll(pointer, button)
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
                    "max_step_px": self.max_step_px,
                    "accelerate_within_s": self.accelerate_within_s,
                    "targets": None if self.targets is None else self.targets.name,
                    "error": self._error}

    def close(self):
        self.release()
