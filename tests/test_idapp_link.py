import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import idapp_link
import test_scenarios as ts

SECRET = "unit-test-idapp-secret-0123456789-abcdef"
CODE, EXAM, GRADE = "980320-2", "demo-computer", "2급"


class IdappLinkUnitTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        idapp_link.init_schema(self.db)
        self.secret = SECRET.encode()
        self.active = lambda exam, cand: True
        self.now = 1000.0

    def pair(self, session="s1", cand="c1"):
        return idapp_link.create_pairing(self.db, self.secret, session, "e", cand, self.now)

    def test_secret_must_be_strong(self):
        self.assertIsNone(idapp_link.secret_from_env({}))
        self.assertIsNone(idapp_link.secret_from_env({"EXAM_SAFE_IDAPP_SECRET": "short"}))
        self.assertIsNone(idapp_link.secret_from_env({"EXAM_SAFE_IDAPP_SECRET": "a" * 40}))
        self.assertIsNotNone(idapp_link.secret_from_env({"EXAM_SAFE_IDAPP_SECRET": SECRET}))

    def test_pairing_code_is_single_use_and_expires(self):
        code = self.pair()
        self.assertEqual(idapp_link.redeem_pairing(self.db, self.secret, code, self.now + 1, self.active)[0], "ok")
        self.assertEqual(idapp_link.redeem_pairing(self.db, self.secret, code, self.now + 2, self.active)[0], "used")
        code = self.pair()
        late = self.now + idapp_link.PAIRING_TTL_SECONDS + 1
        self.assertEqual(idapp_link.redeem_pairing(self.db, self.secret, code, late, self.active)[0], "expired")
        self.assertEqual(idapp_link.redeem_pairing(self.db, self.secret, code, self.now + 1, self.active)[0], "used")

    def test_new_pairing_supersedes_old_code(self):
        old = self.pair()
        self.pair()
        self.assertEqual(idapp_link.redeem_pairing(self.db, self.secret, old, self.now + 1, self.active)[0], "used")

    def test_malformed_codes_are_invalid(self):
        for value in (None, 5, "", "ABC", "0O1I0O1I", "ABCDEFGH' OR 1=1"):
            self.assertEqual(idapp_link.redeem_pairing(self.db, self.secret, value, self.now, self.active)[0], "invalid")

    def test_token_expiry_reuse_session_binding_and_inactive_candidate(self):
        def fresh():
            _, token = idapp_link.redeem_pairing(self.db, self.secret, self.pair(), self.now, self.active)
            return token

        verify = lambda token, session="s1", when=None, active=None: idapp_link.verify_token(
            self.db, self.secret, session, "e", "c1", token, when or self.now + 1, active or self.active)
        token = fresh()
        self.assertEqual(verify(token, session="other"), "mismatch")
        self.assertEqual(verify(token), "ok")
        self.assertEqual(verify(token), "used")
        token = fresh()
        self.assertEqual(verify(token, when=self.now + idapp_link.TOKEN_TTL_SECONDS + 1), "expired")
        token = fresh()
        self.assertEqual(verify(token, active=lambda *_: False), "candidate_inactive")
        self.assertEqual(verify("DEMO-ONLY:IDAPP:" + "x" * 43), "invalid")
        self.assertEqual(verify("DEMO-ONLY:SELF:" + "x" * 43), "invalid")

    def test_inactive_candidate_cannot_redeem_and_revoke_invalidates_unused(self):
        code = self.pair()
        self.assertEqual(idapp_link.redeem_pairing(self.db, self.secret, code, self.now, lambda *_: False)[0],
                         "candidate_inactive")
        code = self.pair()
        idapp_link.revoke_inactive(self.db, set(), self.now)
        self.db.commit()
        self.assertEqual(idapp_link.redeem_pairing(self.db, self.secret, code, self.now, self.active)[0], "used")

    def test_only_keyed_digests_are_stored(self):
        code = self.pair()
        _, token = idapp_link.redeem_pairing(self.db, self.secret, code, self.now, self.active)
        dump = "".join(str(tuple(row)) for table in ("idapp_pairings", "idapp_tokens")
                       for row in self.db.execute(f"SELECT * FROM {table}"))
        self.assertNotIn(code, dump)
        self.assertNotIn(token, dump)


