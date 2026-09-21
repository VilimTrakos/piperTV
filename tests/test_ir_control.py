import struct
import threading
import time
import unittest
from unittest.mock import patch

from pipertv import lirc
from pipertv.ir_control import (FrameReader, IRController, Match, RepeatFilter,
                                SignalMatcher, decode_rc5, is_nec_frame,
                                is_nec_repeat, split_frames, timing_distance)

RC5_UNIT = 889


def rc5_frame(address, command, toggle, unit=RC5_UNIT):
    """Encode 14 RC5 bits as the pulse/space envelope a TSOP2238 reports.

    Half-bit 1 is a mark and 0 a space; the receiver omits the leading and
    trailing idle half-bits, so the envelope starts and ends with a mark.
    """
    bits = [1, 0 if command > 63 else 1, toggle]
    bits += [(address >> shift) & 1 for shift in range(4, -1, -1)]
    bits += [(command >> shift) & 1 for shift in range(5, -1, -1)]
    halves = []
    for bit in bits:
        halves.extend([0, 1] if bit else [1, 0])
    halves = halves[1:]
    durations, current, run = [], halves[0], 0
    for half in halves:
        if half == current:
            run += 1
        else:
            durations.append(run * unit)
            current, run = half, 1
    durations.append(run * unit)
    if len(durations) % 2 == 0:
        durations.pop()  # a trailing space is the gap, not part of the frame
    return tuple(durations)


def nec_frame(code=0x20DF10EF):
    durations = [9000, 4500]
    for bit in range(32):
        durations.extend((560, 1690 if (code >> bit) & 1 else 560))
    durations.append(560)
    return tuple(durations)


def document(samples, source="lirc"):
    return {"recordings": {button: {"label": button, "samples": [
        {"durations_us": list(durations), "source": source}]}
        for button, durations in samples.items()}}


def words(*events):
    return b"".join(struct.pack("=I", word) for word in events)


class FrameSplittingTests(unittest.TestCase):
    def test_a_held_press_splits_into_separate_frames(self):
        durations = [560, 560, 560, 40_000, 900, 900, 900]
        self.assertEqual(list(split_frames(durations)),
                         [(560, 560, 560), (900, 900, 900)])

    def test_fragments_too_short_to_decode_are_dropped(self):
        self.assertEqual(list(split_frames([560, 40_000, 900, 900, 900])),
                         [(900, 900, 900)])
        self.assertEqual(list(split_frames([560, 560])), [])


class Rc5Tests(unittest.TestCase):
    def test_round_trips_address_command_and_toggle(self):
        for address, command, toggle in [(0, 53, 1), (0, 0, 0), (5, 63, 1), (0, 12, 0)]:
            with self.subTest(address=address, command=command, toggle=toggle):
                self.assertEqual(decode_rc5(rc5_frame(address, command, toggle)),
                                 ((address, command), toggle))

    def test_extended_commands_use_the_field_bit(self):
        self.assertEqual(decode_rc5(rc5_frame(0, 100, 0)), ((0, 100), 0))

    def test_alternating_rc5_data_with_few_or_no_short_intervals_still_decodes(self):
        for address, command, toggle in ((5, 21, 0), (1, 85, 1), (10, 106, 1)):
            with self.subTest(address=address, command=command, toggle=toggle):
                self.assertEqual(decode_rc5(rc5_frame(address, command, toggle)),
                                 ((address, command), toggle))

    def test_all_rc5_addresses_commands_and_toggles_round_trip(self):
        for address in range(32):
            for command in range(128):
                for toggle in (0, 1):
                    self.assertEqual(decode_rc5(rc5_frame(address, command, toggle)),
                                     ((address, command), toggle))

    def test_a_modestly_different_clock_still_decodes(self):
        self.assertEqual(decode_rc5(rc5_frame(0, 53, 1, unit=920)), ((0, 53), 1))

    def test_other_protocols_are_not_decoded_as_rc5(self):
        for frame in (nec_frame(), (9000, 2250, 560), (560,), tuple(range(1, 30))):
            with self.subTest(frame=len(frame)):
                self.assertIsNone(decode_rc5(frame))


