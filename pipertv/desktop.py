"""Move the Pi's real cursor with the remote.

Pointer mode nudges the cursor and speeds up while a direction is held.
Snapping mode jumps to the next clickable control in that direction; the
controls come from a target provider (see targets.py).

Runs on the IR reader thread: a press must never raise, and the gate is
checked again after anything slow.
"""

from __future__ import annotations

import logging
import threading
import time

from .pointer import VirtualPointer
from .roles import DIRECTIONS
from .session import DESKTOP_MODES

LOG = logging.getLogger(__name__)

CLICKS = {"ok": "left", "menu": "right"}

# How a page Piper opened is driven: snap between its controls, or nudge the cursor.
DRIVES = ("snap", "nudge")
# hold_delay_s/hold_interval_s: how long a key must be held before it
# repeats, and how often. reserved_top_px: see below.
POINTER_DEFAULTS = {"drive": "snap", "step_px": 24, "max_step_px": 180,
                    "accelerate_within_s": 0.25, "scroll_clicks": 2,
                    "hold_delay_s": 0.65, "hold_interval_s": 0.25,
                    "reserved_top_px": 36}
POINTER_LIMITS = {"step_px": (2, 200), "max_step_px": (8, 600),
                  "accelerate_within_s": (0.05, 2.0), "scroll_clicks": (1, 10),
                  "hold_delay_s": (0.2, 3.0), "hold_interval_s": (0.05, 1.0),
                  "reserved_top_px": (0, 400)}
SECONDS = ("accelerate_within_s", "hold_delay_s", "hold_interval_s")

# At an edge the page scrolls instead of the cursor moving.
EDGE_PX = 2
# reserved_top_px: labwc's hidden panel still takes input in the top 36 px,
# so a wheel turned there scrolls nothing. While a service is open the cursor
# stays below that strip. (On the desktop it doesn't: the panel is there.)

# The desktop's double-click time is 0.4 s, too short for a remote, so a
# second OK on the same spot within this time is sent as a double-click.
DOUBLE_OK_S = 0.8
DOUBLE_CLICK_GAP_S = 0.06

# Snapping geometry, in pixels.
SIDE_WEIGHT = 1.5    # cost of sideways offset when nothing is straight ahead
SPREAD = 3.0         # how wide the cone "in that direction" is...
MARGIN_PX = 200      # ...plus this, so the menu at the top is reachable from mid-page
SAME_LINE_PX = 4     # centres this close along the axis of travel are on the same line


