import logging
import unittest

from pipertv.focus import FRESH_S, FocusWatcher

logging.getLogger("pipertv.focus").addHandler(logging.NullHandler())


class FakeSource:
    """An accessible with the ancestry a page or the browser would give it."""

    def __init__(self, role, name="", ancestors=("section", "document web", "frame")):
        self.role = role
        self.name = name
        self._ancestors = list(ancestors)

    def getRoleName(self):
        return self.role

    @property
    def parent(self):
        if not self._ancestors:
            return None
        return FakeSource(self._ancestors[0], ancestors=self._ancestors[1:])


class FakeEvent:
    def __init__(self, role, name="", detail1=1, type="object:state-changed:focused",
                 ancestors=("section", "document web", "frame")):
        self.source = FakeSource(role, name, ancestors)
        self.detail1 = detail1
        self.type = type


class FakeRegistry:
    def __init__(self):
        self.listeners = []
        self.started = self.stopped = 0

    def registerEventListener(self, callback, event):
        self.listeners.append((callback, event))

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FocusWatcherTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.opened = []
        self.watcher = FocusWatcher(on_text_field=self.opened.append,
                                    registry=FakeRegistry(), clock=self.clock)

    def test_a_search_box_gaining_focus_is_worth_a_keyboard(self):
        self.watcher.observe(FakeEvent("entry", "Search"))
        field = self.watcher.typing_into()
        self.assertEqual((field["role"], field["label"]), ("entry", "Search"))
        self.assertEqual(len(self.opened), 1)

    def test_a_box_with_suggestions_counts_too(self):
        # What a browser calls the search box on a page that autocompletes.
        self.watcher.observe(FakeEvent("combo box", "Search with DuckDuckGo"))
        self.assertIsNotNone(self.watcher.typing_into())

    def test_focus_moving_to_anything_else_is_not_a_request_to_type(self):
        for role in ("push button", "link", "document web", "heading", "list item"):
            with self.subTest(role=role):
                self.watcher.observe(FakeEvent(role, "something"))
                self.assertIsNone(self.watcher.typing_into())
        self.assertEqual(self.opened, [])

    def test_losing_focus_is_not_gaining_it(self):
        self.watcher.observe(FakeEvent("entry", "Search", detail1=0))
        self.assertIsNone(self.watcher.typing_into())

    def test_a_caret_moving_in_a_field_counts_as_being_in_it(self):
        self.watcher.observe(FakeEvent("entry", "Search", detail1=-1,
                                       type="object:text-caret-moved"))
        self.assertIsNotNone(self.watcher.typing_into())

    def test_the_same_field_reported_again_does_not_reopen_the_keyboard(self):
        for _ in range(4):
            self.watcher.observe(FakeEvent("entry", "Search"))
        self.assertEqual(len(self.opened), 1)

    def test_moving_to_another_field_is_a_new_request(self):
        self.watcher.observe(FakeEvent("entry", "Search"))
        self.watcher.observe(FakeEvent("entry", "Address"))
        self.assertEqual([field["label"] for field in self.opened], ["Search", "Address"])

    def test_a_field_left_alone_long_enough_is_no_longer_in_hand(self):
        self.watcher.observe(FakeEvent("entry", "Search"))
        self.clock.now += FRESH_S + 1
        self.assertIsNone(self.watcher.typing_into())

    def test_forgetting_drops_it_at_once(self):
        self.watcher.observe(FakeEvent("entry", "Search"))
        self.watcher.forget()
        self.assertIsNone(self.watcher.typing_into())

    def test_the_browsers_own_address_bar_is_not_a_page_asking_to_be_typed_into(self):
        # It takes focus the moment a window opens, and in kiosk mode it is not
        # even on the screen: a keyboard for it types into nothing anyone sees.
        self.watcher.observe(FakeEvent("entry", "Address and search bar",
                                       ancestors=("tool bar", "panel", "frame")))
        self.assertIsNone(self.watcher.typing_into())
        self.assertEqual(self.opened, [])

    def test_a_field_inside_the_page_is(self):
        self.watcher.observe(FakeEvent("combo box", "Search with DuckDuckGo",
                                       ancestors=("landmark", "section", "document web")))
        self.assertIsNotNone(self.watcher.typing_into())

    def test_an_event_that_cannot_be_read_is_ignored(self):
        class Broken:
            type = "object:state-changed:focused"
            detail1 = 1

            @property
            def source(self):
                raise RuntimeError("it went away mid-event")

        self.watcher.observe(Broken())  # must not raise
        self.assertIsNone(self.watcher.typing_into())

    def test_a_desktop_without_the_bus_reports_nothing_rather_than_failing(self):
        class Missing:
            def registerEventListener(self, *_args):
                raise ImportError("no pyatspi here")

        watcher = FocusWatcher(registry=Missing())
        watcher._run()
        self.assertIsNone(watcher.typing_into())
        self.assertFalse(watcher.health()["ok"])

    def test_listening_covers_both_ways_a_field_announces_itself(self):
        registry = FakeRegistry()
        watcher = FocusWatcher(registry=registry)
        watcher._run()
        self.assertEqual({event for _callback, event in registry.listeners},
                         {"object:state-changed:focused", "object:text-caret-moved"})
        self.assertEqual(registry.started, 1)


if __name__ == "__main__":
    unittest.main()
