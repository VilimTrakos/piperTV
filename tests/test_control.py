import logging
import time
import unittest

from pipertv.control import STEP_HOLD_DELAY_S, STEP_HOLD_INTERVAL_S, RemoteControl

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

    def __init__(self, under=None):
        self.invalidated = 0
        self.under = under          # what the page reports under a point
        self.asked = []

    def targets(self):
        return []

    def at_point(self, x, y):
        self.asked.append((x, y))
        return self.under

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
        self.modes = []
        self.position = (470, 41)   # where a press leaves the cursor

    def press(self, button, mode=None):
        self.presses.append(button)
        self.modes.append(mode)
        self.active = True  # pressing opens the virtual pointer
        return self.position

    def release(self):
        self.released += 1
        self.active = False

    def health(self):
        return {"ok": True, "active": self.active}

    def close(self):
        self.closed += 1


class FakeLauncher:
    """Stands in for the layer that puts a service on the Pi's screen."""

    KNOWN = {"youtube": "YouTube", "prime": "Prime Video"}
    POLICY = {"youtube": "keys", "prime": "snap"}

    def __init__(self):
        self.launched = []
        self.history = []
        self.stopped = self.closed = 0
        self.open = None
        self.available = True

    def launch(self, service):
        if service not in self.KNOWN:
            raise KeyError(f"Piper cannot open {service} yet.")
        if not self.available:
            raise RuntimeError("No chromium browser was found on this Pi.")
        self.launched.append(service)
        self.open = {"id": service, "name": self.KNOWN[service], "started_at": 0}
        return self.snapshot()

    def stop(self):
        self.stopped += 1
        if self.open:
            self.history.insert(0, dict(self.open, ended_at=0, exit_code=0,
                                        seconds=0.0, age_s=0.0))
        self.open = None
        return self.snapshot()

    def running(self):
        return dict(self.open) if self.open else None

    def policy(self, service_id):
        return self.POLICY.get(service_id, "keys")

    def snapshot(self):
        return {"available": self.available, "reason": None, "browser": "/usr/bin/chromium",
                "services": [{"id": key, "name": name, "control": self.policy(key)}
                             for key, name in self.KNOWN.items()],
                "running": self.running(), "error": None,
                "history": list(self.history)}

    def close(self):
        self.closed += 1
        self.open = None


class FakeKeys:
    """Stands in for the keyboard Piper types into an open service with."""

    def __init__(self):
        self.sent = []
        self.typed = []
        self.released = self.closed = 0
        self.active = False

    def send(self, key):
        self.sent.append(key)
        self.active = True
        return key

    def write(self, text):
        self.typed.append(text)
        self.active = True
        return len(text)

    def release(self):
        self.released += 1
        self.active = False

    def health(self):
        return {"ok": True, "active": self.active, "error": None}

    def close(self):
        self.closed += 1
        self.active = False


class FakeOnScreen:
    """Stands in for the keyboard drawn across the bottom of the screen."""

    def __init__(self, available=True):
        self.prepared = self.shown = self.hidden = self.closed = 0
        self.visible = False
        self.ready = False
        self.usable = available

    def prepare(self):
        self.prepared += 1
        self.ready = self.usable
        return self.usable

    def show(self):
        if not self.usable:
            return False
        self.shown += 1
        self.visible = True
        self.ready = True
        return True

    def hide(self):
        self.hidden += 1
        was, self.visible = self.visible, False
        return was

    def showing(self):
        return self.visible

    def height(self):
        return 320          # what wvkbd is started with on the Pi

    def health(self):
        return {"ok": True, "available": self.usable, "ready": self.ready,
                "showing": self.visible, "error": None}

    def close(self):
        self.closed += 1
        self.visible = self.ready = False


class FakeWatcher:
    """Stands in for the accessibility bus reporting what a page focused."""

    def __init__(self):
        self.started = self.closed = self.forgotten = 0
        self.field = None
        self.on_text_field = None

    def start(self):
        self.started += 1

    def focus(self, role="entry", label="Search"):
        """Pretend a page focused something to type into."""
        self.field = {"role": role, "label": label, "at": 0}
        if self.on_text_field is not None:
            self.on_text_field(dict(self.field))

    def typing_into(self):
        return dict(self.field) if self.field else None

    def forget(self):
        self.forgotten += 1
        self.field = None

    def health(self):
        return {"ok": True, "watching": True, "field": self.typing_into(), "error": None}

    def close(self):
        self.closed += 1


