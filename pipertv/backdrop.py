"""A black screen behind everything Piper shows, so the desktop never does.

The interface is a whole browser -- close to 400 MB on a Pi 3B+ -- and nothing
needs it while a service fills the screen, so it is closed then. But as a
full-screen window it was doing a second job nobody had asked of it: labwc
hides the desktop panel while a full-screen window is open, and the panel's own
autohide does nothing under labwc (it waits for a Wayfire protocol). With the
interface gone the panel sat across the top of every service, and coming back
showed the desktop for the ten seconds a browser takes to start.

This is that second job on its own, for about 30 MB: a full-screen window that
is only ever seen between two things, with the wordmark on it. It is started
before the interface, so the interface and everything opened from it land on
top of it, and it stays for as long as Piper fills the screen.

It is a process of its own, as the interface is, so it outlives the app being
restarted; it is recognised by its module name on the command line. GTK is
used because the desktop already has it loaded, which is most of why it is
cheap.
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

LOG = logging.getLogger(__name__)

MODULE = "pipertv.backdrop"
ROOT = Path(__file__).resolve().parent.parent
# Starting Python and GTK on a busy Pi 3B+ takes a second or two; much longer
# means it is not going to appear, and a launch should not wait on it.
READY_S = 8.0
# One that could not be started is not tried again on every launch: each try
# is a wait in front of the service. Without it Piper simply keeps the
# interface behind the service, as it always used to.
RETRY_AFTER_S = 600.0
CLOSE_GRACE_S = 2.0
POLL_S = 0.1
# The interface's own screen colour and wordmark, dimmed: this is seen only
# between two things, and should look like the pause it is.
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
    """Whether the window said it is on the screen before the time ran out."""
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
                return False  # it exited before its window appeared
            if line.strip() == b"ready":
                return True
    finally:
        stream.close()


class Backdrop:
    """The full-screen black window that sits behind the interface."""

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
        """Process ids of backdrops on this Pi, whoever started them."""
        found = []
        try:
            entries = sorted(int(name) for name in os.listdir(self.procfs) if name.isdigit())
        except OSError as exc:
            self._error = f"Could not read {self.procfs}: {exc}"
            return []
        for pid in entries:
            try:
                with open(f"{self.procfs}/{pid}/cmdline", "rb") as handle:
                    arguments = [part.decode("utf-8", "replace")
                                 for part in handle.read().split(b"\0") if part]
            except OSError:
                continue  # the process ended while this was reading
            if MODULE in arguments:
                found.append(pid)
        return found

    def showing(self) -> bool:
        return bool(self.processes())

    def show(self) -> dict:
        """Make sure the backdrop is on the screen, starting it if it is not."""
        if self.showing():
            return self.snapshot()
        if self.clock() - self._failed_at < RETRY_AFTER_S:
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
        """Take it away: Piper is leaving the screen, or no longer fills it."""
        for pid in self.processes():
            try:
                self.kill(pid, signal.SIGTERM)
            except OSError as exc:
                self._error = f"Could not close the black screen behind Piper: {exc}"
                LOG.warning("%s", self._error)
                continue
            deadline = self.clock() + self.grace_s
            while pid in self.processes() and self.clock() < deadline:
                self.sleep(POLL_S)
            if pid in self.processes():
                try:
                    self.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        return self.snapshot()

    def snapshot(self) -> dict:
        return {"showing": self.showing(), "error": self._error}


def main() -> None:
    """The window itself: black, full screen, the wordmark, and nothing else."""
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
        # A pointer resting on an otherwise empty screen would be all there is
        # to look at.
        window.get_window().set_cursor(Gdk.Cursor.new_from_name(window.get_display(), "none"))
        print("ready", flush=True)
        # Nobody reads the rest of this pipe, so nothing else may be written to it.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return False

    window.connect("map-event", shown)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, Gtk.main_quit)
    window.fullscreen()
    window.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
