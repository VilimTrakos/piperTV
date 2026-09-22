"""Piper's own settings file, for what has to be right before the remote works.

Most of Piper is set from the television, with the remote. Which pin the IR
receiver's OUT wire is connected to cannot be: until it is right, the remote
does nothing at all. So it lives in a plain file beside the program as well,
readable and editable with any text editor before Piper has heard a button,
and written by the TV's options whenever a pin is chosen there.

Only the line Piper owns is ever rewritten; comments and anything a person
added stay as they were.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from .gpio_ir import AUTO, validate_pin
from .ini import ini_get, ini_set

LOG = logging.getLogger(__name__)

CONFIG = Path(__file__).resolve().parent.parent / "pipertv.conf"
TEMPLATE = """\
# PiperTV settings. Piper reads this file when it starts. Choosing a pin in
# the TV's options (options > ir receiver) writes it here and takes effect at
# once; after editing it by hand, restart Piper.

[ir]
# The GPIO pin the IR receiver's OUT wire is connected to, by its BCM number
# -- not its position on the header: GPIO17 is physical pin 11.
#
#   pin = auto   the kernel's receiver, if /boot/firmware/config.txt sets one
#                up with dtoverlay=gpio-ir; otherwise GPIO17
#   pin = 18     the receiver is on GPIO18 (physical pin 12); any of 2 to 27
pin = {pin}
"""


def read_pin(path: Path = CONFIG) -> str | int:
    """The pin the file names, or auto when it names none it can be held to.

    A file that says something unusable is not a reason for Piper to stop:
    the remote is what gets it fixed, so it falls back to finding the pin by
    itself, and says why in the log.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return AUTO
    except OSError as exc:
        LOG.warning("Could not read %s: %s", path, exc)
        return AUTO
    value = ini_get(text, "ir", "pin")
    if value is None:
        return AUTO
    try:
        return validate_pin(value)
    except ValueError as exc:
        LOG.warning("Ignoring the IR pin in %s: %s", path, exc)
        return AUTO


def write_pin(value, path: Path = CONFIG) -> str | int:
    """Keep a chosen pin in the file, creating it with its explanation."""
    checked = validate_pin(value)
    path = Path(path)
    try:
        text = ini_set(path.read_text(encoding="utf-8"), "ir", "pin", str(checked))
    except FileNotFoundError:
        text = TEMPLATE.format(pin=checked)
    # Whole or not at all: a half-written file is one Piper could not read.
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return checked