class FakeInterface:
    """Stands in for the browser window showing the interface on the TV."""

    def __init__(self):
        self.closed = 0
        self.visible = True

    def close(self):
        self.closed += 1
        self.visible = False
        return {"closed": [4242], "showing": False, "error": None}

    def showing(self):
        return self.visible

    def snapshot(self):
        return {"port": 8765, "showing": self.visible, "error": None}


class FakeStore:
    def snapshot(self):
        return {"recordings": {}}


def build(state="unknown", **kwargs):
    monitor = FakeMonitor(state)
    controller = FakeController()
    targets = FakeTargets()
    kwargs.setdefault("launcher", FakeLauncher())
    kwargs.setdefault("interface", FakeInterface())
    kwargs.setdefault("keys", FakeKeys())
    kwargs.setdefault("watcher", FakeWatcher())
    kwargs.setdefault("onscreen", FakeOnScreen())
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


class ServiceTests(unittest.TestCase):
    """Opening something on the TV answers to the same gate as the cursor."""

    def session_id(self, control):
        return control.session.snapshot()["session"]["id"]

    def test_the_interface_opens_a_service_for_the_visit_it_is_showing(self):
        control, _monitor, _controller, _targets = build("active")
        select(control, "piper")
        state = control.launch("youtube", self.session_id(control))
        self.assertEqual(control.launcher.launched, ["youtube"])
        self.assertEqual(state["running"]["id"], "youtube")

    def test_nothing_opens_while_the_tv_is_showing_something_else(self):
        control, monitor, _controller, _targets = build("active")
        select(control, "piper")
        visit = self.session_id(control)
        monitor.state = "inactive"
        with self.assertRaises(RuntimeError) as refused:
            control.launch("youtube", visit)
        self.assertIn("not showing the Pi", str(refused.exception))
        self.assertEqual(control.launcher.launched, [])

    def test_a_desktop_mode_is_not_the_interface_and_cannot_launch(self):
        control, _monitor, _controller, _targets = build("active")
        select(control, "pointer")
        with self.assertRaises(RuntimeError):
            control.launch("youtube", self.session_id(control))

    def test_a_page_from_an_earlier_visit_cannot_open_anything(self):
        control, monitor, _controller, _targets = build("active")
        select(control, "piper")
        stale = self.session_id(control)
        monitor.state = "inactive"
        control._tick()
        monitor.state = "active"
        select(control, "piper")
        with self.assertRaises(RuntimeError) as refused:
            control.launch("youtube", stale)
        self.assertIn("earlier visit", str(refused.exception))

    def test_recording_a_button_refuses_a_launch_and_says_why(self):
        control, _monitor, _controller, _targets = build("active")
        select(control, "piper")
        visit = self.session_id(control)
        control.hold()
        with self.assertRaises(RuntimeError) as refused:
            control.launch("youtube", visit)
        self.assertIn("Recording", str(refused.exception))

    def test_an_unknown_service_is_refused_by_the_launcher(self):
        control, _monitor, _controller, _targets = build("active")
        select(control, "piper")
        with self.assertRaises(KeyError):
            control.launch("netflix", self.session_id(control))

    def test_exit_and_home_close_the_open_service(self):
        for button in ("exit", "home"):
            with self.subTest(button=button):
                control, _monitor, _controller, _targets = build("active")
                select(control, "piper")
                control.launch("youtube", self.session_id(control))
                control._press(button)
                self.assertEqual(control.launcher.stopped, 1)
                self.assertIsNone(control.launcher.running())
                # The press is recorded, so the interface can show what happened.
                self.assertEqual([event["button"] for event in control.events(0)["events"]][-1],
                                 button)

    def test_back_belongs_to_the_service_not_to_piper(self):
        # A television's back button leaves a video, and taking it away would
        # make the service unusable. Exit and home are the way out instead.
        control, _monitor, _controller, _targets = build("active")
        select(control, "piper")
        control.launch("youtube", self.session_id(control))
        control._press("back")
        self.assertEqual(control.launcher.stopped, 0)
        self.assertEqual(control.keys.sent, ["back"])

    def test_the_remote_drives_the_open_service_instead_of_the_ring(self):
        control, _monitor, _controller, _targets = build("active")
        select(control, "piper")
        control.launch("youtube", self.session_id(control))
        for button in ("down", "right", "ok"):
            control._press(button)
        self.assertEqual(control.keys.sent, ["down", "right", "ok"])
        self.assertEqual(control.desktop.presses, [])

    def test_a_site_built_for_a_mouse_is_driven_by_snapping(self):
        # Arrow keys do nothing on such a page: a cookie dialog's Accept cannot
        # be reached with them, which is the whole reason this exists.
        control, _monitor, _controller, _targets = build("active")
        select(control, "piper")
        control.launch("prime", self.session_id(control))
        for button in ("down", "right", "ok"):
            control._press(button)
        self.assertEqual(control.desktop.presses, ["down", "right", "ok"])
        self.assertEqual(control.desktop.modes, ["snapping"] * 3,
                         "the visit chose piper; the service asks for snapping")
        self.assertEqual(control.keys.sent, [])

    def test_back_is_typed_even_to_a_snapped_service(self):
        # Escape closes an overlay on a page as much as in an app.
        control, _monitor, _controller, _targets = build("active")
        select(control, "piper")
        control.launch("prime", self.session_id(control))
        control._press("back")
        self.assertEqual(control.keys.sent, ["back"])
        self.assertEqual(control.desktop.presses, [])

    def test_a_television_app_is_typed_at_rather_than_snapped(self):
        control, _monitor, _controller, _targets = build("active")
        select(control, "piper")
        control.launch("youtube", self.session_id(control))
        control._press("down")
        self.assertEqual(control.keys.sent, ["down"])
        self.assertEqual(control.desktop.presses, [])

    def test_the_cursor_survives_between_presses_while_snapping_a_service(self):
        # The bug this answers: the supervisor took the pointer away every pass
        # because the visit chose "piper", and each new device believes it is
        # at the centre of the screen. Every press then started from the
        # centre, which is exactly what made snapping look random.
        control, _monitor, _controller, _targets = build("active")
        select(control, "piper")
        control.launch("prime", self.session_id(control))
        control._press("right")
        self.assertTrue(control.desktop.health()["active"])
        control._tick()
        control._tick()
        self.assertTrue(control.desktop.health()["active"],
                        "the cursor must stay where the last press left it")
        self.assertEqual(control.desktop.released, 0)

    def test_the_cursor_is_taken_away_once_the_service_is_gone(self):
        control, _monitor, _controller, _targets = build("active")
        select(control, "piper")
        control.launch("prime", self.session_id(control))
        control._press("right")
        control._press("exit")
        self.assertFalse(control.desktop.health()["active"])

    def test_a_typed_service_does_not_keep_the_cursor(self):
        control, _monitor, _controller, _targets = build("active")
        select(control, "piper")
        control.launch("youtube", self.session_id(control))
        control.desktop.press("right")  # something opened a pointer
        control._tick()
        self.assertEqual(control.desktop.released, 1)

    def test_closing_a_service_takes_the_keyboard_away(self):
        control, _monitor, _controller, _targets = build("active")
        select(control, "piper")
        control.launch("youtube", self.session_id(control))
        control._press("ok")
        self.assertTrue(control.keys.active)
        control._press("exit")
        self.assertEqual(control.keys.released, 1)
        self.assertFalse(control.keys.active)

    def test_a_service_that_ends_by_itself_takes_the_keyboard_with_it(self):
        control, _monitor, _controller, _targets = build("active")
        select(control, "piper")
        control.launch("youtube", self.session_id(control))
        control._press("ok")
        control.launcher.open = None  # someone closed the window on the desktop
        control._tick()
        self.assertEqual(control.keys.released, 1)

    def test_nothing_is_typed_into_a_service_with_the_gate_shut(self):
        # The TV is showing something else and those presses are meant for it.
        control, monitor, _controller, _targets = build("active")
        select(control, "piper")
        control.launch("youtube", self.session_id(control))
        monitor.state = "inactive"
        control._press("down")
        self.assertEqual(control.keys.sent, [])

    def test_the_way_out_works_in_a_desktop_mode_too(self):
        # A service opened from the interface is Piper's to close whatever mode
        # the visit later chose; otherwise its window owns the TV for good.
        control, _monitor, _controller, _targets = build("active")
        select(control, "piper")
        control.launch("youtube", self.session_id(control))
        control.choose("pointer", self.session_id(control))
        control._press("exit")
        self.assertEqual(control.launcher.stopped, 1)
        self.assertEqual(control.desktop.presses, [])

    def test_the_way_out_does_nothing_when_nothing_is_open(self):
        control, _monitor, _controller, _targets = build("active")
        select(control, "pointer")
        control._press("home")
        self.assertEqual(control.launcher.stopped, 0)

    def test_the_interface_can_close_a_service_without_the_remote(self):
        control, _monitor, _controller, _targets = build("active")
        select(control, "piper")
        control.launch("youtube", self.session_id(control))
        self.assertIsNone(control.stop_service()["running"])

    def test_the_feed_carries_what_can_be_opened_and_what_is_open(self):
        control, _monitor, _controller, _targets = build("active")
        select(control, "piper")
        feed = control.events(0)
        self.assertEqual([service["id"] for service in feed["services"]["services"]],
                         ["youtube", "prime"])
        self.assertIsNone(feed["services"]["running"])
        control.launch("youtube", self.session_id(control))
        self.assertEqual(control.events(0)["services"]["running"]["id"], "youtube")

    def test_a_service_the_tv_switched_away_from_keeps_running(self):
        # Piper stops controlling, but it does not close what someone opened:
        # the TV coming back should find it where they left it.
        control, monitor, _controller, _targets = build("active")
        select(control, "piper")
        control.launch("youtube", self.session_id(control))
        monitor.state = "inactive"
        control._tick()
        self.assertEqual(control.launcher.running()["id"], "youtube")


