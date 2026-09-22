"""Read and change one value in an ini file, leaving every other line alone.

The files Piper touches are other programs' own -- the panel's, the file
manager's -- and its own settings file is one a person edits by hand. A
parser that rewrites the whole file would lose their comments and their
order; these change the one line that is Piper's business.
"""

from __future__ import annotations


def ini_get(text: str, section: str, key: str) -> str | None:
    """One value from an ini file's text, if that section sets it."""
    current = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1].strip()
        elif current == section and "=" in stripped \
                and stripped.split("=", 1)[0].strip() == key:
            return stripped.split("=", 1)[1].strip()
    return None


def ini_set(text: str, section: str, key: str, value: str) -> str:
    """The same ini with one value set, and every other line as it was."""
    wanted = f"{key}={value}"
    lines = text.splitlines()
    current, section_at = None, None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1].strip()
            if current == section and section_at is None:
                section_at = index
        elif current == section and "=" in stripped \
                and stripped.split("=", 1)[0].strip() == key:
            # Written the way the person who wrote the line wrote it.
            name, old = line.split("=", 1)
            lines[index] = f"{name}={' ' if old.startswith(' ') else ''}{value}"
            return "\n".join(lines) + "\n"
    if section_at is None:
        lines += ([""] if lines else []) + [f"[{section}]", wanted]
    else:
        lines.insert(section_at + 1, wanted)
    return "\n".join(lines) + "\n"
