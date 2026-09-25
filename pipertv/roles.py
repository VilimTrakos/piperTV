"""Roles: which remote button performs which of Piper's actions.

The TV obeys the same remote. On this Grundig every learned code is RC5
address 0, the TV's own, so pressing left/right also changes the volume. A
role ("up") can be moved to a button the TV ignores ("play") without
relearning anything; the button that used to carry it then does nothing.
"""

from __future__ import annotations

from .buttons import BUTTON_IDS

DIRECTIONS = frozenset({"up", "down", "left", "right"})
# Everything the interface and the cursor act on.
ROLES = tuple(sorted(DIRECTIONS | {"ok", "back", "home", "menu", "exit"}))

# The playback block is laid out like a d-pad and the TV ignores all of it.
# Offered in the UI as a starting point, never applied automatically.
SUGGESTED = {"up": "play", "down": "three_d", "left": "previous",
             "right": "next", "ok": "pause",
             "back": "rewind", "home": "fast_forward"}


def validate_roles(bindings) -> dict:
    """Check a {role: button} map; every button may carry at most one role."""
    if not isinstance(bindings, dict):
        raise ValueError("Role bindings must be a JSON object of role: button.")
    checked: dict[str, str] = {}
    taken: dict[str, str] = {}
    for role, button in bindings.items():
        if role not in ROLES:
            raise ValueError(f"Unknown role {role!r}. Roles are: {', '.join(ROLES)}.")
        if not isinstance(button, str) or button not in BUTTON_IDS:
            raise ValueError(f"Role {role!r} must be bound to a remote button.")
        if button in ROLES and button != role:
            # e.g. "up" on the down key would clash with "down" itself.
            raise ValueError(f"Bind {role!r} to a button the TV ignores, not to {button!r}, "
                             "which is another navigation key.")
        if button in taken:
            raise ValueError(f"The {button!r} button is already bound to {taken[button]!r}. "
                             "One button can carry only one role.")
        checked[role] = button
        taken[button] = role
    return checked


class RoleMap:
    """Translates a recognised button into the action Piper takes."""

    def __init__(self, bindings=None):
        self._bindings = validate_roles(bindings or {})
        self._by_button = {button: role for role, button in self._bindings.items()}
        # Roles moved to another button stop working on their own key.
        self._moved = {role for role, button in self._bindings.items() if button != role}

    def action(self, button_id):
        """The role this button performs, or None (volume, power, a moved key...)."""
        if button_id in self._by_button:
            return self._by_button[button_id]
        if button_id in self._moved:
            return None
        return button_id if button_id in ROLES else None

    def button_for(self, role):
        if role not in ROLES:
            raise ValueError(f"Unknown role {role!r}.")
        return self._bindings.get(role, role if role in BUTTON_IDS else None)

    def bindings(self) -> dict:
        return dict(self._bindings)

    def describe(self) -> list[dict]:
        return [{"role": role, "button": self.button_for(role),
                 "rebound": role in self._moved} for role in ROLES]
