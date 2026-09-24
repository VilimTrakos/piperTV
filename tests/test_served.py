import logging
import unittest
from pathlib import Path
import tempfile

from pipertv.app import create_app, main
from pipertv.desktop import POINTER_DEFAULTS
from pipertv.served import ServedRemote, pages

logging.getLogger("pipertv.served").addHandler(logging.NullHandler())


class FakeController:
    """Stands in for the reader that turns infrared into button names."""

    def __init__(self):
        self.resumed = self.paused = self.closed = self.reloaded = 0
        self.device = "/dev/lirc0"

    def resume(self):
        self.resumed += 1

    def pause(self):
        self.paused += 1

    def close(self):
        self.closed += 1

    def use(self, device):
        self.device = device

    def reload_recordings(self):
        self.reloaded += 1

    def health(self):
        return {"ok": True, "device": self.device}


class FakeStore:
    def __init__(self, roles=None, pointer=None):
        self._roles = roles
        self._pointer = pointer

    def snapshot(self):
        return {"recordings": {}, "roles": self._roles}

    def pointer(self):
        return self._pointer


def build(store=None, **kwargs):
    controller = kwargs.pop("controller", None) or FakeController()
    return ServedRemote(store or FakeStore(), controller=controller, **kwargs), controller


class PressTests(unittest.TestCase):
    def test_a_press_arrives_at_the_page_under_what_it_performs(self):
        # The remote sent "play"; the interface is asked to go up, because that
        # is where the role was moved to on a television that acts on its own
        # arrow keys.
        remote, _ = build(FakeStore(roles={"up": "play"}))
        remote._press("play")
        event = remote.events()["events"][0]
        self.assertEqual(event["button"], "play")
        self.assertEqual(event["action"], "up")
        self.assertTrue(event["navigation"])

    def test_a_key_the_role_was_taken_from_no_longer_navigates(self):
        remote, _ = build(FakeStore(roles={"up": "play"}))
        remote._press("up")
        event = remote.events()["events"][0]
        self.assertIsNone(event["action"])
        self.assertFalse(event["navigation"])
        self.assertFalse(remote._is_direction("up"))
        self.assertTrue(remote._is_direction("play"))

    def test_the_page_is_given_only_what_it_has_not_seen(self):
        remote, _ = build()
        remote._press("up")
        remote._press("down")
        feed = remote.events(after=1)
        self.assertEqual([event["button"] for event in feed["events"]], ["down"])
        self.assertEqual(feed["sequence"], 2)

    def test_every_press_counts_because_there_is_no_gate_here(self):
        # The desktop's gate exists to keep a press off this Pi's cursor. A
        # press in served mode reaches a web page and nothing else.
        remote, _ = build()
        for button in ("up", "ok", "power"):
            remote._press(button)
        self.assertEqual(len(remote.events()["events"]), 3)


class FeedTests(unittest.TestCase):
    def test_the_feed_says_the_interface_is_somewhere_else(self):
        remote, _ = build()
        feed = remote.events()
        self.assertTrue(feed["served"])
        self.assertEqual(feed["mode"], "piper")
        self.assertEqual(feed["control"], "on")
        self.assertTrue(feed["session_id"])
        self.assertIsNone(feed["hold"])

    def test_one_visit_lasts_as_long_as_the_server_does(self):
        # There is no television input to come and go, so the page is never
        # asked to establish itself again.
        remote, _ = build()
        self.assertEqual(remote.events()["session_id"], remote.events()["session_id"])

    def test_nothing_is_running_here_because_the_browser_opens_it(self):
        remote, _ = build()
        services = remote.events()["services"]
        self.assertTrue(services["available"])
        self.assertIsNone(services["running"])
        self.assertEqual(services["history"], [])

    def test_the_catalogue_carries_where_each_service_lives(self):
        remote, _ = build()
        offered = {service["id"]: service for service in remote.events()["services"]["services"]}
        self.assertIn("youtube", offered)
        self.assertTrue(offered["youtube"]["url"].startswith("https://"))

    def test_what_plays_on_the_pi_itself_is_not_offered(self):
        # Kodi decodes video in the Pi's own hardware, which is the whole
        # reason it has a tile; a laptop has nothing to show of it.
        self.assertNotIn("kodi", [service["id"] for service in pages()])
        self.assertTrue(all(service["url"] for service in pages()))