class IdappServerTests(unittest.TestCase):
    extra_env = {"EXAM_SAFE_IDAPP_SECRET": SECRET, "EXAM_SAFE_LOGIN_MAX_FAILURES": "1000"}
    setUpClass = classmethod(ts.ServerScenarios.setUpClass.__func__)
    tearDownClass = classmethod(ts.ServerScenarios.tearDownClass.__func__)
    setUp = ts.ServerScenarios.setUp

    def session(self, mode="self_demo", number="2026-0012"):
        sid = self.api.create_kiosk_session()[1]["kiosk_session_id"]
        self.assertEqual(self.api.lookup_kiosk_candidate(sid, number=number, mode=mode)[0], 200)
        return sid

    def pair(self, sid):
        status, body = self.api.request("POST", "/api/kiosk/idapp/pair", {"kiosk_session_id": sid})
        self.assertEqual(status, 200, body)
        return body["pairing_code"]

    def app_token(self, code):
        return self.api.request("POST", "/api/idapp/token", {"pairing_code": code})

    def verify(self, sid, payload, **extra):
        return self.api.request("POST", "/api/kiosk/idapp/verify",
                                {"kiosk_session_id": sid, "qr_payload": payload, **extra})

    def issue(self, sid):
        return self.api.request("POST", "/api/kiosk/demo-auto-issue",
                                {"kiosk_session_id": sid, "demo_simulation": True, "scenario": "demo_info_simulation"})

    def demo_info(self, sid):
        return self.api.request("POST", "/api/kiosk/self-demo/verify-demo-info",
                                {"kiosk_session_id": sid, "virtual_code": CODE, "exam": EXAM, "grade": GRADE})

    def db(self, sql, args=()):
        with sqlite3.connect(self.db_path) as db:
            db.execute(sql, args)

    def test_info_is_public_and_reports_demo_only(self):
        status, body = self.api.request("GET", "/api/idapp/info")
        self.assertEqual(status, 200)
        self.assertTrue(body["link_enabled"] and body["demo_only"])
        self.assertFalse(body["real_authentication"])

    def test_four_step_flow_checks_id_qr_without_admission_or_verified(self):
        sid = self.session()
        code = self.pair(sid)
        status, issued = self.app_token(code)
        self.assertEqual(status, 200, issued)
        token = issued["qr_payload"]
        self.assertTrue(token.startswith("DEMO-ONLY:IDAPP:"))
        self.assertEqual(issued["expires_in"], 30)
        self.assertFalse(issued["real_authentication"] or issued["entry_authorized"])
        for forbidden in ("2026-0012", "박세현", "980320", "컴퓨터"):
            self.assertNotIn(forbidden, str(issued))
        status, checked = self.verify(sid, token)
        self.assertEqual(status, 200, checked)
        self.assertEqual(checked["state"], "idapp_demo_checked")
        self.assertFalse(checked["real_authentication"] or checked["entry_authorized"])
        state = self.api.kiosk_status(sid)[1]
        self.assertNotEqual(state.get("state"), "verified")
        self.assertNotIn("qr_png", state)
        self.assertIn(self.api.request("POST", "/api/kiosk/demo-entry",
                                       {"kiosk_session_id": sid, "demo_token": token})[0], (400, 409))
        # Entry QR is a separate, later step with its own existing DEMO gates.
        self.assertEqual(self.demo_info(sid)[0], 200)
        status, qr = self.issue(sid)
        self.assertEqual(status, 200, qr)
        self.assertEqual(qr["expires_in"], 30)
        self.assertTrue(qr["demo_token"].startswith("DEMO-ONLY:SELF:"))

    def test_entry_qr_is_blocked_until_id_qr_checked_once_link_started(self):
        sid = self.session()
        self.demo_info(sid)
        self.pair(sid)
        status, body = self.issue(sid)
        self.assertEqual(status, 409, body)
        self.assertTrue(body["idapp_required"])

    def test_proctor_approval_also_needs_the_id_qr_check_after_link_started(self):
        sid = self.session(mode="proctor")
        code = self.pair(sid)
        self.assertEqual(self.api.kiosk_review(sid)[0], 409)
        _, issued = self.app_token(code)
        self.assertEqual(self.verify(sid, issued["qr_payload"])[0], 200)
        status, body = self.api.kiosk_review(sid)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["state"], "demo_qr_ready")

    def test_proctor_approval_without_link_is_unchanged(self):
        sid = self.session(mode="proctor")
        self.assertEqual(self.api.kiosk_review(sid)[0], 200)

    def test_pairing_code_reuse_wrong_code_and_expiry(self):
        sid = self.session()
        code = self.pair(sid)
        self.assertEqual(self.app_token(code)[0], 200)
        self.assertEqual(self.app_token(code)[0], 409)
        self.assertEqual(self.app_token("ZZZZZZZZ")[0], 400)
        self.assertEqual(self.app_token("")[0], 400)
        code = self.pair(sid)
        self.db("UPDATE idapp_pairings SET expires_at = ?", (time.time() - 1,))
        self.assertEqual(self.app_token(code)[0], 410)
        self.assertEqual(self.app_token(code)[0], 409)

    def test_token_expiry_reuse_and_session_binding(self):
        sid_a, other = self.session(), self.session()
        _, issued = self.app_token(self.pair(sid_a))
        token = issued["qr_payload"]
        self.pair(other)
        self.assertEqual(self.verify(other, token)[0], 403)
        self.assertEqual(self.verify(sid_a, token)[0], 200)
        status, body = self.verify(sid_a, token)
        self.assertEqual(status, 409)
        self.assertNotEqual(body.get("state"), "idapp_demo_checked")
        _, issued = self.app_token(self.pair(sid_a))
        self.db("UPDATE idapp_tokens SET expires_at = ?", (time.time() - 1,))
        self.assertEqual(self.verify(sid_a, issued["qr_payload"])[0], 410)

    def test_forged_and_malformed_qr_payloads_are_rejected(self):
        sid = self.session()
        self.pair(sid)
        for payload in (None, "", 5, "DEMO-ONLY:SELF:" + "a" * 43, "DEMO-ONLY:IDAPP:" + "a" * 43, "x" * 5000):
            with self.subTest(payload=str(payload)[:20]):
                self.assertEqual(self.verify(sid, payload)[0], 400)
        _, issued = self.app_token(self.pair(sid))
        for field in ("face_match", "score", "verified", "liveness", "real_authentication"):
            status, _ = self.verify(sid, issued["qr_payload"], **{field: True})
            self.assertEqual(status, 400)
        self.assertEqual(self.verify(sid, issued["qr_payload"])[0], 200)  # forged claims did not burn the token

    def test_verify_without_pairing_and_with_bad_session(self):
        sid = self.session()
        self.assertEqual(self.verify(sid, "DEMO-ONLY:IDAPP:" + "a" * 43)[0], 409)
        self.assertEqual(self.verify("nope", "DEMO-ONLY:IDAPP:" + "a" * 43)[0], 410)
        self.assertEqual(self.api.request("POST", "/api/kiosk/idapp/pair", {"kiosk_session_id": "nope"})[0], 410)

    def test_pairing_needs_a_looked_up_candidate(self):
        sid = self.api.create_kiosk_session()[1]["kiosk_session_id"]
        self.assertEqual(self.api.request("POST", "/api/kiosk/idapp/pair", {"kiosk_session_id": sid})[0], 409)

    def test_idapp_token_is_rejected_by_operational_and_other_demo_paths(self):
        sid = self.session()
        _, issued = self.app_token(self.pair(sid))
        token = issued["qr_payload"]
        for path, role, key in (
            ("/api/token/inspect", "proctor", ts.PROCTOR_KEY),
            ("/api/demo/token/inspect", "proctor", ts.PROCTOR_KEY),
            ("/api/approve", "proctor", ts.PROCTOR_KEY),
            ("/api/demo/approve", "proctor", ts.PROCTOR_KEY),
        ):
            with self.subTest(path=path):
                status, body = self.api.request("POST", path, {"token": token, "exam_id": ts.EXAM_ID,
                                                               "candidate_id": ts.CAN_1}, role, key)
                self.assertIn(status, (400, 403, 404, 409, 422), body)
                self.assertNotIn("verified", str(body).lower().replace("not verified", ""))
        self.assertNotEqual(self.api.kiosk_demo_entry(sid, token)[0], 200)
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM admissions").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM demo_admissions").fetchone()[0], 0)

    def test_audit_log_and_database_hold_no_code_token_or_personal_data(self):
        sid = self.session()
        code = self.pair(sid)
        _, issued = self.app_token(code)
        self.verify(sid, issued["qr_payload"])
        with sqlite3.connect(self.db_path) as db:
            dump = "".join(str(row) for table in ("events", "idapp_pairings", "idapp_tokens", "demo_tokens")
                           for row in db.execute(f"SELECT * FROM {table}"))
        token_part = issued["qr_payload"].split(":")[-1]
        for secret in (code, token_part, sid, "980320"):
            self.assertNotIn(secret, dump)

    def test_unknown_app_routes_and_get_on_token_are_closed(self):
        self.assertEqual(self.api.request("GET", "/api/idapp/token")[0], 404)
        self.assertEqual(self.api.request("POST", "/api/idapp/admin", {})[0], 404)

    def test_cross_origin_browser_request_cannot_use_app_endpoint(self):
        import urllib.request
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/idapp/token", data=b'{"pairing_code":"ZZZZZZZZ"}',
            headers={"Content-Type": "application/json", "Origin": "https://evil.example"}, method="POST")
        try:
            urllib.request.urlopen(request)
            status = 200
        except urllib.error.HTTPError as error:
            status = error.code
        self.assertEqual(status, 403)

    def test_reset_clears_link_state(self):
        sid = self.session()
        code = self.pair(sid)
        self.api.request("POST", "/api/demo/reset", {}, "proctor", ts.PROCTOR_KEY)
        self.assertEqual(self.app_token(code)[0], 400)


