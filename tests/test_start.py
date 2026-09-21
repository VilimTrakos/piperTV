import json
import logging
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pipertv.backdrop import ROOT
from pipertv.start import Starter, add_to_panel, desktop_folder, install, saved_windowed
from tests.test_control import FakeBackdrop

logging.getLogger("pipertv.start").addHandler(logging.NullHandler())


class FakeApi:
    """Piper's API as seen from the launcher, answering once the app is up."""

    def __init__(self, up=True, control="on", mode="piper", showing=True):
        self.up = up
        self.control = control
        self.mode = mode
        self.showing = showing
        self.calls = []

    def __call__(self, port, method, path, payload=None, timeout=5.0):
        if not self.up:
            raise ConnectionRefusedError("nothing is listening")
        self.calls.append((method, path, payload))
        if path == "/api/tv/interface":
            return {"showing": self.showing, "error": None if self.showing else "no window"}
        if path.startswith("/api/tv/events"):
            return {"control": self.control, "mode": self.mode}
        if path == "/api/control/manual":
            return {"session": {"id": "visit-1"}}
        return {}

    def paths(self):
        return [path for _method, path, _payload in self.calls if path != "/api/health"]


class StarterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.started = []

    def build(self, api, windowed=False, starts_app=True):
        def spawn(command, **kwargs):
            # Never the real Popen: a test must not start Piper on this machine.
            self.started.append((command, kwargs))
            if starts_app:
                api.up = True

        ticks = iter(range(0, 100_000))
        self.backdrop = FakeBackdrop(visible=False)
        return Starter(port=8765, ask=api, spawn=spawn, backdrop=self.backdrop,
                       clock=lambda: float(next(ticks)), sleep=lambda _s: None,
                       windowed=lambda: windowed,
                       log_file=Path(self.temporary.name) / "pipertv.log")

    def test_a_running_piper_is_only_brought_to_the_screen(self):
        api = FakeApi()
        self.assertIsNone(self.build(api).run())
        self.assertEqual(self.started, [])
        self.assertEqual(api.paths(), ["/api/tv/interface", "/api/tv/events?after=0"])

    def test_the_remote_is_connected_when_no_visit_is_open(self):
        # The television never says which input it shows, so the double-click
        # on the Pi's own screen is what says it.
        api = FakeApi(control="off", mode=None)
        self.assertIsNone(self.build(api).run())
        self.assertEqual(api.calls[-2], ("POST", "/api/control/manual", {"confirmed": True}))
        self.assertEqual(api.calls[-1], ("POST", "/api/control/mode",
                                         {"mode": "piper", "session_id": "visit-1"}))

    def test_a_stopped_piper_is_started_behind_a_black_screen(self):
        api = FakeApi(up=False)
        self.assertIsNone(self.build(api).run())
        self.assertEqual(self.backdrop.calls, ["backdrop up"])
        command, kwargs = self.started[0]
        self.assertEqual(command, [sys.executable, "main.py", "--port", "8765"])
        self.assertEqual(kwargs["cwd"], str(ROOT))
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
        self.assertIn("/api/tv/interface", api.paths())

    def test_a_window_on_the_desktop_starts_without_the_black_screen(self):
        api = FakeApi(up=False)
        self.build(api, windowed=True).run()
        self.assertEqual(self.backdrop.calls, [])

    def test_an_app_that_never_answers_leaves_the_desktop_usable(self):
        # A black screen with nothing behind it would be a trap.
        api = FakeApi(up=False)
        problem = self.build(api, starts_app=False).run()
        self.assertIn("did not start", problem)
        self.assertEqual(self.backdrop.calls, ["backdrop up", "backdrop down"])
        self.assertEqual(api.calls, [])

    def test_an_interface_that_does_not_appear_is_reported(self):
        api = FakeApi(showing=False)
        self.assertEqual(self.build(api).run(), "no window")


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "home"
        self.home.mkdir()
        self.defaults = Path(self.temporary.name) / "wf-panel-pi.ini"
        self.defaults.write_text("[panel]\nwidgets_left=smenu launchers window-list\n"
                                 "launchers=x-www-browser pcmanfm x-terminal-emulator\n")

    def test_the_launcher_goes_in_the_menu_on_the_desktop_and_on_the_panel(self):
        written = install(home=self.home, python="/home/rpi/piperTV/.venv/bin/python3",
                          panel_defaults=self.defaults)
        self.assertEqual(written, [self.home / ".local/share/applications/pipertv.desktop",
                                   self.home / "Desktop/pipertv.desktop",
                                   self.home / ".config/wf-panel-pi.ini"])
        for path in written[:2]:
            text = path.read_text()
            self.assertIn('Exec="/home/rpi/piperTV/.venv/bin/python3" -m pipertv.start', text)
            self.assertIn(f"Path={ROOT}", text)

    def test_the_icon_it_names_exists(self):
        entry = install(home=self.home, panel_defaults=self.defaults)[0].read_text()
        icon = next(line for line in entry.splitlines() if line.startswith("Icon="))[5:]
        self.assertTrue(Path(icon).is_file())

    def test_installing_again_changes_nothing(self):
        first = [path.read_text() for path in install(home=self.home,
                                                      panel_defaults=self.defaults)]
        panel = (self.home / ".config/wf-panel-pi.ini").read_text()
        second = [path.read_text() for path in install(home=self.home,
                                                       panel_defaults=self.defaults)]
        self.assertEqual(first[:2], second)
        self.assertEqual((self.home / ".config/wf-panel-pi.ini").read_text(), panel)

    def test_the_panel_keeps_its_own_launchers_and_gains_piper(self):
        add_to_panel(self.home, self.defaults)
        text = (self.home / ".config/wf-panel-pi.ini").read_text()
        self.assertIn("launchers=x-www-browser pcmanfm x-terminal-emulator pipertv", text)

    def test_the_panel_settings_already_there_are_left_alone(self):
        config = self.home / ".config"
        config.mkdir()
        (config / "wf-panel-pi.ini").write_text("[panel]\nautohide=true\nautohide_duration=300\n")
        add_to_panel(self.home, self.defaults)
        self.assertEqual((config / "wf-panel-pi.ini").read_text(),
                         "[panel]\nlaunchers=x-www-browser pcmanfm x-terminal-emulator pipertv\n"
                         "autohide=true\nautohide_duration=300\n")

    def test_launchers_someone_chose_are_kept(self):
        config = self.home / ".config"
        config.mkdir()
        (config / "wf-panel-pi.ini").write_text("[panel]\nlaunchers = pcmanfm\n")
        add_to_panel(self.home, self.defaults)
        self.assertEqual((config / "wf-panel-pi.ini").read_text(),
                         "[panel]\nlaunchers=pcmanfm pipertv\n")

    def test_a_desktop_with_another_name_is_found(self):
        # A Croatian session calls it "Radna površina".
        config = self.home / ".config"
        config.mkdir()
        (config / "user-dirs.dirs").write_text('XDG_DESKTOP_DIR="$HOME/Radna površina"\n')
        self.assertEqual(desktop_folder(self.home), self.home / "Radna površina")

    def test_without_a_setting_the_desktop_is_desktop(self):
        self.assertEqual(desktop_folder(self.home), self.home / "Desktop")


class SavedWindowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Path(self.temporary.name) / "recordings.json"

    def test_the_saved_shape_is_read(self):
        self.store.write_text(json.dumps({"window": {"windowed": True}}))
        self.assertTrue(saved_windowed(self.store))

    def test_anything_unreadable_means_the_whole_screen(self):
        for text in (None, "not json", json.dumps({"window": "yes"}), json.dumps({})):
            with self.subTest(text=text):
                if text is None:
                    self.store.unlink(missing_ok=True)
                else:
                    self.store.write_text(text)
                self.assertFalse(saved_windowed(self.store))


if __name__ == "__main__":
    unittest.main()
