"""The arrangement where the dial is in the television's own browser.

That browser cannot show a streaming service -- it is an engine from 2012, and
these sites need both a modern one and Widevine -- so Piper keeps its own screen
for them: it opens a service here and asks the set to look at this input. Three
things follow, and they are what this file holds to.

The visit cannot depend on what the set is showing, because while the dial is up
the set is showing its own browser, and that is the arrangement working rather
than a reason to take the remote away. The television is asked, never assumed to
have obeyed. And the way out includes the set, or a closed service leaves a
blank input with nothing to say why.
"""

import logging
import unittest

from pipertv.cec import CecInput
from pipertv.served import NoInterface, NotEvidence
from pipertv.session import ControlSession
from tests.test_control import build, FakeLauncher

logging.getLogger("pipertv.cec").addHandler(logging.NullHandler())
logging.getLogger("pipertv.control").addHandler(logging.NullHandler())


class Finished:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRun:
    """Stands in for cec-ctl, remembering what it was asked to transmit."""

    def __init__(self, returncode=0, stderr="", raises=None):
        self.commands = []
        self.returncode = returncode
        self.stderr = stderr
        self.raises = raises

    def __call__(self, command, **_kwargs):
        self.commands.append(list(command))
        if self.raises is not None:
            raise self.raises
        return Finished(self.returncode, "", self.stderr)

    def sent(self):
        """Just the message each call carried."""
        return [part for command in self.commands for part in command if part.startswith("--")
                and part not in ("--playback", "--to")]


class AskingTheTelevisionTests(unittest.TestCase):
    def test_taking_the_input_wakes_the_set_and_names_this_one(self):
        run = FakeRun()
        result = CecInput(run=run, address="1.0.0.0").take()
        self.assertTrue(result["sent"])
        self.assertIn("--image-view-on", run.sent())
        self.assertIn("--active-source", run.sent())
        self.assertIn("phys-addr=1.0.0.0", run.commands[-1])

    def test_leaving_says_this_input_is_done(self):
        run = FakeRun()
        result = CecInput(run=run, address="1.0.0.0").release()
        self.assertTrue(result["sent"])
        self.assertIn("--inactive-source", run.sent())

    def test_a_set_that_refuses_does_not_fail_what_asked(self):
        # Many televisions ignore this, and the service is open either way.
        switch = CecInput(run=FakeRun(returncode=1, stderr="Unable to transmit"), address="1.0.0.0")
        result = switch.take()
        self.assertFalse(result["sent"])
        self.assertIn("Unable to transmit", result["error"])

    def test_a_missing_cec_tool_is_reported_rather_than_raised(self):
        switch = CecInput(run=FakeRun(raises=FileNotFoundError("cec-ctl")), address="1.0.0.0")
        self.assertFalse(switch.take()["sent"])

    def test_with_no_address_there_is_nothing_to_announce(self):
        run = FakeRun()
        switch = CecInput(run=run, address=None)
        switch.address = lambda: None
        self.assertFalse(switch.take()["sent"])
        self.assertEqual(run.commands, [])

    def test_it_reports_what_was_sent_never_what_the_set_did(self):
        # Whether the picture actually changed cannot be seen from this end.
        switch = CecInput(run=FakeRun(), address="1.0.0.0")
        switch.take()
        last = switch.snapshot()["last"]
        self.assertEqual(last["asked"], "take")
        self.assertIn("sent", last)
        self.assertNotIn("done", last)
        self.assertNotIn("showing", last)


class TheVisitThatLastsTests(unittest.TestCase):
    def test_a_served_visit_is_open_from_the_start(self):
        session = ControlSession()
        state = session.start_served("piper")
        self.assertEqual(state["origin"], "served")
        self.assertEqual(state["mode"], "piper")
        self.assertTrue(session.enabled())

    def test_what_the_set_reports_cannot_end_it(self):
        # The set is showing its own browser, which is where the dial is.
        session = ControlSession()
        session.start_served("piper")
        for state in ("inactive", "unknown", "active"):
            session.update({"state": state, "reason": "a report"})
            self.assertTrue(session.enabled(), state)

    def test_recording_still_stands_it_down(self):
        session = ControlSession()
        session.start_served("piper")
        session.hold("Recording a remote button.")
        self.assertFalse(session.enabled())
        session.release()
        self.assertTrue(session.enabled())

    def test_it_is_recorded_as_what_it_is(self):
        # Not dressed up as a confirmation nobody gave.
        session = ControlSession()
        self.assertEqual(session.start_served()["origin"], "served")


class NothingOnThisScreenTests(unittest.TestCase):
    def test_the_interface_says_it_is_elsewhere(self):
        interface = NoInterface()
        self.assertFalse(interface.showing())
        self.assertTrue(interface.snapshot()["elsewhere"])
        self.assertFalse(interface.open()["showing"])
        self.assertFalse(interface.close()["showing"])

    def test_detection_is_not_consulted_and_says_so(self):
        monitor = NotEvidence()
        monitor.start()
        snapshot = monitor.snapshot()
        self.assertEqual(snapshot["state"], "unknown")
        self.assertIn("television's own browser", snapshot["reason"])
        monitor.close()


class OpeningFromTheTelevisionTests(unittest.TestCase):
    def build(self):
        run = FakeRun()
        switch = CecInput(run=run, address="1.0.0.0")
        control, _monitor, _controller, _targets = self.served(switch)
        return control, switch, run

    def served(self, switch):
        control, monitor, controller, targets = build(
            "unknown", switch=switch, interface=NoInterface(), launcher=FakeLauncher())
        control.session.start_served("piper")
        return control, monitor, controller, targets

    def visit(self, control):
        return control.session.snapshot()["session"]["id"]

    def test_opening_a_service_asks_the_set_to_switch_to_this_input(self):
        control, _switch, run = self.build()
        state = control.launch("youtube", self.visit(control))
        self.assertEqual(state["running"]["id"], "youtube")
        self.assertIn("--active-source", run.sent())
        self.assertTrue(state["television"]["sent"])

    def test_closing_it_tells_the_set_it_can_go_back(self):
        control, _switch, run = self.build()
        control.launch("youtube", self.visit(control))
        run.commands = []
        state = control.stop_service()
        self.assertIn("--inactive-source", run.sent())
        self.assertTrue(state["television"]["sent"])

    def test_the_remote_s_own_way_out_tells_it_too(self):
        # Exit on the learned remote is how anyone watching actually leaves.
        control, _switch, run = self.build()
        control.launch("youtube", self.visit(control))
        run.commands = []
        control._way_out("exit")
        self.assertIn("--inactive-source", run.sent())

    def test_a_service_that_fails_to_open_does_not_move_the_set(self):
        control, _switch, run = self.build()
        with self.assertRaises(KeyError):
            control.launch("nothing-like-it", self.visit(control))
        self.assertEqual(run.commands, [])

    def test_the_page_is_told_what_the_set_was_asked(self):
        control, _switch, _run = self.build()
        control.launch("youtube", self.visit(control))
        feed = control.events()
        self.assertEqual(feed["television"]["last"]["asked"], "take")

    def test_without_the_arrangement_nothing_is_asked_of_the_set(self):
        # An ordinary Pi shows the interface itself; its television is already
        # on this input, and Piper has no business changing what it shows.
        control, _monitor, _controller, _targets = build("unknown")
        self.assertIsNone(control.events()["television"])
        self.assertIsNone(control.health()["television"])


if __name__ == "__main__":
    unittest.main()
