"""Offer the desktop's accessible controls as snapping targets.

Targets require application-reported screen coordinates from the accessibility
bus. Support and coordinate accuracy depend on the toolkit and compositor:
https://docs.gtk.org/atk/
https://www.freedesktop.org/wiki/Accessibility/AT-SPI2/

Two things decide what is visible here, and both are properties of the desktop
rather than of this code:

* An application appears only if it registered with the bus. Raspberry Pi OS
  starts its file manager with NO_AT_BRIDGE=1, which opts that process out
  entirely, so its desktop icons are absent no matter what this module does.
* Anything not currently on screen reports its position as INT32_MIN, which is
  a sentinel and not a coordinate.

Walking the tree costs D-Bus round trips, so results are cached briefly: a held
direction key must not re-walk the desktop between repeats.
"""

from __future__ import annotations

import logging
import itertools
import threading
import time

LOG = logging.getLogger(__name__)

# AT-SPI reports this for anything that is not placed on screen.
UNPLACED = -2147483648
DESKTOP_COORDS = 0
MAX_NODES = 600
MAX_DEPTH = 12
CACHE_S = 1.5
SCAN_S = .45

# Roles worth moving a cursor to: things a person would click.
ACTIONABLE = frozenset({
    "push button", "toggle button", "check box", "radio button", "link",
    "icon", "list item", "menu item", "check menu item", "radio menu item",
    "page tab", "table cell", "tree item", "combo box",
})


def valid_extent(box, screen) -> bool:
    """Reject sentinels, empty boxes, and anything off the visible screen."""
    width, height = screen
    try:
        x, y, box_width, box_height = box.x, box.y, box.width, box.height
    except AttributeError:
        return False
    if x <= UNPLACED or y <= UNPLACED:
        return False
    if box_width <= 0 or box_height <= 0:
        return False
    if box_width > width or box_height > height:
        return False
    return 0 <= x < width and 0 <= y < height


def target_point(node, screen, coords=DESKTOP_COORDS):
    """Reduce one accessible object to the point a cursor should land on."""
    if not actionable_state(node):
        return None
    try:
        box = node.queryComponent().getExtents(coords)
    except Exception:
        return None  # No Component interface, or it went away mid-walk.
    if not valid_extent(box, screen):
        return None
    try:
        label = node.name or ""
    except Exception:
        label = ""
    # A partly clipped control is still usable, but its full rectangle's centre
    # may be offscreen. Aim inside the visible intersection instead.
    right, bottom = min(screen[0], box.x + box.width), min(screen[1], box.y + box.height)
    return {"x": (box.x + right) // 2, "y": (box.y + bottom) // 2,
            "label": str(label)[:80]}


def state_names(node):
    """Read AT-SPI states without importing its bindings in geometry tests."""
    try:
        states = node.getState().getStates()
        return {str(getattr(state, "value_nick", state)).lower().replace("_", "-")
                for state in states}
    except Exception:
        return set()


def actionable_state(node):
    # Coordinates alone do not prove that a hidden/disabled widget can be used.
    return {"showing", "visible", "enabled"} <= state_names(node)


def collect_targets(root, screen, roles=ACTIONABLE, coords=DESKTOP_COORDS,
                    max_nodes=MAX_NODES, max_depth=MAX_DEPTH,
                    deadline=None, clock=time.monotonic):
    """Flatten an accessibility tree into clickable screen points.

    The walk is bounded in both depth and node count: a desktop can present a
    very large tree, and a remote button press must not wait on all of it. Any
    node that fails is skipped rather than ending the walk, because applications
    close while their tree is being read.
    """
    found = []
    budget = [max_nodes]
    deadline = clock() + SCAN_S if deadline is None else deadline

    def visit(node, depth):
        if depth > max_depth or budget[0] <= 0 or clock() >= deadline:
            return
        try:
            children = iter(node)
        except Exception:
            return
        # Do not list() an entire application before applying the node budget.
        while budget[0] > 0 and clock() < deadline:
            try:
                child = next(children)
            except StopIteration:
                break
            except Exception:
                break
            budget[0] -= 1
            try:
                role = child.getRoleName()
            except Exception:
                continue
            if role in roles:
                point = target_point(child, screen, coords)
                if point is not None:
                    found.append(dict(point, role=role, _node=child))
            visit(child, depth + 1)

    visit(root, 0)
    return found


class AtspiTargets:
    """Snapping targets read from the accessibility bus, cached briefly.

    Never raises on a failed read: the caller runs on the IR reader thread, and
    an empty target list simply means nothing to snap to right now.
    """

    name = "accessibility"

    def __init__(self, screen, cache_s: float = CACHE_S, clock=time.monotonic,
                 registry=None, roles=ACTIONABLE):
        width, height = screen
        self.screen = (int(width), int(height))
        self.cache_s = float(cache_s)
        self.clock = clock
        self.roles = roles
        self._registry = registry
        self._lock = threading.RLock()
        self._cached: list | None = None
        self._cached_at = -float("inf")
        self._error: str | None = None
        self._applications = 0

    def _desktop(self):
        registry = self._registry
        if registry is None:
            import pyatspi  # Imported late: it pulls in GTK and a D-Bus client.
            from gi.repository import Atspi

            # A hung application must not hold the IR reader lock indefinitely.
            Atspi.set_timeout(150, 150)

            registry = self._registry = pyatspi.Registry
        return registry.getDesktop(0)

    def targets(self):
        with self._lock:
            now = self.clock()
            if self._cached is not None and now - self._cached_at < self.cache_s:
                return self._cached
            try:
                desktop = self._desktop()
                applications = list(itertools.islice(iter(desktop), 64))
                found = []
                deadline = time.monotonic() + SCAN_S
                for application in applications:
                    if time.monotonic() >= deadline:
                        break
                    found.extend(collect_targets(application, self.screen, self.roles,
                                                 deadline=deadline))
                self._applications = len(applications)
                self._error = None
            except Exception as exc:
                # A missing pyatspi or a stopped bus is a desktop condition,
                # not a reason to stop responding to the remote.
                self._error = str(exc) or type(exc).__name__
                self._applications = 0
                found = []
                LOG.warning("Accessibility targets unavailable: %s", exc)
            self._cached, self._cached_at = found, now
            return found

    def resolve(self, target):
        """Re-check a cached target before moving to it; windows can move/close."""
        node = target.get("_node")
        if node is None:
            return None
        point = target_point(node, self.screen)
        if point is None:
            self.invalidate()
            return None
        return dict(point, role=target.get("role"), _node=node)

    def invalidate(self):
        """Drop the cache, so the next press sees the desktop as it is now."""
        with self._lock:
            self._cached, self._cached_at = None, -float("inf")

    def health(self):
        with self._lock:
            count = None if self._cached is None else len(self._cached)
            result = {"ok": self._error is None, "source": self.name,
                      "applications": self._applications, "targets": count,
                      "error": self._error}
            if self._error is None and count == 0:
                result["error"] = (
                    "No accessible targets were found. Applications appear here only "
                    "after registering with the accessibility bus; the Raspberry Pi "
                    "desktop starts its file manager with NO_AT_BRIDGE=1, which hides "
                    "its icons. Enable toolkit-accessibility and start the desktop "
                    "without that setting, or use pointer mode."
                )
            return result
