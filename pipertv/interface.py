"""Open and close the Piper interface itself, so it is never a dead end.

The interface is a full-screen browser window with no title bar, and in front
of it there is a sofa rather than a keyboard. Until now the only ways out were
an SSH session or plugging a keyboard into the Pi -- which is no way out at all
for the person actually watching.

A window is recognised by the address it was opened with: this app's own port
followed by /tv. Never by the browser's name, so an unrelated chromium is left
alone, and never by a window title, which a kiosk does not show. The browser
process is told apart from its dozen child processes by the absence of
--type=, which every child carries.

Closing is asked for politely first. A browser given SIGTERM saves its session
and goes; SIGKILL is the second attempt, because a window that will not close
is exactly the situation this module exists to end.

Opening is here for the same reason it is closed here: whoever decides the
interface should be on the screen -- a deploy, a change of window shape, the
person who closed it by mistake -- should not have to spell out a browser
command line to get it back.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import time

from .window import geometry, validate_window

LOG = logging.getLogger(__name__)

CLOSE_GRACE_S = 2.0
POLL_S = 0.1
# Established with this Pi and this compositor: without the first two the EGL
# context fails and the page never renders, and chromium blocks on the desktop
# keyring prompt, which nobody can answer from a sofa.
INTERFACE_ARGS = ("--disable-gpu", "--password-store=basic", "--no-first-run",
                  "--noerrdialogs", "--no-default-browser-check",
                  "--disable-session-crashed-bubble", "--hide-crash-restore-bubble",
                  "--force-renderer-accessibility")
PROFILE = "/tmp/kiosk-gpu-off"
BROWSERS = ("chromium", "chromium-browser", "google-chrome")
# Long enough for a browser on a Pi 3B+ to have a window, short enough that a
# failure is reported rather than waited on.
START_GRACE_S = 20.0


class Interface:
    """The browser window showing this app's interface on the Pi's screen."""

    def __init__(self, port: int = 8765, procfs: str = "/proc", kill=os.kill,
                 clock=time.monotonic, sleep=time.sleep, grace_s: float = CLOSE_GRACE_S,
                 browser=None, spawn=subprocess.Popen, environ=None,
                 profile: str = PROFILE, screen=(1920, 1080)):
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ValueError("The interface port must be between 1 and 65535.")
        self.port = port
        self.procfs = procfs
        self.kill = kill
        self.clock = clock
        self.sleep = sleep
        self.grace_s = float(grace_s)
        self.spawn = spawn
        self.environ = os.environ if environ is None else environ
        self.profile = profile
        self.screen = (int(screen[0]), int(screen[1]))
        self.browser = browser or self._find_browser()
        self._error: str | None = None

    @staticmethod
    def _find_browser():
        for name in BROWSERS:
            found = shutil.which(name)
            if found:
                return found
        return None

    # --- finding it -------------------------------------------------------

    def _address(self) -> str:
        return f":{self.port}/tv"

    def windows(self) -> list[int]:
        """Process ids of browsers showing the interface, newest last."""
        found = []
        try:
            entries = sorted(int(name) for name in os.listdir(self.procfs) if name.isdigit())
        except OSError as exc:
            self._error = f"Could not read {self.procfs}: {exc}"
            return []
        for pid in entries:
            try:
                with open(f"{self.procfs}/{pid}/cmdline", "rb") as handle:
                    parts = handle.read().split(b"\0")
            except OSError:
                continue  # the process ended while this was reading, or is not ours
            arguments = [part.decode("utf-8", "replace") for part in parts if part]
            if not arguments or any(part.startswith("--type=") for part in arguments):
                continue
            if any(self._address() in part for part in arguments):
                found.append(pid)
        return found

    def showing(self) -> bool:
        return bool(self.windows())

    # --- closing it -------------------------------------------------------

    def _alive(self, pid: int) -> bool:
        try:
            self.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # someone else's process: still there, not ours to end
        except OSError:
            return False
        return True

    def close(self) -> dict:
        """Ask the interface window to go, and insist if it does not."""
        windows = self.windows()
        if not windows:
            return {"closed": [], "showing": False, "error": self._error}
        self._error = None
        closed = []
        for pid in windows:
            try:
                self.kill(pid, signal.SIGTERM)
            except OSError as exc:
                self._error = f"Could not close the interface: {exc}"
                LOG.warning("%s", self._error)
                continue
            deadline = self.clock() + self.grace_s
            while self._alive(pid) and self.clock() < deadline:
                self.sleep(POLL_S)
            if self._alive(pid):
                try:
                    self.kill(pid, signal.SIGKILL)
                except OSError as exc:
                    self._error = f"Could not close the interface: {exc}"
                    LOG.warning("%s", self._error)
                    continue
            closed.append(pid)
            LOG.info("Closed the Piper interface (pid %s)", pid)
        return {"closed": closed, "showing": self.showing(), "error": self._error}

    # --- opening it -------------------------------------------------------

    def command(self, window=None) -> list[str]:
        """The browser command that puts the interface on this Pi's screen."""
        if not self.browser:
            raise RuntimeError("No chromium browser was found on this Pi, so the "
                               "interface cannot be put on the screen.")
        settings = validate_window(window or {})
        command = [self.browser, *INTERFACE_ARGS, f"--user-data-dir={self.profile}"]
        if self.environ.get("WAYLAND_DISPLAY"):
            # Chromium otherwise picks its X11 backend and exits with "Missing X
            # server or $DISPLAY" on a Wayland session such as this Pi's labwc.
            command.append("--ozone-platform=wayland")
        address = f"http://127.0.0.1:{self.port}/tv?boot=0"
        if not settings["windowed"]:
            command += ["--kiosk", "--start-fullscreen", address]
            return command
        # A window of its own, with no tab strip and no address bar: the same
        # page, but with the desktop around it for whoever is working on the Pi.
        (width, height), (left, top) = geometry(settings, self.screen)
        command += [f"--window-size={width},{height}",
                    f"--window-position={left},{top}", f"--app={address}"]
        return command

    def open(self, window=None) -> dict:
        """Put the interface on the screen, replacing any window already showing it.

        Replacing rather than leaving it be, because the one reason to ask for
        this while it is already up is that its shape should change, and a
        browser cannot be talked out of the shape it was started with.
        """
        self.close()
        try:
            command = self.command(window)
        except RuntimeError as exc:
            self._error = str(exc)
            LOG.warning("%s", self._error)
            return self.snapshot()
        try:
            self.spawn(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
            self._error = None
            LOG.info("Started the Piper interface%s",
                     " in a window" if validate_window(window or {})["windowed"] else "")
        except Exception as exc:  # noqa: BLE001 - a missing browser is a desktop condition
            self._error = str(exc) or type(exc).__name__
            LOG.warning("Starting the Piper interface: %s", exc)
            return self.snapshot()
        deadline = self.clock() + START_GRACE_S
        while not self.showing() and self.clock() < deadline:
            self.sleep(POLL_S)
        if not self.showing():
            self._error = "The interface was started but no window appeared."
            LOG.warning("%s", self._error)
        return self.snapshot()

    def snapshot(self) -> dict:
        return {"port": self.port, "showing": self.showing(), "error": self._error,
                "browser": self.browser}
