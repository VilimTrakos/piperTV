import json
import logging
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pipertv.backdrop import ROOT
from pipertv.start import (Starter, add_to_panel, desktop_folder, install,
                           launch_without_asking, saved_windowed)
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
            return {"control": self.control, "mode": self.mode,
                    "session_id": "visit-0" if self.control == "on" else None}
        if path == "/api/control/manual":
            return {"session": {"id": "visit-1"}}
        return {}

    def paths(self):
        return [path for _method, path, _payload in self.calls if path != "/api/health"]


class StarterCase(unittest.TestCase):
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


class StarterTests(StarterCase):
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

    def test_the_remote_is_taken_back_from_the_mouse_in_the_same_visit(self):
        # After leaving Piper the remote moved the mouse; starting Piper hands
        # it back to the interface without opening a visit of its own.
        api = FakeApi(control="on", mode="pointer")
        self.assertIsNone(self.build(api).run())
        self.assertEqual(api.calls[-1], ("POST", "/api/control/mode",
                                         {"mode": "piper", "session_id": "visit-0"}))
        self.assertNotIn("/api/control/manual", api.paths())


class RemoteOnlyTests(StarterCase):
    """At login: the remote as the mouse, and Piper itself left off the screen."""

    def test_a_login_starts_the_app_and_gives_the_remote_the_mouse(self):
        api = FakeApi(up=False, control="off", mode=None)
        self.assertIsNone(self.build(api).remote())
        self.assertEqual(self.started[0][0], [sys.executable, "main.py", "--port", "8765"])
        self.assertEqual(self.backdrop.calls, [], "nothing black goes up at login")
        self.assertNotIn("/api/tv/interface", api.paths())
        self.assertEqual(api.calls[-1], ("POST", "/api/control/mode",
                                         {"mode": "pointer", "session_id": "visit-1"}))

    def test_a_visit_that_already_chose_is_left_alone(self):
        api = FakeApi(control="on", mode="piper")
        self.assertIsNone(self.build(api).remote())
        self.assertEqual(api.paths(), ["/api/tv/events?after=0"])

    def test_an_open_visit_without_a_mode_gets_the_mouse(self):
        api = FakeApi(control="on", mode=None)
        self.build(api).remote()
        self.assertEqual(api.calls[-1], ("POST", "/api/control/mode",
                                         {"mode": "pointer", "session_id": "visit-0"}))

    def test_an_app_that_never_answers_at_login_is_reported(self):
        api = FakeApi(up=False)
        self.assertIn("did not start", self.build(api, starts_app=False).remote())
        self.assertEqual(self.backdrop.calls, [])


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "home"
        self.home.mkdir()
        self.defaults = Path(self.temporary.name) / "wf-panel-pi.ini"
        self.defaults.write_text("[panel]\nwidgets_left=smenu launchers window-list\n"
                                 "launchers=x-www-browser pcmanfm x-terminal-emulator\n")
        self.libfm = Path(self.temporary.name) / "libfm.conf"
        self.libfm.write_text("[config]\nsingle_click=0\nterminal=x-terminal-emulator %s\n"
                              "\n[ui]\nbig_icon_size=48\n")
        self.rc = Path(self.temporary.name) / "rc.xml"
        self.rc.write_text("<openbox_config>\n  <windowRules>\n"
                           '    <windowRule identifier="Kodi" serverDecoration="yes" />\n'
                           "  </windowRules>\n</openbox_config>\n")
        self.environment = Path(self.temporary.name) / "environment"
        self.environment.write_text("XKB_DEFAULT_LAYOUT=gb\n")
        self.ran = []

    def install(self, **kwargs):
        # Never the real gsettings: a test must not change this machine's desktop.
        return install(home=self.home, panel_defaults=self.defaults,
                       libfm_defaults=self.libfm, labwc_defaults=(self.rc, self.environment),
                       run=lambda command, **_kwargs: self.ran.append(command), **kwargs)

    def test_the_launcher_goes_in_the_menu_on_the_desktop_and_on_the_panel(self):
        written = self.install(python="/home/rpi/piperTV/.venv/bin/python3")
        self.assertEqual(written, [self.home / ".local/share/applications/pipertv.desktop",
                                   self.home / "Desktop/pipertv.desktop",
                                   self.home / ".config/autostart/pipertv-remote.desktop",
                                   self.home / ".config/wf-panel-pi/wf-panel-pi.ini",
                                   self.home / ".config/libfm/libfm.conf",
                                   self.home / ".config/labwc/rc.xml",
                                   self.home / ".config/labwc/environment"])
        self.assertEqual(self.ran, [["gsettings", "set", "org.gnome.desktop.interface",
                                     "toolkit-accessibility", "true"]])
        for path in written[:2]:
            text = path.read_text()
            self.assertIn(f'Exec=env "PYTHONPATH={ROOT}" '
                          '"/home/rpi/piperTV/.venv/bin/python3" -m pipertv.start\n', text)

    def test_at_login_only_the_remote_starts(self):
        # Piper itself stays off the screen until someone opens it.
        self.install(python="/usr/bin/python3")
        entry = (self.home / ".config/autostart/pipertv-remote.desktop").read_text()
        self.assertIn('"/usr/bin/python3" -m pipertv.start --remote\n', entry)

    def test_the_login_entry_needs_no_working_directory(self):
        # The Pi's autostart reads Exec and ignores Path=; from the home folder
        # the package would not import, and the remote would never start.
        self.install()
        entry = (self.home / ".config/autostart/pipertv-remote.desktop").read_text()
        self.assertIn(f'env "PYTHONPATH={ROOT}"', entry)
        self.assertNotIn("Path=", entry)

    def test_the_desktop_opens_a_launcher_without_asking(self):
        launch_without_asking(self.home, self.libfm)
        text = (self.home / ".config/libfm/libfm.conf").read_text()
        # A copy of the system file with one line added, and nothing else changed.
        self.assertEqual(text, "[config]\nquick_exec=1\nsingle_click=0\n"
                               "terminal=x-terminal-emulator %s\n\n[ui]\nbig_icon_size=48\n")
        self.assertIsNone(launch_without_asking(self.home, self.libfm), "and only once")

    def test_a_file_manager_setting_of_the_user_s_own_is_kept(self):
        config = self.home / ".config" / "libfm"
        config.mkdir(parents=True)
        (config / "libfm.conf").write_text("[config]\nquick_exec=0\nsingle_click=1\n")
        launch_without_asking(self.home, self.libfm)
        self.assertEqual((config / "libfm.conf").read_text(),
                         "[config]\nquick_exec=1\nsingle_click=1\n")

    def test_the_icon_it_names_exists(self):
        entry = self.install()[0].read_text()
        icon = next(line for line in entry.splitlines() if line.startswith("Icon="))[5:]
        self.assertTrue(Path(icon).is_file())

    def test_installing_again_changes_nothing(self):
        first = [path.read_text() for path in self.install()]
        panel = (self.home / ".config/wf-panel-pi/wf-panel-pi.ini").read_text()
        second = [path.read_text() for path in self.install()]
        self.assertEqual(first[:3], second)
        self.assertEqual((self.home / ".config/wf-panel-pi/wf-panel-pi.ini").read_text(), panel)

    def test_the_panel_keeps_its_own_launchers_and_gains_piper(self):
        add_to_panel(self.home, self.defaults)
        text = (self.home / ".config/wf-panel-pi/wf-panel-pi.ini").read_text()
        self.assertIn("launchers=x-www-browser pcmanfm x-terminal-emulator pipertv", text)

    def test_the_panel_settings_already_there_are_left_alone(self):
        config = self.home / ".config" / "wf-panel-pi"
        config.mkdir(parents=True)
        (config / "wf-panel-pi.ini").write_text("[panel]\nautohide=true\nautohide_duration=300\n")
        add_to_panel(self.home, self.defaults)
        self.assertEqual((config / "wf-panel-pi.ini").read_text(),
                         "[panel]\nlaunchers=x-www-browser pcmanfm x-terminal-emulator pipertv\n"
                         "autohide=true\nautohide_duration=300\n")

    def test_the_empty_file_a_new_desktop_starts_with_is_filled_in(self):
        # What this Pi had: the file exists from the first login, with nothing in it.
        config = self.home / ".config" / "wf-panel-pi"
        config.mkdir(parents=True)
        (config / "wf-panel-pi.ini").write_text("")
        add_to_panel(self.home, self.defaults)
        self.assertEqual((config / "wf-panel-pi.ini").read_text(),
                         "[panel]\nlaunchers=x-www-browser pcmanfm x-terminal-emulator pipertv\n")

    def test_launchers_someone_chose_are_kept(self):
        config = self.home / ".config" / "wf-panel-pi"
        config.mkdir(parents=True)
        (config / "wf-panel-pi.ini").write_text("[panel]\nlaunchers = pcmanfm\n")
        add_to_panel(self.home, self.defaults)
        # Changed the way the person wrote it, spaces and all.
        self.assertEqual((config / "wf-panel-pi.ini").read_text(),
                         "[panel]\nlaunchers = pcmanfm pipertv\n")

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
