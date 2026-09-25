"""Notice when a web page focuses a text field, so the keyboard can be offered.

We listen to focus and caret events on the accessibility bus rather than
walking the tree to ask what is focused: a walk takes about a second on a
big page, and by then the answer is stale.
"""

from __future__ import annotations

import logging
import threading
import time

LOG = logging.getLogger(__name__)

# Roles a browser uses for things you type into ("entry" for a plain search
# box, "combo box" for one with suggestions).
TEXT_ROLES = frozenset({"entry", "text", "password text", "combo box",
                        "search box", "spin button", "terminal"})
# Only fields inside a page count; the browser's own address bar doesn't.
PAGE_ROLES = frozenset({"document web", "document frame", "document",
                        "embedded", "internal frame"})
# Walking up from the browser's own fields reaches one of these before any
# page role.
WINDOW_ROLES = frozenset({"frame", "window", "dialog", "application",
                          "desktop frame"})
PAGE_DEPTH = 40
EVENTS = ("object:state-changed:focused", "object:text-caret-moved")
# A field stays "in hand" until focus moves to something else, or this long.
FRESH_S = 900.0
# Every caret move is an event; report a field at most this often.
REPORT_EVERY_S = 0.4


def in_page(node, depth: int = PAGE_DEPTH, roles=PAGE_ROLES, outside=WINDOW_ROLES) -> bool:
    """Whether a control is part of a web page rather than the browser's UI."""
    for _ in range(depth):
        try:
            node = node.parent
        except Exception:
            return False
        if node is None:
            return False
        try:
            role = node.getRoleName()
        except Exception:
            return False
        if role in roles:
            return True
        if role in outside:
            return False
    return False


class FocusWatcher:
    """Remembers the text field the page focused last. Never raises.

    on_text_field(field) is called from the bus thread when a field is used.
    """

    def __init__(self, on_text_field=None, roles=TEXT_ROLES, registry=None,
                 clock=time.monotonic):
        self.on_text_field = on_text_field
        self.roles = frozenset(roles)
        self.clock = clock
        self._registry = registry
        self._lock = threading.RLock()
        self._thread = None
        self._stop = threading.Event()
        self._field = None
        self._reported_at = -float("inf")
        self._error: str | None = None

    def _load(self):
        if self._registry is None:
            import pyatspi  # late: it loads GTK and a D-Bus client
            self._registry = pyatspi.Registry
        return self._registry

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="piper-focus", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        try:
            registry = self._load()
            for event in EVENTS:
                registry.registerEventListener(self.observe, event)
            registry.start()  # runs the bus's main loop until stop()
        except Exception as exc:
            with self._lock:
                self._error = str(exc) or type(exc).__name__
            LOG.warning("Watching what the page focuses: %s", exc)

    def close(self) -> None:
        self._stop.set()
        if self._registry is None:
            return
        try:
            self._registry.stop()
        except Exception as exc:
            LOG.warning("Closing the focus watcher: %s", exc)

    def observe(self, event) -> None:
        """Handle one bus event (on the bus's thread)."""
        try:
            focused = getattr(event, "type", "").endswith("focused")
            if focused and event.detail1 != 1:
                return  # losing focus
            source = event.source
            role = source.getRoleName()
            if role not in self.roles:
                if focused:
                    self.forget()  # focus moved to a link, a button...
                return
            if not in_page(source):
                return
            field = {"role": role, "label": (source.name or "")[:80], "at": self.clock()}
        except Exception as exc:
            LOG.debug("Reading a focus event: %s", exc)
            return
        with self._lock:
            # Report the same field again once in a while, not only when it
            # changes: a page often focuses its search box while loading, so by
            # the time someone clicks into it nothing about it is new.
            now = field["at"]
            another = self._field is None or self._field["label"] != field["label"]
            report = another or now - self._reported_at >= REPORT_EVERY_S
            self._field = field
            self._error = None
            if report:
                self._reported_at = now
        if report and self.on_text_field is not None:
            try:
                self.on_text_field(dict(field))
            except Exception as exc:
                LOG.warning("Acting on a focused text field: %s", exc)

    def typing_into(self) -> dict | None:
        """The focused text field, or None if focus moved on or it is stale."""
        with self._lock:
            if self._field is None or self.clock() - self._field["at"] > FRESH_S:
                return None
            return dict(self._field)

    def forget(self) -> None:
        with self._lock:
            self._field = None
            self._reported_at = -float("inf")

    def health(self) -> dict:
        with self._lock:
            return {"ok": self._error is None,
                    "watching": self._thread is not None and self._thread.is_alive(),
                    "field": self.typing_into(), "error": self._error}