class IdappRateLimitTests(unittest.TestCase):
    extra_env = {"EXAM_SAFE_IDAPP_SECRET": SECRET, "EXAM_SAFE_LOGIN_MAX_FAILURES": "3"}
    setUpClass = classmethod(ts.ServerScenarios.setUpClass.__func__)
    tearDownClass = classmethod(ts.ServerScenarios.tearDownClass.__func__)

    def test_guessing_pairing_codes_is_rate_limited(self):
        statuses = [self.api.request("POST", "/api/idapp/token", {"pairing_code": "ZZZZZZZZ"})[0] for _ in range(5)]
        self.assertEqual(statuses[:3], [400, 400, 400])
        self.assertEqual(statuses[3:], [429, 429])


class IdappDisabledTests(unittest.TestCase):
    setUpClass = classmethod(ts.ServerScenarios.setUpClass.__func__)
    tearDownClass = classmethod(ts.ServerScenarios.tearDownClass.__func__)

    def test_link_is_fail_closed_without_server_secret(self):
        self.assertFalse(self.api.request("GET", "/api/idapp/info")[1]["link_enabled"])
        self.assertEqual(self.api.request("POST", "/api/idapp/token", {"pairing_code": "ABCDEFGH"})[0], 503)
        sid = self.api.create_kiosk_session()[1]["kiosk_session_id"]
        self.api.lookup_kiosk_candidate(sid, mode="self_demo")
        self.assertEqual(self.api.request("POST", "/api/kiosk/idapp/pair", {"kiosk_session_id": sid})[0], 503)
        self.assertEqual(self.api.request("POST", "/api/kiosk/idapp/verify",
                                          {"kiosk_session_id": sid, "qr_payload": "x"})[0], 503)


