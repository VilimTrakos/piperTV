import json
import logging
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from pipertv.app import create_app, main, say_what_happens
from pipertv.session import ControlSession
from pipertv.tv import ButtonLog

from tests.test_control import FakeLauncher


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "recordings.json"
        self.app = create_app(data=self.path, demo=True)
        self.client = self.app.test_client()
        self.workbench = self.app.extensions["pipertv"]

    def tearDown(self):
        self.workbench.close()
        self.workbench.backend.close()
        self.temporary.cleanup()

    def wait_capture(self, job_id):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.client.get("/api/captures/" + job_id).get_json()
            if result["status"] in {"captured", "cancelled", "timeout", "error"}:
                return result
            time.sleep(.025)
        self.fail("Capture did not finish")

    def test_local_pi_state_health_and_static_ui(self):
        response = self.client.get("/api/state", base_url="http://192.168.1.24:8765")
        self.assertEqual(response.status_code, 200)
        state = response.get_json()
        self.assertEqual(state["mode"], "demo")
        self.assertTrue(state["buttons"])
        self.assertTrue(self.client.get("/api/health").get_json()["ok"])
        for path in ("/", "/index.html", "/style.css"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
                response.close()

    def test_capture_save_rename_export_delete_and_reload(self):
        response = self.client.post("/api/captures", json={"button_id": "power"})
        self.assertEqual(response.status_code, 202)
        job = self.wait_capture(response.get_json()["id"])
        self.assertEqual(job["status"], "captured", job)
        self.assertEqual(job["signal"]["source"], "demo")
        stored = json.loads(self.path.read_text())
        self.assertEqual(len(stored["recordings"]["power"]["samples"]), 1)

        response = self.client.put("/api/buttons/power", json={"label": "Standby"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["recordings"]["power"]["label"], "Standby")
        exported = self.client.get("/api/export")
        self.assertEqual(exported.status_code, 200)
        self.assertIn("attachment", exported.headers["Content-Disposition"])
        self.assertEqual(exported.get_json()["recordings"]["power"]["label"], "Standby")
        raw = self.client.get("/api/export/power?format=irctl&sample=0")
        self.assertEqual(raw.status_code, 200)
        self.assertIn(b"carrier 38000\npulse 9000", raw.data)

        reloaded = create_app(data=self.path, demo=True)
        try:
            state = reloaded.test_client().get("/api/state").get_json()
            self.assertEqual(len(state["recordings"]["power"]["samples"]), 1)
        finally:
            reloaded.extensions["pipertv"].close()
            reloaded.extensions["pipertv"].backend.close()

        response = self.client.delete("/api/buttons/power/samples/0", json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["recordings"]["power"]["samples"], [])

    def test_cancel_then_immediately_record_again(self):
        job = self.client.post("/api/captures", json={"button_id": "power"}).get_json()
        self.assertEqual(self.client.post("/api/captures", json={"button_id": "power"}).status_code, 409)
        response = self.client.post("/api/captures/" + job["id"] + "/cancel", json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.wait_capture(job["id"])["status"], "cancelled")
        self.assertEqual(self.client.post("/api/captures", json={"button_id": "power"}).status_code, 202)

    def test_origin_matches_the_pi_lan_url(self):
        base_url = "http://192.168.1.24:8765"
        allowed = self.client.put("/api/buttons/power", json={"label": "Standby"},
                                  base_url=base_url, headers={"Origin": base_url})
        self.assertEqual(allowed.status_code, 200)
        rejected = self.client.put("/api/buttons/power", json={"label": "Other"},
                                   base_url=base_url, headers={"Origin": "https://example.com"})
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(self.workbench.store.snapshot()["recordings"]["power"]["label"], "Standby")

    def test_cross_site_and_dns_rebinding_requests_are_rejected(self):
        response = self.client.get("/api/state", base_url="http://attacker.example:8765")
        self.assertEqual(response.status_code, 403)
        response = self.client.post("/api/captures", json={"button_id": "power"},
                                    headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(response.status_code, 403)

    def test_invalid_json_body_and_large_body_have_json_errors(self):
        cases = [
            self.client.post("/api/captures", data='{"button_id":"power"}'),
            self.client.post("/api/captures", data="{", content_type="application/json"),
            self.client.post("/api/captures", json=[]),
            self.client.post("/api/captures", json={"button_id": "unknown"}),
        ]
        for response in cases:
            self.assertEqual(response.status_code, 400)
            self.assertIn("error", response.get_json())
        response = self.client.post("/api/captures", json={"padding": "x" * 32768})
        self.assertEqual(response.status_code, 413)
        self.assertIn("error", response.get_json())

    def test_missing_hardware_explains_setup(self):
        app = create_app(data=self.path, device="/missing/ir/device")
        workbench = app.extensions["pipertv"]
        try:
            health = app.test_client().get("/api/health").get_json()
            self.assertFalse(health["ok"])
            self.assertIn("gpio-ir", health["error"])
            self.assertFalse(health["device_exists"])
        finally:
            workbench.close()
            workbench.backend.close()

    def test_unknown_routes_and_export_arguments(self):
        self.assertEqual(self.client.get("/api/unknown").status_code, 404)
        self.assertEqual(self.client.get("/api/captures/missing").status_code, 404)
        self.assertEqual(self.client.get("/api/export/power?format=other").status_code, 400)
        self.assertEqual(self.client.get("/api/export/power?sample=not-a-number").status_code, 400)

    def test_cli_uses_one_process_without_debugger_or_reloader(self):
        # Running it configures logging for the whole process; put that back,
        # or every test after this one writes Piper's account to the screen.
        root = logging.getLogger()
        handlers, level = list(root.handlers), root.level
        self.addCleanup(root.setLevel, level)
        self.addCleanup(setattr, root, "handlers", handlers)
        with patch("pipertv.app.create_app", return_value=self.app), patch.object(self.app, "run") as run:
            main(["--demo", "--host", "127.0.0.1", "--port", "8765", "--data", str(self.path)])
        run.assert_called_once_with(host="127.0.0.1", port=8765, threaded=True,
                                    processes=1, debug=False, use_debugger=False, use_reloader=False)


class FakeRemote:
    """A real control gate with the devices and detector stubbed out."""

    def __init__(self):
        self.session = ControlSession()
        self.buttons = ButtonLog()
        self.launcher = FakeLauncher()
        self.started = self.closed = self.held = self.released = 0
        self.reloaded = 0

    def use_receiver(self, device):
        self.listening_on = device
        return {"receiver": str(device)}

    @property
    def controller(self):
        remote = self

        class Heard:
            def health(self):
                return {"listening": True, "error": None,
                        "device": getattr(remote, "listening_on", "/dev/lirc0")}

        return Heard()

    def events(self, after=0):
        result = self.buttons.since(after)
        state = self.session.snapshot()
        result["mode"] = state["mode"]
        result["control"] = state["control"]
        result["session_id"] = state["session"]["id"] if state["session"] else None
        result["services"] = self.launcher.snapshot()
        return result

    def launch(self, service, session_id):
        state = self.session.snapshot()
        if state["control"] != "on" or state["mode"] != "piper":
            raise RuntimeError("The TV is not showing the Pi, so Piper cannot open anything on it.")
        if not state["session"] or session_id != state["session"]["id"]:
            raise RuntimeError("That belongs to an earlier visit to the Pi's input.")
        return self.launcher.launch(service)

    def stop_service(self):
        return self.launcher.stop()

    def leave(self):
        self.left = getattr(self, "left", 0) + 1
        return {"mode": "pointer", "interface": {"showing": False}}

    def open_keyboard(self):
        self.keyboard_opened = getattr(self, "keyboard_opened", 0) + 1
        return {"showing": True}

    def close_keyboard(self, text=None):
        self.keyboard_closed = getattr(self, "keyboard_closed", 0) + 1
        return {"showing": False}

    def reload_recordings(self):
        self.reloaded += 1

    def reload_roles(self):
        self.rebound = getattr(self, "rebound", 0) + 1
        return {}

    def reload_window(self):
        self.reshaped = getattr(self, "reshaped", 0) + 1
        return {}

    def show_interface(self):
        self.shown = getattr(self, "shown", 0) + 1
        return {"port": 8765, "showing": True, "error": None}

    def start(self):
        self.started += 1

    def close(self):
        self.closed += 1

    def hold(self, reason="Recording a remote button."):
        self.held += 1
        return self.session.hold(reason)

    def release(self):
        self.released += 1
        return self.session.release()

    def snapshot(self):
        return dict(self.session.snapshot(), detection={"state": "active", "reason": "test"})

    def choose(self, mode, session_id):
        return self.session.choose(mode, session_id)

    def manual(self, confirmed):
        return self.session.start_manual(confirmed)

    def stop(self):
        return self.session.stop()

    def health(self):
        return {"screen": [1920, 1080], "pointer": {"ok": True}}


class LoggingTests(unittest.TestCase):
    """What the Pi writes down about itself while it runs."""

    def setUp(self):
        root = logging.getLogger()
        self.handlers = list(root.handlers)
        self.level = root.level
        self.werkzeug = logging.getLogger("werkzeug").level
        self.addCleanup(self.restore)

    def restore(self):
        root = logging.getLogger()
        root.handlers = self.handlers
        root.setLevel(self.level)
        logging.getLogger("werkzeug").setLevel(self.werkzeug)

    def test_pipers_own_account_of_a_press_is_kept(self):
        say_what_happens()
        self.assertTrue(logging.getLogger("pipertv.control").isEnabledFor(logging.INFO))

    def test_the_interfaces_four_requests_a_second_are_not(self):
        say_what_happens()
        self.assertFalse(logging.getLogger("werkzeug").isEnabledFor(logging.INFO))


LINES = [{"gpio": 17, "header_pin": 11, "name": "GPIO17", "used": True,
          "consumer": "ir-receiver@11"},
         {"gpio": 18, "header_pin": 12, "name": "GPIO18", "used": False, "consumer": None},
         {"gpio": 4, "header_pin": 7, "name": "GPIO4", "used": True, "consumer": "w1-gpio"}]


class ReceiverSettingTests(unittest.TestCase):
    """Which pin the IR receiver is on: chosen on the screen or in a file."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "recordings.json"
        self.config = Path(self.temporary.name) / "pipertv.conf"
        lines = patch("pipertv.app.list_lines", side_effect=lambda: [dict(line) for line in LINES])
        lines.start()
        self.addCleanup(lines.stop)
        self.remote = FakeRemote()
        self.app = self.build(remote=self.remote)
        self.client = self.app.test_client()

    def build(self, **kwargs):
        app = create_app(data=self.path, demo=True, config=self.config, **kwargs)
        workbench = app.extensions["pipertv"]
        self.addCleanup(workbench.backend.close)
        self.addCleanup(workbench.close)
        return app

    def test_the_pins_are_offered_with_what_holds_them(self):
        result = self.client.get("/api/receiver").get_json()
        self.assertEqual(result["pin"], "auto")
        self.assertEqual(result["kernel"]["gpio"], 17)
        self.assertIn("/dev/lirc0", result["automatic"])
        lines = {line["gpio"]: line for line in result["lines"]}
        # The kernel's receiver reads its pin for Piper, so that pin is free.
        self.assertTrue(lines[17]["free"] and lines[17]["kernel"])
        self.assertTrue(lines[18]["free"])
        self.assertFalse(lines[4]["free"])

    def test_a_free_pin_is_read_from_the_moment_it_is_chosen(self):
        answer = self.client.put("/api/receiver", json={"pin": 18})
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.get_json()["reading"], "GPIO18 (pin 12)")
        self.assertEqual(self.remote.listening_on, {"kind": "gpio", "pin": 18})
        self.assertEqual(self.app.extensions["pipertv"].backend.device,
                         {"kind": "gpio", "pin": 18})
        self.assertIn("pin = 18", self.config.read_text())

    def test_the_kernel_s_own_pin_is_read_through_it(self):
        answer = self.client.put("/api/receiver", json={"pin": 17})
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(self.remote.listening_on, "/dev/lirc0")

    def test_auto_finds_it_again(self):
        self.client.put("/api/receiver", json={"pin": 18})
        self.client.put("/api/receiver", json={"pin": "auto"})
        self.assertEqual(self.remote.listening_on, "/dev/lirc0")
        self.assertIn("pin = auto", self.config.read_text())

    def test_the_choice_survives_a_restart(self):
        self.client.put("/api/receiver", json={"pin": 18})
        again = self.build()
        self.assertEqual(again.extensions["pipertv"].backend.device, {"kind": "gpio", "pin": 18})
        self.assertEqual(again.test_client().get("/api/receiver").get_json()["pin"], 18)

    def test_a_pin_written_in_the_file_by_hand_is_used_from_the_start(self):
        self.config.write_text("[ir]\npin = GPIO22\n")
        again = self.build()
        self.assertEqual(again.extensions["pipertv"].backend.device, {"kind": "gpio", "pin": 22})

    def test_a_pin_something_else_holds_is_refused_with_its_name(self):
        answer = self.client.put("/api/receiver", json={"pin": 4})
        self.assertEqual(answer.status_code, 409)
        self.assertIn("w1-gpio", answer.get_json()["error"])
        self.assertFalse(hasattr(self.remote, "listening_on"))
        self.assertFalse(self.config.exists())

    def test_a_nonsense_pin_is_refused(self):
        for payload in ({"pin": 99}, {"pin": "eighteen"}, {"kind": "gpio", "pin": 18}):
            with self.subTest(payload=payload):
                self.assertEqual(self.client.put("/api/receiver", json=payload).status_code, 400)

    def test_a_machine_without_gpio_still_answers(self):
        with patch("pipertv.app.list_lines", side_effect=FileNotFoundError("/dev/gpiochip0")):
            result = self.client.get("/api/receiver").get_json()
        self.assertEqual(result["lines"], [])
        self.assertIn("cannot be read", result["error"])


class WindowSettingTests(unittest.TestCase):
    """Whether Piper fills this television or sits in a window on it."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "recordings.json"
        self.remote = FakeRemote()
        self.app = create_app(data=self.path, demo=True, remote=self.remote)
        self.client = self.app.test_client()
        workbench = self.app.extensions["pipertv"]
        self.addCleanup(workbench.backend.close)
        self.addCleanup(workbench.close)

    def test_the_default_is_the_whole_screen(self):
        result = self.client.get("/api/window").get_json()
        self.assertFalse(result["settings"]["windowed"])
        self.assertIn("width", result["limits"])

    def test_a_window_is_saved_and_survives_a_restart(self):
        answer = self.client.put("/api/window", json={"windowed": True})
        self.assertEqual(answer.status_code, 200)
        self.assertTrue(answer.get_json()["settings"]["windowed"])
        again = create_app(data=self.path, demo=True).test_client()
        self.assertTrue(again.get("/api/window").get_json()["settings"]["windowed"])

    def test_changing_it_puts_the_interface_back_in_the_new_shape(self):
        # A browser cannot be talked out of the shape it was started with.
        self.client.put("/api/window", json={"windowed": True})
        self.assertEqual(getattr(self.remote, "reshaped", 0), 1)

    def test_a_nonsense_setting_is_refused_with_a_reason(self):
        answer = self.client.put("/api/window", json={"width": 10})
        self.assertEqual(answer.status_code, 400)
        self.assertIn("width", answer.get_json()["error"])

    def test_the_interface_can_be_asked_for_by_name(self):
        answer = self.client.post("/api/tv/interface", json={})
        self.assertEqual(answer.status_code, 200)
        self.assertTrue(answer.get_json()["showing"])
        self.assertEqual(getattr(self.remote, "shown", 0), 1)


class PointerSettingTests(unittest.TestCase):
    """How the cursor behaves is a preference, kept with the recordings."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "recordings.json"
        self.app = create_app(data=self.path, demo=True)
        self.client = self.app.test_client()
        workbench = self.app.extensions["pipertv"]
        self.addCleanup(workbench.backend.close)
        self.addCleanup(workbench.close)

    def test_the_defaults_are_offered_with_their_limits(self):
        state = self.client.get("/api/pointer").get_json()
        self.assertEqual(state["settings"], state["defaults"])
        self.assertIn("step_px", state["limits"])
        self.assertEqual(state["drives"], ["snap", "nudge"])

    def test_a_preference_is_saved_and_survives_a_restart(self):
        response = self.client.put("/api/pointer", json={"drive": "nudge", "step_px": 40})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["settings"]["drive"], "nudge")

        reopened = create_app(data=self.path, demo=True)
        self.addCleanup(reopened.extensions["pipertv"].close)
        self.addCleanup(reopened.extensions["pipertv"].backend.close)
        saved = reopened.test_client().get("/api/pointer").get_json()["settings"]
        self.assertEqual((saved["drive"], saved["step_px"]), ("nudge", 40))

    def test_a_setting_outside_its_range_is_refused_with_a_reason(self):
        for values in ({"step_px": 5000}, {"drive": "teleport"}, {"nonsense": 1},
                       {"step_px": 100, "max_step_px": 50}):
            with self.subTest(values=values):
                response = self.client.put("/api/pointer", json=values)
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.get_json())


class ControlEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "recordings.json"
        self.remote = FakeRemote()
        self.app = create_app(data=self.path, demo=True, remote=self.remote)
        self.client = self.app.test_client()
        workbench = self.app.extensions["pipertv"]
        self.addCleanup(workbench.backend.close)
        self.addCleanup(workbench.close)

    def visit(self):
        """Open a session the way the manual fallback would."""
        return self.client.post("/api/control/manual", json={"confirmed": True}).get_json()

    def test_the_control_layer_is_started_with_the_app(self):
        self.assertEqual(self.remote.started, 1)

    def test_state_reports_the_gate_and_the_detector(self):
        response = self.client.get("/api/control")
        self.assertEqual(response.status_code, 200)
        state = response.get_json()
        self.assertEqual(state["control"], "off")
        self.assertEqual(state["detection"]["state"], "active")

    def test_a_mode_can_only_be_chosen_for_the_current_visit(self):
        state = self.visit()
        self.assertTrue(state["needs_mode"])
        chosen = self.client.post("/api/control/mode",
                                  json={"mode": "pointer", "session_id": state["session"]["id"]})
        self.assertEqual(chosen.status_code, 200)
        self.assertEqual(chosen.get_json()["control"], "on")
        self.assertTrue(self.remote.session.enabled())

    def test_an_unknown_mode_is_rejected(self):
        state = self.visit()
        response = self.client.post("/api/control/mode",
                                    json={"mode": "magic", "session_id": state["session"]["id"]})
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.get_json())

    def test_a_choice_from_an_earlier_visit_conflicts(self):
        self.visit()
        response = self.client.post("/api/control/mode",
                                    json={"mode": "pointer", "session_id": "an-earlier-visit"})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(self.remote.session.enabled())

    def test_manual_control_needs_an_explicit_confirmation(self):
        response = self.client.post("/api/control/manual", json={"confirmed": False})
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.remote.session.snapshot()["session"])

    def test_control_can_be_stopped(self):
        state = self.visit()
        self.client.post("/api/control/mode",
                         json={"mode": "snapping", "session_id": state["session"]["id"]})
        self.assertTrue(self.remote.session.enabled())
        response = self.client.post("/api/control/stop", json={})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.get_json()["session"])
        self.assertFalse(self.remote.session.enabled())

    def test_recording_stands_the_desktop_down(self):
        state = self.visit()
        self.client.post("/api/control/mode",
                         json={"mode": "pointer", "session_id": state["session"]["id"]})
        self.assertEqual(self.client.post("/api/captures", json={"button_id": "power"}).status_code, 202)
        self.assertEqual(self.remote.held, 1)
        self.assertFalse(self.remote.session.enabled())

    def test_health_includes_the_control_layer(self):
        health = self.client.get("/api/health").get_json()
        self.assertEqual(health["control"]["screen"], [1920, 1080])

    def test_deleting_a_sample_refreshes_the_live_remote_mappings(self):
        job = self.client.post("/api/captures", json={"button_id": "power"}).get_json()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = self.client.get("/api/captures/" + job["id"]).get_json()
            if state["status"] == "captured":
                break
            time.sleep(.025)
        self.assertEqual(state["status"], "captured")
        response = self.client.delete("/api/buttons/power/samples/0", json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.remote.reloaded, 1)


class TvInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.remote = FakeRemote()
        self.app = create_app(data=Path(self.temporary.name) / "recordings.json",
                              demo=True, remote=self.remote)
        self.client = self.app.test_client()
        workbench = self.app.extensions["pipertv"]
        self.addCleanup(workbench.backend.close)
        self.addCleanup(workbench.close)

    def test_the_interface_and_its_assets_are_served(self):
        for path in ("/tv", "/tv.html", "/tv.css", "/tv.js"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                response.close()

    def test_the_page_carries_no_inline_styles_the_policy_would_block(self):
        # style-src 'self' has no 'unsafe-inline', so a style attribute in the
        # markup would silently not apply on the Pi.
        response = self.client.get("/tv")
        page = response.get_data(as_text=True)
        response.close()
        self.assertNotIn("style=", page)
        self.assertNotIn("<style", page)

    def test_presses_reach_the_page_in_order(self):
        for button in ("up", "right", "ok"):
            self.remote.buttons.append(button, "piper")
        feed = self.client.get("/api/tv/events?after=0").get_json()
        self.assertEqual([event["button"] for event in feed["events"]],
                         ["up", "right", "ok"])
        self.assertEqual(feed["sequence"], 3)
        self.assertFalse(feed["missed"])

    def test_the_page_only_receives_what_it_has_not_seen(self):
        self.remote.buttons.append("up", "piper")
        seen = self.client.get("/api/tv/events").get_json()["sequence"]
        self.remote.buttons.append("down", "piper")
        feed = self.client.get(f"/api/tv/events?after={seen}").get_json()
        self.assertEqual([event["button"] for event in feed["events"]], ["down"])

    def test_the_feed_reports_the_gate_verdict(self):
        feed = self.client.get("/api/tv/events").get_json()
        self.assertEqual(feed["control"], "off")
        self.assertIsNone(feed["mode"])

        state = self.client.post("/api/control/manual", json={"confirmed": True}).get_json()
        self.client.post("/api/control/mode",
                         json={"mode": "piper", "session_id": state["session"]["id"]})
        feed = self.client.get("/api/tv/events").get_json()
        self.assertEqual((feed["control"], feed["mode"]), ("on", "piper"))

    def test_a_service_opens_for_the_visit_the_interface_is_showing(self):
        state = self.client.post("/api/control/manual", json={"confirmed": True}).get_json()
        visit = state["session"]["id"]
        self.client.post("/api/control/mode", json={"mode": "piper", "session_id": visit})

        response = self.client.post("/api/tv/launch",
                                    json={"service": "youtube", "session_id": visit})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["running"]["id"], "youtube")
        self.assertEqual(self.remote.launcher.launched, ["youtube"])
        self.assertEqual(self.client.get("/api/tv/events").get_json()["services"]["running"]["id"],
                         "youtube")

        closed = self.client.post("/api/tv/close", json={})
        self.assertEqual(closed.status_code, 200)
        self.assertIsNone(closed.get_json()["running"])

    def test_the_mouse_can_leave_piper(self):
        response = self.client.post("/api/tv/leave", json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["mode"], "pointer")
        self.assertEqual(self.remote.left, 1)

    def test_a_service_piper_cannot_open_is_not_found(self):
        state = self.client.post("/api/control/manual", json={"confirmed": True}).get_json()
        visit = state["session"]["id"]
        self.client.post("/api/control/mode", json={"mode": "piper", "session_id": visit})
        response = self.client.post("/api/tv/launch",
                                    json={"service": "netflix", "session_id": visit})
        self.assertEqual(response.status_code, 404)
        self.assertIn("netflix", response.get_json()["error"])

    def test_opening_is_refused_while_the_tv_shows_another_input(self):
        response = self.client.post("/api/tv/launch",
                                    json={"service": "youtube", "session_id": "whatever"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("not showing the Pi", response.get_json()["error"])
        self.assertEqual(self.remote.launcher.launched, [])

    def test_the_keyboard_can_be_asked_for_and_sent_away(self):
        self.assertEqual(self.client.post("/api/tv/keyboard", json={}).status_code, 200)
        self.assertEqual(self.remote.keyboard_opened, 1)
        self.client.post("/api/tv/keyboard", json={"show": False})
        self.assertEqual(self.remote.keyboard_closed, 1)

    def test_a_nonsense_position_is_rejected(self):
        for after in ("-1", "abc", "1.5", ""):
            with self.subTest(after=after):
                response = self.client.get(f"/api/tv/events?after={after}")
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.get_json())


class ControlDisabledTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.app = create_app(data=Path(self.temporary.name) / "recordings.json", demo=True)
        self.client = self.app.test_client()
        workbench = self.app.extensions["pipertv"]
        self.addCleanup(workbench.backend.close)
        self.addCleanup(workbench.close)

    def test_control_is_off_unless_asked_for(self):
        self.assertIsNone(self.app.extensions["pipertv_control"])

    def test_demo_does_not_construct_a_hardware_controller(self):
        with patch("pipertv.app.RemoteControl") as hardware:
            app = create_app(data=Path(self.temporary.name) / "demo.json",
                             demo=True, control=True)
        self.addCleanup(app.extensions["pipertv"].close)
        hardware.assert_not_called()
        self.assertIsNone(app.extensions["pipertv_control"])

    def test_the_control_endpoints_report_that_it_is_not_running(self):
        cases = [self.client.get("/api/control"),
                 self.client.post("/api/control/mode", json={"mode": "pointer", "session_id": "x"}),
                 self.client.post("/api/control/manual", json={"confirmed": True}),
                 self.client.post("/api/control/stop", json={}),
                 self.client.get("/api/tv/events"),
                 self.client.post("/api/tv/launch", json={"service": "youtube", "session_id": "x"}),
                 self.client.post("/api/tv/close", json={}),
                 self.client.post("/api/tv/leave", json={}),
                 self.client.post("/api/tv/keyboard", json={})]
        for response in cases:
            with self.subTest(path=response.request.path):
                self.assertEqual(response.status_code, 404)
                self.assertIn("error", response.get_json())

    def test_the_tv_interface_is_still_served_without_remote_control(self):
        # Without a remote the page falls back to keyboard navigation, so it
        # must still load rather than 404 alongside its feed.
        for path in ("/tv", "/tv.css", "/tv.js"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                response.close()

    def test_recording_still_works_without_desktop_control(self):
        self.assertEqual(self.client.post("/api/captures", json={"button_id": "power"}).status_code, 202)

    def test_health_omits_control(self):
        self.assertNotIn("control", self.client.get("/api/health").get_json())


if __name__ == "__main__":
    unittest.main()
