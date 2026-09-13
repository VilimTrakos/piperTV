import contextlib
import logging
import struct
import unittest
from unittest.mock import patch

from pipertv import keyboard, pointer
from pipertv.keyboard import (KEY_ENTER, KEY_ESC, KEY_LEFT, KEY_UP, KEYS,
                              ServiceKeys, VirtualKeyboard)

logging.getLogger("pipertv.keyboard").addHandler(logging.NullHandler())


class FakeUinput:
    """Record everything the module would hand to the kernel."""

    def __init__(self, accept_all=True):
        self.ioctls = []
        self.writes = []
        self.closed = []
        self.accept_all = accept_all
        self.fd = 88

    def ioctl(self, fd, request, value=0):
        self.ioctls.append((request, value))
        return 0

    def write(self, fd, payload):
        self.writes.append(payload)
        return len(payload) if self.accept_all else len(payload) - 1

    def requests(self):
        return [request for request, _value in self.ioctls]

    def events(self, since=0):
        decoded = []
        for payload in self.writes[since:]:
            assert len(payload) % pointer.EVENT_SIZE == 0, "events must be whole structs"
            for offset in range(0, len(payload), pointer.EVENT_SIZE):
                _sec, _usec, kind, code, value = struct.unpack_from(
                    pointer.EVENT, payload, offset)
                decoded.append((kind, code, value))
        return decoded

    def keys(self, since=0):
        return [(code, value) for kind, code, value in self.events(since)
                if kind == pointer.EV_KEY]


@contextlib.contextmanager
def fake_keyboard(accept_all=True, **kwargs):
    fake = FakeUinput(accept_all)
    with (patch.object(keyboard.os, "open", return_value=fake.fd),
          patch.object(keyboard.os, "write", side_effect=fake.write),
          patch.object(keyboard.os, "close", side_effect=fake.closed.append),
          patch("fcntl.ioctl", side_effect=fake.ioctl)):
        device = VirtualKeyboard(settle_s=0, **kwargs)
        device.open()
        yield device, fake


class DeviceTests(unittest.TestCase):
    def test_the_device_declares_only_the_keys_a_remote_has(self):
        with fake_keyboard() as (_device, fake):
            declared = [value for request, value in fake.ioctls
                        if request == keyboard.UI_SET_KEYBIT]
        self.assertEqual(sorted(declared), sorted(set(KEYS.values())))
        # Letters and digits are absent by construction, not by convention.
        self.assertNotIn(30, declared)  # KEY_A

    def test_a_key_is_pressed_and_released(self):
        with fake_keyboard() as (device, fake):
            before = len(fake.writes)
            device.tap("ok")
            self.assertEqual(fake.keys(before), [(KEY_ENTER, 1), (KEY_ENTER, 0)])

    def test_every_named_key_reaches_the_kernel_as_itself(self):
        expected = {"up": KEY_UP, "left": KEY_LEFT, "back": KEY_ESC}
        with fake_keyboard() as (device, fake):
            for name, code in expected.items():
                with self.subTest(key=name):
                    before = len(fake.writes)
                    device.tap(name)
                    self.assertEqual(fake.keys(before), [(code, 1), (code, 0)])

    def test_a_key_a_remote_does_not_have_is_refused(self):
        with fake_keyboard() as (device, _fake):
            for key in ("a", "f5", "", None):
                with self.subTest(key=key), self.assertRaises(ValueError):
                    device.tap(key)

    def test_every_event_batch_ends_with_a_report(self):
        # Without the report the kernel holds the press and nothing arrives.
        with fake_keyboard() as (device, fake):
            device.tap("down")
            for index, payload in enumerate(fake.writes):
                with self.subTest(batch=index):
                    kind, _code, _value = struct.unpack_from(
                        pointer.EVENT, payload, len(payload) - pointer.EVENT_SIZE)[2:]
                    self.assertEqual(kind, pointer.EV_SYN)

    def test_closing_releases_every_key_and_destroys_the_device(self):
        with fake_keyboard() as (device, fake):
            device.tap("up")
            before = len(fake.writes)
            device.close()
            released = {code for code, value in fake.keys(before) if value == 0}
            self.assertEqual(released, set(KEYS.values()),
                             "a held key would repeat into the desktop for ever")
            self.assertIn(keyboard.UI_DEV_DESTROY, fake.requests())
            self.assertEqual(fake.closed, [88])

    def test_a_closed_keyboard_cannot_be_reopened(self):
        with fake_keyboard() as (device, _fake):
            device.close()
            with self.assertRaises(RuntimeError):
                device.open()

    def test_a_partial_write_is_an_error_not_a_silent_half_press(self):
        with fake_keyboard(accept_all=False) as (device, _fake):
            with self.assertRaises(OSError):
                device.tap("ok")

    def test_a_nonsense_device_path_is_refused(self):
        for device in ("/etc/passwd", "uinput", "", None):
            with self.subTest(device=device), self.assertRaises(ValueError):
                VirtualKeyboard(device=device)


class FakeKeyboard:
    def __init__(self):
        self.taps = []
        self.opened = self.closed = 0
        self.fail_on_open = False

    def open(self):
        if self.fail_on_open:
            raise OSError("/dev/uinput is not writable")
        self.opened += 1
        return self

    def tap(self, key):
        self.taps.append(key)
        return key

    def close(self):
        self.closed += 1


class ServiceKeysTests(unittest.TestCase):
    def setUp(self):
        self.keyboard = FakeKeyboard()
        self.keys = ServiceKeys(factory=lambda: self.keyboard)

    def test_the_keyboard_appears_with_the_first_key_and_is_reused(self):
        self.assertFalse(self.keys.health()["active"])
        self.keys.send("up")
        self.keys.send("ok")
        self.assertEqual(self.keyboard.opened, 1)
        self.assertEqual(self.keyboard.taps, ["up", "ok"])
        self.assertTrue(self.keys.health()["active"])

    def test_releasing_destroys_the_device(self):
        self.keys.send("up")
        self.keys.release()
        self.assertEqual(self.keyboard.closed, 1)
        self.assertFalse(self.keys.health()["active"])

    def test_what_a_remote_cannot_type_is_ignored_quietly(self):
        # Volume, power and the digits are not navigation and are not forwarded.
        for key in ("volume_up", "power", "digit_3", "menu"):
            with self.subTest(key=key):
                self.assertIsNone(self.keys.send(key))
        self.assertEqual(self.keyboard.taps, [])
        self.assertEqual(self.keyboard.opened, 0)

    def test_a_failure_is_reported_and_the_next_press_tries_again(self):
        self.keyboard.fail_on_open = True
        self.assertIsNone(self.keys.send("up"))
        health = self.keys.health()
        self.assertFalse(health["ok"])
        self.assertIn("uinput", health["error"])

        self.keyboard.fail_on_open = False
        self.assertEqual(self.keys.send("up"), "up")
        self.assertTrue(self.keys.health()["ok"])


if __name__ == "__main__":
    unittest.main()
