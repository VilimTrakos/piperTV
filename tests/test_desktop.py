import logging
import unittest

from pipertv.desktop import CLICKS, DesktopControl, choose_target

# The desktop layer logs whenever it swallows a failure instead of raising into
# the IR thread. These tests assert on health(), so keep that noise out of the
# test output.
logging.getLogger("pipertv.desktop").addHandler(logging.NullHandler())

SCREEN = (1920, 1080)


class FakePointer:
    """Stand in for the uinput device, recording what the desktop was told."""

    def __init__(self, screen, fail_on=None):
        self.screen = screen
        self.fail_on = fail_on
        self.opened = False
        self.closed = 0
        self.clicks = []
        self.moves = []
        self.scrolls = []
        self.position_override = None
        self._x, self._y = screen[0] // 2, screen[1] // 2

    @property
    def position(self):
        return self.position_override or (self._x, self._y)

    def scroll(self, clicks):
        self.scrolls.append(clicks)
        return clicks

    def open(self):
        if self.fail_on == "open":
            raise OSError("/dev/uinput is not writable")
        self.opened = True
        return self

    def move_to(self, x, y):
        if self.fail_on == "move":
            raise OSError("the pointer went away")
        self._x = max(0, min(self.screen[0] - 1, x))
        self._y = max(0, min(self.screen[1] - 1, y))
        self.moves.append((self._x, self._y))
        return self.position

    def move_by(self, dx, dy):
        return self.move_to(self._x + dx, self._y + dy)

    def click(self, button="left"):
        self.clicks.append(button)

    def close(self):
        self.closed += 1


class FakeSession:
    def __init__(self, mode="pointer", enabled=True):
        self.mode = mode
        self._enabled = enabled

    def enabled(self):
        return self._enabled

    def snapshot(self):
        return {"mode": self.mode, "control": "on" if self._enabled else "off"}


class FakeTargets:
    name = "fake"

    def __init__(self, targets, fail=False):
        self._targets = targets
        self.fail = fail

    def targets(self):
        if self.fail:
            raise RuntimeError("the accessibility bus went away")
        return self._targets


def control(mode="pointer", enabled=True, targets=None, fail_on=None, tick=1.0, **kwargs):
    """Build a desktop layer whose own clock advances one tick per press.

    The default tick is far longer than the acceleration window, so presses read
    as separate; acceleration tests pass a tick inside the window instead.
    """
    session = FakeSession(mode, enabled)
    made = []
    now = [0.0]

    def factory(screen):
        pointer = FakePointer(screen, fail_on)
        made.append(pointer)
        return pointer

    def clock():
        now[0] += tick
        return now[0]

    desktop = DesktopControl(session, SCREEN, pointer_factory=factory,
                             targets=targets, clock=clock, **kwargs)
    return desktop, session, made


