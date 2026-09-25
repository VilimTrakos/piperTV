"""Clickable controls on the screen, read from the accessibility bus (AT-SPI).

Used for snapping. What is visible here depends on the desktop:

* an application must be on the bus (NO_AT_BRIDGE=1 hides it) and expose
  its controls individually, which not all do;
* coordinates can be "unplaced" sentinels, and controls in a covered
  background window still say they are showing, so only the active window
  is used.

Walking the tree costs D-Bus round trips, so results are cached briefly.

https://www.freedesktop.org/wiki/Accessibility/AT-SPI2/
"""

from __future__ import annotations

import itertools
import logging
import threading
import time

LOG = logging.getLogger(__name__)

UNPLACED = -2147483648  # what AT-SPI reports for anything not on screen
DESKTOP_COORDS = 0
# Browsers put page controls 12-20 levels below the window.
MAX_NODES = 2000
MAX_DEPTH = 25
CACHE_S = 1.5
SCAN_S = .9

# Things a person would click. Browsers use the short names ("button",
# "entry"), GTK the long ones ("push button").
ACTIONABLE = frozenset({
    "push button", "button", "toggle button", "check box", "radio button",
    "link", "icon", "list item", "menu item", "check menu item",
    "radio menu item", "page tab", "table cell", "tree item", "combo box",
    "entry", "text box", "search box", "image map",
})
WINDOW_ROLES = frozenset({"frame", "window", "dialog", "alert", "desktop frame"})
WINDOW_DEPTH = 3       # how deep to look for the active window itself
SAME_CONTROL_PX = 8    # centres this close are the same control reported twice


def valid_extent(box, screen) -> bool:
    """False for sentinels, empty boxes and anything off the screen."""
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


def state_names(node):
    try:
        states = node.getState().getStates()
        return {str(getattr(state, "value_nick", state)).lower().replace("_", "-")
                for state in states}
    except Exception:
        return set()


def actionable_state(node):
    # Valid coordinates alone don't mean a control is visible and enabled.
    return {"showing", "visible", "enabled"} <= state_names(node)


def target_point(node, screen, coords=DESKTOP_COORDS):
    """The point to click on for one accessible object, with its box and label."""
    if not actionable_state(node):
        return None
    try:
        box = node.queryComponent().getExtents(coords)
    except Exception:
        return None  # no Component interface, or it went away
    if not valid_extent(box, screen):
        return None
    try:
        label = node.name or ""
    except Exception:
        label = ""
    # Aim at the middle of the visible part of a partly clipped control.
    right, bottom = min(screen[0], box.x + box.width), min(screen[1], box.y + box.height)
    left, top = max(0, box.x), max(0, box.y)
    return {"x": (left + right) // 2, "y": (top + bottom) // 2,
            "left": left, "top": top, "right": right, "bottom": bottom,
            "label": str(label)[:80]}


def deepest_at(node, x, y, coords=DESKTOP_COORDS, depth=MAX_DEPTH):
    """The innermost accessible object at a point, descending from `node`."""
    found = None
    for _ in range(depth):
        try:
            child = node.queryComponent().getAccessibleAtPoint(int(x), int(y), coords)
        except Exception:
            break
        if child is None or child == node:
            break
        node, found = child, child
    return found


def collect_targets(root, screen, roles=ACTIONABLE, coords=DESKTOP_COORDS,
                    max_nodes=MAX_NODES, max_depth=MAX_DEPTH,
                    deadline=None, clock=time.monotonic, active_windows_only=False):
    """Flatten an accessibility tree into clickable points.

    Bounded by depth, node count and time. A node that fails is skipped:
    applications close while their tree is being read.
    """
    found = []
    budget = max_nodes
    deadline = clock() + SCAN_S if deadline is None else deadline

    def visit(node, depth, window=None):
        nonlocal budget
        if depth > max_depth or budget <= 0 or clock() >= deadline:
            return
        try:
            children = iter(node)
        except Exception:
            return
        # Iterate lazily: an application may have thousands of children.
        while budget > 0 and clock() < deadline:
            try:
                child = next(children)
            except Exception:
                break
            budget -= 1
            try:
                role = child.getRoleName()
            except Exception:
                continue
            child_window = child if role in WINDOW_ROLES else window
            if role in roles and (not active_windows_only or child_window is not None
                                  and "active" in state_names(child_window)):
                point = target_point(child, screen, coords)
                if point is not None:
                    found.append(dict(point, role=role, _node=child, _window=child_window))
            visit(child, depth + 1, child_window)

    try:
        root_window = root if root.getRoleName() in WINDOW_ROLES else None
    except Exception:
        root_window = None
    visit(root, 0, root_window)
    return found


