import logging
import time
import unittest

from pipertv.control import RemoteControl

SCREEN = (1920, 1080)

logging.getLogger("pipertv.control").addHandler(logging.NullHandler())


class FakeMonitor:
    def __init__(self, state="unknown"):
        self.state = state
        self.started = 0
        self.closed = 0

    def start(self):
        self.started += 1

    def snapshot(self):
        return {"state": self.state, "reason": f"detector says {self.state}", "source": "cec"}

    def close(self):
        self.closed += 1


class FakeController:
    def __init__(self):
        self.resumed = self.paused = self.closed = 0

    def resume(self):
        self.resumed += 1

    def pause(self):
        self.paused += 1

    def close(self):
        self.closed += 1

    def health(self):
        return {"ok": True, "learned_buttons": 31}


class FakeTargets:
    name = "fake"

    def __init__(self):
        self.invalidated = 0

    def targets(self):
        return []

    def invalidate(self):
        self.invalidated += 1

    def health(self):
        return {"ok": True, "source": self.name}


class FakeDesktop:
    """Stands in for the layer that owns the virtual pointer."""

    def __init__(self):
        self.released = self.closed = 0
        self.active = False
        self.presses = []

    def press(self, button):
        self.presses.append(button)
        self.active = True  # pressing opens the virtual pointer

    def release(self):
        self.released += 1
        self.active = False

    def health(self):
        return {"ok": True, "active": self.active}

    def close(self):
        self.closed += 1


class FakeStore:
    def snapshot(self):
        return {"recordings": {}}


def build(state="unknown", **kwargs):
    monitor = FakeMonitor(state)
    controller = FakeController()
    targets = FakeTargets()
    control = RemoteControl(FakeStore(), screen=SCREEN, monitor=monitor,
                            controller=controller, targets=targets,
                            desktop=FakeDesktop(), **kwargs)
    return control, monitor, controller, targets


def select(control, mode="pointer"):
    """Take the gate to a controllable state the way the Pi would."""
    control._tick()
    return control.choose(mode, control.session.snapshot()["session"]["id"])


class SupervisorTests(unittest.TestCase):
    def test_detection_reaches_the_gate(self):
        control, monitor, _controller, _targets = build("active")
        control._tick()
        state = control.session.snapshot()
        self.assertEqual(state["source"], "active")
        self.assertTrue(state["needs_mode"])
        # Detection alone must not move the cursor.
        self.assertFalse(control.session.enabled())

    def test_switching_away_ends_control_without_waiting_for_a_button(self):
        control, monitor, _controller, _targets = build("active")
        select(control)
        control.desktop.press("right")  # a virtual pointer now exists
        self.assertTrue(control.desktop.health()["active"])

        monitor.state = "inactive"
        control._tick()
        self.assertFalse(control.session.enabled())
        self.assertEqual(control.desktop.released, 1,
                         "the virtual pointer must be taken away at once")

    def test_a_mode_chosen_between_passes_is_still_covered(self):
        # The choice and the TV switching away can land in the same gap between
        # supervisor passes; the pointer must still be taken away.
        control, monitor, _controller, _targets = build("active")
        select(control)
        control.desktop.press("right")
        monitor.state = "inactive"
        control._tick()
        self.assertEqual(control.desktop.released, 1)

    def test_the_pointer_is_only_taken_away_once(self):
        control, monitor, _controller, _targets = build("active")
        select(control)
        control.desktop.press("right")
        monitor.state = "unknown"
        control._tick()
        control._tick()
        control._tick()
        self.assertEqual(control.desktop.released, 1)

    def test_nothing_is_released_when_no_pointer_was_ever_opened(self):
        control, monitor, _controller, _targets = build("active")
        select(control)
        monitor.state = "inactive"
        control._tick()
        self.assertEqual(control.desktop.released, 0)

    def test_a_broken_detector_does_not_stop_the_supervisor(self):
        control, monitor, _controller, _targets = build("active")

        def explode():
            raise RuntimeError("the monitor died")

        monitor.snapshot = explode
        control._tick()  # must not raise
        monitor.snapshot = lambda: {"state": "active", "reason": "back"}
        control._tick()
        self.assertEqual(control.session.snapshot()["source"], "active")

    def test_a_detector_failure_withdraws_existing_automatic_control(self):
        control, monitor, _controller, _targets = build("active")
        select(control)
        control.desktop.press("right")

        def explode():
            raise OSError("adapter vanished")

        monitor.snapshot = explode
        control._tick()
        self.assertFalse(control.session.enabled())
        self.assertFalse(control.desktop.active)
        self.assertEqual(control.session.snapshot()["source"], "unknown")

    def test_a_button_checks_switches_between_supervisor_passes(self):
        control, monitor, _controller, _targets = build("active")
        select(control)
        monitor.state = "inactive"
        self.assertFalse(control._enabled())

    def test_starting_runs_the_detector_and_the_receiver(self):
        control, monitor, controller, _targets = build("active", poll_s=0.02)
        self.addCleanup(control.close)
        control.start()
        self.assertEqual(monitor.started, 1)
        self.assertEqual(controller.resumed, 1)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if control.session.snapshot()["source"] == "active":
                break
            time.sleep(0.01)
        self.assertEqual(control.session.snapshot()["source"], "active")
        control.start()  # starting twice must not spawn a second supervisor
        self.assertEqual(monitor.started, 1)


