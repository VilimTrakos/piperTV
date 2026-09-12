"""Feed recognised remote buttons to the Piper interface on the TV.

The interface runs in a browser on the Pi's own HDMI output and asks this app
what was pressed, rather than the app synthesising keystrokes into whatever
happens to hold focus. A press is therefore data with a sequence number: the
page reports the last number it saw and receives everything after it, so a slow
poll or a reload costs latency rather than events.

Two cases have to be visible to the page instead of silently losing presses:
falling far enough behind that older events were discarded, and the app being
restarted, which resets numbering to zero while the page still holds a high
number. Both are reported as a gap so the page can redraw from current state.
"""

from __future__ import annotations

import threading
import time
from collections import deque

from .ir_control import DIRECTIONS

MAX_EVENTS = 200

# What the TV interface itself acts on. Other buttons are still recorded, so
# the page can show them, but they are not navigation.
NAVIGATION = frozenset(set(DIRECTIONS) | {"ok", "back", "home", "menu", "exit"})


class ButtonLog:
    """A bounded, ordered record of recognised presses, safe for all threads."""

    def __init__(self, limit: int = MAX_EVENTS, clock=time.monotonic):
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10_000:
            raise ValueError("The button log must retain between 1 and 10000 presses.")
        self._events: deque = deque(maxlen=limit)
        self._sequence = 0
        self._lock = threading.RLock()
        self._clock = clock

    def append(self, button: str, mode: str | None = None) -> dict:
        """Record one press and return it, numbered."""
        with self._lock:
            self._sequence += 1
            event = {"sequence": self._sequence, "button": button, "mode": mode,
                     "at": round(self._clock(), 3),
                     "navigation": button in NAVIGATION}
            self._events.append(event)
            return dict(event)

    def since(self, after: int = 0) -> dict:
        """Everything numbered above `after`, and whether anything was missed."""
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError("The last seen press must be a whole number, zero or more.")
        with self._lock:
            events = [dict(event) for event in self._events if event["sequence"] > after]
            oldest = self._events[0]["sequence"] if self._events else self._sequence + 1
            # Behind the retained window, or holding a number from before a
            # restart: either way the page cannot trust its own continuity.
            missed = after > self._sequence or (after + 1 < oldest and after != 0)
            return {"sequence": self._sequence, "events": events, "missed": missed}

    def latest(self) -> dict | None:
        with self._lock:
            return dict(self._events[-1]) if self._events else None

    def clear(self) -> None:
        """Forget recorded presses without resetting numbering."""
        with self._lock:
            self._events.clear()

    def health(self) -> dict:
        with self._lock:
            return {"sequence": self._sequence, "retained": len(self._events),
                    "limit": self._events.maxlen}
