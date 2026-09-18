import contextlib
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipertv import pointer
from pipertv.pointer import (AXIS_MAX, VirtualPointer, ask_compositor, health,
                             read_framebuffer_size, read_screen_size, to_axis, to_pixel)

SCREEN = (1920, 1080)


class FakeUinput:
    """Record everything the module would hand to the kernel."""

    def __init__(self, accept_all=True):
        self.ioctls = []
        self.writes = []
        self.closed = []
        self.accept_all = accept_all
        self.fd = 77

    def ioctl(self, fd, request, value=0):
        self.ioctls.append((request, value))
        return 0

    def write(self, fd, payload):
        self.writes.append(payload)
        return len(payload) if self.accept_all else len(payload) - 1

    def requests(self):
        return [request for request, _value in self.ioctls]

    def events(self, since=0):
        """Decode every event written, in order, as (type, code, value)."""
        decoded = []
        for payload in self.writes[since:]:
            assert len(payload) % pointer.EVENT_SIZE == 0, "events must be whole structs"
            for offset in range(0, len(payload), pointer.EVENT_SIZE):
                _sec, _usec, kind, code, value = struct.unpack_from(
                    pointer.EVENT, payload, offset)
                decoded.append((kind, code, value))
        return decoded

    def moves(self, since=0):
        return [(code, value) for kind, code, value in self.events(since)
                if kind == pointer.EV_ABS]

    def keys(self, since=0):
        return [(code, value) for kind, code, value in self.events(since)
                if kind == pointer.EV_KEY]


@contextlib.contextmanager
def fake_pointer(screen=SCREEN, accept_all=True, **kwargs):
    fake = FakeUinput(accept_all)
    with (patch.object(pointer.os, "open", return_value=fake.fd),
          patch.object(pointer.os, "write", side_effect=fake.write),
          patch.object(pointer.os, "close", side_effect=fake.closed.append),
          patch("fcntl.ioctl", side_effect=fake.ioctl)):
        device = VirtualPointer(screen, settle_s=0, **kwargs)
        device.open()
        yield device, fake


