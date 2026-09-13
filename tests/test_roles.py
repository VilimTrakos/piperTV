import unittest

from pipertv.roles import ROLES, SUGGESTED, RoleMap, validate_roles
from pipertv.tv import NAVIGATION


class RoleCatalogueTests(unittest.TestCase):
    def test_roles_are_exactly_what_the_interface_acts_on(self):
        # Otherwise a role could be bound that nothing downstream performs.
        self.assertEqual(set(ROLES), set(NAVIGATION))


class SuggestionTests(unittest.TestCase):
    def test_the_offered_arrangement_is_itself_a_legal_map(self):
        # It is offered for one-click application, so it must never be
        # something the store would then refuse to save.
        self.assertEqual(validate_roles(SUGGESTED), dict(SUGGESTED))

    def test_the_suggestion_moves_every_key_it_touches_off_the_tv_keys(self):
        for role, button in SUGGESTED.items():
            with self.subTest(role=role):
                self.assertNotEqual(role, button)

    def test_the_suggested_cross_can_be_applied_and_translates(self):
        roles = RoleMap(SUGGESTED)
        self.assertEqual(roles.action("play"), "up")
        self.assertEqual(roles.action("pause"), "ok")
        self.assertIsNone(roles.action("up"), "the TV's own cursor key goes quiet")


class ValidationTests(unittest.TestCase):
    def test_an_empty_map_is_valid(self):
        self.assertEqual(validate_roles({}), {})

    def test_a_role_may_be_bound_to_a_button_the_tv_ignores(self):
        self.assertEqual(validate_roles({"up": "play"}), {"up": "play"})

    def test_an_unknown_role_is_refused(self):
        with self.assertRaisesRegex(ValueError, "Unknown role"):
            validate_roles({"scroll": "play"})

    def test_an_unknown_button_is_refused(self):
        with self.assertRaisesRegex(ValueError, "remote button"):
            validate_roles({"up": "nonexistent"})

    def test_two_roles_cannot_share_one_button(self):
        with self.assertRaisesRegex(ValueError, "only one role"):
            validate_roles({"up": "play", "down": "play"})

    def test_a_role_cannot_be_bound_to_another_navigation_key(self):
        with self.assertRaisesRegex(ValueError, "another navigation key"):
            validate_roles({"up": "down"})

    def test_binding_a_role_to_its_own_key_is_allowed(self):
        self.assertEqual(validate_roles({"up": "up"}), {"up": "up"})

    def test_a_non_object_is_refused(self):
        for value in ([], "up", 3, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_roles(value)


class TranslationTests(unittest.TestCase):
    def test_without_bindings_navigation_keys_act_as_themselves(self):
        roles = RoleMap()
        for role in ROLES:
            with self.subTest(role=role):
                self.assertEqual(roles.action(role), role)

    def test_a_bound_button_performs_its_role(self):
        roles = RoleMap({"up": "play", "ok": "pause"})
        self.assertEqual(roles.action("play"), "up")
        self.assertEqual(roles.action("pause"), "ok")

    def test_a_rebound_role_stops_answering_on_its_old_key(self):
        # The whole point: the TV's own cursor key must go quiet.
        roles = RoleMap({"up": "play"})
        self.assertIsNone(roles.action("up"))

    def test_roles_left_alone_keep_working(self):
        roles = RoleMap({"up": "play"})
        self.assertEqual(roles.action("down"), "down")

    def test_a_button_with_no_role_performs_nothing(self):
        roles = RoleMap({"up": "play"})
        for button in ("volume_up", "power", "digit_3", "nettv"):
            with self.subTest(button=button):
                self.assertIsNone(roles.action(button))

    def test_an_unknown_signal_performs_nothing(self):
        self.assertIsNone(RoleMap().action("not-a-button"))


class ReportingTests(unittest.TestCase):
    def test_button_for_reports_the_carrier_of_a_role(self):
        roles = RoleMap({"up": "play"})
        self.assertEqual(roles.button_for("up"), "play")
        self.assertEqual(roles.button_for("down"), "down")

    def test_button_for_refuses_an_unknown_role(self):
        with self.assertRaisesRegex(ValueError, "Unknown role"):
            RoleMap().button_for("scroll")

    def test_describe_lists_every_role_and_marks_the_moved_ones(self):
        described = {entry["role"]: entry for entry in RoleMap({"up": "play"}).describe()}
        self.assertEqual(set(described), set(ROLES))
        self.assertTrue(described["up"]["rebound"])
        self.assertEqual(described["up"]["button"], "play")
        self.assertFalse(described["down"]["rebound"])

    def test_bindings_cannot_be_edited_through_the_accessor(self):
        roles = RoleMap({"up": "play"})
        roles.bindings()["up"] = "tampered"
        self.assertEqual(roles.action("play"), "up")

    def test_invalid_bindings_are_refused_at_construction(self):
        with self.assertRaises(ValueError):
            RoleMap({"up": "down"})


if __name__ == "__main__":
    unittest.main()