class ChooseTargetTests(unittest.TestCase):
    def setUp(self):
        self.icons = [{"x": 100, "y": 100, "label": "a"}, {"x": 300, "y": 100, "label": "b"},
                      {"x": 100, "y": 300, "label": "c"}, {"x": 300, "y": 300, "label": "d"}]

    def test_moves_to_the_next_icon_in_that_direction(self):
        found = choose_target((100, 100), self.icons, "right")
        self.assertEqual(found["label"], "b")
        self.assertEqual(choose_target((100, 100), self.icons, "down")["label"], "c")
        self.assertEqual(choose_target((300, 300), self.icons, "left")["label"], "c")
        self.assertEqual(choose_target((300, 300), self.icons, "up")["label"], "b")

    def test_targets_behind_the_cursor_are_ignored(self):
        self.assertIsNone(choose_target((100, 100), [{"x": 50, "y": 100}], "right"))
        self.assertIsNone(choose_target((100, 100), [{"x": 100, "y": 50}], "down"))

    def test_an_aligned_target_beats_a_nearer_one_off_to_the_side(self):
        icons = [{"x": 400, "y": 100, "label": "ahead"},
                 {"x": 200, "y": 700, "label": "aside"}]
        self.assertEqual(choose_target((100, 100), icons, "right")["label"], "ahead")

    def test_what_is_directly_below_beats_a_nearer_diagonal(self):
        # The complaint this answers: pressing down landed somewhere off to the
        # side because it was a little closer, so a row could not be walked.
        icons = [{"x": 100, "y": 300, "label": "below"},
                 {"x": 260, "y": 220, "label": "diagonal"}]
        self.assertEqual(choose_target((100, 100), icons, "down")["label"], "below")

    def test_a_row_is_walked_one_control_at_a_time(self):
        row = [{"x": 100 + step, "y": 500, "label": str(step)} for step in (0, 120, 240, 360)]
        position, order = (100, 500), []
        for _press in range(3):
            found = choose_target(position, row, "right")
            order.append(found["label"])
            position = (found["x"], found["y"])
        self.assertEqual(order, ["120", "240", "360"])

    def test_a_wide_control_hands_over_to_whatever_is_under_any_part_of_it(self):
        # A banner button spans the screen; what is below its far end is still
        # below it, and judging from the centre alone would miss it.
        wide = {"left": 0, "top": 90, "right": 900, "bottom": 130, "x": 450, "y": 110}
        under_the_end = {"left": 800, "top": 300, "right": 880, "bottom": 340,
                         "x": 840, "y": 320, "label": "under the end"}
        found = choose_target((450, 110), [under_the_end], "down",
                              box=(wide["left"], wide["top"], wide["right"], wide["bottom"]))
        self.assertEqual(found["label"], "under the end")

    def test_without_a_shared_band_a_target_to_the_side_still_answers(self):
        # Nothing directly below: the only control that way is offset, and a
        # narrow cone still finds it rather than leaving the cursor stuck.
        icons = [{"left": 300, "top": 400, "right": 360, "bottom": 440,
                  "x": 330, "y": 420, "label": "offset"}]
        self.assertEqual(choose_target((100, 100), icons, "down")["label"], "offset")

    def test_controls_in_the_same_row_are_not_below_each_other(self):
        beside = [{"left": 300, "top": 90, "right": 380, "bottom": 130,
                   "x": 340, "y": 110, "label": "beside"}]
        self.assertIsNone(choose_target((100, 110), beside, "down"))

    def test_the_nearest_of_a_column_comes_first(self):
        column = [{"x": 100, "y": 700, "label": "far"},
                  {"x": 100, "y": 300, "label": "near"},
                  {"x": 100, "y": 500, "label": "middle"}]
        self.assertEqual(choose_target((100, 100), column, "down")["label"], "near")

    def test_a_target_far_off_the_line_loses_to_anything_recognisably_that_way(self):
        far_off = {"x": 110, "y": 900, "label": "far off"}
        that_way = {"x": 400, "y": 160, "label": "that way"}
        self.assertEqual(choose_target((100, 100), [far_off, that_way], "right")["label"],
                         "that way")

    def test_a_target_far_off_the_line_is_still_better_than_being_stuck(self):
        # A press that does nothing leaves whoever is holding the remote with
        # no way forward, which is the worse of the two failures.
        far_off = [{"x": 110, "y": 900, "label": "far off"}]
        self.assertEqual(choose_target((100, 100), far_off, "right")["label"], "far off")

    def test_the_menu_across_the_screen_is_reachable_from_the_middle(self):
        # Real coordinates from the Prime Video page on the Pi: the cursor sits
        # in the middle of the screen and the menu is at the top left. Pressing
        # up must land there rather than doing nothing.
        menu = [{"left": 177, "top": 12, "right": 252, "bottom": 54,
                 "x": 214, "y": 33, "label": "Home"},
                {"left": 252, "top": 12, "right": 336, "bottom": 54,
                 "x": 294, "y": 33, "label": "Movies"}]
        found = choose_target((960, 540), menu, "up")
        self.assertIsNotNone(found, "up from the middle of the screen must reach the menu")
        self.assertEqual(found["label"], "Movies", "the nearer of the two, sideways")

    def test_nothing_that_way_selects_nothing(self):
        self.assertIsNone(choose_target((100, 100), [], "right"))
        self.assertIsNone(choose_target((100, 100), self.icons, "sideways"))

    def test_malformed_targets_are_skipped(self):
        icons = [{"y": 100}, {"x": "far", "y": 100}, None,
                 {"x": float("inf"), "y": 100}, {"x": 300, "y": 100, "label": "ok"}]
        self.assertEqual(choose_target((100, 100), icons, "right")["label"], "ok")