class TimingTests(unittest.TestCase):
    def test_proportional_timing_differences_are_tolerated(self):
        reference = nec_frame()
        stretched = tuple(round(value * 1.08) for value in reference)
        self.assertIsNotNone(timing_distance(stretched, reference))

    def test_one_wrong_edge_rejects_the_match(self):
        reference = list(nec_frame())
        wrong = list(reference)
        wrong[20] = wrong[20] * 3
        self.assertIsNone(timing_distance(tuple(wrong), tuple(reference)))

    def test_different_lengths_never_match(self):
        self.assertIsNone(timing_distance(nec_frame(), nec_frame()[:-2]))

    def test_nec_shapes_are_recognised(self):
        self.assertTrue(is_nec_frame(nec_frame()))
        self.assertTrue(is_nec_repeat((9000, 2250, 560)))
        self.assertFalse(is_nec_repeat((9000, 4500, 560)))


class MatcherTests(unittest.TestCase):
    def test_rc5_recordings_match_by_decoded_command(self):
        matcher = SignalMatcher(document({"up": rc5_frame(0, 22, 0),
                                          "down": rc5_frame(0, 23, 0)}))
        self.assertEqual(matcher.button_count, 2)
        # A later press flips the toggle; it is still the same button.
        match = matcher.match(rc5_frame(0, 23, 1))
        self.assertEqual((match.button_id, match.protocol, match.toggle), ("down", "rc5", 1))

    def test_raw_recordings_match_by_timing(self):
        matcher = SignalMatcher(document({"power": nec_frame()}))
        match = matcher.match(nec_frame())
        self.assertEqual((match.button_id, match.protocol), ("power", "nec"))

    def test_simulated_recordings_never_control_anything(self):
        matcher = SignalMatcher(document({"power": nec_frame()}, source="demo"))
        self.assertEqual(matcher.button_count, 0)
        self.assertIsNone(matcher.match(nec_frame()))

    def test_two_buttons_recorded_alike_are_never_guessed_between(self):
        matcher = SignalMatcher(document({"play": nec_frame(), "pause": nec_frame()}))
        self.assertIsNone(matcher.match(nec_frame()))

    def test_two_buttons_sharing_an_rc5_code_are_never_guessed_between(self):
        matcher = SignalMatcher(document({"play": rc5_frame(0, 53, 0),
                                          "pause": rc5_frame(0, 53, 1)}))
        self.assertIsNone(matcher.match(rc5_frame(0, 53, 0)))

    def test_an_unknown_signal_matches_nothing(self):
        matcher = SignalMatcher(document({"power": nec_frame()}))
        self.assertIsNone(matcher.match(rc5_frame(0, 53, 0)))
        self.assertIsNone(matcher.match(nec_frame(0x12345678)))


class RepeatFilterTests(unittest.TestCase):
    def setUp(self):
        self.filter = RepeatFilter()
        self.down = Match("down", "rc5", 0)
        self.ok = Match("ok", "rc5", 0)

    def test_a_held_direction_repeats_after_a_delay(self):
        emitted = [self.filter.accept(self.down, step / 10) for step in range(12)]
        self.assertEqual(emitted[0], "down")
        self.assertEqual(emitted[1:4], [None, None, None])
        self.assertTrue(all(value == "down" for value in emitted[4:]))

    def test_a_held_ordinary_button_fires_once(self):
        emitted = [self.filter.accept(self.ok, step / 10) for step in range(12)]
        self.assertEqual(emitted[0], "ok")
        self.assertEqual(set(emitted[1:]), {None})

    def test_a_new_press_is_recognised_by_its_toggle(self):
        self.assertEqual(self.filter.accept(self.ok, 0.0), "ok")
        self.assertEqual(self.filter.accept(Match("ok", "rc5", 1), 0.15), "ok")

    def test_a_press_after_release_is_a_new_press(self):
        self.assertEqual(self.filter.accept(self.ok, 0.0), "ok")
        self.assertEqual(self.filter.accept(self.ok, 1.0), "ok")

    def test_nec_repeat_extends_the_button_it_follows(self):
        # NEC resends a repeat frame about every 108 ms while a button is held,
        # so each one has to arrive within the release window to keep the press.
        nec_down = Match("down", "nec")
        repeat = Match(None, "nec", repeat=True)
        self.assertEqual(self.filter.accept(nec_down, 0.0), "down")
        self.assertIsNone(self.filter.accept(repeat, 0.1))
        self.assertIsNone(self.filter.accept(repeat, 0.2))
        self.assertIsNone(self.filter.accept(repeat, 0.3))
        self.assertEqual(self.filter.accept(repeat, 0.4), "down")

    def test_an_unattached_repeat_presses_nothing(self):
        self.assertIsNone(self.filter.accept(Match(None, "nec", repeat=True), 0.0))

    def test_a_repeat_long_after_release_presses_nothing(self):
        self.assertEqual(self.filter.accept(Match("down", "nec"), 0.0), "down")
        self.assertIsNone(self.filter.accept(Match(None, "nec", repeat=True), 5.0))

    def test_silence_ends_the_press(self):
        self.assertEqual(self.filter.accept(self.ok, 0.0), "ok")
        self.assertIsNone(self.filter.accept(None, 0.05))
        self.assertEqual(self.filter.accept(self.ok, 0.1), "ok")


