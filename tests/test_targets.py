import logging
import unittest

from pipertv.targets import (ACTIONABLE, UNPLACED, AtspiTargets, collect_targets,
                             target_point, valid_extent)

SCREEN = (1920, 1080)

logging.getLogger("pipertv.targets").addHandler(logging.NullHandler())


class Box:
    def __init__(self, x, y, width, height):
        self.x, self.y, self.width, self.height = x, y, width, height


class Node:
    """A stand-in for one pyatspi accessible object."""

    def __init__(self, role="filler", name="", box=None, children=(), broken=None,
                 states=("showing", "visible", "enabled")):
        self.role = role
        self.name = name
        self.box = box
        self.children = list(children)
        self.broken = broken
        self.states = states

    def getState(self):
        return self

    def getStates(self):
        return self.states

    def __iter__(self):
        if self.broken == "children":
            raise RuntimeError("the application closed")
        return iter(self.children)

    def getRoleName(self):
        if self.broken == "role":
            raise RuntimeError("the application closed")
        return self.role

    def queryComponent(self):
        if self.broken == "component" or self.box is None:
            raise LookupError("no Component interface")
        return self

    def getExtents(self, _coords):
        return self.box


def button(name, x, y, width=40, height=36):
    return Node("push button", name, Box(x, y, width, height))


class ExtentTests(unittest.TestCase):
    def test_a_real_on_screen_box_is_usable(self):
        self.assertTrue(valid_extent(Box(1698, 0, 38, 36), SCREEN))

    def test_the_unplaced_sentinel_is_not_a_coordinate(self):
        # AT-SPI reports INT32_MIN for hidden elements; the panel is full of them.
        self.assertFalse(valid_extent(Box(UNPLACED, UNPLACED, 1, 1), SCREEN))
        self.assertFalse(valid_extent(Box(0, UNPLACED, 10, 10), SCREEN))

    def test_empty_and_oversized_boxes_are_rejected(self):
        for box in (Box(10, 10, 0, 36), Box(10, 10, 40, 0), Box(10, 10, -5, 5),
                    Box(0, 0, 4000, 36), Box(0, 0, 40, 4000)):
            with self.subTest(box=(box.width, box.height)):
                self.assertFalse(valid_extent(box, SCREEN))

    def test_boxes_outside_the_screen_are_rejected(self):
        for box in (Box(1920, 10, 40, 36), Box(10, 1080, 40, 36), Box(-40, 10, 20, 36)):
            with self.subTest(box=(box.x, box.y)):
                self.assertFalse(valid_extent(box, SCREEN))

    def test_anything_without_a_box_is_rejected(self):
        self.assertFalse(valid_extent(None, SCREEN))
        self.assertFalse(valid_extent(object(), SCREEN))


class TargetPointTests(unittest.TestCase):
    def test_a_target_is_the_centre_of_its_box(self):
        point = target_point(button("Menu", 1698, 0, 38, 36), SCREEN)
        self.assertEqual((point["x"], point["y"]), (1698 + 19, 18))
        self.assertEqual(point["label"], "Menu")

    def test_an_object_without_a_component_is_skipped(self):
        self.assertIsNone(target_point(Node("push button", "x"), SCREEN))
        self.assertIsNone(target_point(Node("push button", "x", broken="component"), SCREEN))

    def test_an_unreadable_name_still_yields_a_point(self):
        # An application can close between reading its role and its name.
        class Nameless:
            box = Box(100, 100, 40, 40)

            @property
            def name(self):
                raise RuntimeError("the application closed")

            def getRoleName(self):
                return "push button"

            def queryComponent(self):
                return self

            def getExtents(self, _coords):
                return self.box

            def getState(self):
                return self

            def getStates(self):
                return ("showing", "visible", "enabled")

        point = target_point(Nameless(), SCREEN)
        self.assertEqual((point["x"], point["y"]), (120, 120))
        self.assertEqual(point["label"], "")

    def test_a_partly_offscreen_box_targets_its_visible_portion(self):
        point = target_point(button("Clipped", 1910, 1070, 40, 40), SCREEN)
        self.assertEqual((point["x"], point["y"]), (1915, 1075))

    def test_hidden_or_disabled_controls_are_not_targets_even_with_valid_bounds(self):
        for states in (("visible", "enabled"), ("showing", "visible"), ()):
            node = Node("push button", "Hidden", Box(100, 100, 40, 40), states=states)
            self.assertIsNone(target_point(node, SCREEN))