class WayOutTests(unittest.TestCase):
    """Leaving must work when everything else is refused."""

    def visit(self, control, mode="piper"):
        return select(control, mode)["session"]["id"]

    def test_a_service_is_closed_even_with_the_gate_shut(self):
        # How someone gets trapped: the TV stops reporting, control goes off,
        # and a full-screen YouTube is left with nothing that can close it.
        control, monitor, _controller, _targets = build("active")
        control.launch("youtube", self.visit(control))
        monitor.state = "unknown"
        control._tick()
        self.assertFalse(control.session.enabled())

        control._press("exit")
        self.assertEqual(control.launcher.stopped, 1)
        self.assertIsNone(control.launcher.running())

    def test_a_press_with_the_gate_shut_is_still_not_recorded_or_acted_on(self):
        control, monitor, _controller, _targets = build("active")
        select(control, "pointer")
        monitor.state = "inactive"
        control._press("right")
        control._press("ok")
        self.assertEqual(control.desktop.presses, [])
        self.assertEqual(control.events(0)["events"], [])

    def test_the_receiver_keeps_listening_when_the_gate_shuts(self):
        # The reason the gate no longer decides whether a press is heard.
        control, monitor, _controller, _targets = build("active")
        select(control, "piper")
        monitor.state = "inactive"
        control._tick()
        self.assertFalse(control.session.enabled())
        self.assertTrue(control._listening())

    def test_exit_asks_before_closing_the_interface(self):
        control, _monitor, _controller, _targets = build("active")
        select(control, "piper")
        control._press("exit")
        self.assertEqual(control.interface.closed, 0, "one press must never close Piper")
        self.assertTrue(control.events(0)["leaving"]["armed"])

        control._press("exit")
        self.assertEqual(control.interface.closed, 1)
        self.assertFalse(control.events(0)["leaving"]["armed"])

    def test_the_question_expires_on_its_own(self):
        control, _monitor, _controller, _targets = build("active")
        select(control, "piper")
        now = [100.0]
        control.leaving.clock = lambda: now[0]
        control._press("exit")
        now[0] += control.leaving.window_s + 0.1
        self.assertFalse(control.leaving.armed())
        control._press("exit")
        self.assertEqual(control.interface.closed, 0, "the second press asks again")

    def test_any_other_press_takes_the_question_back(self):
        control, _monitor, _controller, _targets = build("active")
        select(control, "piper")
        control._press("exit")
        control._press("right")
        self.assertFalse(control.leaving.armed())
        control._press("exit")
        self.assertEqual(control.interface.closed, 0)

    def test_exit_closes_the_interface_with_the_gate_shut_too(self):
        control, monitor, _controller, _targets = build("active")
        select(control, "piper")
        monitor.state = "unknown"
        control._tick()
        control._press("exit")
        control._press("exit")
        self.assertEqual(control.interface.closed, 1)

    def test_exit_leaves_a_service_first_and_keeps_piper(self):
        # One key, read in context: the service goes, the interface stays.
        control, _monitor, _controller, _targets = build("active")
        control.launch("youtube", self.visit(control))
        control._press("exit")
        self.assertEqual(control.launcher.stopped, 1)
        self.assertEqual(control.interface.closed, 0)
        self.assertFalse(control.leaving.armed())

    def test_back_and_home_never_close_the_interface(self):
        for button in ("back", "home"):  # only exit is the door out of Piper
            with self.subTest(button=button):
                control, _monitor, _controller, _targets = build("active")
                select(control, "piper")
                control._press(button)
                control._press(button)
                self.assertEqual(control.interface.closed, 0)


