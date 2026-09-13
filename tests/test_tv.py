import threading
import unittest

from pipertv.tv import NAVIGATION, ButtonLog


class ButtonLogTests(unittest.TestCase):
    def setUp(self):
        self.now = [0.0]
        self.log = ButtonLog(clock=lambda: self.now[0])

    def test_presses_are_numbered_in_order(self):
        for button in ("up", "down", "ok"):
            self.log.append(button)
        result = self.log.since(0)
        self.assertEqual([event["button"] for event in result["events"]],
                         ["up", "down", "ok"])
        self.assertEqual([event["sequence"] for event in result["events"]], [1, 2, 3])
        self.assertEqual(result["sequence"], 3)
        self.assertFalse(result["missed"])

    def test_a_page_only_receives_what_it_has_not_seen(self):
        self.log.append("up")
        self.log.append("down")
        seen = self.log.since(0)["sequence"]
        self.log.append("ok")
        result = self.log.since(seen)
        self.assertEqual([event["button"] for event in result["events"]], ["ok"])
        self.assertFalse(result["missed"])

    def test_polling_with_nothing_new_returns_nothing(self):
        self.log.append("up")
        result = self.log.since(1)
        self.assertEqual(result["events"], [])
        self.assertEqual(result["sequence"], 1)
        self.assertFalse(result["missed"])

    def test_the_mode_in_force_is_recorded_with_the_press(self):
        self.log.append("left", "piper")
        self.log.append("left", "pointer")
        self.assertEqual([event["mode"] for event in self.log.since(0)["events"]],
                         ["piper", "pointer"])

    def test_navigation_buttons_are_marked(self):
        self.log.append("up")
        self.log.append("volume_up")
        marks = {event["button"]: event["navigation"] for event in self.log.since(0)["events"]}
        self.assertTrue(marks["up"])
        self.assertFalse(marks["volume_up"], "volume is recorded but is not navigation")

    def test_the_press_carries_when_it_happened(self):
        self.now[0] = 12.5
        event = self.log.append("ok")
        self.assertEqual(event["at"], 12.5)

    def test_falling_behind_the_window_is_reported_as_a_gap(self):
        log = ButtonLog(limit=3, clock=lambda: self.now[0])
        for button in ("up", "down", "left", "right", "ok"):
            log.append(button)
        # Asking for everything after 1 cannot be answered: 2 was discarded.
        result = log.since(1)
        self.assertTrue(result["missed"])
        self.assertEqual([event["button"] for event in result["events"]],
                         ["left", "right", "ok"])

    def test_a_page_still_inside_the_window_is_not_told_it_missed_anything(self):
        log = ButtonLog(limit=3, clock=lambda: self.now[0])
        for button in ("up", "down", "left", "right"):
            log.append(button)
        self.assertFalse(log.since(3)["missed"])

    def test_a_first_poll_is_never_a_gap(self):
        log = ButtonLog(limit=2, clock=lambda: self.now[0])
        for button in ("up", "down", "left"):
            log.append(button)
        # A page opening fresh asks from zero and simply takes what is there.
        self.assertFalse(log.since(0)["missed"])

    def test_a_restart_resets_numbering_and_is_reported_as_a_gap(self):
        self.log.append("up")
        # The page still holds a number from before the app was restarted.
        result = self.log.since(57)
        self.assertTrue(result["missed"])
        self.assertEqual(result["sequence"], 1)

    def test_an_empty_log_answers_without_a_gap(self):
        result = self.log.since(0)
        self.assertEqual(result["sequence"], 0)
        self.assertEqual(result["events"], [])
        self.assertFalse(result["missed"])
        self.assertTrue(result["stream_id"])

    def test_restart_has_a_new_stream_even_when_sequence_numbers_overlap(self):
        before = self.log.since(0)["stream_id"]
        self.assertEqual(self.log.since(0)["stream_id"], before)
        self.assertNotEqual(ButtonLog().since(0)["stream_id"], before)

    def test_event_belongs_to_the_visit_that_produced_it(self):
        event = self.log.append("ok", "piper", "first-visit")
        self.assertEqual(event["session_id"], "first-visit")

    def test_the_last_press_can_be_read_directly(self):
        self.assertIsNone(self.log.latest())
        self.log.append("home")
        self.assertEqual(self.log.latest()["button"], "home")

    def test_clearing_forgets_presses_without_reusing_numbers(self):
        self.log.append("up")
        self.log.clear()
        self.assertIsNone(self.log.latest())
        event = self.log.append("down")
        self.assertEqual(event["sequence"], 2, "numbers must never be reused")

    def test_returned_events_cannot_be_edited_in_place(self):
        self.log.append("up")
        self.log.since(0)["events"][0]["button"] = "tampered"
        self.assertEqual(self.log.latest()["button"], "up")

    def test_a_nonsense_position_is_refused(self):
        for after in (-1, "3", None, 1.5, True):
            with self.subTest(after=after), self.assertRaises(ValueError):
                self.log.since(after)

    def test_an_implausible_limit_is_refused(self):
        for limit in (0, -1, "200", None, True, 10_001):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                ButtonLog(limit=limit)

    def test_health_describes_the_log(self):
        self.log.append("up")
        health = self.log.health()
        self.assertEqual((health["sequence"], health["retained"]), (1, 1))
        self.assertGreaterEqual(health["limit"], 1)

    def test_presses_from_several_threads_all_get_distinct_numbers(self):
        log = ButtonLog(limit=1000)
        def press():
            for _ in range(50):
                log.append("up")
        threads = [threading.Thread(target=press) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        numbers = [event["sequence"] for event in log.since(0)["events"]]
        self.assertEqual(len(numbers), 200)
        self.assertEqual(len(set(numbers)), 200)
        self.assertEqual(numbers, sorted(numbers))


class NavigationTests(unittest.TestCase):
    def test_the_directions_and_ok_are_navigation(self):
        for button in ("up", "down", "left", "right", "ok", "back", "home"):
            with self.subTest(button=button):
                self.assertIn(button, NAVIGATION)

    def test_service_buttons_are_not_navigation(self):
        for button in ("power", "volume_up", "digit_3", "nettv"):
            with self.subTest(button=button):
                self.assertNotIn(button, NAVIGATION)


if __name__ == "__main__":
    unittest.main()
