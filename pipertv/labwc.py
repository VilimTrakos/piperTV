"""Desktop (labwc) settings Piper's windows need.

* Window rules: services open as screen-sized windows rather than full
  screen, so the on-screen keyboard can be drawn over them. The rules remove
  their title bar and task switcher entry, so they still look full screen.
* The accessibility bus: snapping and noticing a focused search box read it,
  and labwc's session keeps GTK apps off it unless NO_AT_BRIDGE=0.

Only the user's own copies are changed. If there is none yet it starts as a
copy of the system file, so nothing else about the desktop changes.
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
    for candidate in (path, system):
        try:
            return candidate.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
    return empty


def add_window_rules(home: Path, system: Path = SYSTEM_RC) -> Path | None:
    """Add Piper's window rules to rc.xml. Returns the file if it changed."""
    path = home / RC
    text = _start_from(path, system, SKELETON)
    if 'identifier="chrome-*"' in text:
        return None
    lines = text.splitlines(keepends=True)
    opening = next((index for index, line in enumerate(lines)
                    if line.strip() == "<windowRules>"), None)
    if opening is not None:
        lines.insert(opening + 1, RULES)
    else:
        closing = next((index for index in range(len(lines) - 1, -1, -1)
                        if lines[index].strip() in ROOTS), None)
        if closing is None:
            return None  # not a file we can safely edit
        lines.insert(closing, "  <windowRules>\n" + RULES + "  </windowRules>\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines), encoding="utf-8")
    return path


def allow_accessibility(home: Path, system: Path = SYSTEM_ENVIRONMENT) -> Path | None:
    """Set NO_AT_BRIDGE=0 in labwc's environment. Returns the file if it changed."""
    path = home / ENVIRONMENT
    lines = _start_from(path, system, "").splitlines()
    if ACCESSIBILITY in (line.strip() for line in lines):
        return None
    # Replace an existing value: GTK looks at the value, not just whether it's set.
    replaced = [ACCESSIBILITY if line.strip().startswith("NO_AT_BRIDGE=") else line
                for line in lines]
    if replaced == lines:
        replaced += ([""] if lines and lines[-1].strip() else []) + [
            "# PiperTV: let desktop apps register with the accessibility bus", ACCESSIBILITY]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(replaced) + "\n", encoding="utf-8")
    return path