class KeyboardTests(unittest.TestCase):
    """Typing into a page's search box, with nothing but a remote."""

    def build_with_page(self):
        control, _monitor, _controller, _targets = build("active")
        control.watcher.on_text_field = control._text_field_focused
        visit = select(control, "piper")["session"]["id"]
        control.launch("prime", visit)
        return control

    def test_a_page_gets_its_keyboard_ready_as_it_opens(self):
        # Starting it on demand takes ten seconds on this Pi, which is ten
        # seconds of staring at a search box.
        control = self.build_with_page()
        self.assertEqual(control.onscreen.prepared, 1)
        self.assertFalse(control.onscreen.showing())

    def test_a_search_box_chosen_with_ok_brings_it_up(self):
        control = self.build_with_page()
        control._press("ok")                 # the cursor clicks into the box
        control.watcher.focus("entry", "Search")
        self.assertTrue(control.onscreen.showing())

    def test_a_page_focusing_its_own_box_as_it_loads_is_not_a_request(self):
        control = self.build_with_page()
        control.watcher.focus("entry", "Search")
        self.assertFalse(control.onscreen.showing())

    def test_an_application_with_its_own_keyboard_is_left_alone(self):
        control, _monitor, _controller, _targets = build("active")
        control.watcher.on_text_field = control._text_field_focused
        visit = select(control, "piper")["session"]["id"]
        control.launch("youtube", visit)   # typed at, not pointed at
        control._press("ok")
        control.watcher.focus("entry", "Search")
        self.assertFalse(control.onscreen.showing())
        self.assertEqual(control.onscreen.prepared, 0)

    def test_nothing_appears_when_the_gate_is_shut(self):
        control = self.build_with_page()
        control.session.stop()
        control.open_keyboard()
        self.assertFalse(control.onscreen.showing())

    def test_back_takes_it_away_and_leaves_the_page_open(self):
        control = self.build_with_page()
        control._press("ok")
        control.watcher.focus()
        control._press("back")
        self.assertFalse(control.onscreen.showing())
        self.assertEqual(control.launcher.stopped, 0, "back closed the keyboard, not the page")
        self.assertEqual(control.launcher.running()["id"], "prime")

    def test_exit_takes_it_away_before_it_closes_anything(self):
        control = self.build_with_page()
        control._press("ok")
        control.watcher.focus()
        control._press("exit")
        self.assertFalse(control.onscreen.showing())
        self.assertEqual(control.launcher.stopped, 0)

    def test_exit_closes_the_page_once_the_keyboard_is_gone(self):
        control = self.build_with_page()
        control._press("ok")
        control.watcher.focus()
        control._press("exit")      # takes the keyboard away
        control._press("exit")      # now the page
        self.assertEqual(control.launcher.stopped, 1)

    def test_the_cursor_still_works_its_keys_while_it_is_up(self):
        # They are pressed by clicking them, like anything else on a page.
        control = self.build_with_page()
        control._press("ok")
        control.watcher.focus()
        control.desktop.presses.clear()
        for button in ("down", "right", "ok"):
            control._press(button)
        self.assertEqual(control.desktop.presses, ["down", "right", "ok"])

    def test_closing_the_service_takes_it_with_it(self):
        control = self.build_with_page()
        control._press("ok")
        control.watcher.focus()
        control._press("exit")   # keyboard
        control._press("exit")   # service
        self.assertEqual(control.onscreen.closed, 1)

    def test_a_pi_without_it_installed_carries_on_regardless(self):
        control, _monitor, _controller, _targets = build(
            "active", onscreen=FakeOnScreen(available=False))
        control.watcher.on_text_field = control._text_field_focused
        visit = select(control, "piper")["session"]["id"]
        control.launch("prime", visit)
        control._press("ok")
        control.watcher.focus()
        self.assertFalse(control.onscreen.showing())
        self.assertFalse(control.onscreen.health()["available"])

    def test_clicking_a_search_box_brings_it_up_with_nothing_said_on_the_bus(self):
        # The one that was failing on a page of results: the box there has been
        # focused since the page loaded, so clicking into it moves neither the
        # focus nor the caret and the page announces nothing at all. What the
        # cursor is standing on is asked about instead, and answers.
        control = self.build_with_page()
        control.targets.under = {"role": "combo box", "label": "Search privately"}
        control._look_under_cursor(control.desktop.position)
        self.assertTrue(control.onscreen.showing())
        self.assertEqual(control.targets.asked, [(470, 41)])

    def test_a_click_anywhere_else_on_the_page_brings_up_nothing(self):
        control = self.build_with_page()
        control.targets.under = {"role": "link", "label": "Some result"}
        control._look_under_cursor(control.desktop.position)
        self.assertFalse(control.onscreen.showing())

    def test_a_page_that_says_nothing_about_the_point_is_not_a_reason_to_fail(self):
        control = self.build_with_page()
        control.targets.under = None
        control._look_under_cursor(control.desktop.position)   # must not raise
        self.assertFalse(control.onscreen.showing())

    def test_a_press_of_ok_asks_the_page_what_it_landed_on(self):
        control = self.build_with_page()
        control.targets.under = {"role": "entry", "label": "Search"}
        control._press("ok")
        for _ in range(100):
            if control.onscreen.showing():
                break
            time.sleep(0.01)
        self.assertTrue(control.onscreen.showing())

    def test_an_application_with_its_own_keyboard_is_not_asked(self):
        # YouTube's television app is typed at, not clicked: nothing to look up.
        control, _monitor, _controller, _targets = build("active")
        visit = select(control, "piper")["session"]["id"]
        control.launch("youtube", visit)
        control.targets.under = {"role": "entry", "label": "Search"}
        control._press("ok")
        self.assertEqual(control.targets.asked, [])

    def test_a_click_in_a_box_that_was_already_focused_still_brings_it_up(self):
        # A search box on a page of results is focused already: clicking into
        # it moves neither the focus nor the caret, so the page says nothing.
        control = self.build_with_page()
        control.watcher.field = {"role": "entry", "label": "Search", "at": 0}
        control._press("ok")
        control._look_after_click_now()
        self.assertTrue(control.onscreen.showing())

    def test_a_click_on_something_else_takes_it_away(self):
        control = self.build_with_page()
        control._press("ok")
        control.watcher.focus()
        self.assertTrue(control.onscreen.showing())
        control.targets.under = {"role": "link", "label": "Some result"}
        control._look_under_cursor((470, 41))
        self.assertFalse(control.onscreen.showing())

    def test_a_page_saying_nothing_does_not_take_it_away(self):
        # It flashed up and vanished: the box had been focused since the page
        # loaded, so the click brought the keyboard up and the page, asked a
        # moment later what it had focused, answered nothing at all.
        control = self.build_with_page()
        control.targets.under = {"role": "combo box", "label": "Search privately"}
        control._look_under_cursor((470, 41))
        self.assertTrue(control.onscreen.showing())
        control.watcher.field = None        # the page announced nothing
        control._look_after_click_now()
        self.assertTrue(control.onscreen.showing())

    def test_clicking_its_own_keys_never_closes_it(self):
        # The page underneath does not know the keyboard is there, and would
        # report whatever each key covers.
        control = self.build_with_page()
        control._press("ok")
        control.watcher.focus()
        control.targets.under = {"role": "link", "label": "something under a key"}
        control._look_under_cursor((300, 1000))   # inside the keyboard
        self.assertTrue(control.onscreen.showing())

    def test_the_feed_says_whether_it_is_up(self):
        control = self.build_with_page()
        control._press("ok")
        control.watcher.focus()
        self.assertTrue(control.events(0)["keyboard"]["showing"])


