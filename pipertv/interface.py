"""Close the Piper interface itself, so the remote is never a dead end.

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
"""

from __future__ import annotations

import logging
import os
import signal
import time

LOG = logging.getLogger(__name__)

CLOSE_GRACE_S = 2.0
POLL_S = 0.1


class Interface:
    """The browser window showing this app's interface on the Pi's screen."""

    def __init__(self, port: int = 8765, procfs: str = "/proc", kill=os.kill,
                 clock=time.monotonic, sleep=time.sleep, grace_s: float = CLOSE_GRACE_S):
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ValueError("The interface port must be between 1 and 65535.")
        self.port = port
        self.procfs = procfs
        self.kill = kill
        self.clock = clock
        self.sleep = sleep
        self.grace_s = float(grace_s)
        self._error: str | None = None

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

    def snapshot(self) -> dict:
        return {"port": self.port, "showing": self.showing(), "error": self._error}
