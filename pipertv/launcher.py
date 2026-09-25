"""Open a service (YouTube, Netflix, Kodi...) on the Pi's screen, and close it.

Web services open in their own browser window next to the interface; the
interface page must stay open because it is what listens to the remote.
Each service gets its own browser profile: a second chromium started on the
interface's profile would hand the URL to the running browser and exit, and
then we couldn't close the window. A separate profile also keeps logins.

One service runs at a time, and it is closed when Piper shuts down.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from .util import find_program
from .window import geometry, validate_window

LOG = logging.getLogger(__name__)

BROWSERS = ("chromium-browser", "chromium")  # Raspberry Pi OS uses both names
FIREFOX_BROWSERS = ("firefox", "firefox-esr")

# YouTube picks its UI from the user agent: a Chromecast agent (CrKey) gets
# the "Ready to cast" receiver, a desktop agent the normal site, and only a TV
# agent gets the remote-friendly TV app. This one was tested on the Pi.
TV_USER_AGENT = ("Mozilla/5.0 (SMART-TV; LINUX; Tizen 6.0) AppleWebKit/537.36 "
                 "(KHTML, like Gecko) 76.0.3809.146/6.0 TV Safari/537.36")

# How the remote drives a service: KEYS sends arrow keys and Enter (TV apps,
# Kodi); SNAP moves the cursor between the page's controls (normal websites
# ignore arrow keys).
KEYS, SNAP = "keys", "snap"

# Keys are the TV interface's tile ids.
SERVICES = {
    "youtube": {"name": "YouTube", "url": "https://www.youtube.com/tv",
                "user_agent": TV_USER_AGENT, "control": KEYS},
    "prime": {"name": "Prime Video", "url": "https://www.primevideo.com", "control": SNAP},
    "netflix": {"name": "Netflix", "url": "https://www.netflix.com", "control": SNAP},
    "disney": {"name": "Disney+", "url": "https://www.disneyplus.com", "control": SNAP},
    "hbo": {"name": "HBO Max", "url": "https://www.max.com", "control": SNAP},
    "voyo": {"name": "Voyo", "url": "https://voyo.hr", "control": SNAP},
    "browser": {"name": "Web browser", "url": "https://www.google.com",
                "browser": "firefox", "control": SNAP},
    # Kodi decodes video in hardware, which a browser on a Pi 3B+ doesn't.
    "kodi": {"name": "Kodi", "command": ["kodi"], "control": KEYS},
}

KIOSK_ARGS = (
    # Pi 3B+: without --disable-gpu the EGL context fails and nothing renders;
    # without --password-store=basic chromium waits on a keyring prompt.
    "--disable-gpu", "--password-store=basic",
    # Otherwise the page is empty to the accessibility bus and snapping has nothing.
    "--force-renderer-accessibility",
    # Nothing that opens a dialog: it can't be dismissed with a remote.
    "--noerrdialogs", "--disable-infobars",
    "--no-first-run", "--no-default-browser-check",
    "--disable-session-crashed-bubble", "--hide-crash-restore-bubble",
    "--disable-features=Translate",
    "--autoplay-policy=no-user-gesture-required",
    "--enable-low-end-device-mode",
)
# Not --kiosk/fullscreen: a fullscreen window is drawn above the layer the
# on-screen keyboard uses. A screen-sized window without a title bar (see
# labwc.py) looks the same.

# Firefox has no low-memory switch, so turn off what uses memory or disk in
# the background. Site isolation is left alone: this window goes anywhere.
FIREFOX_PREFS = {
    "browser.shell.checkDefaultBrowser": False,
    "browser.aboutwelcome.enabled": False,
    "browser.startup.homepage_override.mstone": "ignore",
    "dom.ipc.processCount": 1,
    "dom.ipc.processCount.webIsolated": 1,
    "dom.ipc.processPrelaunch.enabled": False,
    "browser.newtab.preload": False,
    "browser.newtabpage.enabled": False,
    "browser.sessionhistory.max_total_viewers": 0,
    "browser.ml.enable": False,
    "browser.ml.chat.enabled": False,
    "app.normandy.enabled": False,
    "app.shield.optoutstudies.enabled": False,
    "datareporting.healthreport.uploadEnabled": False,
    "browser.sessionstore.interval": 60000,  # default is every 15 s
}
PREFS_HEADER = "// PiperTV sets the lines below every time the browser tile opens."
_PREF_NAME = re.compile(r'^\s*user_pref\(\s*"([^"]+)"')

STOP_GRACE_S = 3.0
KILL_GRACE_S = 1.0
# Exit codes from our own terminate()/kill(); anything else is worth reporting.
CLOSED_BY_PIPER = (0, -15, -9)


def firefox_preferences(existing: str = "") -> str:
    """user.js with our preferences, keeping any lines that aren't ours."""
    kept = [line for line in existing.splitlines()
            if line.strip() != PREFS_HEADER
            and not ((match := _PREF_NAME.match(line)) and match.group(1) in FIREFOX_PREFS)]
    while kept and not kept[-1].strip():
        kept.pop()
    ours = [PREFS_HEADER] + [f"user_pref({json.dumps(name)}, {json.dumps(value)});"
                             for name, value in FIREFOX_PREFS.items()]
    return "\n".join(kept + ([""] if kept else []) + ours) + "\n"


