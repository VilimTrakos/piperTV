import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from pipertv.app import create_app, main


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
        with patch("pipertv.app.create_app", return_value=self.app), patch.object(self.app, "run") as run:
            main(["--demo", "--host", "127.0.0.1", "--port", "8765", "--data", str(self.path)])
        run.assert_called_once_with(host="127.0.0.1", port=8765, threaded=True,
                                    processes=1, debug=False, use_debugger=False, use_reloader=False)


if __name__ == "__main__":
    unittest.main()