class FrameReaderTests(unittest.TestCase):
    def test_several_frames_in_one_read_are_all_preserved(self):
        reader = FrameReader()
        stream = words(lirc.PULSE | 560, 560, lirc.PULSE | 560, lirc.TIMEOUT | 10_000,
                       lirc.PULSE | 900, 900, lirc.PULSE | 900, lirc.TIMEOUT | 10_000)
        frames = reader.feed(stream, 0.0)
        self.assertEqual(frames, [(560, 560, 560), (900, 900, 900)])

    def test_a_receiver_overflow_invalidates_the_press(self):
        reader = FrameReader()
        frames = reader.feed(words(lirc.PULSE | 560, lirc.OVERFLOW | lirc.VALUE_MASK), 0.0)
        self.assertEqual(frames, [None])

    def test_a_single_stray_pulse_is_not_a_button(self):
        reader = FrameReader()
        self.assertEqual(reader.feed(words(lirc.PULSE | 100, lirc.TIMEOUT | 10_000), 0.0),
                         [None])


class FakeStore:
    def __init__(self, doc):
        self.doc = doc

    def snapshot(self):
        return self.doc


class ReceiverChoiceTests(unittest.TestCase):
    def test_a_press_recorded_from_a_pin_drives_the_desktop(self):
        matcher = SignalMatcher(document({"down": rc5_frame(0, 23, 0)}, source="gpio"))
        self.assertEqual(matcher.match(rc5_frame(0, 23, 0)).button_id, "down")

    def test_choosing_another_receiver_reopens_on_it_at_once(self):
        opened = []

        class Device:
            def __init__(self, device, _gap):
                opened.append(device)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def read(self, timeout):
                time.sleep(min(timeout, 0.01))
                return None

        controller = IRController(FakeStore(document({})), lambda _button: None,
                                  lambda: True, device="/dev/lirc0", device_factory=Device)
        self.addCleanup(controller.close)
        controller.resume()
        self.assertTrue(wait_for(lambda: opened == ["/dev/lirc0"]))
        controller.use({"kind": "gpio", "pin": 18})
        self.assertTrue(wait_for(lambda: opened[-1] == {"kind": "gpio", "pin": 18}))
        self.assertEqual(controller.health()["receiver"], "GPIO18 (pin 12)")


