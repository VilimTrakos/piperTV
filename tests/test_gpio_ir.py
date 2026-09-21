import os
import struct
import threading
import unittest

from pipertv.gpio_ir import (CHIP_INFO, GPIO_GET_CHIPINFO_IOCTL, GPIO_V2_GET_LINE_IOCTL,
                             GPIO_V2_GET_LINEINFO_IOCTL, HEADER, LINE_EVENT, LINE_INFO,
                             LINE_REQUEST, RECEIVER_DEFAULTS, EdgeTimeline, GpioIrDevice,
                             describe, open_receiver, validate_receiver)
from pipertv.ir_control import FrameReader
from pipertv.lirc import (OVERFLOW, PULSE, SPACE, TIMEOUT, CaptureError, CaptureOptions,
                          LircDevice, Mode2Capture, capture_stream)

# An NEC-shaped press: lead-in, a few bits, the stop pulse. Pulse first.
PRESS = [9000, 4500, 560, 560, 560, 1690, 560, 560, 560]


def ioc(direction, number, size):
    """linux/ioctl.h, asm-generic: what the constants must be."""
    return (direction << 30) | (size << 16) | (0xB4 << 8) | number


def edges_for(durations, start_ns=1_000_000_000):
    """The receiver's output for a press: low for a pulse, high for a space."""
    edges, now = [(start_ns, False)], start_ns
    for index, duration in enumerate(durations):
        now += duration * 1000
        edges.append((now, index % 2 == 0))  # a pulse ends rising, a space falling
    return edges


def words_for(durations, gap_us, start_ns=1_000_000_000):
    timeline = EdgeTimeline(gap_us)
    words = []
    for timestamp, rising in edges_for(durations, start_ns):
        words += timeline.edge(timestamp, rising)
    last = edges_for(durations, start_ns)[-1][0]
    words += timeline.idle(last + gap_us * 1000)
    return words


class ReceiverSettingTests(unittest.TestCase):
    def test_nothing_chosen_is_the_kernel_receiver(self):
        self.assertEqual(validate_receiver({}), RECEIVER_DEFAULTS)
        self.assertEqual(validate_receiver({"kind": "lirc", "pin": 18}),
                         {"kind": "lirc", "pin": None})

    def test_a_header_pin_can_be_chosen(self):
        self.assertEqual(validate_receiver({"kind": "gpio", "pin": 18}),
                         {"kind": "gpio", "pin": 18})

    def test_only_a_pin_on_the_header_is_accepted(self):
        for pin in (0, 1, 28, 40, "18", True, None, 18.0):
            with self.subTest(pin=pin), self.assertRaises(ValueError):
                validate_receiver({"kind": "gpio", "pin": pin})

    def test_anything_else_is_refused_and_says_what_is_allowed(self):
        with self.assertRaises(ValueError) as refused:
            validate_receiver({"kind": "gpio", "pin": 18, "chip": "/dev/gpiochip1"})
        self.assertIn("kind, pin", str(refused.exception))
        for values in ({"kind": "usb"}, "gpio", None):
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate_receiver(values)

    def test_a_pin_is_described_as_it_is_wired(self):
        self.assertEqual(describe({"kind": "gpio", "pin": 18}), "GPIO18 (pin 12)")
        self.assertIn("/dev/lirc0", describe({"kind": "lirc"}))
        self.assertIn("/dev/lirc1", describe("/dev/lirc1"))

    def test_the_header_map_has_every_gpio_once(self):
        self.assertEqual(sorted(HEADER), list(range(2, 28)))
        self.assertEqual(len(set(HEADER.values())), len(HEADER))

    def test_each_choice_opens_its_own_kind_of_device(self):
        self.assertIsInstance(open_receiver("/dev/lirc1", 10_000), LircDevice)
        self.assertEqual(open_receiver("/dev/lirc1", 10_000).path, "/dev/lirc1")
        self.assertIsInstance(open_receiver({"kind": "lirc"}, 10_000), LircDevice)
        device = open_receiver({"kind": "gpio", "pin": 18}, 10_000)
        self.assertIsInstance(device, GpioIrDevice)
        self.assertEqual(device.pin, 18)


class KernelInterfaceTests(unittest.TestCase):
    """The structures are the kernel's; a wrong size is a wrong ioctl."""

    def test_structures_have_the_kernel_s_sizes(self):
        self.assertEqual(CHIP_INFO.size, 68)
        self.assertEqual(LINE_INFO.size, 256)
        self.assertEqual(LINE_REQUEST.size, 592)
        self.assertEqual(LINE_EVENT.size, 48)

    def test_ioctl_numbers_follow_from_the_sizes(self):
        self.assertEqual(GPIO_GET_CHIPINFO_IOCTL, ioc(2, 0x01, CHIP_INFO.size))
        self.assertEqual(GPIO_V2_GET_LINEINFO_IOCTL, ioc(3, 0x05, LINE_INFO.size))
        self.assertEqual(GPIO_V2_GET_LINE_IOCTL, ioc(3, 0x07, LINE_REQUEST.size))


