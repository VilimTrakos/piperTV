"""Get or set one key in an ini file without touching the rest of it.

Used for files people edit by hand (pipertv.conf) and for other programs'
settings (the panel, the file manager), so comments and order must survive.
"""

from __future__ import annotations


def _key(line: str) -> str | None:
    stripped = line.strip()
    return stripped.split("=", 1)[0].strip() if "=" in stripped else None


def _section(line: str) -> str | None:
    stripped = line.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        return stripped[1:-1].strip()
    return None


def ini_get(text: str, section: str, key: str) -> str | None:
    current = None
    for line in text.splitlines():
        if _section(line) is not None:
            current = _section(line)
        elif current == section and _key(line) == key:
            return line.split("=", 1)[1].strip()
    return None


def ini_set(text: str, section: str, key: str, value: str) -> str:
    lines = text.splitlines()
    current, section_at = None, None
    for index, line in enumerate(lines):
        if _section(line) is not None:
            current = _section(line)
            if current == section and section_at is None:
                section_at = index
        elif current == section and _key(line) == key:
            # Keep the spacing style of the existing line ("key=v" or "key = v").
            name, old = line.split("=", 1)
            lines[index] = f"{name}={' ' if old.startswith(' ') else ''}{value}"
            return "\n".join(lines) + "\n"
    if section_at is None:
        lines += ([""] if lines else []) + [f"[{section}]", f"{key}={value}"]
    else:
        lines.insert(section_at + 1, f"{key}={value}")
    return "\n".join(lines) + "\n"