def wait_for(condition, seconds=2.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.pressed = []
        self.allowed = [True]
        self.controller = IRController(
            FakeStore(document({"down": rc5_frame(0, 23, 0), "ok": rc5_frame(0, 53, 0)})),
            self.pressed.append, lambda: self.allowed[0], device_factory=None)
        self.frame = rc5_frame(0, 23, 0)

    def test_nothing_is_pressed_before_the_gate_opens(self):
        self.assertIsNone(self.controller.process_frame(self.frame, now=0.0))
        self.assertEqual(self.pressed, [])

    def test_a_learned_button_is_pressed_once_the_gate_opens(self):
        self.controller._paused.clear()
        self.assertEqual(self.controller.process_frame(self.frame, now=0.0), "down")
        self.assertEqual(self.pressed, ["down"])

    def test_closing_the_gate_stops_control_immediately(self):
        self.controller._paused.clear()
        self.controller.process_frame(self.frame, now=0.0)
        self.allowed[0] = False
        self.assertIsNone(self.controller.process_frame(self.frame, now=1.0))
        self.assertEqual(self.pressed, ["down"])

    def test_pausing_stops_control_without_waiting_on_a_closed_device(self):
        self.controller._paused.clear()
        self.controller.pause()
        self.assertIsNone(self.controller.process_frame(self.frame, now=0.0))
        self.assertTrue(self.controller.health()["paused"])

    def test_resuming_reloads_the_recordings(self):
        self.addCleanup(self.controller.close)
        self.controller.store.doc = document({"up": rc5_frame(0, 22, 0)})
        # Stop the reader first: this covers reloading recordings and lifting the
        # pause, and there is no receiver device to open in a test.
        self.controller._stop.set()
        self.controller.resume()
        health = self.controller.health()
        self.assertEqual(health["learned_buttons"], 1)
        self.assertFalse(health["paused"])

    def test_reloading_deleted_bindings_does_not_resume_a_paused_reader(self):
        self.controller.store.doc = document({})
        self.controller.reload_recordings()
        self.assertEqual(self.controller.health()["learned_buttons"], 0)
        self.assertTrue(self.controller.health()["paused"])
        self.assertIsNone(self.controller.matcher.match(self.frame))

    def test_concurrent_reloads_cannot_restore_a_binding_deleted_by_the_newer_snapshot(self):
        first_read, release_first, second_read = threading.Event(), threading.Event(), threading.Event()
        old_doc = self.controller.store.doc

        class ChangingStore:
            reads = 0

            def snapshot(self):
                self.reads += 1
                if self.reads == 1:
                    first_read.set()
                    release_first.wait(2)
                    return old_doc
                second_read.set()
                return document({})

        self.controller.store = ChangingStore()
        first = threading.Thread(target=self.controller.reload_recordings)
        second = threading.Thread(target=self.controller.reload_recordings)
        self.addCleanup(release_first.set)
        first.start()
        self.assertTrue(first_read.wait(1))
        second.start()
        self.assertFalse(second_read.wait(.05), "the newer snapshot must not overtake the older rebuild")
        release_first.set()
        first.join(1)
        second.join(1)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(self.controller.health()["learned_buttons"], 0)

    def test_simultaneous_starts_create_only_one_reader(self):
        real_thread = threading.Thread
        constructing, finish = threading.Event(), threading.Event()
        made = []

        class ReaderThread:
            def __init__(self, **_kwargs):
                made.append(self)
                constructing.set()
                finish.wait(2)

            def start(self):
                pass

        first = real_thread(target=self.controller.start)
        second = real_thread(target=self.controller.start)
        self.addCleanup(finish.set)
        with patch("pipertv.ir_control.threading.Thread", ReaderThread):
            first.start()
            self.assertTrue(constructing.wait(1))
            second.start()
            finish.set()
            first.join(1)
            second.join(1)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(len(made), 1)

    def test_a_slow_callback_does_not_hold_the_receiver_state_lock(self):
        entered, finish = threading.Event(), threading.Event()

        def callback(_button):
            entered.set()
            finish.wait(2)

        self.controller.on_button = callback
        self.controller._paused.clear()
        worker = threading.Thread(target=self.controller.process_frame, args=(self.frame, 0.0))
        worker.start()
        self.addCleanup(finish.set)
        self.assertTrue(entered.wait(1))
        self.assertTrue(self.controller._lock.acquire(timeout=.1))
        self.controller._lock.release()
        self.controller.pause()
        self.assertTrue(self.controller.health()["paused"])
        finish.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())

    def test_health_describes_the_receiver(self):
        health = self.controller.health()
        self.assertEqual(health["device"], "/dev/lirc0")
        self.assertEqual(health["learned_buttons"], 2)
        self.assertTrue(health["ok"])
        self.assertIsNone(health["last_button"])


class HoldPaceTests(unittest.TestCase):
    """How fast a held key repeats depends on what the press moves."""

    def press(self, filter_, button, at):
        return filter_.accept(Match(button, "rc5", toggle=0), at)

    def test_a_held_direction_repeats_at_the_default_pace(self):
        repeat = RepeatFilter()
        self.assertEqual(self.press(repeat, "up", 0.0), "up")
        self.assertIsNone(self.press(repeat, "up", 0.2))
        self.assertEqual(self.press(repeat, "up", 0.4), "up")

    def test_a_slower_pace_holds_a_press_to_one_step(self):
        # Snapping asks for this: at the pixel pace a press a shade too long
        # steps twice, which reads as the cursor jumping past its target.
        repeat = RepeatFilter(pace=lambda button: (0.65, 0.3))
        self.assertEqual(self.press(repeat, "up", 0.0), "up")
        for at in (0.2, 0.4, 0.6):
            with self.subTest(at=at):
                self.assertIsNone(self.press(repeat, "up", at))
        self.assertEqual(self.press(repeat, "up", 0.7), "up")

    def test_a_pace_of_none_leaves_the_default_alone(self):
        repeat = RepeatFilter(pace=lambda button: None)
        self.assertEqual(self.press(repeat, "up", 0.0), "up")
        self.assertEqual(self.press(repeat, "up", 0.4), "up")


if __name__ == "__main__":
    unittest.main()