class CollectTests(unittest.TestCase):
    def setUp(self):
        # Shaped after the real wf-panel-pi tree read from the Pi.
        self.panel = Node("application", "wf-panel-pi", children=[
            Node("frame", "", Box(0, 0, 1920, 36), children=[
                Node("filler", "", Box(0, 0, 1920, 36), children=[
                    button("Menu", 0, 0, 50, 36),
                    Node("filler", "", Box(50, 0, 1, 36)),
                    Node("scroll pane", "", Box(UNPLACED, UNPLACED, 1, 1)),
                    button("", 1698, 0, 38, 36),
                    button("13:43", 1858, 0, 58, 36),
                    Node("push button", "hidden", Box(UNPLACED, UNPLACED, 1, 1)),
                ]),
            ]),
        ])

    def test_only_clickable_things_with_real_positions_are_offered(self):
        found = collect_targets(self.panel, SCREEN)
        self.assertEqual([point["label"] for point in found], ["Menu", "", "13:43"])
        self.assertTrue(all(0 <= point["x"] < 1920 for point in found))
        self.assertTrue(all(point["role"] == "push button" for point in found))

    def test_containers_are_walked_but_never_offered(self):
        roles = {point["role"] for point in collect_targets(self.panel, SCREEN)}
        self.assertNotIn("filler", roles)
        self.assertNotIn("frame", roles)

    def test_desktop_icons_would_be_offered_if_an_app_exposed_them(self):
        # pcmanfm is hidden by NO_AT_BRIDGE today; this is the shape it would take.
        files = Node("application", "pcmanfm", children=[
            Node("frame", "Desktop", Box(0, 0, 1920, 1080), children=[
                Node("icon", "Trash", Box(40, 40, 64, 64)),
                Node("icon", "Home", Box(40, 140, 64, 64)),
            ]),
        ])
        found = collect_targets(files, SCREEN)
        self.assertEqual([(point["label"], point["x"], point["y"]) for point in found],
                         [("Trash", 72, 72), ("Home", 72, 172)])

    def test_the_walk_is_bounded_by_depth(self):
        deep = button("deep", 10, 10)
        for _ in range(20):
            deep = Node("filler", "", Box(0, 0, 100, 100), children=[deep])
        self.assertEqual(collect_targets(deep, SCREEN, max_depth=3), [])
        self.assertTrue(collect_targets(deep, SCREEN, max_depth=30))

    def test_the_walk_is_bounded_by_node_count(self):
        wide = Node("filler", "", Box(0, 0, 100, 100),
                    children=[button(str(index), index, 0, 2, 2) for index in range(100)])
        found = collect_targets(wide, SCREEN, max_nodes=10)
        self.assertEqual(len(found), 10)

    def test_a_large_child_iterator_is_not_materialized_before_budgeting(self):
        reads = []

        class HugeTree:
            def __iter__(self):
                for index in range(10000):
                    reads.append(index)
                    yield button(str(index), index, 0, 2, 2)

        self.assertEqual(len(collect_targets(HugeTree(), SCREEN, max_nodes=10)), 10)
        self.assertEqual(len(reads), 10)

    def test_a_slow_tree_stops_at_the_scan_deadline(self):
        now = [0.0]

        class SlowTree:
            def __iter__(self):
                for index in range(100):
                    now[0] += .1
                    yield button(str(index), index, 0, 2, 2)

        found = collect_targets(SlowTree(), SCREEN, deadline=.3, clock=lambda: now[0])
        self.assertLessEqual(len(found), 3)

    def test_an_application_closing_mid_walk_does_not_lose_everything(self):
        tree = Node("application", "mixed", children=[
            button("before", 10, 10),
            Node("filler", "", Box(0, 0, 10, 10), broken="children"),
            Node("push button", "unreadable", Box(0, 0, 10, 10), broken="role"),
            button("after", 20, 20),
        ])
        found = collect_targets(tree, SCREEN)
        self.assertEqual([point["label"] for point in found], ["before", "after"])

    def test_every_actionable_role_is_offered(self):
        tree = Node("application", "roles", children=[
            Node(role, role, Box(100, 100, 20, 20)) for role in sorted(ACTIONABLE)])
        self.assertEqual(len(collect_targets(tree, SCREEN)), len(ACTIONABLE))


