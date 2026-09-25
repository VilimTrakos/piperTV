"""A black full-screen window behind the interface.

While a service is open the interface is closed to free memory (a chromium
window is ~400 MB on a Pi 3B+). But labwc only hides the desktop panel while
a full-screen window is open, and the panel's own autohide doesn't work under
labwc, so without a full-screen window the panel covers the top of the
service and the desktop flashes up while the interface restarts. This window
(about 30 MB, GTK) takes over that job. It is started before the interface so
that everything else lands on top of it.

It runs as its own process (python -m pipertv.backdrop), so it survives an
app restart, and is found again by that command line.
"""

from __future__ import annotations

import logging
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

from .util import find_processes, stop_process

LOG = logging.getLogger(__name__)

MODULE = "pipertv.backdrop"
ROOT = Path(__file__).resolve().parent.parent
READY_S = 8.0          # Python + GTK can take a couple of seconds on a Pi
RETRY_AFTER_S = 600.0  # after a failure, don't make every launch wait again
CLOSE_GRACE_S = 2.0
# The interface's background colour and a dimmed wordmark.
STYLE = b"""
window { background: #08090B; }
label {
  color: rgba(242, 244, 247, .3);
  font-family: Cantarell, "Nimbus Sans", "Liberation Sans", sans-serif;
  font-weight: 300;
  font-size: 120px;
  letter-spacing: .28em;
  padding-left: .28em;
}
"""


def _wait_until_shown(process, clock=time.monotonic, timeout_s: float = READY_S) -> bool:
    """Wait for the child to print "ready" (it does once its window is mapped)."""
    stream = process.stdout
    if stream is None:
        return False
    deadline = clock() + timeout_s
    try:
        while True:
            remaining = deadline - clock()
            if remaining <= 0:
                return False
            readable, _, _ = select.select([stream], [], [], remaining)
            if not readable:
                return False
            line = stream.readline()
            if not line:
                return False  # exited before showing a window
            if line.strip() == b"ready":
                return True
    finally:
        stream.close()


class Backdrop:
    def __init__(self, procfs: str = "/proc", spawn=subprocess.Popen, kill=os.kill,
                 clock=time.monotonic, sleep=time.sleep, wait_until_shown=None,
                 python: str = sys.executable, grace_s: float = CLOSE_GRACE_S):
        self.procfs = procfs
        self.spawn = spawn
        self.kill = kill
        self.clock = clock
        self.sleep = sleep
        self.wait_until_shown = wait_until_shown or _wait_until_shown
        self.python = python
        self.grace_s = float(grace_s)
        self._error: str | None = None
        self._failed_at = -float("inf")

    def processes(self) -> list[int]:
        return find_processes(lambda argv: MODULE in argv, self.procfs)

    def showing(self) -> bool:
        return bool(self.processes())

    def show(self) -> dict:
        if self.showing() or self.clock() - self._failed_at < RETRY_AFTER_S:
            return self.snapshot()
        try:
            process = self.spawn([self.python, "-m", MODULE], cwd=str(ROOT),
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, start_new_session=True,
                                 close_fds=True)
        except OSError as exc:
            self._error = f"Could not start the black screen behind Piper: {exc}"
            self._failed_at = self.clock()
            LOG.warning("%s", self._error)
            return self.snapshot()
        if self.wait_until_shown(process):
            self._error = None
            LOG.info("Started the black screen behind the interface")
        else:
            self._error = "The black screen behind Piper did not appear."
            self._failed_at = self.clock()
            LOG.warning("%s", self._error)
            try:
                process.kill()
            except OSError:
                pass
        return self.snapshot()

    def close(self) -> dict:
        for pid in self.processes():
            try:
                stop_process(pid, self.grace_s, self.kill, self.procfs, self.clock, self.sleep)
            except OSError as exc:
                self._error = f"Could not close the black screen behind Piper: {exc}"
                LOG.warning("%s", self._error)
        return self.snapshot()

    def snapshot(self) -> dict:
        return {"showing": self.showing(), "error": self._error}


def main() -> None:
    """The window itself: black, full screen, the wordmark, no cursor."""
    import gi

    gi.require_version("Gdk", "3.0")
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gdk, GLib, Gtk

    style = Gtk.CssProvider()
    style.load_from_data(STYLE)
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), style,
                                             Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    window = Gtk.Window(title="Piper")
    window.add(Gtk.Label(label="piper"))
    window.connect("destroy", Gtk.main_quit)

    def shown(*_args):
        window.get_window().set_cursor(Gdk.Cursor.new_from_name(window.get_display(), "none"))
        print("ready", flush=True)
        # Nobody reads the pipe after this, so stop writing to it.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return False

    window.connect("map-event", shown)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, Gtk.main_quit)
    window.fullscreen()
    window.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
