"""ID·DEMO app link inside the Render judge sandbox: app endpoints need no cookie but only work with a pairing code."""
import tempfile
import unittest

import test_scenarios as ts
from tests.test_judge_mode import JudgeBase, JudgeServer
from tests.test_idapp_link import SECRET


class JudgeIdappTests(JudgeBase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.srv = JudgeServer(cls.tmp.name, EXAM_SAFE_IDAPP_SECRET=SECRET)
        status, body, cls.cookie, _ = cls.srv.login()
        assert status == 200, body
        cls.csrf = body["csrf_token"]

    def app(self, path, payload=None, method="POST"):
        return self.srv.request(method, path, payload, cookie="", csrf="", origin=False)

    def test_info_is_public_and_names_the_judge_environment(self):
        status, body, _ = self.app("/api/idapp/info", method="GET")
        self.assertEqual(status, 200)
        self.assertEqual(body["environment"], "judge-demo")
        self.assertTrue(body["link_enabled"])

    def test_kiosk_pairing_and_verification_still_require_the_judge_session(self):
        sid, _ = self.new_kiosk("2026-0013")
        for path in ("/api/kiosk/idapp/pair", "/api/kiosk/idapp/verify"):
            self.assertEqual(self.app(path, {"kiosk_session_id": sid})[0], 401)
        self.assertEqual(self.post("/api/kiosk/idapp/pair", {"kiosk_session_id": sid}, csrf="bad")[0], 403)

    def test_app_endpoint_without_judge_cookie_works_only_with_a_valid_pairing_code(self):
        self.assertEqual(self.app("/api/idapp/token", {"pairing_code": "ZZZZZZZZ"})[0], 400)
        sid, _ = self.new_kiosk("2026-0014")
        status, pair, _ = self.post("/api/kiosk/idapp/pair", {"kiosk_session_id": sid})
        self.assertEqual(status, 200, pair)
        status, issued, _ = self.app("/api/idapp/token", {"pairing_code": pair["pairing_code"]})
        self.assertEqual(status, 200, issued)
        self.assertEqual(self.app("/api/idapp/token", {"pairing_code": pair["pairing_code"]})[0], 409)
        status, checked, _ = self.post("/api/kiosk/idapp/verify", {"kiosk_session_id": sid, "qr_payload": issued["qr_payload"]})
        self.assertEqual(status, 200, checked)
        self.assertEqual(checked["state"], "idapp_demo_checked")
        self.assertFalse(checked["real_authentication"])
        self.assertEqual(self.post("/api/kiosk/idapp/verify", {"kiosk_session_id": sid, "qr_payload": issued["qr_payload"]})[0], 409)

    def test_entry_qr_in_judge_mode_needs_id_check_after_link_started(self):
        sid, _ = self.new_kiosk("2026-0015")
        status, pair, _ = self.post("/api/kiosk/idapp/pair", {"kiosk_session_id": sid})
        issue = {"kiosk_session_id": sid, "demo_simulation": True, "scenario": "preset_auto_issue"}
        self.assertEqual(self.post("/api/kiosk/demo-auto-issue", issue)[0], 409)
        _, issued, _ = self.app("/api/idapp/token", {"pairing_code": pair["pairing_code"]})
        self.post("/api/kiosk/idapp/verify", {"kiosk_session_id": sid, "qr_payload": issued["qr_payload"]})
        status, qr, _ = self.post("/api/kiosk/demo-auto-issue", issue)
        self.assertEqual(status, 200, qr)
        self.assertEqual(qr["expires_in"], 30)

    def test_other_idapp_paths_and_admin_routes_stay_closed(self):
        self.assertEqual(self.app("/api/idapp/anything", {})[0], 404)
        self.assertEqual(self.app("/api/demo-candidates", method="GET")[0], 401)


class JudgeIdappDisabledTests(unittest.TestCase):
    def test_without_secret_link_is_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            srv = JudgeServer(tmp)
            try:
                status, body, _ = srv.request("GET", "/api/idapp/info")
                self.assertFalse(body["link_enabled"])
                self.assertEqual(srv.request("POST", "/api/idapp/token", {"pairing_code": "ABCDEFGH"}, origin=False)[0], 503)
            finally:
                srv.stop()


if __name__ == "__main__":
    unittest.main()