if __name__ == "__main__":
    unittest.main()


class SimulatorTests(unittest.TestCase):
    def test_base_url_must_be_https_except_localhost(self):
        from tools import idapp_app_simulator as sim
        self.assertEqual(sim.validate_base_url("https://example.onrender.com/"), "https://example.onrender.com")
        self.assertEqual(sim.validate_base_url("http://127.0.0.1:8000"), "http://127.0.0.1:8000")
        for bad in ("http://example.com", "ftp://x", "example.com", "https://", "javascript:alert(1)"):
            with self.subTest(url=bad):
                with self.assertRaises(ValueError):
                    sim.validate_base_url(bad)


class SimulatorServerTests(unittest.TestCase):
    extra_env = {"EXAM_SAFE_IDAPP_SECRET": SECRET, "EXAM_SAFE_LOGIN_MAX_FAILURES": "1000"}
    setUpClass = classmethod(ts.ServerScenarios.setUpClass.__func__)
    tearDownClass = classmethod(ts.ServerScenarios.tearDownClass.__func__)

    def test_reference_client_gets_token_and_reports_failures_safely(self):
        from tools import idapp_app_simulator as sim
        sid = self.api.create_kiosk_session()[1]["kiosk_session_id"]
        self.api.lookup_kiosk_candidate(sid, mode="self_demo")
        code = self.api.request("POST", "/api/kiosk/idapp/pair", {"kiosk_session_id": sid})[1]["pairing_code"]
        result = sim.request_token(f"http://127.0.0.1:{self.port}", code)
        self.assertTrue(result["qr_payload"].startswith("DEMO-ONLY:IDAPP:"))
        with self.assertRaises(RuntimeError) as caught:
            sim.request_token(f"http://127.0.0.1:{self.port}", code)
        self.assertIn("인증되지 않았습니다", str(caught.exception))
        with self.assertRaises(RuntimeError):
            sim.request_token("http://127.0.0.1:1", code, timeout=2)
