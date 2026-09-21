import logging
import os
import signal
import subprocess
import tempfile
import unittest

from pipertv.backdrop import MODULE, ROOT, Backdrop, _wait_until_shown
from tests.test_interface import FakeKill, FakeProcfs

logging.getLogger("pipertv.backdrop").addHandler(logging.NullHandler())

BACKDROP = ["/home/rpi/piperTV/.venv/bin/python3", "-m", "pipertv.backdrop"]
APP = ["/home/rpi/piperTV/.venv/bin/python3", "main.py"]


class FakeProcess:
    def __init__(self, command, **kwargs):
        self.command = command
        self.kwargs = kwargs
        self.killed = False

    def kill(self):
        self.killed = True


class BackdropTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.started = []

    def spawn(self, command, **kwargs):
        # Never the real Popen: a test must not put a window on the screen of
        # the machine it is running on.
        process = FakeProcess(command, **kwargs)
        self.started.append(process)
        return process

    def build(self, processes, stubborn=(), shown=True, **kwargs):
        FakeProcfs(self.temporary.name, processes)
        self.kill = FakeKill(self.temporary.name, processes, stubborn)
        ticks = iter(range(0, 100_000))
        return Backdrop(procfs=self.temporary.name, spawn=self.spawn, kill=self.kill,
                        clock=lambda: float(next(ticks)), sleep=lambda _s: None,
                        wait_until_shown=lambda _process: shown, python="/usr/bin/python3",
                        grace_s=5.0, **kwargs)

    def test_it_is_found_by_its_module_whoever_started_it(self):
        # The app that started it may have been restarted since.
        backdrop = self.build({10: BACKDROP, 11: APP,
                               12: ["python3", "-m", "pipertv.backdrop_old"]})
        self.assertEqual(backdrop.processes(), [10])
        self.assertTrue(backdrop.showing())

    def test_one_already_on_the_screen_is_kept_rather_than_doubled(self):
        backdrop = self.build({10: BACKDROP})
        backdrop.show()
        self.assertEqual(self.started, [])

    def test_it_is_started_as_a_process_of_its_own(self):
        backdrop = self.build({})
        result = backdrop.show()
        self.assertIsNone(result["error"])
        process = self.started[0]
        self.assertEqual(process.command, ["/usr/bin/python3", "-m", MODULE])
        self.assertEqual(process.kwargs["cwd"], str(ROOT))
        # Outlives an app restart, the same as the interface in front of it.
        self.assertTrue(process.kwargs["start_new_session"])
        self.assertEqual(process.kwargs["stdout"], subprocess.PIPE)

    def test_one_that_never_appears_is_reported_and_ended(self):
        backdrop = self.build({}, shown=False)
        result = backdrop.show()
        self.assertIn("did not appear", result["error"])
        self.assertTrue(self.started[0].killed)

    def test_one_that_failed_is_not_waited_on_again_at_every_launch(self):
        backdrop = self.build({}, shown=False)
        backdrop.show()
        backdrop.show()
        self.assertEqual(len(self.started), 1)

    def test_a_python_that_cannot_be_started_is_reported(self):
        backdrop = self.build({})

        def refuse(*_args, **_kwargs):
            raise OSError("no such file")

        backdrop.spawn = refuse
        self.assertIn("no such file", backdrop.show()["error"])

    def test_closing_asks_first(self):
        backdrop = self.build({10: BACKDROP, 11: APP})
        self.assertFalse(backdrop.close()["showing"])
        self.assertEqual(self.kill.signals, [(10, signal.SIGTERM)])

    def test_one_that_will_not_go_is_made_to(self):
        backdrop = self.build({10: BACKDROP}, stubborn={10})
        backdrop.close()
        self.assertEqual(self.kill.signals, [(10, signal.SIGTERM), (10, signal.SIGKILL)])


class WaitUntilShownTests(unittest.TestCase):
    """The window says when it is on the screen, over the pipe it was given."""

    def process_saying(self, text: bytes):
        read, write = os.pipe()
        os.write(write, text)
        os.close(write)
        process = FakeProcess([])
        process.stdout = os.fdopen(read, "rb")
        return process

    def test_ready_means_shown(self):
        process = self.process_saying(b"ready\n")
        self.assertTrue(_wait_until_shown(process, timeout_s=2.0))
        self.assertTrue(process.stdout.closed)

    def test_ending_before_saying_so_means_not_shown(self):
        process = self.process_saying(b"")
        self.assertFalse(_wait_until_shown(process, timeout_s=2.0))

    def test_silence_until_the_deadline_means_not_shown(self):
        read, write = os.pipe()
        self.addCleanup(os.close, write)
        process = FakeProcess([])
        process.stdout = os.fdopen(read, "rb")
        self.assertFalse(_wait_until_shown(process, timeout_s=0.05))


if __name__ == "__main__":
    unittest.main()
