"""Open and close the Piper interface (a chromium window showing /tv).

The window is recognised by the URL it was opened with, this app's port plus
/tv, never by the browser name or window title, so other chromium windows are
left alone. Chromium's child processes carry --type=, the main one doesn't.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from urllib.parse import quote

from .util import find_processes, find_program, stop_process
from .window import geometry, validate_window

LOG = logging.getLogger(__name__)

CLOSE_GRACE_S = 2.0
POLL_S = 0.1
# See launcher.KIOSK_ARGS for why the first two are needed on a Pi 3B+.
INTERFACE_ARGS = ("--disable-gpu", "--password-store=basic", "--no-first-run",
                  "--noerrdialogs", "--no-default-browser-check",
                  "--disable-session-crashed-bubble", "--hide-crash-restore-bubble",
                  "--force-renderer-accessibility", "--enable-low-end-device-mode")
PROFILE = "/tmp/kiosk-gpu-off"
BROWSERS = ("chromium", "chromium-browser", "google-chrome")
# How long a browser on a Pi 3B+ may take to show a window.
START_GRACE_S = 20.0


class Interface:
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
        self.browser = browser or find_program(*BROWSERS)
        self._error: str | None = None

    def windows(self) -> list[int]:
        """Pids of the browsers showing the interface."""
        address = f":{self.port}/tv"

        def is_interface(argv):
            return (not any(arg.startswith("--type=") for arg in argv)
                    and any(address in arg for arg in argv))

        return find_processes(is_interface, self.procfs)

    def showing(self) -> bool:
        return bool(self.windows())

    def close(self) -> dict:
        windows = self.windows()
        if not windows:
            return {"closed": [], "showing": False, "error": self._error}
        self._error = None
        closed = []
        for pid in windows:
            try:
                stop_process(pid, self.grace_s, self.kill, self.procfs, self.clock, self.sleep)
            except OSError as exc:
                self._error = f"Could not close the interface: {exc}"
                LOG.warning("%s", self._error)
                continue
            closed.append(pid)
            LOG.info("Closed the Piper interface (pid %s)", pid)
        return {"closed": closed, "showing": self.showing(), "error": self._error}

    def command(self, window=None, focus=None) -> list[str]:
        """The chromium command line. `focus` is the tile to start on."""
        if not self.browser:
            raise RuntimeError("No chromium browser was found on this Pi, so the "
                               "interface cannot be put on the screen.")
        settings = validate_window(window or {})
        command = [self.browser, *INTERFACE_ARGS, f"--user-data-dir={self.profile}"]
        if self.environ.get("WAYLAND_DISPLAY"):
            command.append("--ozone-platform=wayland")
        address = f"http://127.0.0.1:{self.port}/tv?boot=0"
        if focus:
            address += f"&focus={quote(str(focus), safe='')}"
        if not settings["windowed"]:
            return command + ["--kiosk", "--start-fullscreen", address]
        (width, height), (left, top) = geometry(settings, self.screen)
        return command + [f"--window-size={width},{height}",
                          f"--window-position={left},{top}", f"--app={address}"]

    def open(self, window=None, focus=None) -> dict:
        """Show the interface, replacing any window already showing it.

        Replacing, because the usual reason to call this while it's up is a
        new window shape, and a running browser can't change its shape.
        """
        self.close()
        try:
            command = self.command(window, focus)
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
        except Exception as exc:
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