class ServiceLauncher:
    """Starts and stops one service at a time. Thread-safe."""

    def __init__(self, services=None, browser=None, spawn=subprocess.Popen,
                 environ=None, profiles=None, clock=time.time, remembered: int = 5,
                 screen=(1920, 1080), window=None):
        self.services = dict(SERVICES if services is None else services)
        self.environ = os.environ if environ is None else environ
        self.requested = browser
        self.browser = shutil.which(browser) if browser else find_program(*BROWSERS)
        self.spawn = spawn
        self.profiles = Path(profiles) if profiles else self._default_profiles()
        self.clock = clock
        self.screen = (int(screen[0]), int(screen[1]))
        self.window = validate_window(window or {})
        self._lock = threading.RLock()
        self._process = None
        self._running: dict | None = None
        self._error: str | None = None
        self._history: deque = deque(maxlen=max(1, int(remembered)))

    def configure(self, window) -> None:
        """Window settings for whatever opens next."""
        checked = validate_window(window)
        with self._lock:
            self.window = checked

    def _default_profiles(self) -> Path:
        cache = self.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
        return Path(cache) / "pipertv" / "services"

    def _missing(self, service_id: str) -> str | None:
        """Why this service can't be opened on this Pi, if it can't."""
        service = self.services.get(service_id) or {}
        if service.get("browser") == "firefox" and not find_program(*FIREFOX_BROWSERS):
            return "Firefox is not installed on this Pi. Install firefox or firefox-esr first."
        own = service.get("command")
        if own and shutil.which(own[0]) is None:
            return (f"{service.get('name', service_id)} is not installed on this Pi. "
                    f"Install it first: sudo apt install {own[0]}")
        return None

    def _unavailable(self) -> str | None:
        """Why no browser-based service can be opened, if none can."""
        if self.browser is None:
            if self.requested:
                return (f"The browser {self.requested!r} was not found on this Pi, so Piper "
                        "cannot open a service. Check --browser.")
            return ("No chromium browser was found on this Pi, so Piper cannot open a service. "
                    "Install one with: sudo apt install chromium")
        return self._session_missing()

    def _session_missing(self) -> str | None:
        if not (self.environ.get("WAYLAND_DISPLAY") or self.environ.get("DISPLAY")):
            return ("PiperTV is not running inside the Pi's desktop session, so it cannot put "
                    "anything on the TV. Start it from the desktop session on the Pi.")
        return None

    def catalogue(self) -> list[dict]:
        return [{"id": key, "name": service["name"], "control": self.policy(key)}
                for key, service in self.services.items()]

    def policy(self, service_id) -> str:
        """KEYS or SNAP: how the remote drives this service."""
        service = self.services.get(service_id) or {}
        return SNAP if service.get("control") == SNAP else KEYS

    def command(self, service: dict) -> list[str]:
        if service.get("command"):
            return list(service["command"])  # an application, not a web page
        if service.get("browser") == "firefox":
            return self._firefox_command(service)
        profile = self.profiles / service["id"]
        profile.mkdir(parents=True, exist_ok=True)
        (width, height), (left, top) = geometry(self.window, self.screen)
        command = [self.browser, *KIOSK_ARGS, f"--user-data-dir={profile}",
                   f"--window-size={width},{height}", f"--window-position={left},{top}"]
        if self.environ.get("WAYLAND_DISPLAY"):
            # Otherwise chromium tries X11 and exits with "Missing X server".
            command.append("--ozone-platform=wayland")
        if service.get("user_agent"):
            command.append(f"--user-agent={service['user_agent']}")
        # An app window: no tabs or address bar.
        command.append(f"--app={service['url']}")
        return command

    def _firefox_command(self, service: dict) -> list[str]:
        # A profile of its own, and --no-remote so the process we start owns
        # the window and closing it doesn't close another Firefox.
        profile = self.profiles / f"{service['id']}-firefox"
        profile.mkdir(parents=True, exist_ok=True)
        prefs = profile / "user.js"
        try:
            existing = prefs.read_text(encoding="utf-8")
        except FileNotFoundError:
            existing = ""
        wanted = firefox_preferences(existing)
        if wanted != existing:
            prefs.write_text(wanted, encoding="utf-8")
        (width, height), _ = geometry(self.window, self.screen)
        # A normal window, so the on-screen keyboard can be drawn over it.
        return [find_program(*FIREFOX_BROWSERS), "--no-remote", "--profile", str(profile),
                "--width", str(width), "--height", str(height),
                "--new-window", service["url"]]

    def launch(self, service_id) -> dict:
        """Open a service, closing whatever was open before."""
        if not isinstance(service_id, str) or not service_id:
            raise ValueError("Say which service to open.")
        if service_id not in self.services:
            raise KeyError(f"Piper cannot open {service_id} yet.")
        service = dict(self.services[service_id], id=service_id)
        with self._lock:
            self._reap()
            missing = self._missing(service_id)
            if missing:
                raise RuntimeError(missing)
            # Kodi and Firefox don't need chromium, only the desktop session.
            needs_chromium = not (service.get("command") or service.get("browser") == "firefox")
            reason = self._unavailable() if needs_chromium else self._session_missing()
            if reason:
                raise RuntimeError(reason)
            if self._running and self._running["id"] == service_id:
                return self._state()  # already open
            self._close()
            started = self.clock()
            extra = {}
            if service.get("browser") == "firefox":
                env = {**os.environ, **self.environ}
                if env.get("WAYLAND_DISPLAY"):
                    env["MOZ_ENABLE_WAYLAND"] = "1"
                extra["env"] = env
            try:
                self._process = self.spawn(
                    self.command(service), stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True, close_fds=True, **extra)
            except OSError as exc:
                self._process = None
                self._error = f"Could not open {service['name']}: {exc}"
                raise RuntimeError(self._error) from exc
            self._error = None
            self._running = {"id": service_id, "name": service["name"], "started_at": started}
            LOG.info("Opened %s on the TV", service["name"])
            return self._state()

    def stop(self) -> dict:
        with self._lock:
            self._reap()
            self._close()
            return self._state()

    def running(self) -> dict | None:
        with self._lock:
            self._reap()
            return dict(self._running) if self._running else None

    def close(self) -> None:
        with self._lock:
            self._close()

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
                "services": self.catalogue(), "running": running,
                "error": self._error,
                "history": [dict(entry, age_s=max(0.0, round(now - entry["ended_at"], 1)))
                            for entry in self._history]}

    def _reap(self) -> None:
        """Notice a service that exited by itself (closed, crashed, failed to start)."""
        if self._process is None:
            return
        code = self._process.poll()
        if code is None:
            return
        ended = self._remember(code)
        if code not in CLOSED_BY_PIPER:
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
            except OSError as exc:
                LOG.warning("Closing the open service: %s", exc)
                code = process.poll()
        self._remember(code if code is not None else -1)

    @staticmethod
    def _wait(process, timeout: float):
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
