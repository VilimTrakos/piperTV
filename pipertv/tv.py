"""The queue of button presses that the TV interface polls.

The interface (a browser page on the Pi's HDMI output) asks for everything
after the last sequence number it saw, so a slow poll or a reload only adds
latency. Two situations are reported as `missed` so the page can resync:
falling behind the retained window, and holding a number from before an app
restart (a new stream_id also tells the page about the restart).
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque

from .roles import ROLES

MAX_EVENTS = 200
# What the interface acts on; other presses are still recorded.
NAVIGATION = frozenset(ROLES)

# action=None is meaningful (a key whose role moved away), so "not given" needs its own marker.
_UNSET = object()


class ButtonLog:
    """A bounded, numbered, thread-safe log of recognised presses."""

    def __init__(self, limit: int = MAX_EVENTS, clock=time.monotonic):
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10_000:
            raise ValueError("The button log must retain between 1 and 10000 presses.")
        self._events: deque = deque(maxlen=limit)
        self._sequence = 0
        self._stream_id = uuid.uuid4().hex
        self._lock = threading.RLock()
        self._clock = clock

    def append(self, button: str, mode: str | None = None,
               session_id: str | None = None, action=_UNSET) -> dict:
        """Record a press. `button` is what the remote sent, `action` the role it performs."""
        performed = button if action is _UNSET else action
        with self._lock:
            self._sequence += 1
            event = {"sequence": self._sequence, "button": button, "mode": mode,
                     "session_id": session_id, "action": performed,
                     "at": round(self._clock(), 3),
                     "navigation": performed in NAVIGATION}
            self._events.append(event)
            return dict(event)

    def since(self, after: int = 0) -> dict:
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError("The last seen press must be a whole number, zero or more.")
        with self._lock:
            events = [dict(event) for event in self._events if event["sequence"] > after]
            oldest = self._events[0]["sequence"] if self._events else self._sequence + 1
            missed = after > self._sequence or (after + 1 < oldest and after != 0)
            return {"sequence": self._sequence, "stream_id": self._stream_id,
                    "events": events, "missed": missed}

    def latest(self) -> dict | None:
        with self._lock:
            return dict(self._events[-1]) if self._events else None

    def clear(self) -> None:
        """Drop the retained presses; numbering continues."""
        with self._lock:
            self._events.clear()

    def health(self) -> dict:
        with self._lock:
            return {"sequence": self._sequence, "retained": len(self._events),
                    "limit": self._events.maxlen}
