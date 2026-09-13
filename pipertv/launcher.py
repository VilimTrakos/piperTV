"""Open a service on the Pi's own screen from the Piper interface, and close it.

The interface is a web page, so opening YouTube means starting a real browser
window on the Pi's HDMI output beside it, not navigating the interface away from
itself: that page is the only thing listening to the remote, and leaving it
would leave no way back.

The launched browser gets a profile directory of its own. A second chromium
started on the profile the interface is already displayed in would hand the
address to the running browser and exit immediately, leaving Piper holding a
process that is not the window on the screen and therefore cannot close it. The
separate profile also keeps a YouTube sign-in between launches.

One service runs at a time, the way a television behaves, and Piper closes it
when it shuts down: a full-screen kiosk window with nothing listening to the
remote would otherwise own the TV until someone reached for a keyboard.

What is deliberately absent: Piper does not type into what it opens. Back, Exit
and Home close the service and return to the interface; driving the service
itself needs synthesised key presses, which this project does not do yet.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

LOG = logging.getLogger(__name__)

# Raspberry Pi OS and Debian ship the same browser under different names.
BROWSERS = ("chromium-browser", "chromium")

# YouTube decides which of three interfaces to serve from this string, and the
# difference is not cosmetic. A Chromecast identity (CrKey) returns the cast
# receiver -- the "Ready to cast" screen, which waits for a phone and can do
# nothing by itself. A plain desktop identity returns the pointer-and-keyboard
# site. A television returns the ten-foot app. All three were tried on the Pi
# and photographed; this is the one that gives the app. If that screen ever
# comes back as "Ready to cast", this line is why.
TV_USER_AGENT = ("Mozilla/5.0 (SMART-TV; LINUX; Tizen 6.0) AppleWebKit/537.36 "
                 "(KHTML, like Gecko) 76.0.3809.146/6.0 TV Safari/537.36")

# How a service answers a remote. A television app expects the four-way pad and
# an OK button, so its keys are typed at it. A site built for a mouse ignores
# arrow keys entirely -- there is no way to reach a cookie dialog's Accept with
# them -- so the cursor is snapped from one of its controls to the next instead.
KEYS, SNAP = "keys", "snap"

SERVICES = {
    "youtube": {"name": "YouTube", "url": "https://www.youtube.com/tv",
                "user_agent": TV_USER_AGENT, "control": KEYS},
    # Amazon publishes no ten-foot web app, so this is the ordinary site.
    "prime": {"name": "Prime Video", "url": "https://www.primevideo.com",
              "control": SNAP},
}

# Full screen, and nothing that opens a dialog: no one can dismiss a dialog
# with a remote control. The first two are not tidiness but necessity on a
# Pi 3B+: without --disable-gpu chromium's EGL context fails and the page never
# renders, and without --password-store=basic it blocks on the desktop keyring
# prompt. Both were established with the kiosk that shows the interface itself.
KIOSK_ARGS = ("--disable-gpu", "--password-store=basic",
              # Without this the page is a blank box to the accessibility bus,
              # and snapping has nothing in it to move the cursor to.
              "--force-renderer-accessibility",
              "--kiosk", "--start-fullscreen", "--noerrdialogs", "--disable-infobars",
              "--no-first-run", "--no-default-browser-check",
              "--disable-session-crashed-bubble", "--disable-features=Translate",
              "--autoplay-policy=no-user-gesture-required")

# Closing runs on the IR reader thread, so waiting for the browser to go is
# bounded. Chromium leaves well within this on SIGTERM.
STOP_GRACE_S = 3.0
KILL_GRACE_S = 1.0
# What our own terminate and kill leave behind. Any other non-zero status is the
# browser stopping for its own reasons, which is worth saying out loud.
CLOSED_BY_PIPER = (0, -15, -9)


class ServiceLauncher:
    """Start and stop one service at a time on this Pi's screen.

    Safe to call from the IR reader thread, the supervisor, and request threads
    at once: every method takes the same lock, and none of them blocks for
    longer than closing a browser takes.
    """

    def __init__(self, services=None, browser=None, spawn=subprocess.Popen,
                 environ=None, profiles=None, clock=time.time, remembered: int = 5):
        self.services = dict(SERVICES if services is None else services)
        self.environ = os.environ if environ is None else environ
        self.requested = browser
        self.browser = self._find_browser(browser)
        self.spawn = spawn
        self.profiles = Path(profiles) if profiles else self._default_profiles()
        self.clock = clock
        self._lock = threading.RLock()
        self._process = None
        self._running: dict | None = None
        self._error: str | None = None
        self._history: deque = deque(maxlen=max(1, int(remembered)))

    # --- what this Pi can do ---------------------------------------------

    def _find_browser(self, named):
        if named:
            return shutil.which(named)
        for name in BROWSERS:
            found = shutil.which(name)
            if found:
                return found
        return None

    def _default_profiles(self) -> Path:
        cache = self.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
        return Path(cache) / "pipertv" / "services"

    def _unavailable(self) -> str | None:
        """Why opening a service would fail here, before anything is attempted."""
        if self.browser is None:
            if self.requested:
                return (f"The browser {self.requested!r} was not found on this Pi, so Piper "
                        "cannot open a service. Check --browser.")
            return ("No chromium browser was found on this Pi, so Piper cannot open a service. "
                    "Install one with: sudo apt install chromium")
        if not (self.environ.get("WAYLAND_DISPLAY") or self.environ.get("DISPLAY")):
            return ("PiperTV is not running inside the Pi's desktop session, so it cannot put "
                    "anything on the TV. Start it from the desktop session on the Pi.")
        return None

    def catalogue(self) -> list[dict]:
        """Everything Piper can open, for an interface that lists more than that."""
        return [{"id": key, "name": service["name"], "control": self.policy(key)}
                for key, service in self.services.items()]

    def policy(self, service_id) -> str:
        """How the remote drives this service: by typing at it, or by snapping."""
        service = self.services.get(service_id) or {}
        return SNAP if service.get("control") == SNAP else KEYS

    def command(self, service: dict) -> list[str]:
        profile = self.profiles / service["id"]
        profile.mkdir(parents=True, exist_ok=True)
        command = [self.browser, *KIOSK_ARGS, f"--user-data-dir={profile}"]
        if self.environ.get("WAYLAND_DISPLAY"):
            # Chromium otherwise picks its X11 backend and exits with "Missing X
            # server or $DISPLAY" on a Wayland session such as this Pi's labwc.
            command.append("--ozone-platform=wayland")
        if service.get("user_agent"):
            command.append(f"--user-agent={service['user_agent']}")
        command.append(service["url"])
        return command

    # --- opening and closing ---------------------------------------------

    def launch(self, service_id) -> dict:
        """Put one service on the screen, replacing whatever was there."""
        if not isinstance(service_id, str) or not service_id:
            raise ValueError("Say which service to open.")
        if service_id not in self.services:
            raise KeyError(f"Piper cannot open {service_id} yet.")
        service = dict(self.services[service_id], id=service_id)
        with self._lock:
            self._reap()
            reason = self._unavailable()
            if reason:
                raise RuntimeError(reason)
            if self._running and self._running["id"] == service_id:
                # The page may ask twice; the window is already there.
                return self._state()
            self._close()
            started = self.clock()
            try:
                self._process = self.spawn(
                    self.command(service), stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True, close_fds=True)
            except OSError as exc:
                self._process = None
                self._error = f"Could not open {service['name']}: {exc}"
                raise RuntimeError(self._error) from exc
            self._error = None
            self._running = {"id": service_id, "name": service["name"], "started_at": started}
            LOG.info("Opened %s on the TV", service["name"])
            return self._state()

    def stop(self) -> dict:
        """Close the open service and give the screen back to the interface."""
        with self._lock:
            self._reap()
            self._close()
            return self._state()

    def running(self) -> dict | None:
        with self._lock:
            self._reap()
            return dict(self._running) if self._running else None

    def close(self) -> None:
        """Leave nothing full screen that the remote can no longer close."""
        with self._lock:
            self._close()

    # --- state ------------------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            self._reap()
            return self._state()

    def _state(self) -> dict:
        reason = self._unavailable()
        now = self.clock()
        running = None
        if self._running:
            running = dict(self._running,
                           seconds=max(0.0, round(now - self._running["started_at"], 1)))
        return {"available": reason is None, "reason": reason, "browser": self.browser,
                "services": self.catalogue(), "running": running, "error": self._error,
                "history": [dict(entry, age_s=max(0.0, round(now - entry["ended_at"], 1)))
                            for entry in self._history]}

    # --- process bookkeeping ---------------------------------------------

    def _reap(self) -> None:
        """Notice a service that ended by itself: closed, crashed, or refused to start."""
        if self._process is None:
            return
        code = self._process.poll()
        if code is None:
            return
        ended = self._remember(code)
        if code not in CLOSED_BY_PIPER:
            # Judged by how it ended, never by how long it ran: "seconds" counts
            # up to the moment this noticed, so a failure seen late would
            # otherwise pass for an evening's viewing.
            self._error = (f"{ended['name']} stopped on its own (exit {code}). "
                           "Check that the browser runs on this Pi's desktop.")
            LOG.warning("%s", self._error)

    def _remember(self, code) -> dict:
        entry = dict(self._running or {"id": "unknown", "name": "A service",
                                       "started_at": self.clock()},
                     ended_at=self.clock(), exit_code=code)
        entry["seconds"] = max(0.0, round(entry["ended_at"] - entry["started_at"], 1))
        self._history.appendleft(entry)
        self._running = None
        self._process = None
        return entry

    def _close(self) -> None:
        """Stop the browser, escalating only if it does not go on its own."""
        process = self._process
        if process is None:
            return
        code = process.poll()
        if code is None:
            try:
                process.terminate()
                code = self._wait(process, STOP_GRACE_S)
                if code is None:
                    process.kill()
                    code = self._wait(process, KILL_GRACE_S)
            except OSError as exc:  # already gone, or no longer ours to signal
                LOG.warning("Closing the open service: %s", exc)
                code = process.poll()
        self._remember(code if code is not None else -1)

    @staticmethod
    def _wait(process, timeout: float):
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
