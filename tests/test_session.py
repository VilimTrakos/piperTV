"""The numbered tests mirror the hardware acceptance checks in
docs/hdmi-detection.md, so a change in behaviour shows up against that list."""

import unittest

from pipertv.session import ControlSession


def cec(state, reason="test"):
    return {"state": state, "reason": reason, "source": "cec"}


class ControlSessionTests(unittest.TestCase):
    def setUp(self):
        self.session = ControlSession()

    def select(self, mode="pointer"):
        """Reach a controllable state the way the detector and browser would."""
        state = self.session.update(cec("active"))
        return self.session.choose(mode, state["session"]["id"])

    # 1. Start while the TV shows another source: control stays off.
    def test_control_is_off_before_anything_is_detected(self):
        state = self.session.snapshot()
        self.assertEqual(state["control"], "off")
        self.assertIsNone(state["session"])
        self.assertFalse(self.session.enabled())

    def test_another_source_never_enables_control(self):
        state = self.session.update(cec("inactive", "Another source is selected."))
        self.assertEqual(state["control"], "off")
        self.assertIsNone(state["session"])
        self.assertFalse(self.session.enabled())

    # 2. Select the Pi input: a fresh mode choice is required.
    def test_selecting_the_pi_asks_for_a_mode_before_control(self):
        state = self.session.update(cec("active"))
        self.assertTrue(state["needs_mode"])
        self.assertEqual(state["origin"], "cec")
        self.assertIsNone(state["mode"])
        # Detection alone must not move the cursor.
        self.assertEqual(state["control"], "off")
        self.assertFalse(self.session.enabled())

    def test_staying_selected_does_not_keep_asking(self):
        first = self.session.update(cec("active"))
        self.session.choose("snapping", first["session"]["id"])
        again = self.session.update(cec("active"))
        self.assertEqual(again["session"]["id"], first["session"]["id"])
        self.assertEqual(again["mode"], "snapping")
        self.assertTrue(self.session.enabled())

    # 3. Choose a mode: only the current session becomes eligible.
    def test_choosing_a_mode_turns_control_on(self):
        for mode in ("pointer", "snapping", "piper"):
            with self.subTest(mode=mode):
                session = ControlSession()
                state = session.update(cec("active"))
                chosen = session.choose(mode, state["session"]["id"])
                self.assertEqual(chosen["mode"], mode)
                self.assertEqual(chosen["control"], "on")
                self.assertFalse(chosen["needs_mode"])
                self.assertTrue(session.enabled())

    def test_the_piper_interface_is_gated_like_any_other_mode(self):
        # Driving Piper's own interface is still driving the Pi, so it needs
        # the same evidence and the same per-visit choice.
        state = self.session.update(cec("active"))
        self.assertTrue(state["needs_mode"])
        chosen = self.session.choose("piper", state["session"]["id"])
        self.assertEqual(chosen["mode"], "piper")
        self.assertTrue(self.session.enabled())
        self.session.update(cec("inactive"))
        self.assertFalse(self.session.enabled())

    def test_an_unknown_mode_is_refused(self):
        state = self.session.update(cec("active"))
        for mode in ("magic", "", None, "POINTER"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.session.choose(mode, state["session"]["id"])
        self.assertFalse(self.session.enabled())

    def test_a_mode_cannot_be_chosen_when_the_pi_is_not_showing(self):
        with self.assertRaises(RuntimeError):
            self.session.choose("pointer", "any-session")

    # 4. Switch away: control turns off and the choice is cleared.
    def test_switching_away_stops_control_and_clears_the_mode(self):
        self.select()
        state = self.session.update(cec("inactive", "Another source is selected."))
        self.assertEqual(state["control"], "off")
        self.assertIsNone(state["mode"])
        self.assertIsNone(state["session"])
        self.assertFalse(self.session.enabled())

    # 5. Switch back: the mode choice is asked again, never reused.
    def test_returning_to_the_pi_requires_a_new_choice(self):
        first = self.select()["session"]["id"]
        self.session.update(cec("inactive"))
        state = self.session.update(cec("active"))
        self.assertNotEqual(state["session"]["id"], first)
        self.assertTrue(state["needs_mode"])
        self.assertIsNone(state["mode"])
        self.assertFalse(self.session.enabled())

    def test_a_choice_from_an_earlier_visit_is_refused(self):
        stale = self.select()["session"]["id"]
        self.session.update(cec("inactive"))
        self.session.update(cec("active"))
        # A browser left open on the previous visit must not re-enable control.
        with self.assertRaises(RuntimeError):
            self.session.choose("pointer", stale)
        self.assertFalse(self.session.enabled())

    # 6. Losing detection turns automatic control off.
    def test_inconclusive_detection_stops_automatic_control(self):
        self.select()
        state = self.session.update(cec("unknown", "CEC monitoring stopped."))
        self.assertEqual(state["control"], "off")
        self.assertIsNone(state["session"])
        self.assertFalse(self.session.enabled())

    # 7. Unknown detection must never become a manual session by itself.
    def test_unknown_detection_never_opens_a_manual_session(self):
        state = self.session.update(cec("unknown", "The TV reports nothing usable."))
        self.assertIsNone(state["session"])
        self.assertEqual(state["control"], "off")

    def test_a_manual_session_needs_an_explicit_confirmation(self):
        for confirmed in (False, None, "yes", 1, 0):
            with self.subTest(confirmed=confirmed), self.assertRaises(ValueError):
                self.session.start_manual(confirmed)
        self.assertFalse(self.session.enabled())

    # 8. The manual path is separate from CEC detection.
    def test_a_manual_session_is_marked_manual_and_still_needs_a_mode(self):
        state = self.session.start_manual(True)
        self.assertEqual(state["origin"], "manual")
        self.assertTrue(state["needs_mode"])
        self.assertEqual(state["control"], "off")
        chosen = self.session.choose("pointer", state["session"]["id"])
        self.assertEqual(chosen["control"], "on")
        self.assertEqual(chosen["origin"], "manual")

    def test_a_manual_session_survives_detection_it_was_created_for(self):
        state = self.session.start_manual(True)
        self.session.choose("pointer", state["session"]["id"])
        # A TV that cannot report its input is exactly why manual exists.
        kept = self.session.update(cec("unknown", "The TV reports nothing usable."))
        self.assertEqual(kept["origin"], "manual")
        self.assertTrue(self.session.enabled())

    def test_evidence_of_another_input_outranks_a_manual_assertion(self):
        state = self.session.start_manual(True)
        self.session.choose("pointer", state["session"]["id"])
        ended = self.session.update(cec("inactive", "Another source is selected."))
        self.assertIsNone(ended["session"])
        self.assertFalse(self.session.enabled())

    def test_a_manual_session_can_always_be_stopped(self):
        state = self.session.start_manual(True)
        self.session.choose("snapping", state["session"]["id"])
        self.assertTrue(self.session.enabled())
        stopped = self.session.stop()
        self.assertIsNone(stopped["session"])
        self.assertEqual(stopped["control"], "off")
        self.assertFalse(self.session.enabled())

    def test_an_automatic_session_can_also_be_stopped(self):
        self.select()
        self.session.stop()
        self.assertFalse(self.session.enabled())

    # Recording interlock: the remote is being learned, not used.
    def test_recording_suspends_control_without_losing_the_visit(self):
        state = self.select("snapping")
        session_id = state["session"]["id"]
        held = self.session.hold("Recording a remote button.")
        self.assertEqual(held["control"], "off")
        self.assertEqual(held["hold"], "Recording a remote button.")
        self.assertFalse(self.session.enabled())
        # The visit and its mode survive, so recording does not re-ask.
        self.assertEqual(held["session"]["id"], session_id)
        self.assertEqual(held["mode"], "snapping")

        released = self.session.release()
        self.assertEqual(released["control"], "on")
        self.assertIsNone(released["hold"])
        self.assertTrue(self.session.enabled())

    def test_a_hold_must_say_why(self):
        for reason in ("", None, 5):
            with self.subTest(reason=reason), self.assertRaises(ValueError):
                self.session.hold(reason)

    def test_releasing_a_hold_cannot_revive_an_ended_session(self):
        self.select()
        self.session.hold("Recording a remote button.")
        self.session.update(cec("inactive"))
        state = self.session.release()
        self.assertIsNone(state["session"])
        self.assertEqual(state["control"], "off")
        self.assertFalse(self.session.enabled())

    # Detector input is data, not a command.
    def test_an_unrecognised_detector_state_is_treated_as_unknown(self):
        self.select()
        state = self.session.update({"state": "definitely-on", "reason": "nonsense"})
        self.assertEqual(state["source"], "unknown")
        self.assertIsNone(state["session"])
        self.assertFalse(self.session.enabled())

    def test_a_malformed_snapshot_is_refused(self):
        for snapshot in ([], "active", None, 7):
            with self.subTest(snapshot=snapshot), self.assertRaises(ValueError):
                self.session.update(snapshot)

    def test_the_detector_reason_is_reported_for_the_interface(self):
        state = self.session.update(cec("unknown", "CEC access was denied."))
        self.assertEqual(state["detail"], "CEC access was denied.")
        self.assertEqual(state["source"], "unknown")


if __name__ == "__main__":
    unittest.main()
