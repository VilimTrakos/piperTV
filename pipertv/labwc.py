"""What the Pi's desktop (labwc) has to be told for Piper's windows to work.

Two things, both found by hand on the first Pi and written down here so the
next one gets them without the search:

  Window rules. A service opens as a window the size of the screen rather
  than a full-screen one, so the on-screen keyboard can be drawn over it --
  and a title bar or a task-switcher entry would give that away.

  The accessibility bus. Snapping the cursor from one control to the next,
  and noticing a search box has been chosen, both ask the desktop's
  accessibility bus what is on the screen; labwc's session keeps GTK off it
  unless NO_AT_BRIDGE says otherwise.

Only the user's own copies are changed. When there is none yet, it starts as
a copy of the system's, so nothing else about the desktop changes whether
labwc merges the two files or reads only the user's.
"""

from __future__ import annotations

from pathlib import Path

RC = Path(".config") / "labwc" / "rc.xml"
ENVIRONMENT = Path(".config") / "labwc" / "environment"
SYSTEM_RC = Path("/etc/xdg/labwc/rc.xml")
SYSTEM_ENVIRONMENT = Path("/etc/xdg/labwc/environment")
RULES = """\
    <!-- PiperTV: pages Piper opens fill the screen without being full screen,
         so a keyboard can be drawn over them. Nothing should give that away:
         no title bar, and no place in the task switcher. -->
    <windowRule identifier="chrome-*" serverDecoration="no" skipTaskbar="yes"/>
    <windowRule identifier="chromium*" serverDecoration="no"/>
"""
ROOTS = ("</openbox_config>", "</labwc_config>")
SKELETON = '<?xml version="1.0" encoding="UTF-8"?>\n<labwc_config>\n</labwc_config>\n'
ACCESSIBILITY = "NO_AT_BRIDGE=0"


def _start_from(path: Path, system: Path, empty: str) -> str:
    """The user's file, or a copy of the system's to add to."""
    for candidate in (path, system):
        try:
            return candidate.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
    return empty


def add_window_rules(home: Path, system: Path = SYSTEM_RC) -> Path | None:
    """Give Piper's windows no title bar. Returns the file, if it changed."""
    path = home / RC
    text = _start_from(path, system, SKELETON)
    if 'identifier="chrome-*"' in text:
        return None  # there already, whoever put it there
    lines = text.splitlines(keepends=True)
    opening = next((index for index, line in enumerate(lines)
                    if line.strip() == "<windowRules>"), None)
    if opening is not None:
        lines.insert(opening + 1, RULES)
    else:
        closing = next((index for index in range(len(lines) - 1, -1, -1)
                        if lines[index].strip() in ROOTS), None)
        if closing is None:
            return None  # not a file this can safely add to
        lines.insert(closing, "  <windowRules>\n" + RULES + "  </windowRules>\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines), encoding="utf-8")
    return path


def allow_accessibility(home: Path, system: Path = SYSTEM_ENVIRONMENT) -> Path | None:
    """Let the desktop's apps onto the accessibility bus. Returns the file, if changed."""
    path = home / ENVIRONMENT
    text = _start_from(path, system, "")
    lines = text.splitlines()
    if ACCESSIBILITY in (line.strip() for line in lines):
        return None
    # A value of its own is replaced rather than contradicted: GTK reads the
    # value, not whether the name is set at all.
    replaced = [ACCESSIBILITY if line.strip().startswith("NO_AT_BRIDGE=") else line
                for line in lines]
    if replaced == lines:
        replaced += ([""] if lines and lines[-1].strip() else []) + [
            "# PiperTV: let desktop apps register with the accessibility bus", ACCESSIBILITY]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(replaced) + "\n", encoding="utf-8")
    return path