class EdgeTimelineTests(unittest.TestCase):
    def test_edges_become_the_pulses_and_spaces_between_them(self):
        words = words_for(PRESS, gap_us=120_000)
        kinds = [PULSE if index % 2 == 0 else SPACE for index in range(len(PRESS))]
        self.assertEqual(words[:-1], [kind | value for kind, value in zip(kinds, PRESS)])
        self.assertEqual(words[-1], TIMEOUT | 120_000)

    def test_quiet_is_reported_once_and_only_after_the_gap(self):
        timeline = EdgeTimeline(10_000)
        timeline.edge(0, False)
        timeline.edge(560_000, True)
        self.assertEqual(timeline.idle(560_000 + 9_000_000), [])
        self.assertEqual(timeline.idle(560_000 + 10_000_000), [TIMEOUT | 10_000])
        self.assertEqual(timeline.idle(560_000 + 50_000_000), [])

    def test_the_first_edge_after_quiet_only_marks_a_start(self):
        timeline = EdgeTimeline(10_000)
        timeline.edge(0, False)
        timeline.edge(560_000, True)
        timeline.idle(100_000_000)
        self.assertEqual(timeline.edge(200_000_000, False), [])
        self.assertEqual(timeline.edge(200_560_000, True), [PULSE | 560])

    def test_a_line_held_low_is_not_quiet(self):
        # The receiver is still seeing carrier: nothing has ended.
        timeline = EdgeTimeline(10_000)
        timeline.edge(0, False)
        self.assertEqual(timeline.idle(1_000_000_000), [])

    def test_a_learned_press_is_the_same_whichever_receiver_heard_it(self):
        # What the kernel's receiver reports, MODE2 word for MODE2 word.
        parser = Mode2Capture(CaptureOptions(gap_us=120_000))
        for word in words_for(PRESS, gap_us=120_000):
            parser.event(word, now=1.0)
        self.assertTrue(parser.complete)
        signal = parser.result("gpio")
        self.assertEqual(signal["durations_us"], PRESS)
        self.assertEqual(signal["source"], "gpio")

    def test_the_remote_sees_one_frame_per_press(self):
        reader = FrameReader()
        data = b"".join(struct.pack("=I", word)
                        for word in words_for(PRESS, 10_000) + words_for(PRESS, 10_000))
        frames = reader.feed(data, now=1.0)
        self.assertEqual(frames, [tuple(PRESS), tuple(PRESS)])


def event(timestamp_ns, rising, seqno):
    return LINE_EVENT.pack(timestamp_ns, 1 if rising else 2, 18, seqno, seqno, b"")


class DeviceTests(unittest.TestCase):
    """Reading a line's events, with a pipe standing in for the line."""

    def device(self, clock_ns=lambda: 0):
        read, write = os.pipe()
        self.addCleanup(os.close, write)
        device = GpioIrDevice("/dev/gpiochip0", 18, 10_000, clock_ns=clock_ns)
        device.fd = read
        self.addCleanup(device.__exit__)
        return device, write

    def test_events_are_read_as_mode2(self):
        device, write = self.device()
        os.write(write, event(0, False, 1) + event(9_000_000, True, 2)
                 + event(13_500_000, False, 3))
        data = device.read(0.5)
        self.assertEqual(struct.unpack("=2I", data), (PULSE | 9000, SPACE | 4500))

    def test_nothing_to_say_is_none(self):
        device, _write = self.device()
        self.assertIsNone(device.read(0.01))

    def test_quiet_after_a_press_is_reported_while_waiting(self):
        now = [0]
        device, write = self.device(clock_ns=lambda: now[0])
        os.write(write, event(0, False, 1) + event(560_000, True, 2))
        device.read(0.5)
        now[0] = 560_000 + 10_000_000
        self.assertEqual(struct.unpack("=I", device.read(0.01)), (TIMEOUT | 10_000,))

    def test_dropped_edges_are_reported_rather_than_guessed(self):
        device, write = self.device()
        os.write(write, event(0, False, 1) + event(560_000, True, 5))
        words = struct.unpack("=2I", device.read(0.5))
        self.assertEqual(words, (OVERFLOW, PULSE | 560))

    def test_a_capture_reads_a_pin_to_the_end(self):
        now = [0]
        device, write = self.device(clock_ns=lambda: now[0])
        device.timeline.gap_ns = 120_000_000
        edges = edges_for(PRESS, start_ns=0)
        os.write(write, b"".join(event(ts, rising, n + 1) for n, (ts, rising) in enumerate(edges)))
        now[0] = edges[-1][0] + 120_000_000
        signal = capture_stream(device, CaptureOptions(gap_us=120_000), threading.Event())
        self.assertEqual(signal["durations_us"], PRESS)
        self.assertEqual(signal["source"], "gpio")

    def test_a_line_that_stops_is_an_error(self):
        read, write = os.pipe()
        os.close(write)
        device = GpioIrDevice("/dev/gpiochip0", 18, 10_000)
        device.fd = read
        self.addCleanup(device.__exit__)
        with self.assertRaises(CaptureError):
            device.read(0.5)


if __name__ == "__main__":
    unittest.main()