class BrowserActionTests(unittest.TestCase):
    def test_a_choice_checks_current_source_without_waiting_for_supervisor(self):
        control, monitor, _controller, _targets = build("active")
        control._tick()
        session_id = control.session.snapshot()["session"]["id"]
        monitor.state = "inactive"
        with self.assertRaises(RuntimeError):
            control.choose("pointer", session_id)
        self.assertFalse(control.session.enabled())

    def test_choosing_a_mode_enables_control_and_refreshes_targets(self):
        control, _monitor, _controller, targets = build("active")
        state = select(control, "snapping")
        self.assertEqual(state["mode"], "snapping")
        self.assertEqual(state["control"], "on")
        self.assertTrue(control.session.enabled())
        # A new visit must not snap to where icons used to be.
        self.assertEqual(targets.invalidated, 1)

    def test_a_stale_choice_is_refused(self):
        control, monitor, _controller, _targets = build("active")
        stale = control.session.snapshot()
        select(control)
        monitor.state = "inactive"
        control._tick()
        monitor.state = "active"
        control._tick()
        with self.assertRaises(RuntimeError):
            control.choose("pointer", "an-earlier-visit")
        self.assertFalse(control.session.enabled())

    def test_the_manual_path_needs_confirmation_and_can_be_stopped(self):
        control, _monitor, _controller, _targets = build("unknown")
        with self.assertRaises(ValueError):
            control.manual(False)
        state = control.manual(True)
        self.assertEqual(state["origin"], "manual")
        control.choose("pointer", state["session"]["id"])
        self.assertTrue(control.session.enabled())

        released = []
        control.desktop.release = lambda: released.append(True)
        stopped = control.stop()
        self.assertIsNone(stopped["session"])
        self.assertFalse(control.session.enabled())
        self.assertEqual(released, [True])


class RecordingInterlockTests(unittest.TestCase):
    def test_recording_stands_down_the_receiver_and_the_cursor(self):
        control, _monitor, controller, _targets = build("active")
        select(control, "snapping")
        released = []
        control.desktop.release = lambda: released.append(True)

        state = control.hold()
        self.assertEqual(state["control"], "off")
        self.assertFalse(control.session.enabled())
        # The reader must let go of the LIRC device so a capture can open it.
        self.assertEqual(controller.paused, 1)
        self.assertEqual(released, [True])
        # The visit survives, so recording does not force a new mode choice.
        self.assertEqual(state["mode"], "snapping")

    def test_control_resumes_after_recording(self):
        control, _monitor, controller, _targets = build("active")
        select(control)
        control.hold()
        state = control.release()
        self.assertEqual(state["control"], "on")
        self.assertTrue(control.session.enabled())
        self.assertEqual(controller.resumed, 1)

    def test_recording_before_a_visit_leaves_receiver_ready_for_a_later_choice(self):
        control, monitor, controller, _targets = build("unknown")
        control.hold()
        control.release()
        self.assertEqual(controller.resumed, 1)
        self.assertFalse(control._enabled(), "ready must not mean allowed to read IR")
        monitor.state = "active"
        select(control)
        self.assertTrue(control._enabled())

    def test_releasing_after_a_visit_keeps_the_receiver_gated(self):
        control, monitor, controller, _targets = build("active")
        select(control)
        control.hold()
        monitor.state = "inactive"
        control._tick()
        control.release()
        self.assertFalse(control.session.enabled())
        self.assertEqual(controller.resumed, 1, "reader waits on the gate for the next visit")