class AxisTests(unittest.TestCase):
    def test_screen_edges_map_to_axis_limits(self):
        self.assertEqual(to_axis(0, 1920), 0)
        self.assertEqual(to_axis(1919, 1920), AXIS_MAX)
        self.assertEqual(to_pixel(0, 1920), 0)
        self.assertEqual(to_pixel(AXIS_MAX, 1920), 1919)

    def test_positions_survive_a_round_trip(self):
        for extent in (800, 1920, 3840):
            for pixel in (0, 1, 37, extent // 2, extent - 2, extent - 1):
                with self.subTest(extent=extent, pixel=pixel):
                    self.assertEqual(to_pixel(to_axis(pixel, extent), extent), pixel)

    def test_positions_outside_the_screen_are_clamped(self):
        self.assertEqual(to_axis(-500, 1920), 0)
        self.assertEqual(to_axis(99999, 1920), AXIS_MAX)
        self.assertEqual(to_pixel(-10, 1920), 0)
        self.assertEqual(to_pixel(999999, 1920), 1919)

    def test_an_implausible_screen_is_rejected(self):
        for extent in (1, 0, -1920, True, "1920", 19.2):
            with self.subTest(extent=extent), self.assertRaises(ValueError):
                to_axis(10, extent)


class ScreenSizeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "virtual_size"

    def test_reads_the_framebuffer_size(self):
        self.path.write_text("1920,1080\n")
        self.assertEqual(read_framebuffer_size(str(self.path)), (1920, 1080))

    def test_unusable_contents_report_nothing(self):
        for text in ("", "garbage", "1920", "1,1", "1920,1080,60", "-1920,1080"):
            with self.subTest(text=text):
                self.path.write_text(text)
                self.assertIsNone(read_framebuffer_size(str(self.path)))

    def test_a_missing_file_reports_nothing(self):
        self.assertIsNone(read_framebuffer_size(str(self.path.with_name("absent"))))


class Answer:
    """Stands in for the display tool the compositor answers through."""

    def __init__(self, stdout="", fail=None):
        self.stdout = stdout
        self.fail = fail
        self.calls = 0

    def __call__(self, command, **_kwargs):
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return self


WLR_RANDR = """HDMI-A-1 "GRU GRUNDIG TV (HDMI-A-1)"
  Modes:
    1920x1080 px, 50.000000 Hz (preferred, current)
    1600x1200 px, 60.000000 Hz
"""


class ScreenSizeTests(unittest.TestCase):
    """What the screen measures, which is not what the console framebuffer says."""

    def setUp(self):
        pointer._asked.update({"at": -float("inf"), "size": None})
        self.addCleanup(pointer._asked.update, {"at": -float("inf"), "size": None})
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "virtual_size"
        self.path.write_text("1024,768\n")   # the mode the kernel set at boot

    def clock(self):
        return 1000.0

    def test_the_mode_the_screen_is_running_is_the_one_that_counts(self):
        answer = Answer(WLR_RANDR)
        self.assertEqual(ask_compositor(run=answer, clock=self.clock), (1920, 1080))

    def test_the_stale_console_size_does_not_win(self):
        # A Pi that booted with the television off keeps 1024x768 in fb0 for
        # ever, and half the screen would be out of the cursor's reach.
        answer = Answer(WLR_RANDR)
        size = read_screen_size(str(self.path),
                                ask=lambda: ask_compositor(run=answer, clock=self.clock))
        self.assertEqual(size, (1920, 1080))

    def test_a_desktop_that_answers_nothing_falls_back_to_the_framebuffer(self):
        self.assertEqual(read_screen_size(str(self.path), ask=lambda: None), (1024, 768))

    def test_a_pi_without_the_tool_is_not_a_failure(self):
        answer = Answer(fail=FileNotFoundError("no wlr-randr here"))
        self.assertIsNone(ask_compositor(run=answer, clock=self.clock))

    def test_an_answer_with_no_current_mode_reports_nothing(self):
        answer = Answer("HDMI-A-1 \"TV\"\n  Enabled: no\n")
        self.assertIsNone(ask_compositor(run=answer, clock=self.clock))

    def test_it_is_not_asked_again_between_presses(self):
        answer = Answer(WLR_RANDR)
        for _ in range(5):
            ask_compositor(run=answer, clock=self.clock)
        self.assertEqual(answer.calls, 1)

    def test_a_television_switched_on_later_is_noticed(self):
        answer = Answer(WLR_RANDR)
        moments = iter([1000.0, 1000.0 + pointer.SIZE_CACHE_S + 1])
        self.assertEqual(ask_compositor(run=answer, clock=lambda: next(moments)),
                         (1920, 1080))
        ask_compositor(run=answer, clock=lambda: next(moments))
        self.assertEqual(answer.calls, 2)


class HealthTests(unittest.TestCase):
    def test_a_missing_device_explains_the_setup_step(self):
        result = health("/definitely/not/uinput")
        self.assertFalse(result["ok"])
        self.assertFalse(result["device_exists"])
        self.assertIn("modprobe", result["error"])

    def test_a_writable_device_is_healthy(self):
        with tempfile.NamedTemporaryFile() as handle:
            result = health(handle.name)
        self.assertTrue(result["ok"])
        self.assertTrue(result["device_writable"])
        self.assertNotIn("error", result)


class SetupTests(unittest.TestCase):
    def test_an_implausible_screen_or_device_is_refused(self):
        for kwargs in ({"screen": (1, 1080)}, {"screen": (1920, True)},
                       {"screen": ("1920", 1080)}, {"screen": (1920, 99999999)},
                       {"device": "/dev/uinput; rm -rf /"}, {"device": "uinput"},
                       {"device": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                VirtualPointer(**{"screen": SCREEN, **kwargs})

    def test_the_device_is_declared_before_it_is_created(self):
        with fake_pointer() as (_device, fake):
            requests = fake.requests()
        self.assertEqual(requests[-1], pointer.UI_DEV_CREATE)
        self.assertIn(pointer.UI_DEV_SETUP, requests)
        # Both axes, all three buttons and the wheel must be declared up front.
        self.assertEqual(requests.count(pointer.UI_SET_ABSBIT), 2)
        self.assertEqual(requests.count(pointer.UI_SET_KEYBIT), 3)
        self.assertEqual(requests.count(pointer.UI_SET_RELBIT), 1)
        self.assertEqual(requests.count(pointer.UI_SET_EVBIT), 4)

    def test_the_axes_span_the_whole_screen(self):
        with fake_pointer() as (_device, fake):
            setups = [value for request, value in fake.ioctls
                      if request == pointer.UI_ABS_SETUP]
        self.assertEqual(len(setups), 2)
        for axis, payload in zip((pointer.ABS_X, pointer.ABS_Y), setups):
            code, _filler, value, minimum, maximum, _fuzz, _flat, _res = struct.unpack(
                pointer.ABS_SETUP, payload)
            self.assertEqual((code, value, minimum, maximum), (axis, 0, 0, AXIS_MAX))

    def test_opening_does_not_move_before_the_control_gate_is_rechecked(self):
        with fake_pointer() as (device, fake):
            x, y = device.position
            opening = fake.moves()
        self.assertAlmostEqual(x, 960, delta=1)
        self.assertAlmostEqual(y, 540, delta=1)
        # A slow udev setup can outlive the selected input. Only a later explicit
        # movement may use the centre as its initial origin.
        self.assertEqual(opening, [])

    def test_opening_twice_is_refused(self):
        with fake_pointer() as (device, _fake), self.assertRaises(RuntimeError):
            device.open()


class MovementTests(unittest.TestCase):
    def test_snapping_jumps_to_an_exact_pixel(self):
        with fake_pointer() as (device, fake):
            mark = len(fake.writes)
            self.assertEqual(device.move_to(0, 0), (0, 0))
            self.assertEqual(fake.moves(mark), [(pointer.ABS_X, 0), (pointer.ABS_Y, 0)])

            mark = len(fake.writes)
            self.assertEqual(device.move_to(1919, 1079), (1919, 1079))
            self.assertEqual(fake.moves(mark),
                             [(pointer.ABS_X, AXIS_MAX), (pointer.ABS_Y, AXIS_MAX)])

    def test_every_batch_ends_with_a_sync(self):
        with fake_pointer() as (device, fake):
            mark = len(fake.writes)
            device.move_to(100, 100)
            events = fake.events(mark)
        self.assertEqual(events[-1], (pointer.EV_SYN, pointer.SYN_REPORT, 0))

    def test_pointer_mode_nudges_from_where_it_left_off(self):
        with fake_pointer() as (device, _fake):
            device.move_to(500, 500)
            self.assertEqual(device.move_by(10, -10), (510, 490))
            self.assertEqual(device.move_by(-10, 10), (500, 500))

    def test_moves_beyond_the_screen_stop_at_the_edge(self):
        with fake_pointer() as (device, _fake):
            device.move_to(5, 5)
            self.assertEqual(device.move_by(-100, -100), (0, 0))
            device.move_to(1915, 1075)
            self.assertEqual(device.move_by(100, 100), (1919, 1079))

    def test_a_click_presses_and_releases(self):
        with fake_pointer() as (device, fake):
            mark = len(fake.writes)
            device.click()
            self.assertEqual(fake.keys(mark),
                             [(pointer.BTN_LEFT, 1), (pointer.BTN_LEFT, 0)])
            mark = len(fake.writes)
            device.click("right")
            self.assertEqual(fake.keys(mark),
                             [(pointer.BTN_RIGHT, 1), (pointer.BTN_RIGHT, 0)])

    def test_an_unknown_button_is_refused(self):
        with fake_pointer() as (device, _fake), self.assertRaises(ValueError):
            device.click("scroll")

    def test_a_partial_write_is_an_error_not_a_silent_half_move(self):
        with self.assertRaises(OSError):
            with fake_pointer(accept_all=False) as (device, _fake):
                device.move_to(100, 100)


class ShutdownTests(unittest.TestCase):
    def test_closing_releases_every_button_and_removes_the_device(self):
        with fake_pointer() as (device, fake):
            mark = len(fake.writes)
            device.close()
            released = fake.keys(mark)
            requests = fake.requests()
        self.assertEqual(sorted(released),
                         [(pointer.BTN_LEFT, 0), (pointer.BTN_RIGHT, 0),
                          (pointer.BTN_MIDDLE, 0)])
        self.assertEqual(requests[-1], pointer.UI_DEV_DESTROY)
        self.assertEqual(fake.closed, [fake.fd])

    def test_closing_twice_removes_the_device_once(self):
        with fake_pointer() as (device, fake):
            device.close()
            device.close()
        self.assertEqual(fake.closed, [fake.fd])
        self.assertEqual(fake.requests().count(pointer.UI_DEV_DESTROY), 1)

    def test_a_closed_pointer_cannot_move_the_cursor(self):
        with fake_pointer() as (device, _fake):
            device.close()
            with self.assertRaises(RuntimeError):
                device.move_to(100, 100)
            with self.assertRaises(RuntimeError):
                device.click()

    def test_a_closed_pointer_cannot_be_reopened(self):
        with fake_pointer() as (device, _fake):
            device.close()
            with self.assertRaisesRegex(RuntimeError, "reopened"):
                device.open()

    def test_destroy_is_attempted_even_if_releasing_buttons_fails(self):
        with fake_pointer() as (device, fake):
            fake.accept_all = False
            device.close()
            self.assertIn(pointer.UI_DEV_DESTROY, fake.requests())
            self.assertEqual(fake.closed, [fake.fd])


class WheelTests(unittest.TestCase):
    """A page scrolls where it sits, without dragging a scrollbar to do it."""

    def test_the_wheel_turns_up_and_down(self):
        with fake_pointer() as (device, fake):
            before = len(fake.writes)
            device.scroll(2)
            self.assertEqual([(code, value) for kind, code, value in fake.events(before)
                              if kind == pointer.EV_REL], [(pointer.REL_WHEEL, 2)])
            before = len(fake.writes)
            device.scroll(-3)
            self.assertEqual([(code, value) for kind, code, value in fake.events(before)
                              if kind == pointer.EV_REL], [(pointer.REL_WHEEL, -3)])

    def test_turning_the_wheel_leaves_the_cursor_where_it_is(self):
        with fake_pointer() as (device, _fake):
            before = device.position
            device.scroll(-2)
            self.assertEqual(device.position, before)

    def test_nothing_is_written_for_no_turn(self):
        with fake_pointer() as (device, fake):
            before = len(fake.writes)
            self.assertEqual(device.scroll(0), 0)
            self.assertEqual(len(fake.writes), before)

    def test_an_implausible_turn_is_refused(self):
        with fake_pointer() as (device, _fake):
            for clicks in (1000, -99, 1.5, True, "2", None):
                with self.subTest(clicks=clicks), self.assertRaises(ValueError):
                    device.scroll(clicks)


if __name__ == "__main__":
    unittest.main()
