"""pipertv.conf: settings that must be right before the remote can work.

Everything else is set from the TV with the remote, but the IR receiver's pin
can't be: until it's right the remote does nothing. So it lives in a plain
file next to the program, editable by hand, and is also written when a pin is
chosen in the TV's options. Only the pin line is ever rewritten.
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
#   pin = 27     the receiver is on GPIO27 (physical pin 13); any of 2 to 27
#
# Pins with no second name on the pinout (5, 6, 16, 17, 22 to 27) are the
# safe choice. The others work too while their other job is off, but they
# are the ones I2C, SPI, the serial port, PWM, 1-Wire or I2S audio would use.
pin = {pin}
"""


def read_pin(path: Path = CONFIG) -> str | int:
    """The pin from the file, or auto if it is missing or unusable."""
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
        # Don't refuse to start: the remote may still work with auto.
        LOG.warning("Ignoring the IR pin in %s: %s", path, exc)
        return AUTO


def write_pin(value, path: Path = CONFIG) -> str | int:
    """Save the pin, creating the file from TEMPLATE if needed."""
    checked = validate_pin(value)
    path = Path(path)
    try:
        text = ini_set(path.read_text(encoding="utf-8"), "ir", "pin", str(checked))
    except FileNotFoundError:
        text = TEMPLATE.format(pin=checked)
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
