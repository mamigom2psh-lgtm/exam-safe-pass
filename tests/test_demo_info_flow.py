import sqlite3
import unittest

import test_scenarios as ts

CODE, EXAM = "980320-2", "demo-computer-2"
SCORE_FIELDS = ("face_match", "face_similarity", "liveness", "score", "verified", "real_authentication", "face_score")


class DemoInfoFlowTests(unittest.TestCase):
    setUpClass = classmethod(ts.ServerScenarios.setUpClass.__func__)
    tearDownClass = classmethod(ts.ServerScenarios.tearDownClass.__func__)
    setUp = ts.ServerScenarios.setUp

    def new_session(self):
        status, body = self.api.request("POST", "/api/kiosk/session", {})
        self.assertEqual(status, 201)
        sid = body["kiosk_session_id"]
        status, record = self.api.lookup_kiosk_candidate(sid, mode="self_demo")
        self.assertEqual(status, 200)
        self.record = record
        return sid

    def info(self, sid, code=CODE, exam=EXAM, **extra):
        return self.api.request("POST", "/api/kiosk/self-demo/verify-demo-info",
                                {"kiosk_session_id": sid, "virtual_code": code, "exam": exam, **extra})

    def issue(self, sid, scenario="demo_info_simulation", **extra):
        return self.api.request("POST", "/api/kiosk/demo-auto-issue",
                                {"kiosk_session_id": sid, "demo_simulation": True, "scenario": scenario, **extra})

    def count(self, table):
        with sqlite3.connect(self.db_path) as db:
            return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_lookup_lists_demo_exams_including_computer_skill_2(self):
        self.new_session()
        names = [item["name"] for item in self.record["demo_exam_options"]]
        self.assertIn("컴퓨터활용능력 2급", names)
        default = self.record["demo_default_exam_id"]
        self.assertEqual({o["id"]: o["name"] for o in self.record["demo_exam_options"]}[default], "컴퓨터활용능력 2급")

    def test_matching_info_issues_marked_simulation_qr_without_proctor(self):
        sid = self.new_session()
        status, body = self.info(sid)
        self.assertEqual(status, 200, body)
        self.assertFalse(body["real_authentication"])
        self.assertNotIn("qr_png", self.api.kiosk_status(sid)[1])  # info match alone issues nothing
        status, qr = self.issue(sid)
        self.assertEqual(status, 200, qr)
        self.assertTrue(qr["simulation"] and qr["demo_only"])
        self.assertFalse(qr["real_authentication"])
        self.assertEqual(qr["expires_in"], 30)
        self.assertTrue(qr["demo_token"].startswith("DEMO-ONLY:"))
        self.assertEqual(ts.decode_qr_png(qr["qr_png"]), qr["demo_token"])
        self.assertNotEqual(self.api.kiosk_status(sid)[1].get("state"), "verified")
        self.assertEqual(self.issue(sid)[0], 409)  # no duplicate QR

    def test_issue_without_server_side_info_match_is_denied(self):
        sid = self.new_session()
        status, _ = self.issue(sid)
        self.assertEqual(status, 403)
        self.info(sid, code="990101-1")
        self.assertEqual(self.issue(sid)[0], 403)
        self.assertEqual(self.count("demo_tokens"), 0)

    def test_format_empty_and_mismatch_are_rejected(self):
        sid = self.new_session()
        for code in ("", "   ", "980320", "980320-5", "98032-2", "abcdef-2", "981340-2", "980230-2", "980320-22"):
            with self.subTest(code=code):
                self.assertEqual(self.info(sid, code=code)[0], 400)
        self.assertEqual(self.info(sid, code="990101-1")[0], 403)  # well-formed but not the preset
        self.assertEqual(self.issue(sid)[0], 403)

    def test_exam_name_missing_or_mismatched_is_rejected(self):
        sid = self.new_session()
        for exam in ("", None, 5, "정보처리기사"):
            with self.subTest(exam=exam):
                status, _ = self.info(sid, exam=exam)
                self.assertIn(status, (400, 403))
        self.assertEqual(self.info(sid, exam="demo-mock-2026")[0], 403)  # real exam, but not the preset one
        self.assertEqual(self.issue(sid)[0], 403)
        self.assertEqual(self.info(sid, exam="컴퓨터활용능력 2급")[0], 200)  # name entry also matches

    def test_attempts_are_limited(self):
        sid = self.new_session()
        for _ in range(5):
            self.assertEqual(self.info(sid, code="990101-1")[0], 403)
        self.assertEqual(self.info(sid)[0], 429)
        self.assertEqual(self.issue(sid)[0], 403)

    def test_browser_scores_and_forged_fields_are_rejected(self):
        sid = self.new_session()
        for field in SCORE_FIELDS:
            for value in (True, 0.66, "NaN", None):
                with self.subTest(field=field, value=value):
                    self.assertEqual(self.info(sid, **{field: value})[0], 400)
                    self.assertEqual(self.issue(sid, **{field: value})[0], 400)
        self.assertEqual(self.count("demo_tokens"), 0)
        self.assertEqual(self.count("tokens"), 0)
        self.assertEqual(self.api.kiosk_status(sid)[1]["state"], "awaiting_self_auth")

    def test_info_path_requires_self_demo_session_and_lookup_is_single_use(self):
        status, body = self.api.request("POST", "/api/kiosk/session", {})
        sid = body["kiosk_session_id"]
        self.api.lookup_kiosk_candidate(sid, mode="proctor")
        self.assertEqual(self.info(sid)[0], 409)
        self.assertNotEqual(self.issue(sid)[0], 200)
        sid2 = self.new_session()
        self.info(sid2)
        # the session cannot be re-pointed at another candidate after the match
        self.assertEqual(self.api.lookup_kiosk_candidate(sid2, number="2026-0099", mode="self_demo")[0], 409)

    def test_candidate_without_preset_profile_cannot_use_info_path(self):
        status, body = self.api.request("POST", "/api/kiosk/session", {})
        sid = body["kiosk_session_id"]
        self.assertEqual(self.api.lookup_kiosk_candidate(sid, number="2026-0099", mode="self_demo")[0], 200)
        self.assertEqual(self.info(sid)[0], 403)
        self.assertEqual(self.issue(sid)[0], 403)

    def test_info_qr_expires_is_single_use_and_operational_stays_blocked(self):
        sid = self.new_session()
        self.info(sid)
        status, qr = self.issue(sid)
        tokens, admissions = self.count("tokens"), self.count("admissions")
        for path in ("/api/token/inspect", "/api/admit"):
            status, _ = self.api.request("POST", path, {"token": qr["demo_token"], "exam_id": ts.EXAM_ID}, "proctor", ts.PROCTOR_KEY)
            self.assertNotEqual(status, 200, path)
        self.assertEqual((self.count("tokens"), self.count("admissions")), (tokens, admissions))
        self.assertEqual(self.api.kiosk_demo_entry(sid, qr["demo_token"])[0], 200)
        self.assertEqual(self.api.kiosk_demo_entry(sid, qr["demo_token"])[0], 409)  # reuse

    def test_info_qr_expiry(self):
        sid = self.new_session()
        self.info(sid)
        status, qr = self.issue(sid)
        with sqlite3.connect(self.db_path) as db:
            db.execute("UPDATE demo_tokens SET expires_at = 1")
        self.assertEqual(self.api.kiosk_demo_entry(sid, qr["demo_token"])[0], 409)

    def test_audit_log_never_contains_entered_values_or_token(self):
        sid = self.new_session()
        self.info(sid, code="991231-1")
        self.info(sid)
        status, qr = self.issue(sid)
        with sqlite3.connect(self.db_path) as db:
            rows = " ".join(f"{r[0]} {r[1]} {r[2]}" for r in db.execute("SELECT event, details, outcome FROM events"))
        self.assertIn("DEMO 추가 정보 확인", rows)
        for secret in ("991231", CODE, "980320", qr["demo_token"]):
            self.assertNotIn(secret, rows)

    def test_regular_supervisor_path_still_works(self):
        sid = self.new_session()
        self.assertEqual(self.api.request("POST", "/api/kiosk/self-demo/request-review", {"kiosk_session_id": sid})[0], 200)
        self.assertEqual(self.api.kiosk_review(sid)[0], 200)
        self.assertEqual(self.api.kiosk_status(sid)[1]["state"], "demo_qr_ready")

    def test_preset_auto_issue_still_needs_proctor(self):
        sid = self.new_session()
        self.assertEqual(self.issue(sid, scenario="preset_auto_issue")[0], 403)


class DemoSimulationDisabledTests(unittest.TestCase):
    extra_env = {"EXAM_SAFE_DEMO_SIMULATION": "0"}
    setUpClass = classmethod(ts.ServerScenarios.setUpClass.__func__)
    tearDownClass = classmethod(ts.ServerScenarios.tearDownClass.__func__)
    setUp = ts.ServerScenarios.setUp

    def test_matching_info_cannot_issue_when_server_denies_simulation(self):
        status, body = self.api.request("POST", "/api/kiosk/session", {})
        sid = body["kiosk_session_id"]
        self.api.lookup_kiosk_candidate(sid, mode="self_demo")
        self.assertEqual(self.api.request("POST", "/api/kiosk/self-demo/verify-demo-info",
                                          {"kiosk_session_id": sid, "virtual_code": CODE, "exam": EXAM})[0], 200)
        status, _ = self.api.request("POST", "/api/kiosk/demo-auto-issue",
                                     {"kiosk_session_id": sid, "demo_simulation": True, "scenario": "demo_info_simulation"})
        self.assertEqual(status, 403)
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM demo_tokens").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
