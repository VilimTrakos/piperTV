"""Start Piper from the Pi's own desktop, and have the remote there before it.

Until now Piper was started by deploy.sh over SSH: a way in for whoever works
on it, and none at all for whoever is at the television. After a restart the
TV showed the desktop, and the remote did nothing, because nothing was
listening to it.

Two things run from the desktop now, and only one of them by itself:

  At login, the remote alone. The app starts without its interface and the
  remote moves the desktop's mouse -- which is how, from the sofa, anyone gets
  to the PiperTV icon at all. Piper itself stays off the screen until asked.

  The PiperTV icon, which does what a deploy does once the files are there:
  the app if nothing answers on its port, the interface on the screen, and a
  visit for the remote in the interface's hands.

Opening a visit is not a formality. This television never reports over CEC
which input it shows, so the remote stays inert until someone says the Pi is
what the screen is showing -- and someone at the Pi's own desktop has.

The black backdrop goes up first, so a double-click is answered within a
second or two rather than after the half-minute a cold start takes; a second,
impatient double-click then finds a start already under way and leaves it be.

    python -m pipertv.start             put Piper on the screen
    python -m pipertv.start --remote    only the remote, as the mouse (at login)
    python -m pipertv.start --install   the icons, the login entry, no "Execute?"
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

# Named rather than __name__, which is "__main__" when run with -m.
LOG = logging.getLogger("pipertv.start")

# The app loads the accessibility bus and the receiver before it answers; just
# after a boot a Pi 3B+ takes a while over that, and a minute means it is not
# going to.
APP_START_S = 60.0
POLL_S = 0.5
LOG_FILE = ROOT / "pipertv.log"
LOCK_FILE = ROOT / ".start.lock"
STORE = ROOT / "data" / "recordings.json"
ENTRY = "pipertv.desktop"
DESKTOP_ENTRY = """[Desktop Entry]
Type=Application
Name=PiperTV
Comment=Put Piper on the television and let the remote drive it
Exec="{python}" -m pipertv.start
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
Exec="{python}" -m pipertv.start --remote
Path={root}
Terminal=false
NoDisplay=true
"""
# The panel reads its user settings from a file in a folder of its own. A
# wf-panel-pi.ini directly in ~/.config is an older place it no longer looks:
# the autohide setting on this Pi sat there, and never took effect.
PANEL_CONFIG = Path(".config") / "wf-panel-pi" / "wf-panel-pi.ini"
PANEL_DEFAULTS = Path("/etc/xdg/wf-panel-pi/wf-panel-pi.ini")
PANEL_FALLBACK = ("x-www-browser", "pcmanfm", "x-terminal-emulator")
LAUNCHER = "pipertv"
LIBFM_CONFIG = Path(".config") / "libfm" / "libfm.conf"
LIBFM_DEFAULTS = Path("/etc/xdg/libfm/libfm.conf")


def ask(port: int, method: str, path: str, payload=None, timeout: float = 5.0) -> dict:
    """One call to Piper's own API on this Pi."""
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def saved_windowed(store: Path = STORE) -> bool:
    """Whether Piper was last set to a window rather than the whole screen."""
    try:
        document = json.loads(store.read_text(encoding="utf-8"))
        return document.get("window", {}).get("windowed") is True
    except (OSError, ValueError, AttributeError):
        return False


class Starter:
    """Everything one double-click does, in order -- or one login."""

    def __init__(self, port: int = 8765, ask=ask, spawn=subprocess.Popen, backdrop=None,
                 clock=time.monotonic, sleep=time.sleep, windowed=saved_windowed,
                 log_file: Path = LOG_FILE):
        self.port = port
        self.ask = ask
        self.spawn = spawn
        self.backdrop = Backdrop() if backdrop is None else backdrop
        self.clock = clock
        self.sleep = sleep
        self.windowed = windowed
        self.log_file = Path(log_file)

    def answering(self) -> bool:
        try:
            self.ask(self.port, "GET", "/api/health", timeout=2.0)
            return True
        except (OSError, ValueError):
            return False

    def _start_app(self) -> None:
        # Detached, and writing where deploy.sh's app writes, so there is one
        # log to read whichever way Piper was started.
        with open(self.log_file, "ab") as log:
            self.spawn([sys.executable, "main.py", "--port", str(self.port)], cwd=str(ROOT),
                       stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                       start_new_session=True, close_fds=True)

    def _app_running(self, backdrop: bool) -> str | None:
        """Start the app if nothing answers. Returns what went wrong, or None."""
        if self.answering():
            return None
        if backdrop:
            # Something on the screen at once, where the interface will be.
            self.backdrop.show()
        LOG.info("Starting the Piper app from the desktop")
        try:
            self._start_app()
        except OSError as exc:
            if backdrop:
                self.backdrop.close()
            return f"Piper could not be started: {exc}"
        deadline = self.clock() + APP_START_S
        while not self.answering():
            if self.clock() >= deadline:
                if backdrop:
                    # A black screen with nothing behind it would be a trap.
                    self.backdrop.close()
                return f"Piper did not start. The end of {self.log_file} says why."
            self.sleep(POLL_S)
        return None

    def _visit(self, mode: str, feed: dict) -> str | None:
        """Give the remote to `mode`, in the visit that is open or a new one."""
        session = feed.get("session_id") if feed.get("control") == "on" else None
        if not session:
            visit = self.ask(self.port, "POST", "/api/control/manual", {"confirmed": True})
            session = (visit.get("session") or {}).get("id")
        if not session:
            return "The remote could not be connected to the Pi."
        self.ask(self.port, "POST", "/api/control/mode", {"mode": mode, "session_id": session})
        return None

    def run(self) -> str | None:
        """Put Piper on the screen. Returns what went wrong, or None."""
        problem = self._app_running(backdrop=not self.windowed())
        if problem:
            return problem
        shown = self.ask(self.port, "POST", "/api/tv/interface", {}, timeout=60.0)
        if not shown.get("showing"):
            return shown.get("error") or "The Piper interface did not appear."
        feed = self.ask(self.port, "GET", "/api/tv/events?after=0")
        if feed.get("control") == "on" and feed.get("mode") == "piper":
            return None
        return self._visit("piper", feed)

    def remote(self) -> str | None:
        """Only the remote, as the desktop's mouse; Piper stays off the screen."""
        problem = self._app_running(backdrop=False)
        if problem:
            return problem
        feed = self.ask(self.port, "GET", "/api/tv/events?after=0")
        if feed.get("control") == "on" and feed.get("mode"):
            return None  # a visit already chose; a login does not overrule it
        return self._visit("pointer", feed)


