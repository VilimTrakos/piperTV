"""Notice when a page focuses something a person could type into.

A search box is reached the same way as anything else on a page -- the cursor
moves onto it and OK clicks it -- and at that moment the remote has nothing to
offer it. This is how Piper finds out that the moment has come: the page says
so itself, over the accessibility bus, the same bus snapping reads.

Listening rather than looking. Walking the tree to ask what is focused costs a
second on a page the size of a shop front, and by then the answer is stale;
the bus announces the change instead, and this remembers the last one.

What counts as typing-into is a short list of roles. A browser calls a plain
search box an entry, and one with suggestions a combo box; anything outside
the list -- a document, a heading, a button -- is focus moving around the page
rather than a request for a keyboard.
"""

from __future__ import annotations

import logging
import threading
import time

LOG = logging.getLogger(__name__)

# The roles a browser uses for the things a keyboard belongs to.
TEXT_ROLES = frozenset({"entry", "text", "password text", "combo box",
                        "search box", "spin button", "terminal"})
# A browser has text fields of its own -- the address bar above all -- and in
# kiosk mode they are not even on the screen. Only a field inside the page
# counts, and a page announces itself in the ancestry of everything in it.
PAGE_ROLES = frozenset({"document web", "document frame", "document",
                        "embedded", "internal frame"})
PAGE_DEPTH = 10
EVENTS = ("object:state-changed:focused", "object:text-caret-moved")
# How long after the last report a field still counts as the one in hand.
FRESH_S = 30.0
# A field reports itself many times over while it is used -- every caret move
# is another word from it. Only the first of a burst is worth passing on.
REPORT_EVERY_S = 0.4


def in_page(node, depth: int = PAGE_DEPTH, roles=PAGE_ROLES) -> bool:
    """Whether this control belongs to a web page rather than to the browser.

    Everything in a page hangs below a document; the address bar and the rest
    of the browser's own furniture hang below panels and tool bars. Walking up
    a few levels is enough to tell them apart, and costs nothing next to
    walking down.
    """
    for _ in range(depth):
        try:
            node = node.parent
        except Exception:
            return False
        if node is None:
            return False
        try:
            if node.getRoleName() in roles:
                return True
        except Exception:
            return False
    return False


class FocusWatcher:
    """What the active window has focused, as the bus reports it.

    Never raises into its caller: a desktop without an accessibility bus
    simply reports nothing focused, and the remote goes on working as it did
    before any of this existed.
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

    # --- the bus ----------------------------------------------------------

    def _load(self):
        registry = self._registry
        if registry is None:
            import pyatspi  # Imported late: it pulls in GTK and a D-Bus client.

            registry = self._registry = pyatspi.Registry
        return registry

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
            registry.start()
        except Exception as exc:  # noqa: BLE001 - a missing bus is a desktop condition
            with self._lock:
                self._error = str(exc) or type(exc).__name__
            LOG.warning("Watching what the page focuses: %s", exc)

    def close(self) -> None:
        self._stop.set()
        registry = self._registry
        if registry is None:
            return
        try:
            registry.stop()
        except Exception as exc:  # noqa: BLE001 - shutdown must finish
            LOG.warning("Closing the focus watcher: %s", exc)

    # --- what it heard ----------------------------------------------------

    def observe(self, event) -> None:
        """One event from the bus. Called on the bus's own thread."""
        try:
            if getattr(event, "type", "").endswith("focused") and event.detail1 != 1:
                return  # something losing focus is not something gaining it
            source = event.source
            role = source.getRoleName()
            if role not in self.roles:
                if getattr(event, "type", "").endswith("focused"):
                    # Focus moved to a link or a button: whatever was being
                    # typed into is no longer in hand.
                    self.forget()
                return
            if not in_page(source):
                return  # the browser's own address bar, not the page's search box
            field = {"role": role, "label": (source.name or "")[:80], "at": self.clock()}
        except Exception as exc:  # noqa: BLE001 - an event must never raise here
            LOG.debug("Reading a focus event: %s", exc)
            return
        with self._lock:
            # Not only when the field changes: a page focuses its search box as
            # it loads, so by the time someone clicks into it the field is
            # already the one in hand and says nothing new about itself. What
            # is worth reporting is that it is in use now -- whoever listens
            # decides whether the moment calls for a keyboard.
            now = field["at"]
            another = self._field is None or self._field["label"] != field["label"]
            fresh = another or now - self._reported_at >= REPORT_EVERY_S
            self._field = field
            self._error = None
            if fresh:
                self._reported_at = now
        if fresh and self.on_text_field is not None:
            try:
                self.on_text_field(dict(field))
            except Exception as exc:  # noqa: BLE001 - the bus thread must survive
                LOG.warning("Acting on a focused text field: %s", exc)

    def typing_into(self) -> dict | None:
        """The text field in hand, or None if focus has moved on or gone stale."""
        with self._lock:
            if self._field is None:
                return None
            if self.clock() - self._field["at"] > FRESH_S:
                return None
            return dict(self._field)

    def forget(self) -> None:
        """Let go of the field, so its keyboard is not offered twice."""
        with self._lock:
            self._field = None
            self._reported_at = -float("inf")

    def health(self) -> dict:
        with self._lock:
            return {"ok": self._error is None,
                    "watching": self._thread is not None and self._thread.is_alive(),
                    "field": self.typing_into(), "error": self._error}