class ScrollTests(unittest.TestCase):
    """At an edge the page moves under the cursor instead of the cursor stalling."""

    def build(self, targets=None, mode="pointer"):
        session = FakeSession(mode=mode)
        control = DesktopControl(session, SCREEN, pointer_factory=FakePointer,
                                 targets=targets)
        return control, session

    def test_pushing_down_at_the_bottom_scrolls_the_page(self):
        control, _session = self.build()
        control.press("down")
        pointer = control._pointer
        pointer.position_override = (960, SCREEN[1] - 1)
        control.press("down")
        self.assertEqual(pointer.scrolls[-1], -control.scroll_clicks)

    def test_pushing_up_at_the_top_scrolls_the_other_way(self):
        control, _session = self.build()
        control.press("up")
        pointer = control._pointer
        pointer.position_override = (960, 0)
        control.press("up")
        self.assertEqual(pointer.scrolls[-1], control.scroll_clicks)

    def test_the_cursor_still_moves_when_it_is_not_at_an_edge(self):
        control, _session = self.build()
        control.press("down")
        self.assertEqual(control._pointer.scrolls, [])

    def test_sideways_at_an_edge_does_not_scroll(self):
        control, _session = self.build()
        control.press("right")
        control._pointer.position_override = (SCREEN[0] - 1, 540)
        control.press("right")
        self.assertEqual(control._pointer.scrolls, [])

    def test_snapping_scrolls_when_nothing_is_that_way(self):
        # A long page has plenty below the fold; the press should reveal it
        # rather than doing nothing at all.
        control, _session = self.build(targets=FakeTargets([]), mode="snapping")
        control.press("down")
        self.assertEqual(control._pointer.scrolls[-1], -control.scroll_clicks)

    def test_snapping_moves_to_a_target_rather_than_scrolling(self):
        targets = FakeTargets([{"x": 400, "y": 800, "label": "below"}])
        control, _session = self.build(targets=targets, mode="snapping")
        control.press("down")
        self.assertEqual(control._pointer.scrolls, [])
        self.assertEqual(control._pointer.position, (400, 800))