class ButtonRoutingTests(unittest.TestCase):
    def test_an_input_change_before_callback_prevents_dispatch(self):
        control, monitor, _controller, _targets = build("active")
        select(control, "pointer")
        monitor.state = "inactive"
        control._press("right")
        self.assertEqual(control.desktop.presses, [])
        self.assertEqual(control.events(0)["events"], [])

    def test_queued_buttons_cannot_cross_a_mode_change(self):
        control, _monitor, _controller, _targets = build("active")
        state = select(control, "pointer")
        control._press("ok")
        control.choose("piper", state["session"]["id"])
        self.assertEqual(control.events(0)["events"], [])
        control._press("down")
        event = control.events(0)["events"][0]
        self.assertEqual(event["mode"], "piper")
        self.assertEqual(event["session_id"], state["session"]["id"])

    def test_queued_buttons_cannot_cross_a_recording_hold(self):
        control, _monitor, _controller, _targets = build("active")
        select(control, "piper")
        control._press("ok")
        control.hold()
        control._press("right")
        control.release()
        self.assertEqual(control.events(0)["events"], [])

    def test_feed_checks_new_source_evidence_before_returning_old_presses(self):
        control, monitor, _controller, _targets = build("active")
        select(control, "piper")
        control._press("ok")
        monitor.state = "inactive"
        feed = control.events(0)
        self.assertEqual(feed["events"], [])
        self.assertEqual(feed["control"], "off")
        self.assertIsNone(feed["session_id"])

    def test_a_desktop_mode_moves_the_cursor_and_records_the_press(self):
        control, _monitor, _controller, _targets = build("active")
        select(control, "pointer")
        control._press("right")
        self.assertEqual(control.desktop.presses, ["right"])
        events = control.events(0)["events"]
        self.assertEqual([event["button"] for event in events], ["right"])
        self.assertEqual(events[0]["mode"], "pointer")

    def test_the_piper_interface_never_drags_the_cursor(self):
        control, _monitor, _controller, _targets = build("active")
        select(control, "piper")
        control._press("down")
        self.assertEqual(control.desktop.presses, [],
                         "the TV interface must not pull the mouse along behind it")
        self.assertEqual([event["button"] for event in control.events(0)["events"]], ["down"])

    def test_the_feed_reports_the_gate_verdict_alongside_the_presses(self):
        control, _monitor, _controller, _targets = build("active")
        select(control, "piper")
        result = control.events(0)
        self.assertEqual(result["mode"], "piper")
        self.assertEqual(result["control"], "on")
        self.assertFalse(result["missed"])

    def test_the_page_receives_only_presses_it_has_not_seen(self):
        control, _monitor, _controller, _targets = build("active")
        select(control, "piper")
        control._press("up")
        seen = control.events(0)["sequence"]
        control._press("ok")
        self.assertEqual([event["button"] for event in control.events(seen)["events"]], ["ok"])


class ReportingTests(unittest.TestCase):
    def test_the_snapshot_carries_the_detector_reason(self):
        control, _monitor, _controller, _targets = build("inactive")
        control._tick()
        state = control.snapshot()
        self.assertEqual(state["source"], "inactive")
        self.assertEqual(state["detection"]["state"], "inactive")
        self.assertIn("detector says inactive", state["detail"])

    def test_health_gathers_every_part(self):
        control, _monitor, _controller, _targets = build("active")
        health = control.health()
        self.assertEqual(health["screen"], [1920, 1080])
        for key in ("pointer", "receiver", "desktop", "targets", "detection"):
            with self.subTest(key=key):
                self.assertIn(key, health)
        self.assertEqual(health["targets"]["source"], "fake")

    def test_an_unknown_screen_falls_back_to_a_usable_default(self):
        control = RemoteControl(FakeStore(), monitor=FakeMonitor(),
                                controller=FakeController(), targets=FakeTargets())
        self.addCleanup(control.close)
        width, height = control.screen
        self.assertGreaterEqual(width, 2)
        self.assertGreaterEqual(height, 2)


class ShutdownTests(unittest.TestCase):
    def test_closing_stops_everything_and_ends_the_visit(self):
        control, monitor, controller, _targets = build("active", poll_s=0.02)
        select(control)
        control.start()
        control.close()
        self.assertEqual(controller.closed, 1)
        self.assertEqual(monitor.closed, 1)
        self.assertFalse(control.session.enabled())
        self.assertIsNone(control.session.snapshot()["session"])

    def test_closing_twice_is_safe(self):
        control, _monitor, controller, _targets = build()
        control.close()
        control.close()
        self.assertEqual(controller.closed, 2)


if __name__ == "__main__":
    unittest.main()