def validate_pointer(values) -> dict:
    """Check pointer settings; missing fields keep their defaults.

    Out-of-range values are rejected, not clamped.
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
        settings[name] = round(float(value), 3) if name in SECONDS else int(value)
    if settings["step_px"] > settings["max_step_px"]:
        raise ValueError("The first step cannot be larger than the fastest one.")
    return settings


def _box(target):
    """(left, top, right, bottom) of a target, or its point if it has no extent."""
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
    return max(0, min(high, other_high) - max(low, other_low))


def choose_target(position, targets, direction, box=None):
    """The control that is next in `direction` from the cursor.

    Prefer the nearest control that overlaps the cursor's band (the same
    column for up/down, the same row for left/right). Only if there is none,
    take the best one off to the side within a cone, and as a last resort
    anything ahead at all, so a press never does nothing when there is
    somewhere to go.

    `box` is the control the cursor is on; when not given, the innermost
    control under the cursor is used.
    """
    if direction not in DIRECTIONS:
        return None
    x, y = position
    left, top, right, bottom = box if box is not None else standing_box(position, targets)
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
            shared = _overlap(left, right, t_left, t_right)
            edge = (t_top - bottom) if direction == "down" else (top - t_bottom)
        else:
            ahead = (t_x - x) if direction == "right" else (x - t_x)
            aside = abs(t_y - y)
            shared = _overlap(top, bottom, t_top, t_bottom)
            edge = (t_left - right) if direction == "right" else (left - t_right)
        if ahead <= SAME_LINE_PX:
            continue  # level with the cursor or behind it
        if shared > 0:
            rank = (0, max(edge, 0), aside)
        elif aside <= ahead * SPREAD + MARGIN_PX:
            rank = (1, ahead + aside * SIDE_WEIGHT, aside)
        else:
            rank = (2, ahead + aside * SIDE_WEIGHT, aside)
        if best is None or rank < best[0]:
            best = (rank, target)
    return best[1] if best else None


def standing_box(position, targets):
    """The smallest control containing the cursor, or the cursor's own point.

    Pages nest a link inside a row inside a panel; the link is what the
    cursor is "on".
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
    """Applies presses to the cursor in the session's mode.

    The virtual pointer is created on the first press of a session and
    removed as soon as control ends, so nothing can move the cursor after
    the TV has switched away.
    """

    def __init__(self, session, screen, pointer_factory=VirtualPointer, targets=None,
                 step_px=POINTER_DEFAULTS["step_px"], max_step_px=POINTER_DEFAULTS["max_step_px"],
                 accelerate_within_s=POINTER_DEFAULTS["accelerate_within_s"],
                 clock=time.monotonic, enabled=None, sleep=time.sleep):
        if not 2 <= step_px <= max_step_px:
            raise ValueError("Pointer step sizes must grow from at least 2 pixels.")
        self.session = session
        self.enabled = enabled or session.enabled
        self.screen = (int(screen[0]), int(screen[1]))
        self.pointer_factory = pointer_factory
        self.targets = targets
        self.step_px = int(step_px)
        self.max_step_px = int(max_step_px)
        self.accelerate_within_s = float(accelerate_within_s)
        self.scroll_clicks = POINTER_DEFAULTS["scroll_clicks"]
        self.reserved_top_px = POINTER_DEFAULTS["reserved_top_px"]
        self.clock = clock
        self.sleep = sleep
        self._lock = threading.RLock()
        self._pointer = None
        self._error = None
        self._last_button = None
        self._last_at = -float("inf")
        self._streak = 0
        # The control the last snap landed on, and where the cursor was put.
        self._standing = None
        self._standing_at = None
        self._clicked_at = -float("inf")
        self._clicked_where = None

    def configure(self, settings: dict) -> None:
        checked = validate_pointer(settings)
        with self._lock:
            self.step_px = checked["step_px"]
            self.max_step_px = checked["max_step_px"]
            self.accelerate_within_s = checked["accelerate_within_s"]
            self.scroll_clicks = checked["scroll_clicks"]
            self.reserved_top_px = checked["reserved_top_px"]
            self._streak = 0

    def press(self, button, mode=None):
        """Act on one button. Returns the cursor position, or None.

        `mode` overrides the session's mode while a service Piper opened is
        on screen (a web page is always snapped through). It only changes how
        the cursor moves, never whether it may.
        """
        with self._lock:
            try:
                if not self.enabled():
                    self.release()
                    return None
                if button not in DIRECTIONS and button not in CLICKS:
                    return None  # volume, power...: don't even create a pointer
                original = self.session.snapshot()
                serving = mode is not None
                top = self.reserved_top_px if serving else 0
                mode = mode or original.get("mode")
                if mode not in DESKTOP_MODES:
                    self.release()
                    return None
                pointer = self._open()
                if not self._same_visit(original):
                    self.release()
                    return None
                if button in CLICKS:
                    position = self._click(pointer, button, double=button == "ok" and not serving)
                elif mode == "snapping":
                    position = self._snap(pointer, button, original, top)
                else:
                    position = self._nudge(pointer, button, top)
                self._error = None
                return position
            except Exception as exc:
                # The IR reader thread must survive whatever the desktop does.
                self._error = str(exc) or type(exc).__name__
                LOG.warning("Desktop control: %s", exc)
                self.release()
                return None

    def _open(self):
        if self._pointer is None:
            self._pointer = self.pointer_factory(self.screen)
            self._pointer.open()
        return self._pointer

    def _same_visit(self, original):
        """Whether control is still on, for the visit the press started in."""
        # enabled() may refresh detection and replace the session, so read
        # the session after it.
        allowed = self.enabled()
        current = self.session.snapshot()
        return (allowed and current.get("mode") == original.get("mode")
                and (current.get("session") or {}).get("id")
                == (original.get("session") or {}).get("id"))

    def _step(self, button, now):
        """Step size: grows while the same direction keeps being pressed."""
        if button == self._last_button and now - self._last_at <= self.accelerate_within_s:
            self._streak += 1
        else:
            self._streak = 0
        self._last_button, self._last_at = button, now
        return min(self.max_step_px, round(self.step_px * (1 + self._streak * 0.35)))

    def _at_edge(self, pointer, button, top: int) -> bool:
        _x, y = pointer.position
        if button == "up":
            return y <= top + EDGE_PX
        if button == "down":
            return y >= self.screen[1] - 1 - EDGE_PX
        return False

    def _scroll(self, pointer, button):
        pointer.scroll(self.scroll_clicks if button == "up" else -self.scroll_clicks)
        return pointer.position

    def _nudge(self, pointer, button, top: int):
        if self._at_edge(pointer, button, top):
            return self._scroll(pointer, button)
        step = self._step(button, self.clock())
        dx = {"left": -step, "right": step}.get(button, 0)
        dy = {"up": -step, "down": step}.get(button, 0)
        x, y = pointer.position
        return pointer.move_to(x + dx, max(top, y + dy))

    def _snap(self, pointer, button, original, top: int):
        if self.targets is None:
            raise RuntimeError("Snapping has no source of targets on this desktop.")
        standing = self._standing if self._standing_at == pointer.position else None
        # Controls in the reserved strip at the top can't be clicked.
        targets = [target for target in self.targets.targets() if target.get("y", 0) >= top]
        found = choose_target(pointer.position, targets, button, box=standing)
        if found is not None:
            # The target list is cached; make sure it's still there and still that way.
            found = self.targets.resolve(found)
            if found is not None:
                found = choose_target(pointer.position, [found], button)
        if found is None:
            # Nothing more that way on screen: scroll for more when going up or down.
            return self._scroll(pointer, button) if button in ("up", "down") else None
        if not self._same_visit(original):
            self.release()
            return None
        position = pointer.move_to(int(found["x"]), int(found["y"]))
        self._standing = _box(found)
        self._standing_at = position
        return position

    def _click(self, pointer, button, double: bool):
        pointer.click(CLICKS[button])
        if double:
            self._double_ok(pointer)
        self._last_button, self._streak = None, 0
        if self.targets is not None:
            self.targets.invalidate()  # the click probably changed the screen
        return pointer.position

    def _double_ok(self, pointer) -> None:
        now = self.clock()
        if now - self._clicked_at <= DOUBLE_OK_S and pointer.position == self._clicked_where:
            self.sleep(DOUBLE_CLICK_GAP_S)
            pointer.click(CLICKS["ok"])
            self._clicked_at = -float("inf")  # a third press starts over
        else:
            self._clicked_at, self._clicked_where = now, pointer.position

    def release(self):
        """Remove the virtual pointer."""
        with self._lock:
            pointer, self._pointer = self._pointer, None
            self._last_button, self._streak = None, 0
            self._standing, self._standing_at = None, None
            if pointer is not None:
                try:
                    pointer.close()
                except Exception as exc:
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
