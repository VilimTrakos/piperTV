"""Bind Piper's navigation roles to whichever physical button sends them.

The television obeys its own remote, so the buttons Piper wants for navigation
are exactly the ones the TV also acts on. On this Grundig every learned code is
RC5 address 0 -- the set's own address -- and the cursor keys arrive as its
volume commands, which is why moving left and right changes the volume.

Roles break that tie. A role is what Piper does ("up"); a button id is what the
remote sends ("play"). Binding "up" to the play key lets the playback block --
which this TV ignores -- drive the interface, without relearning any signal:
the recordings stay exactly as they were captured.

Rebinding a role also takes it away from its original key. Once "up" lives on
the play button, the remote's own up arrow no longer navigates; otherwise the
conflicting key would keep working and the conflict would not actually go away.
"""

from __future__ import annotations

from .buttons import BUTTON_IDS
from .ir_control import DIRECTIONS

# Everything the interface and the cursor can be asked to do. Kept equal to
# tv.NAVIGATION by test, so a role can never be introduced that nothing acts on.
ROLES = tuple(sorted(set(DIRECTIONS) | {"ok", "back", "home", "menu", "exit"}))


# The playback block is physically a five-way pad -- play sits above pause,
# with previous and next either side and 3D below -- and this TV acts on none
# of it. Offered as a starting point, never applied without being asked for.
SUGGESTED = {"up": "play", "down": "three_d", "left": "previous",
             "right": "next", "ok": "pause",
             "back": "rewind", "home": "fast_forward"}


def validate_roles(bindings) -> dict:
    """Check a role->button map, returning a plain copy of it.

    Rejects anything that would make a press ambiguous, because the reader has
    to turn one signal into exactly one action with no tie to break.
    """
    if not isinstance(bindings, dict):
        raise ValueError("Role bindings must be a JSON object of role: button.")
    checked: dict[str, str] = {}
    for role, button in bindings.items():
        if role not in ROLES:
            raise ValueError(f"Unknown role {role!r}. Roles are: {', '.join(ROLES)}.")
        if not isinstance(button, str) or button not in BUTTON_IDS:
            raise ValueError(f"Role {role!r} must be bound to a remote button.")
        if button in ROLES and button != role:
            # "up" on the down key would fight the identity binding of "down".
            raise ValueError(
                f"Bind {role!r} to a button the TV ignores, not to {button!r}, "
                "which is another navigation key.")
        checked[role] = button
    taken: dict[str, str] = {}
    for role, button in checked.items():
        if button in taken:
            raise ValueError(
                f"The {button!r} button is already bound to {taken[button]!r}. "
                "One button can carry only one role.")
        taken[button] = role
    return checked


class RoleMap:
    """Translate a recognised button into the action Piper should take."""

    def __init__(self, bindings=None):
        self._bindings = validate_roles(bindings or {})
        self._by_button = {button: role for role, button in self._bindings.items()}
        # A role moved elsewhere must stop answering on its original key.
        self._moved = {role for role, button in self._bindings.items() if button != role}

    def action(self, button_id):
        """The role this button now performs, or None if it performs none.

        A button with no role is still a real press worth recording; it simply
        does not navigate. That covers volume, power and every unbound key.
        """
        if button_id in self._by_button:
            return self._by_button[button_id]
        if button_id in self._moved:
            return None
        return button_id if button_id in ROLES else None

    def button_for(self, role):
        """Which physical button currently carries a role."""
        if role not in ROLES:
            raise ValueError(f"Unknown role {role!r}.")
        return self._bindings.get(role, role if role in BUTTON_IDS else None)

    def bindings(self) -> dict:
        return dict(self._bindings)

    def describe(self) -> list[dict]:
        """Every role with the button that carries it, for the studio to show."""
        return [{"role": role, "button": self.button_for(role),
                 "rebound": role in self._moved} for role in ROLES]

    def __eq__(self, other):
        return isinstance(other, RoleMap) and other._bindings == self._bindings

    def __repr__(self):
        return f"RoleMap({self._bindings!r})"