class HoldPaceTests(unittest.TestCase):
    """The receiver is told how fast to repeat by what the press moves."""

    def test_snapping_a_service_asks_for_the_slower_pace(self):
        control, _monitor, _controller, _targets = build("active")
        select(control, "piper")
        control.launch("prime", control.session.snapshot()["session"]["id"])
        self.assertEqual(control._hold_pace("up"), (STEP_HOLD_DELAY_S, STEP_HOLD_INTERVAL_S))

    def test_snapping_mode_on_the_desktop_asks_for_it_too(self):
        control, _monitor, _controller, _targets = build("active")
        select(control, "snapping")
        self.assertEqual(control._hold_pace("up"), (STEP_HOLD_DELAY_S, STEP_HOLD_INTERVAL_S))

    def test_nudging_a_cursor_keeps_the_pace_it_was_tuned_for(self):
        control, _monitor, _controller, _targets = build("active")
        select(control, "pointer")
        self.assertIsNone(control._hold_pace("up"))

    def test_a_typed_service_steps_too_and_asks_for_the_slower_pace(self):
        # Kodi's menu moves by one item per press. At the pixel pace a press a
        # shade too long moved two, and the item being aimed at was skipped.
        control, _monitor, _controller, _targets = build("active")
        select(control, "piper")
        control.launch("youtube", control.session.snapshot()["session"]["id"])
        self.assertEqual(control._hold_pace("up"), (STEP_HOLD_DELAY_S, STEP_HOLD_INTERVAL_S))

    def test_the_hold_threshold_is_a_setting_not_a_constant(self):
        # How long a press lasts is a habit; the one that suits this room is
        # saved with the recordings like everything else about the remote.
        control, _monitor, _controller, _targets = build("active")
        select(control, "piper")
        control.pointer = dict(control.pointer, hold_delay_s=1.2, hold_interval_s=0.4)
        self.assertEqual(control._hold_pace("up"), (1.2, 0.4))

    def test_the_ring_itself_steps_as_well(self):
        control, _monitor, _controller, _targets = build("active")
        select(control, "piper")
        self.assertEqual(control._hold_pace("up"), (STEP_HOLD_DELAY_S, STEP_HOLD_INTERVAL_S))

    def test_a_nudged_service_keeps_the_fast_repeat_that_makes_it_glide(self):
        control, _monitor, _controller, _targets = build("active")
        select(control, "piper")
        control.pointer = dict(control.pointer, drive="nudge")
        control.launch("prime", control.session.snapshot()["session"]["id"])
        self.assertIsNone(control._hold_pace("up"))


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
        for key in ("pointer", "receiver", "desktop", "targets", "detection",
                    "services", "interface"):
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
        self.assertEqual(control.launcher.closed, 1,
                         "a kiosk window must not outlive the app that opened it")
        self.assertFalse(control.session.enabled())
        self.assertIsNone(control.session.snapshot()["session"])

    def test_closing_twice_is_safe(self):
        control, _monitor, controller, _targets = build()
        control.close()
        control.close()
        self.assertEqual(controller.closed, 2)


if __name__ == "__main__":
    unittest.main()
