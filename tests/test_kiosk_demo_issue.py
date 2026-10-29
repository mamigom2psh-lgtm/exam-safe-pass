import sqlite3
import unittest

import test_scenarios as ts

FACE_FIELDS = ("face_match", "face_similarity", "liveness", "score", "verified", "real_authentication")


class KioskDemoIssueTests(unittest.TestCase):
    setUpClass = classmethod(ts.ServerScenarios.setUpClass.__func__)
    tearDownClass = classmethod(ts.ServerScenarios.tearDownClass.__func__)
    setUp = ts.ServerScenarios.setUp

    def new_session(self, mode="self_demo"):
        status, body = self.api.request("POST", "/api/kiosk/session", {})
        self.assertEqual(status, 201)
        sid = body["kiosk_session_id"]
        status, _ = self.api.lookup_kiosk_candidate(sid, mode=mode)
        self.assertEqual(status, 200)
        return sid

    def auto(self, sid, role="proctor", key=ts.PROCTOR_KEY, **extra):
        payload = {"kiosk_session_id": sid, "demo_simulation": True, "scenario": "preset_auto_issue", **extra}
        return self.api.request("POST", "/api/kiosk/demo-auto-issue", payload, role, key)

    def request_review(self, sid, **extra):
        return self.api.request("POST", "/api/kiosk/self-demo/request-review", {"kiosk_session_id": sid, **extra})

    def count(self, table):
        with sqlite3.connect(self.db_path) as db:
            return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    # --- real face path: supervisor approval ----------------------------
    def test_real_face_path_needs_supervisor_and_issues_30s_demo_qr(self):
        sid = self.new_session()
        status, state = self.api.kiosk_status(sid)
        self.assertNotEqual(state["state"], "demo_qr_ready")
        status, body = self.request_review(sid)
        self.assertEqual(status, 200)
        self.assertFalse(body["real_authentication"])
        # still no QR until the proctor decides
        self.assertNotIn("qr_png", self.api.kiosk_status(sid)[1])
        status, body = self.api.kiosk_review(sid)
        self.assertEqual(status, 200, body)
        status, shown = self.api.kiosk_status(sid)
        self.assertEqual(shown["state"], "demo_qr_ready")
        self.assertTrue(shown["demo_only"])
        self.assertFalse(shown["simulation"])
        self.assertLessEqual(shown["expires_in"], 30)
        self.assertFalse(shown["verification"]["real_authentication"])
        self.assertEqual(ts.decode_qr_png(shown["qr_png"]), shown["demo_token"])
        status, admitted = self.api.kiosk_demo_entry(sid, shown["demo_token"])
        self.assertEqual(status, 200, admitted)
        self.assertEqual(self.api.kiosk_demo_entry(sid, shown["demo_token"])[0], 409)

    def test_supervisor_review_without_request_or_role_is_blocked(self):
        sid = self.new_session()
        self.assertEqual(self.api.kiosk_review(sid)[0], 409)  # not queued yet
        self.request_review(sid)
        for role, key in (("candidate", "x"), ("admin", ts.ADMIN_KEY), (None, None)):
            self.assertEqual(self.api.kiosk_review(sid, role=role, key=key)[0], 403)
        self.assertNotIn("qr_png", self.api.kiosk_status(sid)[1])

    def test_request_review_rejects_browser_scores(self):
        sid = self.new_session()
        for field in FACE_FIELDS:
            status, _ = self.request_review(sid, **{field: True})
            self.assertEqual(status, 400, field)
        self.assertEqual(self.api.kiosk_status(sid)[1]["state"], "awaiting_self_auth")

    def test_review_mismatch_does_not_issue(self):
        sid = self.new_session()
        self.request_review(sid)
        status, body = self.api.kiosk_review(sid, live_face_match=False)
        self.assertEqual(body["state"], "supervisor_pending")
        self.assertNotIn("qr_png", self.api.kiosk_status(sid)[1])

    # --- preset simulation ---------------------------------------------
    def test_auto_issue_requires_server_side_proctor_role(self):
        sid = self.new_session()
        tokens_before = self.count("demo_tokens")
        for role, key in ((None, None), ("candidate", "x"), ("admin", ts.ADMIN_KEY), ("proctor", "wrong")):
            self.assertEqual(self.auto(sid, role=role, key=key)[0], 403, role)
        self.assertEqual(self.count("demo_tokens"), tokens_before)
        self.assertNotIn("qr_png", self.api.kiosk_status(sid)[1])

    def test_auto_issue_ignores_forged_face_fields_and_needs_explicit_scenario(self):
        sid = self.new_session()
        for field in FACE_FIELDS:
            self.assertEqual(self.auto(sid, **{field: True})[0], 400, field)
        status, _ = self.api.request("POST", "/api/kiosk/demo-auto-issue", {"kiosk_session_id": sid}, "proctor", ts.PROCTOR_KEY)
        self.assertEqual(status, 400)
        status, _ = self.api.request("POST", "/api/kiosk/demo-auto-issue",
                                     {"kiosk_session_id": sid, "demo_simulation": True, "scenario": "other"}, "proctor", ts.PROCTOR_KEY)
        self.assertEqual(status, 400)

    def test_auto_issue_creates_marked_demo_qr_once_and_is_single_use(self):
        sid = self.new_session()
        status, body = self.auto(sid)
        self.assertEqual(status, 200, body)
        self.assertTrue(body["simulation"] and body["demo_only"])
        self.assertFalse(body["real_authentication"])
        self.assertEqual(body["expires_in"], 30)
        self.assertEqual(ts.decode_qr_png(body["qr_png"]), body["demo_token"])
        self.assertTrue(body["demo_token"].startswith("DEMO-ONLY:"))
        self.assertEqual(self.auto(sid)[0], 409)  # no duplicate QR
        self.assertTrue(self.api.kiosk_status(sid)[1]["simulation"])
        admissions_before = self.count("admissions")
        self.assertEqual(self.api.kiosk_demo_entry(sid, body["demo_token"])[0], 200)
        self.assertEqual(self.api.kiosk_demo_entry(sid, body["demo_token"])[0], 409)  # reuse
        self.assertEqual(self.count("admissions"), admissions_before)  # operational untouched
        status, fresh = self.api.create_kiosk_session()  # duplicate admission is stopped at lookup
        self.assertEqual(self.api.lookup_kiosk_candidate(fresh["kiosk_session_id"], mode="self_demo")[0], 409)

    def test_auto_issue_token_expires(self):
        sid = self.new_session()
        status, body = self.auto(sid)
        with sqlite3.connect(self.db_path) as db:
            db.execute("UPDATE demo_tokens SET expires_at = 1")
        status, result = self.api.kiosk_demo_entry(sid, body["demo_token"])
        self.assertEqual(status, 409)
        self.assertFalse(result.get("demo_admitted", False))
        self.assertNotIn("qr_png", self.api.kiosk_status(sid)[1])

    def test_auto_issue_not_allowed_after_review_requested(self):
        sid = self.new_session()
        self.request_review(sid)
        self.assertEqual(self.auto(sid)[0], 409)

    # --- operational stays closed --------------------------------------
    def test_operational_qr_and_admission_stay_blocked(self):
        sid = self.new_session()
        status, body = self.auto(sid)
        tokens, admissions = self.count("tokens"), self.count("admissions")
        for path in ("/api/token/inspect", "/api/admit"):
            status, _ = self.api.request("POST", path, {"token": body["demo_token"], "exam_id": ts.EXAM_ID}, "proctor", ts.PROCTOR_KEY)
            self.assertNotEqual(status, 200, path)
        self.assertEqual((self.count("tokens"), self.count("admissions")), (tokens, admissions))

    def test_audit_has_no_secrets(self):
        sid = self.new_session()
        status, body = self.auto(sid)
        with sqlite3.connect(self.db_path) as db:
            rows = " ".join(f"{r[0]} {r[1]}" for r in db.execute("SELECT event, details FROM events"))
        self.assertIn("자동 발급 시연", rows)
        self.assertNotIn(body["demo_token"], rows)

    def test_kiosk_page_labels_are_present(self):
        import urllib.request
        html = urllib.request.urlopen(self.api.base_url + "/kiosk", timeout=5).read().decode()
        for text in ("자동 발급 시연(DEMO)", "사전 설정", "무관한", "감독관 확인 요청", "공모전 시제품 · 실제 입실 불가"):
            self.assertIn(text, html)


if __name__ == "__main__":
    unittest.main()
