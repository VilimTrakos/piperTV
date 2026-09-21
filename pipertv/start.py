"""Start Piper from the Pi's own desktop, with a double-click.

Until now Piper was started by deploy.sh over SSH: a way in for whoever works
on it, and none at all for whoever is at the television. After a restart the
TV showed the desktop and nothing else.

The PiperTV icon does what a deploy does once the files are in place. It
starts the app if nothing answers on its port, puts the interface on the
screen, and opens a visit for the remote. That last step is not a formality:
this television never reports over CEC which input it shows, so the remote
stays inert until someone says the Pi is what the screen is showing -- and
someone double-clicking an icon on the Pi's own screen has just said it.

The black backdrop goes up first, so a double-click is answered within a
second or two rather than after the half-minute a cold start takes; a second,
impatient double-click then finds a start already under way and leaves it be.

    python -m pipertv.start             put Piper on the screen
    python -m pipertv.start --install   add the icon to the desktop and the menu
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

LOG = logging.getLogger(__name__)

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
    """Everything one double-click does, in order."""

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

    def run(self) -> str | None:
        """Put Piper on the screen. Returns what went wrong, or None."""
        if not self.answering():
            if not self.windowed():
                # Something on the screen at once, where the interface will be.
                self.backdrop.show()
            LOG.info("Starting the Piper app from the desktop")
            try:
                self._start_app()
            except OSError as exc:
                self.backdrop.close()
                return f"Piper could not be started: {exc}"
            deadline = self.clock() + APP_START_S
            while not self.answering():
                if self.clock() >= deadline:
                    # A black screen with nothing behind it would be a trap.
                    self.backdrop.close()
                    return f"Piper did not start. The end of {self.log_file} says why."
                self.sleep(POLL_S)
        shown = self.ask(self.port, "POST", "/api/tv/interface", {}, timeout=60.0)
        if not shown.get("showing"):
            return shown.get("error") or "The Piper interface did not appear."
        feed = self.ask(self.port, "GET", "/api/tv/events?after=0")
        if feed.get("control") == "on" and feed.get("mode") == "piper":
            return None
        visit = self.ask(self.port, "POST", "/api/control/manual", {"confirmed": True})
        session = (visit.get("session") or {}).get("id")
        if not session:
            return "Piper is on the screen, but the remote could not be connected to it."
        self.ask(self.port, "POST", "/api/control/mode",
                 {"mode": "piper", "session_id": session})
        return None


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


def install(home: Path | None = None, python: str = sys.executable) -> list[Path]:
    """Put the PiperTV launcher in the menu and on the desktop."""
    home = Path.home() if home is None else home
    entry = DESKTOP_ENTRY.format(python=python, root=ROOT)
    written = []
    for folder in (home / ".local" / "share" / "applications", desktop_folder(home)):
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / ENTRY
        path.write_text(entry, encoding="utf-8")
        # The desktop runs a launcher only if it may be executed.
        path.chmod(0o755)
        written.append(path)
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
    parser.add_argument("--install", action="store_true",
                        help="Add the PiperTV icon to the desktop and the menu, then stop.")
    args = parser.parse_args(argv)
    if args.install:
        for path in install():
            print(f"Wrote {path}")
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
            problem = Starter(port=args.port).run()
        except (OSError, ValueError) as exc:
            problem = f"Piper did not answer as expected: {exc}"
    if problem:
        LOG.warning("%s", problem)
        tell(problem)
        return 1
    LOG.info("Piper is on the screen")
    return 0


if __name__ == "__main__":
    sys.exit(main())
