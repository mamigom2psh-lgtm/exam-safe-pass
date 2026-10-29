import sqlite3
import unittest

import test_scenarios as ts

APPROVE = {"decision": "approve", "reason": "normal", "document_match": True, "live_face_match": True}
SCENARIO_PATHS = (
    ("GET", "/api/demo/scenarios", None),
    ("GET", "/api/demo/scenario/events", None),
    ("GET", "/api/proctor/overview", None),
    ("POST", "/api/demo/scenario/start", {"scenario": "normal_entry"}),
    ("POST", "/api/demo/scenario/act", {"run_id": "x", "action": "lookup"}),
    ("POST", "/api/demo/scenario/reset", {}),
)
TABLES = ("events", "demo_admissions", "demo_tokens", "admissions", "tokens")


class DemoScenarioServerTests(unittest.TestCase):
    setUpClass = classmethod(ts.ServerScenarios.setUpClass.__func__)
    tearDownClass = classmethod(ts.ServerScenarios.tearDownClass.__func__)
    setUp = ts.ServerScenarios.setUp

    def call(self, method, path, payload=None):
        return self.api.request(method, path, payload, "proctor", ts.PROCTOR_KEY)

    def counts(self):
        with sqlite3.connect(self.db_path) as db:
            return {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}

    def run_normal(self):
        status, run = self.call("POST", "/api/demo/scenario/start", {"scenario": "normal_entry"})
        self.assertEqual(status, 200)
        rid = run["run_id"]
        for action, params in (("lookup", None), ("capture", None), ("analyze", None), ("review", APPROVE), ("issue_qr", None)):
            status, run = self.call("POST", "/api/demo/scenario/act", {"run_id": rid, "action": action, "params": params})
            self.assertEqual(status, 200, run)
        return rid, run

    def test_roles_are_enforced_on_every_scenario_endpoint(self):
        for method, path, payload in SCENARIO_PATHS:
            for role, key in ((None, None), ("candidate", "x"), ("admin", ts.ADMIN_KEY), ("proctor", "wrong-key")):
                with self.subTest(path=path, role=role):
                    status, _ = self.api.request(method, path, payload, role, key)
                    self.assertEqual(status, 403)
            status, _ = self.call(method, path, payload)
            self.assertIn(status, (200, 404))

    def test_full_flow_and_qr_is_png_demo_only(self):
        rid, run = self.run_normal()
        self.assertTrue(run["demo_only"])
        self.assertFalse(run["real_authentication"])
        self.assertIn("qr_png", run)
        status, entered = self.call("POST", "/api/demo/scenario/act", {"run_id": rid, "action": "enter"})
        self.assertEqual(status, 200)
        status, again = self.call("POST", "/api/demo/scenario/act", {"run_id": rid, "action": "enter"})
        self.assertEqual(status, 409)

    def test_forged_fields_cannot_skip_review_or_issue_qr(self):
        status, run = self.call("POST", "/api/demo/scenario/start", {"scenario": "spoof_suspected"})
        rid = run["run_id"]
        status, body = self.call("POST", "/api/demo/scenario/act",
                                 {"run_id": rid, "action": "issue_qr", "params": {"face_match": True, "liveness": True, "score": 0.99}})
        self.assertEqual(status, 400)
        status, body = self.call("POST", "/api/demo/scenario/act", {"run_id": rid, "action": "issue_qr"})
        self.assertEqual(status, 409)
        self.assertNotIn("qr_png", body)

    def test_scenarios_do_not_touch_databases_or_dashboard(self):
        before = self.counts()
        rid, _ = self.run_normal()
        self.call("POST", "/api/demo/scenario/act", {"run_id": rid, "action": "enter"})
        self.call("POST", "/api/demo/scenario/start", {"scenario": "failure_recovery"})
        self.call("POST", "/api/demo/scenario/reset", {})
        self.assertEqual(self.counts(), before)
        with sqlite3.connect(self.db_path) as db:
            hits = db.execute("SELECT COUNT(*) FROM events WHERE details LIKE '%scenario:%' OR event LIKE '%시나리오%'").fetchone()[0]
        self.assertEqual(hits, 0)

    def test_reset_clears_runs_and_events(self):
        rid, _ = self.run_normal()
        self.call("POST", "/api/demo/scenario/reset", {})
        status, _ = self.call("POST", "/api/demo/scenario/act", {"run_id": rid, "action": "enter"})
        self.assertEqual(status, 404)
        status, body = self.call("GET", "/api/demo/scenario/events")
        self.assertEqual(body["events"], [])

    def test_scenario_token_is_not_valid_for_operational_or_demo_inspect(self):
        forged = "DEMO-ONLY:SCENARIO:" + "A" * 22
        for path in ("/api/token/inspect", "/api/demo/token/inspect"):
            status, body = self.api.request("POST", path, {"token": forged, "exam_id": ts.EXAM_ID}, "proctor", ts.PROCTOR_KEY)
            self.assertNotEqual(status, 200, path)
        self.assertEqual(self.counts()["tokens"], 0)

    def test_overview_masks_candidates_and_requires_proctor(self):
        status, body = self.call("GET", "/api/proctor/overview")
        self.assertEqual(status, 200)
        self.assertTrue(body["demo_only"])
        for item in body["recent_reviews"]:
            self.assertTrue(item["candidate_id"] is None or item["candidate_id"].startswith("***"))

    def test_bad_requests_return_errors_not_success(self):
        for payload in ({"scenario": "operational"}, {"scenario": 5}, {}):
            status, _ = self.call("POST", "/api/demo/scenario/start", payload)
            self.assertEqual(status, 400)
        status, _ = self.call("POST", "/api/demo/scenario/act", {"run_id": "nope", "action": "lookup"})
        self.assertEqual(status, 404)

    def test_pages_carry_demo_banner(self):
        import urllib.request
        for path in ("/demo-scenarios", "/kiosk"):
            with urllib.request.urlopen(self.api.base_url + path, timeout=5) as response:
                html = response.read().decode()
            self.assertIn("실제 입실 불가", html, path)
            if path == "/demo-scenarios":
                self.assertIn("본문 바로가기", html)


if __name__ == "__main__":
    unittest.main()
