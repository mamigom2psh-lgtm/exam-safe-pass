"""Judge-only Render deployment mode: authentication gate, closed route set, DEMO-only QR, safe startup."""
import http.client
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import auth_config
import judge_mode
import test_scenarios as ts

ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "judge-test-pass-2026"
CODES = {"2026-0012": "980320-2"}
NUMBERS = ["2026-0012", "2026-0013", "2026-0014", "2026-0015", "2026-0016"]


def judge_env(tmp: str, port: int, **extra) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("EXAM_SAFE_", "RENDER", "PORT"))}
    env.update(
        EXAM_SAFE_JUDGE_MODE="1", EXAM_SAFE_JUDGE_PASSWORD=PASSWORD, EXAM_SAFE_DB=str(Path(tmp) / "judge.sqlite3"),
        EXAM_SAFE_PHOTO_DIR=str(Path(tmp) / "photos"), EXAM_SAFE_HOST="127.0.0.1", PORT=str(port),
    )
    env.update(extra)
    return env


class JudgeServer:
    def __init__(self, tmp: str, **extra):
        self.tmp = tmp
        self.port = ts.reserve_port()
        self.extra = extra
        self.process = None
        self.start()

    def start(self):
        self.process = subprocess.Popen(
            [sys.executable, str(ROOT / "server.py")], cwd=self.tmp, env=judge_env(self.tmp, self.port, **self.extra),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            line = self.process.stdout.readline()
            if "LISTENING" in line:
                self.listening = line.strip()
                return
            if self.process.poll() is not None:
                raise RuntimeError("judge server exited: " + line)
        raise RuntimeError("judge server did not start")

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=10)
        if self.process and self.process.stdout:
            self.process.stdout.close()

    def request(self, method, path, payload=None, cookie="", csrf="", origin=True, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        send = {"Accept": "application/json"}
        if payload is not None:
            send["Content-Type"] = "application/json"
        if cookie:
            send["Cookie"] = cookie
        if csrf:
            send["X-CSRF-Token"] = csrf
        if origin and method != "GET":
            send["Origin"] = f"http://127.0.0.1:{self.port}"
        send.update(headers or {})
        connection.request(method, path, json.dumps(payload) if payload is not None else None, send)
        response = connection.getresponse()
        raw = response.read()
        try:
            body = json.loads(raw)
        except ValueError:
            body = raw
        result = (response.status, body, response)
        connection.close()
        return result

    def login(self, password=PASSWORD, **kwargs):
        status, body, response = self.request("POST", "/api/judge/login", {"password": password}, **kwargs)
        cookie = ""
        for name, value in response.getheaders():
            if name.lower() == "set-cookie":
                cookie = value.split(";", 1)[0]
        return status, body, cookie, response


class JudgeBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.srv = JudgeServer(cls.tmp.name)
        status, body, cls.cookie, _ = cls.srv.login()
        assert status == 200, body
        cls.csrf = body["csrf_token"]

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        cls.tmp.cleanup()

    def post(self, path, payload=None, **kw):
        kw.setdefault("cookie", self.cookie)
        kw.setdefault("csrf", self.csrf)
        return self.srv.request("POST", path, {} if payload is None else payload, **kw)

    def new_kiosk(self, number="2026-0012"):
        status, body, _ = self.post("/api/kiosk/session")
        self.assertEqual(status, 201, body)
        sid = body["kiosk_session_id"]
        status, record, _ = self.post("/api/kiosk/candidate", {
            "kiosk_session_id": sid, "exam_id": ts.EXAM_ID, "candidate_number": number, "mode": "self_demo"})
        self.assertEqual(status, 200, record)
        return sid, record


class JudgeAuthGateTests(JudgeBase):
    def test_every_demo_surface_rejects_unauthenticated_requests(self):
        gets = ["/kiosk", "/", "/face_similarity.js", "/vendor/human/human.bundle.js", "/api/dashboard", "/api/session",
                "/api/health", "/api/demo/scenarios", "/api/proctor/overview", "/demo-candidates", "/demo-scenarios",
                "/api/admin/candidates", "/api/admin/session", "/api/candidate-photo/candidate-001", "/images/2026-0012-registration.jpg",
                "/api/judge/session", "/api/kiosk/status", "/anything-else"]
        posts = ["/api/kiosk/session", "/api/kiosk/candidate", "/api/kiosk/demo-auto-issue", "/api/kiosk/demo-entry",
                 "/api/kiosk/self-demo/verify-demo-info", "/api/token", "/api/token/inspect", "/api/demo/token", "/api/demo/approve",
                 "/api/approve", "/api/mobile-id/authenticate", "/api/login", "/api/demo/reset", "/api/judge/reset", "/api/judge/logout",
                 "/api/admin/candidates", "/api/hold", "/api/physical-id/decision"]
        for path in gets:
            status, _, _ = self.srv.request("GET", path)
            self.assertEqual(status, 401, path)
        for path in posts:
            status, _, _ = self.srv.request("POST", path, {})
            self.assertEqual(status, 401, path)
        for method in ("PUT", "DELETE"):
            self.assertEqual(self.srv.request(method, "/api/admin/candidates/x", {})[0], 401)

    def test_html_pages_redirect_to_login_and_login_page_is_data_free(self):
        status, _, response = self.srv.request("GET", "/kiosk", headers={"Accept": "text/html"})
        self.assertEqual((status, response.getheader("Location")), (302, "/judge-login"))
        status, body, _ = self.srv.request("GET", "/judge-login", headers={"Accept": "text/html"})
        self.assertEqual(status, 200)
        for leak in (b"2026-0012", "박세현".encode(), b"980320"):
            self.assertNotIn(leak, body)

    def test_healthz_is_public_and_leaks_nothing(self):
        status, body, _ = self.srv.request("GET", "/healthz")
        self.assertEqual((status, body), (200, {"status": "ok"}))

    def test_wrong_or_malformed_password_is_rejected_without_session(self):
        for bad in ("nope", "", PASSWORD + "x"):
            status, body, cookie, _ = self.srv.login(bad)
            self.assertIn(status, (400, 401), bad)
            self.assertEqual(cookie, "")
        self.assertEqual(self.srv.request("POST", "/api/judge/login", {"password": 123})[0], 400)
        self.assertEqual(self.srv.request("POST", "/api/judge/login", {})[0], 400)

    def test_login_cookie_flags_and_fixation(self):
        status, body, cookie, response = self.srv.login(headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(status, 200)
        header = response.getheader("Set-Cookie")
        for flag in ("HttpOnly", "SameSite=Lax", "Secure", "Max-Age="):
            self.assertIn(flag, header)
        self.assertNotIn(PASSWORD, json.dumps(body))
        # logging in again with an old cookie revokes it
        status, new_body, new_cookie, _ = self.srv.login(cookie=cookie)
        self.assertNotEqual(cookie, new_cookie)
        self.assertEqual(self.srv.request("GET", "/api/judge/session", cookie=cookie)[0], 401)
        self.assertEqual(self.srv.request("GET", "/api/judge/session", cookie=new_cookie)[0], 200)

    def test_cross_origin_login_and_state_changes_are_rejected(self):
        status, _, _ = self.srv.request("POST", "/api/judge/login", {"password": PASSWORD},
                                        headers={"Origin": "https://evil.example"}, origin=False)
        self.assertEqual(status, 403)
        status, _, _ = self.post("/api/kiosk/session", headers={"Origin": "https://evil.example"}, origin=False)
        self.assertEqual(status, 403)

    def test_missing_or_wrong_csrf_is_rejected(self):
        for csrf in ("", "wrong-token"):
            status, _, _ = self.srv.request("POST", "/api/kiosk/session", {}, cookie=self.cookie, csrf=csrf)
            self.assertEqual(status, 403, csrf)

    def test_session_expiry_is_enforced_server_side(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = JudgeServer(tmp, EXAM_SAFE_JUDGE_SESSION_SECONDS="300")
            try:
                status, body, cookie, _ = server.login()
                self.assertEqual(status, 200)
                self.assertEqual(body["expires_in"], 300)
                server.stop()
                with sqlite3.connect(Path(tmp) / "judge.sqlite3") as db:  # simulate time passing: expire stored session
                    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE name LIKE '%session%'")]
                    self.assertTrue(tables)
                    for table in tables:
                        cols = [c[1] for c in db.execute(f"PRAGMA table_info({table})")]
                        if "expires_at" in cols:
                            db.execute(f"UPDATE {table} SET expires_at = 1")
                server.start()
                self.assertEqual(server.request("GET", "/api/judge/session", cookie=cookie)[0], 401)
            finally:
                server.stop()


class JudgeClosedRoutesTests(JudgeBase):
    def test_admin_operational_and_scenario_routes_are_blocked_even_with_judge_session(self):
        for path in ("/demo-candidates", "/api/admin/candidates", "/api/admin/session", "/demo-scenarios", "/api/demo/scenarios",
                     "/api/dashboard", "/api/proctor/overview", "/images/2026-0012-registration.jpg", "/api/session"):
            self.assertEqual(self.srv.request("GET", path, cookie=self.cookie)[0], 403, path)
        for path in ("/api/token", "/api/token/inspect", "/api/demo/token", "/api/demo/approve", "/api/approve", "/api/login",
                     "/api/mobile-id/authenticate", "/api/mobile-id/official-authenticate", "/api/demo/reset", "/api/hold",
                     "/api/admin/candidates", "/api/admin/seed", "/api/physical-id/decision", "/api/exception/decision"):
            self.assertEqual(self.post(path)[0], 403, path)
        for method in ("PUT", "DELETE"):
            self.assertEqual(self.srv.request(method, "/api/admin/candidates/candidate-001", {}, cookie=self.cookie,
                                              csrf=self.csrf)[0], 403)

    def test_role_headers_and_keys_do_not_grant_anything(self):
        for role in ("admin", "proctor", "supervisor", "candidate"):
            headers = {"X-Demo-Role": role, "X-Demo-Key": "demo-admin-key"}
            self.assertEqual(self.srv.request("GET", "/api/admin/candidates", headers=headers)[0], 401)
            self.assertEqual(self.srv.request("POST", "/api/token", {}, headers=headers)[0], 401)
            self.assertEqual(self.srv.request("GET", "/api/admin/candidates", cookie=self.cookie, headers=headers)[0], 403)

    def test_only_the_five_virtual_candidates_with_synthetic_photos_exist(self):
        with sqlite3.connect(Path(self.tmp.name) / "judge.sqlite3") as db:
            rows = db.execute("SELECT number, name, photo_file FROM demo_candidates ORDER BY number").fetchall()
        self.assertEqual([r[0] for r in rows], NUMBERS)
        self.assertTrue(all(r[2] for r in rows))
        self.assertNotIn("candidate-002", json.dumps(rows))
        self.assertNotIn("2026-0099", json.dumps(rows))
        photos = list((Path(self.tmp.name) / "photos").glob("*.png"))
        self.assertEqual(len(photos), 5)
        self.assertEqual(judge_mode.make_avatar(0)[:8], b"\x89PNG\r\n\x1a\n")

    def test_photo_needs_both_judge_session_and_matching_kiosk_session(self):
        sid, record = self.new_kiosk("2026-0013")
        with sqlite3.connect(Path(self.tmp.name) / "judge.sqlite3") as db:
            candidate_id = db.execute("SELECT id FROM demo_candidates WHERE number = '2026-0013'").fetchone()[0]
        path = f"/api/candidate-photo/{candidate_id}"
        self.assertEqual(self.srv.request("GET", path, headers={"X-Kiosk-Session": sid})[0], 401)
        self.assertEqual(self.srv.request("GET", path, cookie=self.cookie)[0], 403)
        status, body, response = self.srv.request("GET", path, cookie=self.cookie, headers={"X-Kiosk-Session": sid})
        self.assertEqual((status, response.getheader("Content-Type")), (200, "image/png"))
        self.assertIn("no-store", response.getheader("Cache-Control"))
        other = self.new_kiosk("2026-0014")[0]
        self.assertEqual(self.srv.request("GET", path, cookie=self.cookie, headers={"X-Kiosk-Session": other})[0], 403)


class JudgeDemoFlowTests(JudgeBase):
    def issue(self, sid):
        return self.post("/api/kiosk/demo-auto-issue",
                         {"kiosk_session_id": sid, "demo_simulation": True, "scenario": "preset_auto_issue"})

    def test_preset_simulation_issues_demo_only_qr_without_camera_or_proctor(self):
        sid, _ = self.new_kiosk("2026-0015")
        status, qr, _ = self.issue(sid)
        self.assertEqual(status, 200, qr)
        self.assertTrue(qr["demo_only"] and qr["simulation"])
        self.assertFalse(qr["real_authentication"])
        self.assertEqual(qr["expires_in"], 30)
        self.assertTrue(qr["demo_token"].startswith("DEMO-ONLY:"))
        for forbidden in ("최서연", "2026-0015", "020711"):
            self.assertNotIn(forbidden, qr["demo_token"])
        self.assertNotEqual(self.post("/api/kiosk/status", {"kiosk_session_id": sid})[1].get("state"), "verified")
        self.assertEqual(self.issue(sid)[0], 409)  # one QR per session

    def test_qr_is_single_use_and_rejected_by_operational_apis(self):
        sid, _ = self.new_kiosk("2026-0016")
        token = self.issue(sid)[1]["demo_token"]
        for path in ("/api/token/inspect", "/api/demo/token/inspect", "/api/approve", "/api/token", "/api/mobile-id/authenticate"):
            status, _, _ = self.post(path, {"token": token, "demo_token": token})
            self.assertEqual(status, 403, path)
        self.assertEqual(self.post("/api/kiosk/demo-entry", {"kiosk_session_id": sid, "demo_token": token})[0], 200)
        self.assertEqual(self.post("/api/kiosk/demo-entry", {"kiosk_session_id": sid, "demo_token": token})[0], 409)
        with sqlite3.connect(Path(self.tmp.name) / "judge.sqlite3") as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM admissions").fetchone()[0], 0)  # no operational admission

    def test_demo_qr_expires_after_30_seconds_on_the_server(self):
        sid, _ = self.new_kiosk("2026-0012")
        token = self.issue(sid)[1]["demo_token"]
        with sqlite3.connect(Path(self.tmp.name) / "judge.sqlite3", timeout=10) as db:
            db.execute("UPDATE demo_tokens SET expires_at = 1")
        status, body, _ = self.post("/api/kiosk/demo-entry", {"kiosk_session_id": sid, "demo_token": token})
        self.assertGreaterEqual(status, 400)

    def test_virtual_info_path_still_checks_server_registered_data(self):
        sid, _ = self.new_kiosk("2026-0013")
        wrong = self.post("/api/kiosk/self-demo/verify-demo-info",
                          {"kiosk_session_id": sid, "virtual_code": "980320-2", "exam": "x", "grade": "심화"})
        self.assertGreaterEqual(wrong[0], 400)
        denied = self.post("/api/kiosk/demo-auto-issue",
                           {"kiosk_session_id": sid, "demo_simulation": True, "scenario": "demo_info_simulation"})
        self.assertEqual(denied[0], 403)

    def test_browser_supplied_scores_cannot_issue_or_verify(self):
        sid, _ = self.new_kiosk("2026-0014")
        status, _, _ = self.post("/api/kiosk/self-demo/request-review",
                                 {"kiosk_session_id": sid, "face_match": True, "score": 0.99, "verified": True})
        self.assertEqual(status, 400)
        self.assertNotIn("qr_png", self.post("/api/kiosk/status", {"kiosk_session_id": sid})[1])

    def test_reset_clears_demo_admissions_but_keeps_virtual_candidates(self):
        sid, _ = self.new_kiosk("2026-0012")
        self.issue(sid)
        status, body, _ = self.post("/api/judge/reset")
        self.assertEqual(status, 200, body)
        with sqlite3.connect(Path(self.tmp.name) / "judge.sqlite3") as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM demo_tokens").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM demo_candidates").fetchone()[0], 5)

    def test_logout_invalidates_session_and_audit_log_has_no_secrets(self):
        status, body, cookie, _ = self.srv.login()
        self.assertEqual(self.srv.request("POST", "/api/judge/logout", {}, cookie=cookie, csrf=body["csrf_token"])[0], 200)
        self.assertEqual(self.srv.request("GET", "/api/judge/session", cookie=cookie)[0], 401)
        with sqlite3.connect(Path(self.tmp.name) / "judge.sqlite3") as db:
            dump = json.dumps(db.execute("SELECT * FROM events").fetchall(), ensure_ascii=False)
        self.assertNotIn(PASSWORD, dump)
        self.assertNotIn("980320-2", dump)


class JudgeRestartAndBindTests(unittest.TestCase):
    def test_binds_to_render_port_variable_on_all_interfaces_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = JudgeServer.__new__(JudgeServer)
            server.tmp, server.port, server.extra, server.process = tmp, ts.reserve_port(), {}, None
            env = judge_env(tmp, server.port)
            del env["EXAM_SAFE_HOST"]
            process = subprocess.Popen([sys.executable, str(ROOT / "server.py")], cwd=tmp, env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                line = ""
                deadline = time.time() + 30
                while "LISTENING" not in line and time.time() < deadline:
                    line = process.stdout.readline()
                self.assertIn(f"LISTENING http://0.0.0.0:{server.port}", line)
                self.assertEqual(server.request("GET", "/healthz")[0], 200)
            finally:
                process.terminate()
                process.wait(timeout=10)
                process.stdout.close()

    def test_restart_reseeds_five_candidates_and_expired_qr_stays_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = JudgeServer(tmp)
            try:
                status, body, cookie, _ = server.login()
                csrf = body["csrf_token"]
                sid = server.request("POST", "/api/kiosk/session", {}, cookie=cookie, csrf=csrf)[1]["kiosk_session_id"]
                server.request("POST", "/api/kiosk/candidate", {"kiosk_session_id": sid, "exam_id": ts.EXAM_ID,
                               "candidate_number": "2026-0012", "mode": "self_demo"}, cookie=cookie, csrf=csrf)
                qr = server.request("POST", "/api/kiosk/demo-auto-issue", {"kiosk_session_id": sid, "demo_simulation": True,
                                    "scenario": "preset_auto_issue"}, cookie=cookie, csrf=csrf)[1]
                with sqlite3.connect(Path(tmp) / "judge.sqlite3", timeout=10) as db:
                    db.execute("UPDATE demo_tokens SET expires_at = 1")
                server.stop()
                server.start()
                status, _, _ = server.request("POST", "/api/kiosk/demo-entry", {"kiosk_session_id": sid, "demo_token": qr["demo_token"]},
                                              cookie=cookie, csrf=csrf)
                self.assertGreaterEqual(status, 400)
                with sqlite3.connect(Path(tmp) / "judge.sqlite3") as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM demo_candidates").fetchone()[0], 5)
            finally:
                server.stop()

    def test_fresh_filesystem_recreates_database_and_photos(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = JudgeServer(tmp)
            server.stop()
            for path in Path(tmp).glob("judge.sqlite3*"):
                path.unlink()
            for path in (Path(tmp) / "photos").glob("**/*"):
                if path.is_file():
                    path.unlink()
            server.start()
            try:
                with sqlite3.connect(Path(tmp) / "judge.sqlite3") as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM demo_candidates").fetchone()[0], 5)
                self.assertEqual(len(list((Path(tmp) / "photos").glob("*.png"))), 5)
            finally:
                server.stop()


class JudgeRateLimitTests(unittest.TestCase):
    def test_repeated_failures_are_rate_limited_then_correct_password_is_still_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = JudgeServer(tmp, EXAM_SAFE_LOGIN_MAX_FAILURES="3")
            try:
                statuses = [server.login("wrong-password")[0] for _ in range(5)]
                self.assertEqual(statuses[:3], [401, 401, 401])
                self.assertEqual(statuses[3:], [429, 429])
                status, _, cookie, response = server.login()
                self.assertEqual(status, 429)
                self.assertEqual(cookie, "")
                self.assertTrue(response.getheader("Retry-After"))
            finally:
                server.stop()

    def test_proxy_header_is_ignored_unless_proxy_is_trusted(self):
        self.assertEqual(judge_mode.client_key({"X-Forwarded-For": "1.2.3.4, 5.6.7.8"}, "10.0.0.1", False), "10.0.0.1")
        self.assertEqual(judge_mode.client_key({"X-Forwarded-For": "1.2.3.4, 5.6.7.8"}, "10.0.0.1", True), "5.6.7.8")
        self.assertEqual(judge_mode.client_key({"X-Forwarded-For": "not an ip"}, "10.0.0.1", True), "10.0.0.1")


class JudgeStartupGuardTests(unittest.TestCase):
    def run_server(self, tmp, **extra):
        env = judge_env(tmp, 0, **extra)
        for key in [k for k, v in extra.items() if v is None]:
            env.pop(key, None)
        return subprocess.run([sys.executable, str(ROOT / "server.py")], cwd=tmp, env=env, capture_output=True, text=True, timeout=30)

    def test_startup_fails_without_or_with_weak_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            for extra in ({"EXAM_SAFE_JUDGE_PASSWORD": None}, {"EXAM_SAFE_JUDGE_PASSWORD": "short"},
                          {"EXAM_SAFE_JUDGE_PASSWORD": "changeme"}, {"EXAM_SAFE_JUDGE_PASSWORD": " leading-space-pass "},
                          {"EXAM_SAFE_JUDGE_PASSWORD": None, "EXAM_SAFE_JUDGE_PASSWORD_HASH": "plain-text-not-a-hash"}):
                result = self.run_server(tmp, **extra)
                self.assertEqual(result.returncode, 2, (extra, result.stdout, result.stderr))
                self.assertNotIn("LISTENING", result.stdout)
                self.assertNotIn("short", result.stderr.replace("EXAM_SAFE_JUDGE_PASSWORD", ""))

    def test_judge_mode_refuses_development_database_and_photo_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            for extra in ({"EXAM_SAFE_DB": str(ROOT / "exam_safe_pass.sqlite3")},
                          {"EXAM_SAFE_PHOTO_DIR": str(ROOT / "images")},
                          {"EXAM_SAFE_PHOTO_DIR": str(ROOT / "demo_photos")}):
                result = self.run_server(tmp, **extra)
                self.assertEqual(result.returncode, 2, (extra, result.stderr))

    def test_hash_form_of_password_is_accepted_and_plaintext_not_needed(self):
        stored = auth_config.hash_password(PASSWORD)
        with tempfile.TemporaryDirectory() as tmp:
            server = JudgeServer(tmp, EXAM_SAFE_JUDGE_PASSWORD="", EXAM_SAFE_JUDGE_PASSWORD_HASH=stored)
            try:
                self.assertEqual(server.login()[0], 200)
                self.assertEqual(server.login("other-password-123")[0], 401)
            finally:
                server.stop()

    def test_render_external_url_is_trusted_origin_only_when_configured(self):
        self.assertEqual(judge_mode.render_origin({"RENDER_EXTERNAL_URL": "https://judge-demo.onrender.com"}),
                         "https://judge-demo.onrender.com")
        self.assertIsNone(judge_mode.render_origin({}))
        self.assertIsNone(judge_mode.render_origin({"RENDER_EXTERNAL_URL": "javascript:alert(1)"}))

    def test_route_allowlist_is_closed_by_default(self):
        self.assertTrue(judge_mode.route_allowed("GET", "/kiosk"))
        self.assertTrue(judge_mode.route_allowed("POST", "/api/kiosk/demo-auto-issue"))
        for method, path in (("GET", "/demo-candidates"), ("POST", "/api/token"), ("PUT", "/api/kiosk/x"),
                             ("DELETE", "/api/kiosk/x"), ("POST", "/api/admin/seed")):
            self.assertFalse(judge_mode.route_allowed(method, path), (method, path))


if __name__ == "__main__":
    unittest.main()