def _ini_get(text: str, section: str, key: str) -> str | None:
    """One value from an ini file's text, if that section sets it."""
    current = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1].strip()
        elif current == section and "=" in stripped \
                and stripped.split("=", 1)[0].strip() == key:
            return stripped.split("=", 1)[1].strip()
    return None


def _ini_set(text: str, section: str, key: str, value: str) -> str:
    """The same ini with one value set, and every other line as it was."""
    wanted = f"{key}={value}"
    lines = text.splitlines()
    current, section_at = None, None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1].strip()
            if current == section and section_at is None:
                section_at = index
        elif current == section and "=" in stripped \
                and stripped.split("=", 1)[0].strip() == key:
            lines[index] = wanted
            return "\n".join(lines) + "\n"
    if section_at is None:
        lines += ([""] if lines else []) + [f"[{section}]", wanted]
    else:
        lines.insert(section_at + 1, wanted)
    return "\n".join(lines) + "\n"


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def add_to_panel(home: Path, defaults: Path = PANEL_DEFAULTS) -> Path | None:
    """Put PiperTV beside the browser, the files and the terminal on the panel.

    The user's own ini overrides the system one key by key, so the list written
    there is the one the panel shows now, with PiperTV added at its end.
    """
    config = home / PANEL_CONFIG
    text = _read(config) or ""
    listed = _ini_get(text, "panel", "launchers")
    if listed is None:
        try:
            listed = _ini_get(defaults.read_text(encoding="utf-8"), "panel", "launchers")
        except OSError:
            listed = None
    launchers = listed.split() if listed is not None else list(PANEL_FALLBACK)
    if LAUNCHER in launchers:
        return None
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(_ini_set(text, "panel", "launchers", " ".join(launchers + [LAUNCHER])),
                      encoding="utf-8")
    return config


def launch_without_asking(home: Path, defaults: Path = LIBFM_DEFAULTS) -> Path | None:
    """Let a double-click on a desktop launcher start it, without "Execute?".

    This Pi's file manager asks of every launcher on the desktop, whatever its
    permissions, and a remote's double OK on the PiperTV icon would otherwise
    end on a question. The setting is the file manager's own "don't ask"; it
    holds for every executable file it is asked to open, not only this one.
    A file of the user's own starts as a copy of the system one, so nothing
    else about the file manager changes.
    """
    config = home / LIBFM_CONFIG
    text = _read(config)
    if text is None:
        try:
            text = defaults.read_text(encoding="utf-8")
        except OSError:
            text = ""
    if _ini_get(text, "config", "quick_exec") == "1":
        return None
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(_ini_set(text, "config", "quick_exec", "1"), encoding="utf-8")
    return config


def desktop_folder(home: Path) -> Path:
    """The desktop as this user's session names it -- not always "Desktop"."""
    try:
        text = (home / ".config" / "user-dirs.dirs").read_text(encoding="utf-8")
    except OSError:
        return home / "Desktop"
    match = re.search(r'^XDG_DESKTOP_DIR="([^"]+)"', text, re.MULTILINE)
    if not match:
        return home / "Desktop"
    return Path(match.group(1).replace("$HOME", str(home)))


def install(home: Path | None = None, python: str = sys.executable,
            panel_defaults: Path = PANEL_DEFAULTS,
            libfm_defaults: Path = LIBFM_DEFAULTS) -> list[Path]:
    """The icons, the remote at login, and a desktop that does not ask."""
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
    for changed in (add_to_panel(home, panel_defaults),
                    launch_without_asking(home, libfm_defaults)):
        if changed is not None:
            written.append(changed)
    return written


def tell(message: str) -> None:
    """Say what went wrong on the screen, where the double-click came from."""
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk

        dialog = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR,
                                   buttons=Gtk.ButtonsType.CLOSE, text="PiperTV",
                                   secondary_text=message)
        dialog.run()
        dialog.destroy()
    except Exception:  # noqa: BLE001 - the log already has it
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Put Piper on this Pi's screen.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--remote", action="store_true",
                        help="Only let the remote move the mouse; leave Piper off the screen.")
    parser.add_argument("--install", action="store_true",
                        help="Add the icons and the login entry, then stop.")
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
            # Nobody asked at login; a dialog then would be a surprise.
            tell(problem)
        return 1
    LOG.info("The remote moves the mouse" if args.remote else "Piper is on the screen")
    return 0


if __name__ == "__main__":
    sys.exit(main())
