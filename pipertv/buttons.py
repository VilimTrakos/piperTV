"""The buttons of the One For All remote, row by row as they sit on it.

Ids are what the library stores and never change; labels can be renamed.
"""

_ROWS = [
    ("power", [("power", "Power")]),
    ("numbers", [(f"digit_{n}", str(n)) for n in (1, 2, 3)]),
    ("numbers", [(f"digit_{n}", str(n)) for n in (4, 5, 6)]),
    ("numbers", [(f"digit_{n}", str(n)) for n in (7, 8, 9)]),
    ("numbers", [("favorites", "Favorites"), ("digit_0", "0"), ("guide", "Guide")]),
    ("colors", [("red", "Red"), ("green", "Green"), ("yellow", "Yellow"), ("blue", "Blue")]),
    ("navigation", [("exit", "Exit"), ("help", "Help"), ("info", "Info")]),
    ("navigation", [("home", "Home"), ("back", "Back")]),
    ("navigation", [("up", "Up")]),
    ("navigation", [("left", "Left"), ("ok", "OK"), ("right", "Right")]),
    ("navigation", [("down", "Down")]),
    ("navigation", [("menu", "Menu"), ("list", "List")]),
    ("controls", [("volume_up", "Volume +"), ("source", "Source"), ("channel_up", "Channel +")]),
    ("controls", [("volume_down", "Volume −"), ("mute", "Mute"), ("channel_down", "Channel −")]),
    ("apps", [("media", "Media"), ("nettv", "NETTV")]),
    ("playback", [("rewind", "Rewind"), ("play", "Play"), ("fast_forward", "Fast forward")]),
    ("playback", [("previous", "Previous"), ("pause", "Pause"), ("next", "Next")]),
    ("playback", [("record", "Record"), ("three_d", "3D"), ("stop", "Stop")]),
    ("teletext", [("tv_radio", "TV / Radio"), ("audio", "Audio"), ("format", "Picture format")]),
]

BUTTONS = [
    {"id": key, "label": label, "section": section, "row": row, "col": col}
    for row, (section, entries) in enumerate(_ROWS)
    for col, (key, label) in enumerate(entries)
]
BUTTON_IDS = frozenset(button["id"] for button in BUTTONS)