def distinct_targets(found):
    """Drop duplicates and wrappers, keeping the innermost control.

    A browser reports a link inside a row inside a panel, all clickable and
    covering the same pixels; snapping through all three would feel stuck.
    """
    kept: list[dict] = []
    for target in sorted(found, key=lambda t: ((t["right"] - t["left"]) * (t["bottom"] - t["top"])
                                               if "right" in t else 0)):
        if any(abs(target["x"] - other["x"]) <= SAME_CONTROL_PX
               and abs(target["y"] - other["y"]) <= SAME_CONTROL_PX for other in kept):
            continue
        if "right" in target and any(
                target["left"] <= other["x"] <= target["right"]
                and target["top"] <= other["y"] <= target["bottom"] for other in kept):
            continue  # wraps a control we already have
        kept.append(target)
    return kept


class AtspiTargets:
    """Targets in the active window, cached for CACHE_S. Never raises."""

    name = "accessibility"

    def __init__(self, screen, cache_s: float = CACHE_S, clock=time.monotonic,
                 registry=None, roles=ACTIONABLE):
        self.screen = (int(screen[0]), int(screen[1]))
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
        if self._registry is None:
            import pyatspi  # late: it loads GTK and a D-Bus client
            from gi.repository import Atspi

            # Don't let a hung application stall the remote.
            Atspi.set_timeout(150, 150)
            self._registry = pyatspi.Registry
        return self._registry.getDesktop(0)

    def _active_windows(self, applications):
        """Active windows, found without walking what is inside other windows.

        Looking for them first matters for browsers: a walk from the desktop
        spends its whole time budget on panels and background windows before
        reaching page controls twenty levels down.
        """
        windows = []

        def consider(node, depth):
            if depth > WINDOW_DEPTH:
                return
            try:
                role = node.getRoleName()
            except Exception:
                return
            if role not in WINDOW_ROLES:
                return
            if "active" in state_names(node):
                windows.append(node)
                return
            # A dialog can be active inside a frame that isn't.
            try:
                children = itertools.islice(iter(node), 16)
            except Exception:
                return
            for child in children:
                consider(child, depth + 1)

        for application in applications:
            try:
                children = itertools.islice(iter(application), 32)
            except Exception:
                continue
            for window in children:
                consider(window, 1)
        return windows

    def targets(self):
        with self._lock:
            now = self.clock()
            if self._cached is not None and now - self._cached_at < self.cache_s:
                return self._cached
            try:
                applications = list(itertools.islice(iter(self._desktop()), 64))
                found = []
                deadline = time.monotonic() + SCAN_S
                for window in self._active_windows(applications):
                    if time.monotonic() >= deadline:
                        break
                    found.extend(collect_targets(window, self.screen, self.roles,
                                                 deadline=deadline, active_windows_only=True))
                found = distinct_targets(found)
                self._applications = len(applications)
                self._error = None
            except Exception as exc:
                # No pyatspi or no bus: nothing to snap to, but the remote keeps working.
                self._error = str(exc) or type(exc).__name__
                self._applications = 0
                found = []
                LOG.warning("Accessibility targets unavailable: %s", exc)
            self._cached, self._cached_at = found, now
            return found

    def resolve(self, target):
        """Re-read a cached target before moving to it; windows move and close."""
        node = target.get("_node")
        window = target.get("_window")
        if node is None or window is None or "active" not in state_names(window):
            self.invalidate()
            return None
        point = target_point(node, self.screen)
        if point is None:
            self.invalidate()
            return None
        return dict(point, role=target.get("role"), _node=node, _window=window)

    def at_point(self, x, y):
        """{role, label} of what is under a point in the active window, or None."""
        try:
            applications = list(itertools.islice(iter(self._desktop()), 64))
            for window in self._active_windows(applications):
                found = deepest_at(window, x, y)
                if found is None:
                    continue
                try:
                    return {"role": found.getRoleName(), "label": str(found.name or "")[:80]}
                except Exception:
                    continue
        except Exception as exc:
            LOG.warning("Asking what is under the cursor: %s", exc)
        return None

    def invalidate(self):
        with self._lock:
            self._cached, self._cached_at = None, -float("inf")

    def health(self):
        with self._lock:
            count = None if self._cached is None else len(self._cached)
            result = {"ok": self._error is None, "source": self.name,
                      "applications": self._applications, "targets": count,
                      "scope": "active accessible window",
                      "error": self._error}
            if self._error is None and count == 0:
                result["error"] = (
                    "No clickable controls were found in the active accessible window. "
                    "Background windows are excluded because their controls may be covered. "
                    "Some apps do not expose icons or usable screen coordinates; use Pointer "
                    "mode there. NO_AT_BRIDGE=1 also hides an app from accessibility.")
            return result
