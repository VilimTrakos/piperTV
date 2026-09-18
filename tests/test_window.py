import unittest

from pipertv.window import WINDOW_DEFAULTS, geometry, validate_window

SCREEN = (1920, 1080)


class ValidateWindowTests(unittest.TestCase):
    def test_nothing_asked_for_is_the_whole_screen(self):
        self.assertEqual(validate_window({}), WINDOW_DEFAULTS)
        self.assertFalse(validate_window({})["windowed"])

    def test_one_field_can_be_sent_on_its_own(self):
        self.assertEqual(validate_window({"windowed": True}),
                         dict(WINDOW_DEFAULTS, windowed=True))

    def test_a_size_is_kept_in_whole_pixels(self):
        self.assertEqual(validate_window({"width": 1600.7})["width"], 1600)

    def test_an_unknown_setting_says_what_the_settings_are(self):
        with self.assertRaises(ValueError) as refused:
            validate_window({"fullscreen": True})
        self.assertIn("windowed", str(refused.exception))

    def test_windowed_is_a_yes_or_no(self):
        for value in ("yes", 1, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_window({"windowed": value})

    def test_a_size_outside_its_range_is_refused_rather_than_clamped(self):
        for values in ({"width": 10}, {"height": 1}, {"width": 99999}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate_window(values)

    def test_anything_that_is_not_a_map_of_settings_is_refused(self):
        with self.assertRaises(ValueError):
            validate_window("windowed")


class GeometryTests(unittest.TestCase):
    def test_full_screen_is_the_screen_at_its_corner(self):
        self.assertEqual(geometry({}, SCREEN), ((1920, 1080), (0, 0)))

    def test_a_window_is_centred_on_the_screen(self):
        size, position = geometry({"windowed": True, "width": 1280, "height": 720}, SCREEN)
        self.assertEqual(size, (1280, 720))
        self.assertEqual(position, (320, 180))

    def test_a_window_never_hangs_off_the_screen_it_has_to_fit(self):
        # Its title bar would be above the top of the screen, and nothing could
        # bring it back.
        size, position = geometry({"windowed": True, "width": 3000, "height": 2000},
                                  SCREEN)
        self.assertEqual(size, SCREEN)
        self.assertEqual(position, (0, 0))

    def test_an_unusable_preference_is_not_a_reason_to_fail(self):
        self.assertEqual(geometry(None, SCREEN), ((1920, 1080), (0, 0)))


if __name__ == "__main__":
    unittest.main()