class PointerModeTests(unittest.TestCase):
    def test_each_direction_moves_the_cursor_the_right_way(self):
        for button, (dx, dy) in [("right", (1, 0)), ("left", (-1, 0)),
                                 ("down", (0, 1)), ("up", (0, -1))]:
            with self.subTest(button=button):
                desktop, _session, made = control()
                start = (SCREEN[0] // 2, SCREEN[1] // 2)
                position = desktop.press(button)
                self.assertEqual(position, (start[0] + dx * 24, start[1] + dy * 24))
                self.assertTrue(made[0].opened)

    def test_holding_a_direction_accelerates_then_resets_on_release(self):
        # Presses arrive 0.1 s apart, inside the acceleration window.
        desktop, _session, _made = control(tick=0.1)
        first = desktop.press("right")
        second = desktop.press("right")
        third = desktop.press("right")
        steps = [second[0] - first[0], third[0] - second[0]]
        self.assertTrue(steps[0] > 24 and steps[1] > steps[0],
                        f"expected growing steps, got {steps}")

        # A different button ends the streak, so the next step is small again.
        desktop.press("down")
        before = desktop.press("right")
        after = desktop.press("right")
        self.assertEqual(after[0] - before[0], 24 + round(24 * 0.35))

    def test_acceleration_is_capped(self):
        desktop, _session, _made = control(tick=0.1, step_px=24, max_step_px=50)
        positions = [desktop.press("right") for _ in range(20)]
        largest = max(b[0] - a[0] for a, b in zip(positions, positions[1:]))
        self.assertLessEqual(largest, 50)

    def test_the_cursor_stops_at_the_screen_edge(self):
        desktop, _session, made = control()
        desktop.press("left")
        made[0]._x = 0
        self.assertEqual(desktop.press("left"), (0, made[0]._y))

    def test_an_implausible_step_is_refused(self):
        with self.assertRaises(ValueError):
            DesktopControl(FakeSession(), SCREEN, step_px=0)
        with self.assertRaises(ValueError):
            DesktopControl(FakeSession(), SCREEN, step_px=200, max_step_px=100)


class SnappingModeTests(unittest.TestCase):
    def test_directions_jump_straight_to_a_target(self):
        icons = FakeTargets([{"x": 1200, "y": 540, "label": "b"}])
        desktop, _session, made = control("snapping", targets=icons)
        self.assertEqual(desktop.press("right"), (1200, 540))
        self.assertEqual(made[0].moves, [(1200, 540)])

    def test_nothing_in_that_direction_leaves_the_cursor_alone(self):
        icons = FakeTargets([{"x": 100, "y": 540}])
        desktop, _session, made = control("snapping", targets=icons)
        self.assertIsNone(desktop.press("right"))
        self.assertEqual(made[0].moves, [])

    def test_a_moved_cached_target_must_still_be_in_the_pressed_direction(self):
        class MovingTargets(FakeTargets):
            def resolve(self, target):
                return {"x": 100, "y": 540, "label": target["label"]}

        targets = MovingTargets([{"x": 1200, "y": 540, "label": "Moved window"}])
        desktop, _session, made = control("snapping", targets=targets)
        self.assertIsNone(desktop.press("right"))
        self.assertEqual(made[0].moves, [])

    def test_snapping_without_any_target_source_is_reported_not_raised(self):
        desktop, _session, _made = control("snapping", targets=None)
        self.assertIsNone(desktop.press("right"))
        health = desktop.health()
        self.assertFalse(health["ok"])
        self.assertIn("targets", health["error"])

    def test_a_failing_target_source_does_not_stop_the_receiver(self):
        desktop, _session, _made = control("snapping", targets=FakeTargets([], fail=True))
        self.assertIsNone(desktop.press("right"))
        self.assertFalse(desktop.health()["ok"])


class ClickAndGateTests(unittest.TestCase):
    def test_ok_clicks_and_menu_right_clicks(self):
        desktop, _session, made = control()
        desktop.press("ok")
        desktop.press("menu")
        self.assertEqual(made[0].clicks, [CLICKS["ok"], CLICKS["menu"]])

    def test_buttons_with_no_desktop_meaning_do_nothing(self):
        desktop, _session, made = control()
        for button in ("power", "digit_5", "volume_up", "nettv"):
            with self.subTest(button=button):
                self.assertIsNone(desktop.press(button))
        self.assertEqual(made, [], "TV-only buttons must not create or reposition a pointer")

    def test_a_gate_closed_during_device_open_prevents_the_action(self):
        session = FakeSession()
        made = []

        class SlowPointer(FakePointer):
            def open(self):
                session._enabled = False
                return super().open()

        def factory(screen):
            made.append(SlowPointer(screen))
            return made[-1]

        desktop = DesktopControl(session, SCREEN, pointer_factory=factory)
        self.assertIsNone(desktop.press("ok"))
        self.assertEqual(made[0].clicks, [])
        self.assertEqual(made[0].moves, [])
        self.assertEqual(made[0].closed, 1)

    def test_a_gate_closed_during_snapping_lookup_prevents_movement(self):
        desktop, session, made = control("snapping")

        class SlowTargets(FakeTargets):
            def targets(self):
                session._enabled = False
                return [{"x": 1200, "y": 540}]

        desktop.targets = SlowTargets([])
        self.assertIsNone(desktop.press("right"))
        self.assertEqual(made[0].moves, [])
        self.assertEqual(made[0].closed, 1)

    def test_live_detection_is_rechecked_after_snapping_even_before_supervisor_tick(self):
        evidence = [True]
        desktop, session, made = control("snapping")
        desktop.enabled = lambda: evidence[0]

        class SlowTargets(FakeTargets):
            def targets(self):
                evidence[0] = False
                return [{"x": 1200, "y": 540}]

        desktop.targets = SlowTargets([])
        # The stored session remains on; fresh detection alone closes the gate.
        self.assertTrue(session.enabled())
        self.assertIsNone(desktop.press("right"))
        self.assertEqual(made[0].moves, [])

    def test_session_identity_is_read_after_the_live_predicate_refreshes_it(self):
        desktop, session, made = control()
        visit = ["first"]
        calls = [0]
        session.snapshot = lambda: {"mode": "pointer", "session": {"id": visit[0]}}

        def enabled():
            calls[0] += 1
            if calls[0] == 2:
                visit[0] = "second"
            return True

        desktop.enabled = enabled
        self.assertIsNone(desktop.press("ok"))
        self.assertEqual(made[0].clicks, [])

    def test_an_input_from_an_earlier_visit_does_not_act_in_a_new_visit(self):
        desktop, session, made = control("snapping")
        visit = ["first"]
        session.snapshot = lambda: {"mode": "snapping", "session": {"id": visit[0]}}

        class SlowTargets(FakeTargets):
            def targets(self):
                visit[0] = "second"
                return [{"x": 1200, "y": 540}]

        desktop.targets = SlowTargets([])
        self.assertIsNone(desktop.press("right"))
        self.assertEqual(made[0].moves, [])

    def test_a_successful_retry_clears_a_prior_desktop_error(self):
        desktop, _session, made = control(fail_on="move")
        desktop.press("right")
        self.assertFalse(desktop.health()["ok"])
        desktop.pointer_factory = lambda screen: FakePointer(screen)
        self.assertIsNotNone(desktop.press("right"))
        self.assertTrue(desktop.health()["ok"])

    def test_a_closed_gate_moves_nothing_and_takes_the_device_away(self):
        desktop, session, made = control()
        desktop.press("right")
        self.assertTrue(made[0].opened)
        session._enabled = False
        self.assertIsNone(desktop.press("right"))
        self.assertEqual(made[0].closed, 1)
        self.assertFalse(desktop.health()["active"])

    def test_control_resumes_with_a_new_device_after_the_gate_reopens(self):
        desktop, session, made = control()
        desktop.press("right")
        session._enabled = False
        desktop.press("right")
        session._enabled = True
        desktop.press("right")
        self.assertEqual(len(made), 2, "a new session must get a new virtual pointer")
        self.assertTrue(made[1].opened)

    def test_a_pointer_that_cannot_open_is_reported_not_raised(self):
        desktop, _session, _made = control(fail_on="open")
        self.assertIsNone(desktop.press("right"))
        health = desktop.health()
        self.assertFalse(health["ok"])
        self.assertIn("uinput", health["error"])
        self.assertFalse(health["active"])

    def test_a_pointer_that_fails_mid_session_is_reported_not_raised(self):
        desktop, _session, made = control(fail_on="move")
        self.assertIsNone(desktop.press("right"))
        self.assertFalse(desktop.health()["ok"])
        self.assertEqual(made[0].closed, 1)

    def test_closing_removes_the_device(self):
        desktop, _session, made = control()
        desktop.press("right")
        desktop.close()
        self.assertEqual(made[0].closed, 1)
        desktop.close()
        self.assertEqual(made[0].closed, 1, "closing twice must not reopen anything")

    def test_health_describes_the_desktop_layer(self):
        desktop, _session, _made = control(targets=FakeTargets([]))
        health = desktop.health()
        self.assertEqual(health["screen"], [1920, 1080])
        self.assertEqual(health["targets"], "fake")
        self.assertFalse(health["active"])
        self.assertTrue(health["ok"])


if __name__ == "__main__":
    unittest.main()
