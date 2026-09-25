"""Whether Piper fills the screen or runs in a window (e.g. when used over VNC).

The setting applies to the interface and to every service it opens, and is
saved in the library.
"""

from __future__ import annotations

WINDOW_DEFAULTS = {"windowed": False, "width": 1280, "height": 720}
# A size bigger than the screen is allowed; the compositor clamps it.
WINDOW_LIMITS = {"width": (320, 7680), "height": (240, 4320)}


def validate_window(values) -> dict:
    """Check window settings; missing fields keep their defaults."""
    if not isinstance(values, dict):
        raise ValueError("Window settings must be a JSON object.")
    settings = dict(WINDOW_DEFAULTS)
    for name, value in values.items():
        if name not in settings:
            raise ValueError(f"Unknown window setting {name!r}. "
                             f"Settings are: {', '.join(sorted(settings))}.")
        if name == "windowed":
            if not isinstance(value, bool):
                raise ValueError("windowed is either true or false.")
            settings[name] = value
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a number.")
        low, high = WINDOW_LIMITS[name]
        if not low <= value <= high:
            raise ValueError(f"{name} must be between {low} and {high}.")
        settings[name] = int(value)
    return settings


def geometry(settings, screen) -> tuple[tuple[int, int], tuple[int, int]]:
    """((width, height), (x, y)) for a window: the whole screen, or centred on it."""
    checked = validate_window(settings if isinstance(settings, dict) else {})
    width, height = int(screen[0]), int(screen[1])
    if not checked["windowed"]:
        return (width, height), (0, 0)
    # Never larger than the screen: a title bar above the top edge can't be grabbed.
    size = (min(checked["width"], width), min(checked["height"], height))
    return size, ((width - size[0]) // 2, (height - size[1]) // 2)
