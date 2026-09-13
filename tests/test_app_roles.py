"""Arranging role bindings from the studio, with or without control running."""

import json
from pathlib import Path
import tempfile
import unittest

from pipertv.app import create_app

from tests.test_app import FakeRemote


class RoleEndpointTests(unittest.TestCase):
    """Bindings are a property of the library, so control need not be running."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "recordings.json"
        self.app = create_app(data=self.path, demo=True)
        self.client = self.app.test_client()
        workbench = self.app.extensions["pipertv"]
        self.addCleanup(workbench.backend.close)
        self.addCleanup(workbench.close)

    def test_a_fresh_library_reports_every_key_acting_as_itself(self):
        state = self.client.get("/api/roles").get_json()
        self.assertEqual(state["bindings"], {})
        described = {entry["role"]: entry for entry in state["roles"]}
        self.assertEqual(described["up"]["button"], "up")
        self.assertFalse(described["up"]["rebound"])

    def test_the_playback_cross_is_offered_as_a_starting_point(self):
        suggested = self.client.get("/api/roles").get_json()["suggested"]
        self.assertEqual(suggested["up"], "play")
        self.assertEqual(suggested["ok"], "pause")
        self.assertEqual(suggested["left"], "previous")
        self.assertEqual(suggested["right"], "next")

    def test_binding_a_role_is_saved_and_reported(self):
        response = self.client.put("/api/roles/up", json={"button": "play"})
        self.assertEqual(response.status_code, 200)
        state = response.get_json()
        self.assertEqual(state["bindings"], {"up": "play"})
        described = {entry["role"]: entry for entry in state["roles"]}
        self.assertTrue(described["up"]["rebound"])
        # It reached the file, not only the reply.
        stored = json.loads(self.path.read_text())
        self.assertEqual(stored["roles"], {"up": "play"})

    def test_a_binding_can_be_released(self):
        self.client.put("/api/roles/up", json={"button": "play"})
        state = self.client.put("/api/roles/up", json={"button": None}).get_json()
        self.assertEqual(state["bindings"], {})

    def test_an_unknown_role_is_rejected(self):
        response = self.client.put("/api/roles/scroll", json={"button": "play"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.get_json())

    def test_an_unknown_button_is_rejected(self):
        response = self.client.put("/api/roles/up", json={"button": "nonexistent"})
        self.assertEqual(response.status_code, 404)
        self.assertIn("error", response.get_json())

    def test_a_colliding_binding_is_rejected_and_keeps_the_first(self):
        self.client.put("/api/roles/up", json={"button": "play"})
        response = self.client.put("/api/roles/down", json={"button": "play"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/api/roles").get_json()["bindings"],
                         {"up": "play"})

    def test_a_role_cannot_be_moved_onto_another_navigation_key(self):
        response = self.client.put("/api/roles/up", json={"button": "down"})
        self.assertEqual(response.status_code, 400)

    def test_bindings_survive_a_restart_of_the_app(self):
        self.client.put("/api/roles/ok", json={"button": "pause"})
        reloaded = create_app(data=self.path, demo=True)
        try:
            state = reloaded.test_client().get("/api/roles").get_json()
            self.assertEqual(state["bindings"], {"ok": "pause"})
        finally:
            reloaded.extensions["pipertv"].close()
            reloaded.extensions["pipertv"].backend.close()


class RoleEndpointsWithControlTests(unittest.TestCase):
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

    def test_a_new_binding_takes_effect_without_a_restart(self):
        response = self.client.put("/api/roles/up", json={"button": "play"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(getattr(self.remote, "rebound", 0), 1,
                         "the live remote must be told the binding changed")

    def test_a_rejected_binding_does_not_disturb_the_live_remote(self):
        self.client.put("/api/roles/up", json={"button": "nonexistent"})
        self.assertEqual(getattr(self.remote, "rebound", 0), 0)


if __name__ == "__main__":
    unittest.main()
