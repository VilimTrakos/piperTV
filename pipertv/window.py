"""How much of the screen Piper takes: all of it, or a window on it.

A television wants the whole screen and nothing else on it. The same Pi seen
over VNC, with a keyboard and a mouse and work to do, wants Piper in a window
like any other program -- and until now the only way to get one was to stop
Piper altogether.

One setting decides it for everything Piper puts on the screen: its own
interface and whatever service it opens. Kept with the recordings, beside the
role bindings and the cursor's behaviour, because it belongs to this room
rather than to this process.
"""

from __future__ import annotations

WINDOW_DEFAULTS = {"windowed": False, "width": 1280, "height": 720}
# Nothing smaller than a usable window, nothing larger than a screen Piper
# could plausibly be shown on. A size larger than the screen is not refused:
# the compositor clamps it, and refusing it would be a rule about the monitor
# rather than about the setting.
WINDOW_LIMITS = {"width": (320, 7680), "height": (240, 4320)}


def validate_window(values) -> dict:
    """Check a window preference, returning a complete, plain copy of it.

    Anything absent keeps its default, so one field can be sent on its own.
    """
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
    """The size and position a window should have: (width, height), (x, y).

    Full screen means exactly the screen, at its corner. A window is centred
    on it, and never bigger than the screen it has to fit on -- a window whose
    title bar is off the top of the screen cannot be moved back.
    """
    checked = validate_window(settings if isinstance(settings, dict) else {})
    width, height = int(screen[0]), int(screen[1])
    if not checked["windowed"]:
        return (width, height), (0, 0)
    size = (min(checked["width"], width), min(checked["height"], height))
    return size, ((width - size[0]) // 2, (height - size[1]) // 2)
