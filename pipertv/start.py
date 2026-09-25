"""Start Piper from the Pi's desktop.

    python -m pipertv.start             put Piper on the screen (the PiperTV icon)
    python -m pipertv.start --remote    only the remote, as the mouse (run at login)
    python -m pipertv.start --install   install the icons and the login entry

At login only the app starts, without its interface, and the remote moves
the mouse, so the PiperTV icon can be reached with the remote. The icon
starts the app if nothing answers on its port, shows the interface, and
opens a visit so the remote drives it. This TV never reports its input over
CEC, so starting from the Pi's own desktop counts as confirming that the TV
shows the Pi.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from .backdrop import ROOT, Backdrop
from .ini import ini_get, ini_set
from .labwc import SYSTEM_ENVIRONMENT, SYSTEM_RC, add_window_rules, allow_accessibility

# Not __name__, which is "__main__" when run with -m.
LOG = logging.getLogger("pipertv.start")

# Just after boot a Pi 3B+ can take a while to start the app.
APP_START_S = 60.0
POLL_S = 0.5
LOG_FILE = ROOT / "pipertv.log"
LOCK_FILE = ROOT / ".start.lock"
STORE = ROOT / "data" / "recordings.json"
ENTRY = "pipertv.desktop"
# PYTHONPATH rather than Path=: autostart ignores Path=, and from the home
# folder "-m pipertv.start" wouldn't import.
COMMAND = 'env "PYTHONPATH={root}" "{python}" -m pipertv.start'
DESKTOP_ENTRY = """[Desktop Entry]
Type=Application
Name=PiperTV
Comment=Put Piper on the television and let the remote drive it
Exec=""" + COMMAND + """
Path={root}
Icon={root}/pipertv/static/favicon.svg
Terminal=false
Categories=AudioVideo;Video;
"""
AUTOSTART = Path(".config") / "autostart" / "pipertv-remote.desktop"
AUTOSTART_ENTRY = """[Desktop Entry]
Type=Application
Name=PiperTV remote
Comment=The remote moves the mouse from login; Piper waits for its icon
Exec=""" + COMMAND + """ --remote
Terminal=false
"""
# The panel reads ~/.config/wf-panel-pi/wf-panel-pi.ini (not ~/.config/wf-panel-pi.ini).
PANEL_CONFIG = Path(".config") / "wf-panel-pi" / "wf-panel-pi.ini"
PANEL_DEFAULTS = Path("/etc/xdg/wf-panel-pi/wf-panel-pi.ini")
PANEL_FALLBACK = ("x-www-browser", "pcmanfm", "x-terminal-emulator")
LAUNCHER = "pipertv"
LIBFM_CONFIG = Path(".config") / "libfm" / "libfm.conf"
LIBFM_DEFAULTS = Path("/etc/xdg/libfm/libfm.conf")


def call_api(port: int, method: str, path: str, payload=None, timeout: float = 5.0) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def saved_windowed(store: Path = STORE) -> bool:
    """Whether Piper is set to run in a window rather than full screen."""
    try:
        document = json.loads(store.read_text(encoding="utf-8"))
        return document.get("window", {}).get("windowed") is True
    except (OSError, ValueError, AttributeError):
        return False


class Starter:
    def __init__(self, port: int = 8765, api=call_api, spawn=subprocess.Popen, backdrop=None,
                 clock=time.monotonic, sleep=time.sleep, windowed=saved_windowed,
                 log_file: Path = LOG_FILE):
        self.port = port
        self.api = api
        self.spawn = spawn
        self.backdrop = backdrop or Backdrop()
        self.clock = clock
        self.sleep = sleep
        self.windowed = windowed
        self.log_file = Path(log_file)

    def is_up(self) -> bool:
        try:
            self.api(self.port, "GET", "/api/health", timeout=2.0)
            return True
        except (OSError, ValueError):
            return False

    def _start_app(self) -> None:
        # Detached, and logging to the same file deploy.sh uses.
        with open(self.log_file, "ab") as log:
            self.spawn([sys.executable, "main.py", "--port", str(self.port)], cwd=str(ROOT),
                       stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                       start_new_session=True, close_fds=True)

    def _ensure_app(self, backdrop: bool) -> str | None:
        """Start the app unless it's already up. Returns an error message or None."""
        if self.is_up():
            return None
        if backdrop:
            self.backdrop.show()  # something on screen right away
        LOG.info("Starting the Piper app from the desktop")
        try:
            self._start_app()
        except OSError as exc:
            if backdrop:
                self.backdrop.close()
            return f"Piper could not be started: {exc}"
        deadline = self.clock() + APP_START_S
        while not self.is_up():
            if self.clock() >= deadline:
                if backdrop:
                    self.backdrop.close()  # don't leave the screen black
                return f"Piper did not start. The end of {self.log_file} says why."
            self.sleep(POLL_S)
        return None

    def _open_visit(self, mode: str, feed: dict) -> str | None:
        """Set `mode` for the open visit, opening one first if there is none."""
        session = feed.get("session_id") if feed.get("control") == "on" else None
        if not session:
            visit = self.api(self.port, "POST", "/api/control/manual", {"confirmed": True})
            session = (visit.get("session") or {}).get("id")
        if not session:
            return "The remote could not be connected to the Pi."
        self.api(self.port, "POST", "/api/control/mode", {"mode": mode, "session_id": session})
        return None

    def run(self) -> str | None:
        """Put Piper on the screen. Returns an error message or None."""
        problem = self._ensure_app(backdrop=not self.windowed())
        if problem:
            return problem
        shown = self.api(self.port, "POST", "/api/tv/interface", {}, timeout=60.0)
        if not shown.get("showing"):
            return shown.get("error") or "The Piper interface did not appear."
        feed = self.api(self.port, "GET", "/api/tv/events?after=0")
        if feed.get("control") == "on" and feed.get("mode") == "piper":
            return None
        return self._open_visit("piper", feed)

    def remote(self) -> str | None:
        """Only the remote, as the desktop's mouse."""
        problem = self._ensure_app(backdrop=False)
        if problem:
            return problem
        feed = self.api(self.port, "GET", "/api/tv/events?after=0")
        if feed.get("control") == "on" and feed.get("mode"):
            return None  # a mode was already chosen; don't override it
        return self._open_visit("pointer", feed)


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def add_to_panel(home: Path, defaults: Path = PANEL_DEFAULTS) -> Path | None:
    """Add PiperTV to the panel's launchers. Returns the file if it changed.

    The user's ini overrides the system one key by key, so the user's list
    has to be the full current list plus PiperTV.
    """
    config = home / PANEL_CONFIG
    text = _read(config) or ""
    listed = ini_get(text, "panel", "launchers")
    if listed is None:
        try:
            listed = ini_get(defaults.read_text(encoding="utf-8"), "panel", "launchers")
        except OSError:
            listed = None
    launchers = listed.split() if listed is not None else list(PANEL_FALLBACK)
    if LAUNCHER in launchers:
        return None
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(ini_set(text, "panel", "launchers", " ".join(launchers + [LAUNCHER])),
                      encoding="utf-8")
    return config