class RecordingTests(unittest.TestCase):
    def test_learning_a_button_stands_the_remote_down(self):
        remote, controller = build()
        state = remote.hold()
        self.assertEqual(state["control"], "off")
        self.assertTrue(state["hold"])
        self.assertEqual(controller.paused, 1)
        self.assertEqual(remote.events()["control"], "off")

    def test_the_remote_comes_back_when_the_recording_is_over(self):
        remote, controller = build()
        remote.hold()
        state = remote.release()
        self.assertEqual(state["control"], "on")
        self.assertIsNone(state["hold"])
        self.assertEqual(controller.resumed, 1)


class SettingsTests(unittest.TestCase):
    def test_a_held_direction_repeats_at_the_pace_that_was_set(self):
        remote, _ = build(FakeStore(pointer={"hold_delay_s": 1.2, "hold_interval_s": 0.4}))
        self.assertEqual(remote._hold_pace("up"), (1.2, 0.4))

    def test_unusable_settings_leave_the_remote_working(self):
        # A hand-edited library must not be the reason a remote stops working:
        # the remote is what people have in their hand when they find out.
        remote, _ = build(FakeStore(roles="nonsense", pointer="nonsense"))
        self.assertEqual(remote._hold_pace("up"), (POINTER_DEFAULTS["hold_delay_s"],
                                                   POINTER_DEFAULTS["hold_interval_s"]))
        remote._press("up")
        self.assertEqual(remote.events()["events"][0]["action"], "up")

    def test_the_receiver_can_be_moved_to_another_pin(self):
        remote, controller = build()
        remote.use_receiver("/dev/gpiochip0:18")
        self.assertEqual(controller.device, "/dev/gpiochip0:18")

    def test_an_edited_recording_reloads_the_bindings_with_the_signals(self):
        remote, controller = build()
        remote.reload_recordings()
        self.assertEqual(controller.reloaded, 1)

    def test_starting_and_closing_follow_the_reader(self):
        remote, controller = build()
        remote.start()
        self.assertEqual(controller.resumed, 1)
        self.assertTrue(remote._listening())
        remote.close()
        self.assertEqual(controller.closed, 1)
        self.assertFalse(remote._listening())


class TheOtherScreenTests(unittest.TestCase):
    """Everything that only means something on the Pi's own screen."""

    def test_each_one_says_so_rather_than_pretending(self):
        remote, _ = build()
        asks = [lambda: remote.snapshot(),
                lambda: remote.choose("pointer", "abc"),
                lambda: remote.manual(True),
                lambda: remote.stop(),
                lambda: remote.launch("youtube", "abc"),
                lambda: remote.stop_service(),
                lambda: remote.leave(),
                lambda: remote.open_keyboard(),
                lambda: remote.close_keyboard(),
                lambda: remote.show_interface()]
        for ask in asks:
            with self.assertRaises(KeyError) as caught:
                ask()
            self.assertIn("browser", str(caught.exception))


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.remote = ServedRemote(FakeStore(), controller=FakeController())
        app = create_app(data=Path(self.temporary.name) / "recordings.json",
                         demo=True, remote=self.remote)
        self.client = app.test_client()

    def test_the_interface_gets_its_feed(self):
        response = self.client.get("/api/tv/events?after=0")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["served"])

    def test_the_page_itself_is_served_to_any_browser_on_the_network(self):
        self.assertEqual(self.client.get("/tv").status_code, 200)

    def test_desktop_control_answers_as_a_server_without_one(self):
        # The studio already knows what to do with this: it is the same answer
        # a server started with --no-control gives.
        for path in ("/api/control",):
            self.assertEqual(self.client.get(path).status_code, 404)
        for path in ("/api/tv/launch", "/api/tv/close", "/api/tv/leave"):
            response = self.client.post(path, json={})
            self.assertEqual(response.status_code, 404)
            self.assertIn("browser", response.get_json()["error"])

    def test_health_reports_the_receiver_it_is_listening_on(self):
        control = self.client.get("/api/health").get_json()["control"]
        self.assertTrue(control["served"])
        self.assertTrue(control["receiver"]["ok"])


class CommandLineTests(unittest.TestCase):
    def test_serving_the_interface_and_refusing_the_remote_are_opposites(self):
        with self.assertRaises(SystemExit):
            main(["--served", "--no-control"])


if __name__ == "__main__":
    unittest.main()
