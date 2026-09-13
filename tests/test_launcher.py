import logging
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipertv.launcher import ServiceLauncher

logging.getLogger("pipertv.launcher").addHandler(logging.NullHandler())

DESKTOP = {"WAYLAND_DISPLAY": "wayland-0"}


class FakeProcess:
    """A browser that can be well behaved, stubborn, or already gone."""

    def __init__(self, command, **kwargs):
        self.command = command
        self.kwargs = kwargs
        self.returncode = None
        self.signals = []
        self.stubborn = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.signals.append("terminate")
        if not self.stubborn:
            self.returncode = -15

    def kill(self):
        self.signals.append("kill")
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("browser", timeout)
        return self.returncode


class Spawn:
    def __init__(self):
        self.started = []

    def __call__(self, command, **kwargs):
        process = FakeProcess(command, **kwargs)
        self.started.append(process)
        return process


class Clock:
    def __init__(self, now=1_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.browser = self.root / "chromium"
        self.browser.write_text("#!/bin/sh\n")
        self.browser.chmod(0o755)
        self.spawn = Spawn()
        self.clock = Clock()

    def build(self, environ=None, browser=None, **kwargs):
        # "auto" leaves the launcher to look for chromium the way it does on a Pi.
        return ServiceLauncher(browser=None if browser == "auto" else browser or str(self.browser),
                               spawn=self.spawn, environ=dict(DESKTOP if environ is None else environ),
                               profiles=self.root / "profiles", clock=self.clock, **kwargs)

    # --- the command ------------------------------------------------------

    def test_youtube_opens_full_screen_in_its_own_profile(self):
        launcher = self.build()
        launcher.launch("youtube")
        command = self.spawn.started[0].command
        self.assertEqual(command[0], str(self.browser))
        self.assertIn("--kiosk", command)
        # Both are required on a Pi 3B+: the GPU context fails, and the keyring
        # prompt cannot be answered from a sofa.
        self.assertIn("--disable-gpu", command)
        self.assertIn("--password-store=basic", command)
        self.assertEqual(command[-1], "https://www.youtube.com/tv")
        profile = self.root / "profiles" / "youtube"
        self.assertIn(f"--user-data-dir={profile}", command)
        self.assertTrue(profile.is_dir(), "the profile directory must exist before chromium needs it")

    def test_prime_video_opens_as_the_site_it_publishes(self):
        # Amazon has no television web app, so no identity is pretended here.
        launcher = self.build()
        launcher.launch("prime")
        command = self.spawn.started[0].command
        self.assertEqual(command[-1], "https://www.primevideo.com")
        self.assertFalse([part for part in command if part.startswith("--user-agent=")])

    def test_a_wayland_session_gets_the_wayland_backend(self):
        # Without this chromium chooses X11 and exits with "Missing X server".
        launcher = self.build()
        launcher.launch("youtube")
        self.assertIn("--ozone-platform=wayland", self.spawn.started[0].command)

    def test_an_x11_session_is_left_to_chromium_s_default(self):
        launcher = self.build(environ={"DISPLAY": ":0"})
        launcher.launch("youtube")
        self.assertNotIn("--ozone-platform=wayland", self.spawn.started[0].command)

    def test_the_browser_says_it_is_a_television(self):
        # A Chromecast identity returns the cast receiver and a desktop one the
        # pointer site; only a television gets the ten-foot app. The check is on
        # the fact rather than the exact string, except for the one that bit us.
        launcher = self.build()
        launcher.launch("youtube")
        agents = [part for part in self.spawn.started[0].command if part.startswith("--user-agent=")]
        self.assertEqual(len(agents), 1)
        self.assertIn("TV", agents[0])
        self.assertNotIn("CrKey", agents[0], "a Chromecast is sent the cast receiver")

    def test_the_browser_is_started_detached_and_silent(self):
        launcher = self.build()
        launcher.launch("youtube")
        started = self.spawn.started[0].kwargs
        self.assertTrue(started["start_new_session"], "a Ctrl+C in the terminal must not reach the TV")
        self.assertEqual(started["stdout"], subprocess.DEVNULL)
        self.assertEqual(started["stderr"], subprocess.DEVNULL)

    # --- what this Pi can do ---------------------------------------------

    def test_a_pi_without_chromium_says_so_instead_of_failing_silently(self):
        with patch("pipertv.launcher.shutil.which", return_value=None):
            launcher = self.build(browser="auto")
        state = launcher.snapshot()
        self.assertFalse(state["available"])
        self.assertIn("sudo apt install chromium", state["reason"])
        with self.assertRaises(RuntimeError) as refused:
            launcher.launch("youtube")
        self.assertIn("chromium", str(refused.exception))
        self.assertEqual(self.spawn.started, [])

    def test_a_named_browser_that_is_not_installed_names_the_option(self):
        launcher = self.build(browser="not-a-real-browser")
        self.assertIn("--browser", launcher.snapshot()["reason"])

    def test_outside_the_desktop_session_nothing_can_be_put_on_the_screen(self):
        launcher = self.build(environ={})
        self.assertFalse(launcher.snapshot()["available"])
        self.assertIn("desktop session", launcher.snapshot()["reason"])
        with self.assertRaises(RuntimeError):
            launcher.launch("youtube")

    def test_an_x11_session_is_a_desktop_session_too(self):
        self.assertTrue(self.build(environ={"DISPLAY": ":0"}).snapshot()["available"])

    def test_only_services_piper_knows_can_be_asked_for(self):
        launcher = self.build()
        with self.assertRaises(KeyError):
            launcher.launch("netflix")
        for value in ("", None, 7):
            with self.subTest(value=value), self.assertRaises(ValueError):
                launcher.launch(value)
        self.assertEqual([service["id"] for service in launcher.catalogue()],
                         ["youtube", "prime"])

    # --- one service at a time -------------------------------------------

    def test_the_open_service_is_reported_with_how_long_it_has_been_up(self):
        launcher = self.build()
        launcher.launch("youtube")
        self.clock.advance(90)
        running = launcher.snapshot()["running"]
        self.assertEqual((running["id"], running["name"]), ("youtube", "YouTube"))
        self.assertEqual(running["seconds"], 90.0)

    def test_asking_twice_does_not_open_a_second_browser(self):
        launcher = self.build()
        launcher.launch("youtube")
        launcher.launch("youtube")
        self.assertEqual(len(self.spawn.started), 1)

    def test_a_second_service_replaces_the_first(self):
        launcher = self.build(services={
            "youtube": {"name": "YouTube", "url": "https://www.youtube.com/tv"},
            "kodi": {"name": "Kodi", "url": "http://localhost:8080"}})
        launcher.launch("youtube")
        launcher.launch("kodi")
        self.assertEqual(self.spawn.started[0].signals, ["terminate"])
        self.assertEqual(launcher.snapshot()["running"]["id"], "kodi")

    def test_closing_gives_the_screen_back_and_remembers_the_visit(self):
        launcher = self.build()
        launcher.launch("youtube")
        self.clock.advance(1_560)
        state = launcher.stop()
        self.assertIsNone(state["running"])
        self.assertEqual(self.spawn.started[0].signals, ["terminate"])
        entry = state["history"][0]
        self.assertEqual(entry["id"], "youtube")
        self.assertEqual(entry["seconds"], 1_560.0)
        self.assertEqual(entry["age_s"], 0.0)

    def test_a_browser_that_ignores_the_polite_request_is_killed(self):
        launcher = self.build()
        launcher.launch("youtube")
        self.spawn.started[0].stubborn = True
        launcher.stop()
        self.assertEqual(self.spawn.started[0].signals, ["terminate", "kill"])
        self.assertIsNone(launcher.snapshot()["running"])

    def test_stopping_nothing_is_harmless(self):
        launcher = self.build()
        state = launcher.stop()
        self.assertIsNone(state["running"])
        self.assertEqual(state["history"], [])

    def test_a_service_closed_from_the_desktop_is_noticed(self):
        # Someone quits the browser, or it crashes: the interface must come
        # back rather than wait for a window that is no longer there.
        launcher = self.build()
        launcher.launch("youtube")
        self.clock.advance(600)
        self.spawn.started[0].returncode = 0
        state = launcher.snapshot()
        self.assertIsNone(state["running"])
        self.assertIsNone(launcher.running())
        self.assertEqual(state["history"][0]["seconds"], 600.0)
        self.assertIsNone(state["error"])

    def test_a_browser_that_fails_to_start_is_reported_not_hidden(self):
        launcher = self.build()
        launcher.launch("youtube")
        self.clock.advance(0.4)
        self.spawn.started[0].returncode = 1
        state = launcher.snapshot()
        self.assertIsNone(state["running"])
        self.assertIn("exit 1", state["error"])
        launcher.launch("youtube")
        self.assertIsNone(launcher.snapshot()["error"], "a new launch clears the old failure")

    def test_a_failure_noticed_late_is_still_a_failure(self):
        # "seconds" counts to the moment the exit was noticed, so a failed
        # launch nobody asked about for a while must not pass for viewing.
        launcher = self.build()
        launcher.launch("youtube")
        self.spawn.started[0].returncode = 1
        self.clock.advance(600)
        self.assertIn("exit 1", launcher.snapshot()["error"])

    def test_a_browser_that_cannot_be_started_at_all_says_why(self):
        def refuse(*_args, **_kwargs):
            raise OSError("Permission denied")

        launcher = self.build()
        launcher.spawn = refuse
        with self.assertRaises(RuntimeError) as failure:
            launcher.launch("youtube")
        self.assertIn("Permission denied", str(failure.exception))
        self.assertIsNone(launcher.snapshot()["running"])

    def test_history_keeps_the_most_recent_visits_newest_first(self):
        launcher = self.build(remembered=2)
        for _ in range(3):
            launcher.launch("youtube")
            self.clock.advance(60)
            launcher.stop()
            self.clock.advance(60)
        history = launcher.snapshot()["history"]
        self.assertEqual(len(history), 2)
        self.assertLess(history[0]["age_s"], history[1]["age_s"])

    def test_shutting_down_leaves_nothing_on_the_television(self):
        launcher = self.build()
        launcher.launch("youtube")
        launcher.close()
        self.assertEqual(self.spawn.started[0].signals, ["terminate"])
        self.assertIsNone(launcher.running())

    def test_profiles_live_under_the_cache_directory_by_default(self):
        environ = dict(DESKTOP, XDG_CACHE_HOME=str(self.root / "cache"))
        launcher = ServiceLauncher(browser=str(self.browser), spawn=self.spawn,
                                   environ=environ, clock=self.clock)
        launcher.launch("youtube")
        self.assertIn(f"--user-data-dir={self.root / 'cache' / 'pipertv' / 'services' / 'youtube'}",
                      self.spawn.started[0].command)


if __name__ == "__main__":
    unittest.main()