def launch_without_asking(home: Path, defaults: Path = LIBFM_DEFAULTS) -> Path | None:
    """Turn off the file manager's "Execute?" question for desktop launchers.

    Otherwise a double OK on the PiperTV icon ends in a dialog. This is
    pcmanfm's own "don't ask" option, so it applies to every launcher.
    """
    config = home / LIBFM_CONFIG
    text = _read(config)
    if text is None:
        try:
            text = defaults.read_text(encoding="utf-8")
        except OSError:
            text = ""
    if ini_get(text, "config", "quick_exec") == "1":
        return None
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(ini_set(text, "config", "quick_exec", "1"), encoding="utf-8")
    return config


def desktop_folder(home: Path) -> Path:
    """The desktop folder, which is localised (e.g. "Radna površina")."""
    try:
        text = (home / ".config" / "user-dirs.dirs").read_text(encoding="utf-8")
    except OSError:
        return home / "Desktop"
    match = re.search(r'^XDG_DESKTOP_DIR="([^"]+)"', text, re.MULTILINE)
    if not match:
        return home / "Desktop"
    return Path(match.group(1).replace("$HOME", str(home)))


def accessibility_setting(run=subprocess.run) -> bool:
    try:
        run(["gsettings", "set", "org.gnome.desktop.interface", "toolkit-accessibility",
             "true"], check=True, capture_output=True, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def install(home: Path | None = None, python: str = sys.executable,
            panel_defaults: Path = PANEL_DEFAULTS,
            libfm_defaults: Path = LIBFM_DEFAULTS,
            labwc_defaults: tuple[Path, Path] = (SYSTEM_RC, SYSTEM_ENVIRONMENT),
            run=subprocess.run) -> list[Path]:
    """Install the icons, the login entry and the desktop settings Piper needs.

    Our own files are rewritten every time; other programs' settings are only
    added to, and only when something is missing.
    """
    home = Path.home() if home is None else home
    written = []
    entry = DESKTOP_ENTRY.format(python=python, root=ROOT)
    for folder in (home / ".local" / "share" / "applications", desktop_folder(home)):
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / ENTRY
        path.write_text(entry, encoding="utf-8")
        path.chmod(0o644)
        written.append(path)
    autostart = home / AUTOSTART
    autostart.parent.mkdir(parents=True, exist_ok=True)
    autostart.write_text(AUTOSTART_ENTRY.format(python=python, root=ROOT), encoding="utf-8")
    written.append(autostart)
    rc, environment = labwc_defaults
    for changed in (add_to_panel(home, panel_defaults),
                    launch_without_asking(home, libfm_defaults),
                    add_window_rules(home, rc),
                    allow_accessibility(home, environment)):
        if changed is not None:
            written.append(changed)
    accessibility_setting(run)
    return written


def show_error(message: str) -> None:
    """Show an error dialog on the Pi's screen (best effort; it's logged anyway)."""
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk

        dialog = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR,
                                   buttons=Gtk.ButtonsType.CLOSE, text="PiperTV",
                                   secondary_text=message)
        dialog.run()
        dialog.destroy()
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Put Piper on this Pi's screen.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--remote", action="store_true",
                        help="only let the remote move the mouse; leave Piper off the screen")
    parser.add_argument("--install", action="store_true",
                        help="add the icons and the login entry, then stop")
    args = parser.parse_args(argv)
    if args.install:
        for path in install():
            print(f"Wrote {path}")
        print("The panel and the desktop pick these up the next time they start.")
        return 0
    logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    with open(LOCK_FILE, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # An impatient second double-click: let the first one finish.
            LOG.info("Piper is already being started; leaving that start to finish")
            return 0
        try:
            starter = Starter(port=args.port)
            problem = starter.remote() if args.remote else starter.run()
        except (OSError, ValueError) as exc:
            problem = f"Piper did not answer as expected: {exc}"
    if problem:
        LOG.warning("%s", problem)
        if not args.remote:
            show_error(problem)  # no dialogs at login
        return 1
    LOG.info("The remote moves the mouse" if args.remote else "Piper is on the screen")
    return 0


if __name__ == "__main__":
    sys.exit(main())
