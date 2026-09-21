"""A rebound key drives Piper; the key it left behind stops driving anything."""

import logging
import unittest

from pipertv.control import RemoteControl

from tests.test_control import (FakeBackdrop, FakeController, FakeDesktop, FakeInterface,
                                FakeKeys, FakeLauncher, FakeMonitor, FakeTargets, select)

SCREEN = (1920, 1080)

logging.getLogger("pipertv.control").addHandler(logging.NullHandler())


class RoleStore:
    def __init__(self, roles=None):
        self.roles = roles

    def snapshot(self):
        document = {"recordings": {}}
        if self.roles is not None:
            document["roles"] = self.roles
        return document


def build(roles=None, state="active"):
    control = RemoteControl(RoleStore(roles), screen=SCREEN,
                            monitor=FakeMonitor(state), controller=FakeController(),
                            targets=FakeTargets(), desktop=FakeDesktop(),
                            launcher=FakeLauncher(), interface=FakeInterface(),
                            keys=FakeKeys(), backdrop=FakeBackdrop())
    return control


class TranslationTests(unittest.TestCase):
    def test_the_bound_key_performs_the_role(self):
        control = build({"up": "play"})
        select(control, "pointer")
        control._press("play")
        self.assertEqual(control.desktop.presses, ["up"])

    def test_the_key_the_role_left_behind_does_nothing(self):
        # The reason the feature exists: this key is the one the TV also obeys.
        control = build({"up": "play"})
        select(control, "pointer")
        control._press("up")
        self.assertEqual(control.desktop.presses, [])

    def test_a_vacated_key_is_still_recorded_so_the_page_can_show_it(self):
        control = build({"up": "play"})
        select(control, "piper")
        control._press("up")
        event = control.events(0)["events"][0]
        self.assertEqual(event["button"], "up")
        self.assertIsNone(event["action"])
        self.assertFalse(event["navigation"])

    def test_a_press_records_the_key_sent_and_the_action_performed(self):
        control = build({"up": "play"})
        select(control, "piper")
        control._press("play")
        event = control.events(0)["events"][0]
        self.assertEqual((event["button"], event["action"]), ("play", "up"))
        self.assertTrue(event["navigation"])

    def test_untouched_roles_still_act_as_themselves(self):
        control = build({"up": "play"})
        select(control, "pointer")
        control._press("down")
        self.assertEqual(control.desktop.presses, ["down"])

    def test_a_key_carrying_no_role_moves_nothing(self):
        control = build({"up": "play"})
        select(control, "pointer")
        control._press("volume_up")
        self.assertEqual(control.desktop.presses, [])
        self.assertIsNone(control.events(0)["events"][0]["action"])

    def test_without_bindings_every_key_behaves_as_before(self):
        control = build()
        select(control, "pointer")
        control._press("right")
        self.assertEqual(control.desktop.presses, ["right"])
        self.assertEqual(control.events(0)["events"][0]["action"], "right")


class HoldToRepeatTests(unittest.TestCase):
    def test_repeat_follows_the_role_onto_its_new_key(self):
        control = build({"up": "play"})
        self.assertTrue(control._is_direction("play"))

    def test_the_vacated_key_no_longer_repeats(self):
        control = build({"up": "play"})
        self.assertFalse(control._is_direction("up"))

    def test_keys_that_were_never_directions_still_do_not_repeat(self):
        control = build({"up": "play"})
        self.assertFalse(control._is_direction("volume_up"))

    def test_without_bindings_the_directions_repeat(self):
        control = build()
        for button in ("up", "down", "left", "right"):
            with self.subTest(button=button):
                self.assertTrue(control._is_direction(button))


class LoadingTests(unittest.TestCase):
    def test_a_corrupt_saved_binding_leaves_the_remote_usable(self):
        # Starting at all matters more than honouring an unusable map.
        control = build({"up": "down"})
        select(control, "pointer")
        control._press("up")
        self.assertEqual(control.desktop.presses, ["up"])

    def test_reloading_picks_up_an_edited_binding(self):
        control = build({})
        select(control, "pointer")
        control.store.roles = {"up": "play"}
        self.assertEqual(control.reload_roles(), {"up": "play"})
        control._press("play")
        self.assertEqual(control.desktop.presses, ["up"])

    def test_the_rebound_key_is_also_the_way_out_of_a_service(self):
        # Exit lives on the stop key here, so closing YouTube has to follow the
        # role rather than the key the remote actually sent.
        control = build({"exit": "stop"})
        select(control, "piper")
        control.launch("youtube", control.session.snapshot()["session"]["id"])
        control._press("stop")
        self.assertEqual(control.launcher.stopped, 1)
        self.assertIsNone(control.launcher.running())

    def test_the_key_exit_left_behind_does_not_close_a_service(self):
        control = build({"exit": "stop"})
        select(control, "piper")
        control.launch("youtube", control.session.snapshot()["session"]["id"])
        control._press("exit")
        self.assertEqual(control.launcher.stopped, 0)

    def test_a_rebound_key_types_into_the_service_as_its_role(self):
        control = build({"up": "play"})
        select(control, "piper")
        control.launch("youtube", control.session.snapshot()["session"]["id"])
        control._press("play")
        self.assertEqual(control.keys.sent, ["up"])

    def test_leaving_piper_follows_the_key_exit_was_moved_to(self):
        control = build({"exit": "stop"})
        select(control, "piper")
        control._press("stop")
        control._press("stop")
        self.assertEqual(control.interface.closed, 1)

    def test_the_key_exit_left_behind_cannot_close_piper(self):
        control = build({"exit": "stop"})
        select(control, "piper")
        control._press("exit")
        control._press("exit")
        self.assertEqual(control.interface.closed, 0)

    def test_the_snapshot_reports_every_role_and_its_key(self):
        control = build({"up": "play"})
        described = {entry["role"]: entry for entry in control.snapshot()["roles"]}
        self.assertEqual(described["up"]["button"], "play")
        self.assertTrue(described["up"]["rebound"])
        self.assertFalse(described["down"]["rebound"])


if __name__ == "__main__":
    unittest.main()