class FakeRegistry:
    def __init__(self, applications, fail=False):
        self.applications = applications
        self.fail = fail
        self.reads = 0

    def getDesktop(self, _index):
        self.reads += 1
        if self.fail:
            raise RuntimeError("the accessibility bus is not running")
        return Node("desktop", "main", children=self.applications)


class AtspiTargetsTests(unittest.TestCase):
    def setUp(self):
        self.now = [0.0]
        self.window = Node("frame", "Panel", Box(0, 0, 1920, 36),
                           children=[button("Menu", 0, 0, 50, 36)],
                           states=("active", "showing", "visible", "enabled"))
        self.panel = Node("application", "wf-panel-pi",
                          children=[self.window])

    def build(self, registry):
        return AtspiTargets(SCREEN, cache_s=1.5, clock=lambda: self.now[0],
                            registry=registry)

    def test_background_windows_do_not_supply_targets_at_covered_coordinates(self):
        registry = FakeRegistry([self.panel,
                                 Node("application", "other",
                                      children=[Node("frame", "Background", Box(0, 0, 800, 600),
                                                     children=[button("Other", 500, 500)])])])
        found = self.build(registry).targets()
        self.assertEqual([point["label"] for point in found], ["Menu"])

    def test_no_active_window_means_no_assumed_foreground_targets(self):
        self.window.states = ("showing", "visible", "enabled")
        targets = self.build(FakeRegistry([self.panel]))
        self.assertEqual(targets.targets(), [])
        self.assertEqual(targets.health()["scope"], "active accessible window")

    def test_an_active_child_dialog_is_used_instead_of_its_background_parent(self):
        self.window.states = ("showing", "visible", "enabled")
        self.window.children.append(Node("dialog", "Dialog", Box(100, 100, 400, 300),
                                         children=[button("Confirm", 200, 200)],
                                         states=("active", "showing", "visible", "enabled")))
        targets = self.build(FakeRegistry([self.panel])).targets()
        self.assertEqual([target["label"] for target in targets], ["Confirm"])

    def test_repeated_presses_do_not_re_walk_the_desktop(self):
        registry = FakeRegistry([self.panel])
        targets = self.build(registry)
        targets.targets()
        targets.targets()
        self.assertEqual(registry.reads, 1)
        # The cache expires, so a moved window is picked up shortly after.
        self.now[0] = 2.0
        targets.targets()
        self.assertEqual(registry.reads, 2)

    def test_invalidating_forces_a_fresh_read(self):
        registry = FakeRegistry([self.panel])
        targets = self.build(registry)
        targets.targets()
        targets.invalidate()
        targets.targets()
        self.assertEqual(registry.reads, 2)

    def test_a_cached_target_is_rechecked_after_its_window_moves_or_hides(self):
        node = button("Move", 100, 100)
        self.window.children = [node]
        targets = self.build(FakeRegistry([self.panel]))
        target = targets.targets()[0]
        node.box.x = 400
        self.assertEqual(targets.resolve(target)["x"], 420)
        node.states = ("visible", "enabled")
        self.assertIsNone(targets.resolve(target))

    def test_a_cached_target_is_rejected_when_its_window_loses_activation(self):
        targets = self.build(FakeRegistry([self.panel]))
        target = targets.targets()[0]
        self.window.states = ("showing", "visible", "enabled")
        self.assertIsNone(targets.resolve(target))

    def test_a_stopped_bus_yields_no_targets_instead_of_raising(self):
        targets = self.build(FakeRegistry([], fail=True))
        self.assertEqual(targets.targets(), [])
        health = targets.health()
        self.assertFalse(health["ok"])
        self.assertIn("accessibility bus", health["error"])

    def test_an_empty_desktop_explains_why_nothing_is_offered(self):
        targets = self.build(FakeRegistry([]))
        self.assertEqual(targets.targets(), [])
        health = targets.health()
        self.assertTrue(health["ok"])
        self.assertIn("NO_AT_BRIDGE", health["error"])

    def test_health_counts_what_was_found(self):
        targets = self.build(FakeRegistry([self.panel]))
        targets.targets()
        health = targets.health()
        self.assertEqual((health["applications"], health["targets"]), (1, 1))
        self.assertEqual(health["source"], "accessibility")
        self.assertIsNone(health["error"])


if __name__ == "__main__":
    unittest.main()
