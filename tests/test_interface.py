import logging
import shutil
import signal
import tempfile
import unittest
from pathlib import Path

from pipertv.interface import Interface

logging.getLogger("pipertv.interface").addHandler(logging.NullHandler())

KIOSK = ["/usr/lib/chromium/chromium", "--ozone-platform=wayland", "--kiosk",
         "--user-data-dir=/tmp/kiosk-gpu-off", "http://127.0.0.1:8765/tv?boot=0"]
RENDERER = ["/usr/lib/chromium/chromium", "--type=renderer",
            "--url=http://127.0.0.1:8765/tv?boot=0"]
YOUTUBE = ["/usr/lib/chromium/chromium", "--kiosk", "https://www.youtube.com/tv"]


class FakeProcfs:
    """A /proc with the command lines of a few processes."""

    def __init__(self, root, processes):
        self.root = Path(root)
        for pid, arguments in processes.items():
            directory = self.root / str(pid)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "cmdline").write_bytes(b"\0".join(a.encode() for a in arguments) + b"\0")
        (self.root / "self").mkdir(exist_ok=True)  # a non-numeric entry to skip


class FakeKill:
    """Signals, and a /proc entry that disappears with the process, as it does."""

    def __init__(self, root, alive=(), stubborn=()):
        self.root = Path(root)
        self.alive = set(alive)
        self.stubborn = set(stubborn)
        self.signals = []

    def __call__(self, pid, number):
        if pid not in self.alive:
            raise ProcessLookupError(pid)
        if number == 0:
            return
        self.signals.append((pid, number))
        if number == signal.SIGKILL or pid not in self.stubborn:
            self.alive.discard(pid)
            shutil.rmtree(self.root / str(pid), ignore_errors=True)


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

    def build(self, processes, alive=(), stubborn=()):
        FakeProcfs(self.temporary.name, processes)
        self.kill = FakeKill(self.temporary.name, alive or processes, stubborn)
        return Interface(port=8765, procfs=self.temporary.name, kill=self.kill,
                         clock=lambda: 0.0, sleep=lambda _s: None, grace_s=0.0)

    def test_the_window_showing_the_interface_is_found(self):
        interface = self.build({4242: KIOSK, 99: ["/usr/bin/pcmanfm"]})
        self.assertEqual(interface.windows(), [4242])
        self.assertTrue(interface.showing())

    def test_a_browser_child_process_is_not_the_window(self):
        # Chromium runs a dozen of these; killing one closes nothing.
        interface = self.build({4242: KIOSK, 4243: RENDERER})
        self.assertEqual(interface.windows(), [4242])

    def test_an_open_service_is_not_mistaken_for_the_interface(self):
        # youtube.com/tv ends in /tv as well; the port is what distinguishes it.
        interface = self.build({5000: YOUTUBE})
        self.assertEqual(interface.windows(), [])
        self.assertFalse(interface.showing())

    def test_another_port_is_another_app(self):
        interface = self.build({4242: ["chromium", "http://127.0.0.1:9000/tv"]})
        self.assertEqual(interface.windows(), [])

    def test_closing_asks_first_and_reports_what_went(self):
        interface = self.build({4242: KIOSK})
        result = interface.close()
        self.assertEqual(self.kill.signals, [(4242, signal.SIGTERM)])
        self.assertEqual(result["closed"], [4242])
        self.assertFalse(result["showing"])

    def test_a_window_that_will_not_go_is_made_to(self):
        interface = self.build({4242: KIOSK}, stubborn={4242})
        interface.close()
        self.assertEqual(self.kill.signals, [(4242, signal.SIGTERM), (4242, signal.SIGKILL)])

    def test_closing_nothing_is_harmless(self):
        interface = self.build({99: ["/usr/bin/pcmanfm"]})
        result = interface.close()
        self.assertEqual(result["closed"], [])
        self.assertEqual(self.kill.signals, [])

    def test_a_process_that_ends_while_being_read_is_skipped(self):
        interface = self.build({4242: KIOSK})
        Path(self.temporary.name, "7777").mkdir()  # a pid directory with no cmdline
        self.assertEqual(interface.windows(), [4242])

    def test_a_nonsense_port_is_refused(self):
        for port in (0, 70000, "8765", True):
            with self.subTest(port=port), self.assertRaises(ValueError):
                Interface(port=port)


if __name__ == "__main__":
    unittest.main()
