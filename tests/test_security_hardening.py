"""Regression tests for the security/stability hardening: secrets, origin/CSRF, uploads, sessions, admin page."""
import http.client
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import auth_config
import demo_candidates as dc
import session_store
import test_scenarios as ts
from tests.test_demo_candidates import RegistryBase, make_png, COMPUTER

ROOT = Path(__file__).resolve().parent.parent
GOOD_ENV = {
    "EXAM_SAFE_ADMIN_PASSWORD_HASH": auth_config.hash_password("Adm1n-unit-test-pw"),
    "EXAM_SAFE_ADMIN_KEY": "unit-test-admin-key-0123456789",
}


class AuthConfigTests(unittest.TestCase):
    def test_missing_admin_secrets_fail_closed(self):
        with self.assertRaises(auth_config.ConfigError):
            auth_config.load_auth_config({}, 8000)
        with self.assertRaises(auth_config.ConfigError):
            auth_config.load_auth_config({"EXAM_SAFE_ADMIN_KEY": GOOD_ENV["EXAM_SAFE_ADMIN_KEY"]}, 8000)

    def test_default_and_plaintext_credentials_are_rejected(self):
        for weak in ("admin-demo-123", "password"):
            env = dict(GOOD_ENV, EXAM_SAFE_ADMIN_PASSWORD_HASH=auth_config.hash_password(weak))
            with self.assertRaises(auth_config.ConfigError, msg=weak):
                auth_config.load_auth_config(env, 8000)
        with self.assertRaises(auth_config.ConfigError):  # plaintext is not a hash
            auth_config.load_auth_config(dict(GOOD_ENV, EXAM_SAFE_ADMIN_PASSWORD_HASH="a-plain-password-value"), 8000)
        for key in ("demo-admin-key", "short"):
            with self.assertRaises(auth_config.ConfigError, msg=key):
                auth_config.load_auth_config(dict(GOOD_ENV, EXAM_SAFE_ADMIN_KEY=key), 8000)

    def test_valid_config_and_password_hashing(self):
        config = auth_config.load_auth_config(GOOD_ENV, 8000)
        self.assertTrue(auth_config.verify_password("Adm1n-unit-test-pw", config.accounts["admin"]["password_hash"]))
        self.assertFalse(auth_config.verify_password("wrong", config.accounts["admin"]["password_hash"]))
        self.assertNotIn("Adm1n-unit-test-pw", config.accounts["admin"]["password_hash"])
        self.assertNotIn("proctor", config.accounts)  # roles without secrets stay disabled
        self.assertNotEqual(auth_config.hash_password("x" * 12), auth_config.hash_password("x" * 12))  # salted

    def test_server_process_refuses_to_start_without_or_with_default_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = {k: v for k, v in os.environ.items() if not k.startswith("EXAM_SAFE_")}
            base.update(EXAM_SAFE_DB=str(Path(tmp) / "x.sqlite3"), EXAM_SAFE_PORT="0", EXAM_SAFE_PHOTO_DIR=tmp,
                        EXAM_SAFE_LEGACY_IMAGE_DIR=tmp)
            for extra in ({}, {"EXAM_SAFE_ADMIN_KEY": "demo-admin-key",
                               "EXAM_SAFE_ADMIN_PASSWORD_HASH": auth_config.hash_password("admin-demo-123")}):
                result = subprocess.run([sys.executable, str(ROOT / "server.py")], cwd=tmp, env={**base, **extra},
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("서버를 시작할 수 없습니다", result.stderr)
                self.assertNotIn("LISTENING", result.stdout)
                self.assertNotIn("admin-demo-123", result.stderr + result.stdout)


class OriginPolicyTests(unittest.TestCase):
    allowed = frozenset({"https://abc-8000.app.github.dev", "http://127.0.0.1:8000"})

    def check(self, **headers):
        return auth_config.origin_allowed(headers, self.allowed)

    def test_origin_must_match_exactly(self):
        self.assertTrue(self.check(Origin="https://abc-8000.app.github.dev"))
        self.assertTrue(self.check(Origin="HTTPS://ABC-8000.app.github.dev"))
        for bad in ("https://evil.example", "https://abc-8000.app.github.dev.evil.example", "http://abc-8000.app.github.dev",
                    "null", "", "https://abc-8000.app.github.dev/path", "https://abc-8000.app.github.dev:444"):
            self.assertFalse(self.check(Origin=bad), bad)

    def test_missing_origin_uses_referer_then_fetch_metadata(self):
        self.assertTrue(self.check(Referer="https://abc-8000.app.github.dev/kiosk"))
        self.assertFalse(self.check(Referer="https://evil.example/x"))
        self.assertFalse(self.check(**{"Sec-Fetch-Site": "cross-site"}))
        self.assertTrue(self.check())  # non-browser client; cookie sessions still need the CSRF token

    def test_host_headers_are_not_trusted_and_wildcards_rejected(self):
        self.assertFalse(self.check(Origin="https://evil.example", Host="abc-8000.app.github.dev",
                                    **{"X-Forwarded-Host": "abc-8000.app.github.dev"}))
        with self.assertRaises(auth_config.ConfigError):
            auth_config.trusted_origins({"EXAM_SAFE_ALLOWED_ORIGINS": "https://*.github.dev"}, 8000)
        origins = auth_config.trusted_origins({"CODESPACE_NAME": "demo", "GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN": "app.github.dev"}, 8000)
        self.assertIn("https://demo-8000.app.github.dev", origins)

    def test_login_rate_limiter(self):
        limiter = auth_config.LoginRateLimiter(max_failures=3, window_seconds=60)
        for _ in range(3):
            self.assertEqual(limiter.retry_after("k"), 0)
            limiter.record_failure("k")
        self.assertGreater(limiter.retry_after("k"), 0)
        self.assertEqual(limiter.retry_after("other"), 0)
        limiter.reset("k")
        self.assertEqual(limiter.retry_after("k"), 0)


class SessionStoreTests(unittest.TestCase):
    def make(self, path, **kwargs):
        from contextlib import contextmanager

        @contextmanager
        def connect():
            connection = sqlite3.connect(path)
            try:
                yield connection
                connection.commit()
            finally:
                connection.close()

        return session_store.PersistentSessions(connect, "t", lambda item: item["exp"], **kwargs)

    def test_sessions_survive_restart_but_never_revive_or_extend(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.sqlite3"
            store = self.make(path, hash_keys=True)
            store["live-cookie"] = {"exp": time.time() + 600, "role": "admin", "demo_token": "SECRET-TOKEN", "qr_png": b"png"}
            store["old-cookie"] = {"exp": time.time() + 600, "role": "admin"}
            store.flush()
            store["old-cookie"]["exp"] = time.time() - 1  # expires while the server is down
            store.flush()
            raw = sqlite3.connect(path).execute("SELECT session_key, data FROM session_store").fetchall()
            blob = json.dumps(raw)
            self.assertNotIn("live-cookie", blob)  # only a hash of the id is stored
            self.assertNotIn("SECRET-TOKEN", blob)  # transient secrets are never persisted
            reloaded = self.make(path, hash_keys=True)
            self.assertEqual(reloaded.get("live-cookie")["role"], "admin")
            self.assertIsNone(reloaded.get("old-cookie"))
            self.assertNotIn("demo_token", reloaded.get("live-cookie"))

    def test_pop_and_remove_where_are_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.sqlite3"
            store = self.make(path)
            for name in "abc":
                store[name] = {"exp": time.time() + 600, "candidate_id": name}
            store.flush()
            store.pop("a")
            self.assertEqual(store.remove_where(lambda item: item["candidate_id"] == "b"), 1)
            store.flush()
            self.assertEqual([key for key, _ in self.make(path).items()], ["c"])


class HardeningServerTests(RegistryBase):
    def login_cookie(self, password=None):
        return self.raw("POST", "/api/login", {"role": "admin", "username": "admin-demo", "password": password or ts.ADMIN_PASSWORD})

    def test_admin_page_requires_session_and_is_not_cached(self):
        status, headers, page = self.raw("GET", "/demo-candidates")
        self.assertEqual(status, 401)
        self.assertIn("no-store", headers["Cache-Control"])
        self.assertIn(b"loginForm", page)
        self.assertNotIn(b"candidateRows", page)  # the admin UI is not delivered
        self.assertEqual(self.raw("GET", "/api/admin/demo-candidates")[0], 403)
        self.assertEqual(self.raw("GET", "/api/admin/session")[0], 401)
        _, headers, _ = self.login_cookie()
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        status, headers, page = self.raw("GET", "/demo-candidates", headers={"Cookie": cookie.split(";")[0]})
        self.assertEqual(status, 200)
        self.assertIn(b"candidateRows", page)
        self.assertIn("no-store", headers["Cache-Control"])
        # a non-admin cookie must not open the page
        _, p_headers, _ = self.raw("POST", "/api/login", {"role": "proctor", "username": "proctor-demo", "password": ts.PROCTOR_PASSWORD})
        self.assertEqual(self.raw("GET", "/demo-candidates", headers={"Cookie": p_headers["Set-Cookie"].split(";")[0]})[0], 401)

    def test_secure_cookie_flag_behind_https_proxy_and_session_id_rotates(self):
        _, first, _ = self.raw("POST", "/api/login", {"role": "admin", "username": "admin-demo", "password": ts.ADMIN_PASSWORD},
                               {"X-Forwarded-Proto": "https"})
        self.assertIn("Secure", first["Set-Cookie"])
        old = first["Set-Cookie"].split(";")[0]
        _, second, _ = self.raw("POST", "/api/login", {"role": "admin", "username": "admin-demo", "password": ts.ADMIN_PASSWORD}, {"Cookie": old})
        self.assertNotEqual(second["Set-Cookie"].split(";")[0], old)
        self.assertEqual(self.raw("GET", "/api/admin/demo-candidates", headers={"Cookie": old})[0], 403)  # fixation: old id dead

    def test_untrusted_origin_is_rejected_for_state_changes(self):
        _, headers, content = self.login_cookie()
        cookie, token = headers["Set-Cookie"].split(";")[0], json.loads(content)["csrf_token"]
        payload = {"name": "출처", "number": "2027-9101", "virtual_code": "990101-1", "exam_ref": COMPUTER, "grade": "1급"}
        good = {"Cookie": cookie, "X-CSRF-Token": token}
        for origin in ("https://evil.example", "null"):
            status, _, body = self.raw("POST", "/api/admin/demo-candidates", payload, {**good, "Origin": origin})
            self.assertEqual(status, 403, origin)
        status, _, _ = self.raw("POST", "/api/admin/demo-candidates", payload, {**good, "Referer": "https://evil.example/p"})
        self.assertEqual(status, 403)
        self.assertEqual(self.raw("POST", "/api/login", {"role": "admin", "username": "admin-demo", "password": ts.ADMIN_PASSWORD},
                                  {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.db("SELECT COUNT(*) FROM demo_candidates WHERE number = '2027-9101'")[0][0], 0)
        trusted = f"http://127.0.0.1:{self.port}"
        self.assertEqual(self.raw("POST", "/api/admin/demo-candidates", payload, {**good, "Origin": trusted})[0], 201)
        # trusted origin but wrong/missing CSRF token still fails
        payload["number"] = "2027-9102"
        self.assertEqual(self.raw("POST", "/api/admin/demo-candidates", payload, {"Cookie": cookie, "Origin": trusted})[0], 403)

    def test_login_is_rate_limited_and_never_accepts_old_defaults(self):
        for _ in range(3):
            self.assertEqual(self.raw("POST", "/api/login", {"role": "supervisor", "username": "supervisor-demo", "password": "supervisor-demo-123"})[0], 401)
        for _ in range(2):
            self.raw("POST", "/api/login", {"role": "supervisor", "username": "supervisor-demo", "password": "x"})
        status, headers, _ = self.raw("POST", "/api/login", {"role": "supervisor", "username": "supervisor-demo", "password": ts.TEST_PASSWORDS["supervisor"]})
        self.assertEqual(status, 429)
        self.assertIn("Retry-After", headers)
        self.assertEqual(self.raw("GET", "/api/admin/demo-candidates", headers={"X-Demo-Role": "admin", "X-Demo-Key": "demo-admin-key"})[0], 403)

    def test_corrupt_disguised_and_bomb_images_are_rejected(self):
        cand = self.create()
        png = make_png()
        bomb = bytearray(png)
        bomb[16:24] = (30000).to_bytes(4, "big") * 2  # IHDR claims 30000x30000 (CRC no longer matches)
        cases = {
            "text with png name": b"\x89PNG\r\n\x1a\n" + b"not really an image " * 10,
            "truncated png": png[:-20],
            "html payload": b"<html><script>alert(1)</script></html>" * 3,
            "polyglot trailing script": b"GIF89a" + b"<script>" * 20,
            "bomb header": bytes(bomb),
            "jpeg without data": b"\xff\xd8\xff\xe0" + b"\x00" * 60,
        }
        for label, data in cases.items():
            status, _ = self.admin("POST", f"/api/admin/demo-candidates/{cand['id']}/photo", binary=data)
            self.assertIn(status, (400, 415), label)
        self.assertEqual(self.db("SELECT photo_file FROM demo_candidates WHERE id = ?", cand["id"])[0][0], None)

    def test_valid_jpeg_and_png_are_reencoded_without_metadata(self):
        from PIL import Image
        cand = self.create()
        image = Image.new("RGB", (400, 500), (120, 80, 60))
        exif = Image.Exif()
        exif[0x010F] = "SecretCameraMaker"
        exif[0x0112] = 6  # rotate: stored photo must be upright
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", exif=exif.tobytes())
        original = buffer.getvalue() + b"TRAILING-PAYLOAD"
        self.assertIn(b"SecretCameraMaker", original)
        status, body = self.admin("POST", f"/api/admin/demo-candidates/{cand['id']}/photo", binary=original.rstrip(b"TRAILING-PAYLOAD"))
        self.assertEqual(status, 200, body)
        stored_name = self.db("SELECT photo_file FROM demo_candidates WHERE id = ?", cand["id"])[0][0]
        self.assertRegex(stored_name, r"^[0-9a-f]{32}\.jpg$")
        stored = (Path(type(self).temp_dir.name) / "demo_photos" / stored_name).read_bytes()
        self.assertNotIn(b"SecretCameraMaker", stored)
        self.assertEqual(Image.open(io.BytesIO(stored)).size, (500, 400))  # EXIF orientation applied then dropped
        self.assertEqual(self.upload(cand["id"])[0], 200)  # PNG replaces JPEG
        self.assertEqual(self.upload(cand["id"], shade=40)[0], 200)
        status, headers, served = self.raw("GET", f"/api/candidate-photo/{cand['id']}", headers={"X-Demo-Role": "admin", "X-Demo-Key": ts.ADMIN_KEY})
        self.assertEqual((status, headers["Content-Type"]), (200, "image/png"))
        self.assertIn("no-store", headers["Cache-Control"])
        self.assertEqual(self.raw("GET", f"/api/candidate-photo/{cand['id']}")[0], 403)
        self.assertEqual(self.raw("GET", f"/images/{stored_name}")[0], 404)

    def verified_session(self, number):
        sid, status, record = self.kiosk(number)
        self.assertEqual(status, 200, record)
        status, body = self.api.request("POST", "/api/kiosk/self-demo/verify-demo-info",
                                        {"kiosk_session_id": sid, "virtual_code": "990101-1", "exam": COMPUTER, "grade": "2급"})
        self.assertEqual(status, 200, body)
        return sid

    def issue(self, sid):
        return self.api.request("POST", "/api/kiosk/demo-auto-issue",
                                {"kiosk_session_id": sid, "demo_simulation": True, "scenario": "demo_info_simulation"})

    def test_deactivation_invalidates_session_and_unused_demo_token(self):
        cand = self.create(number="2027-9201")
        control = self.verified_session("2027-9201")
        self.assertEqual(self.issue(control)[0], 200)  # the flow works while the candidate is active
        sid = self.verified_session("2027-9201")
        self.assertEqual(self.admin("PUT", f"/api/admin/demo-candidates/{cand['id']}", {"active": False})[0], 200)
        status, body = self.issue(sid)  # session is gone: a safe 4xx, never a 500
        self.assertIn(status, (404, 409, 410), body)
        self.assertEqual(self.db("SELECT COUNT(*) FROM session_store WHERE namespace = 'kiosk' AND data LIKE ?", f'%{cand["id"]}%')[0][0], 0)
        status, body = self.api.request("POST", "/api/kiosk/status", {"kiosk_session_id": control})
        self.assertLess(status, 500)
        self.assertNotEqual(self.kiosk("2027-9201")[1], 200)  # a new session cannot start for an inactive candidate

    def test_deleted_candidate_with_stale_session_gets_4xx(self):
        cand = self.create(number="2027-9203")
        sid = self.verified_session("2027-9203")
        self.assertEqual(self.admin("DELETE", f"/api/admin/demo-candidates/{cand['id']}?confirm=2027-9203")[0], 200)
        status, body = self.issue(sid)
        self.assertIn(status, (404, 409, 410), body)

    def test_unused_demo_tokens_of_deactivated_candidate_are_burned(self):
        cand = self.create(number="2027-9202")
        now = time.time()
        with sqlite3.connect(self.db_path) as db:
            columns = [row[1] for row in db.execute("PRAGMA table_info(demo_tokens)")]
        self.assertIn("used_at", columns)
        with sqlite3.connect(self.db_path) as db:
            db.execute("INSERT INTO demo_tokens (token_hash, exam_id, candidate_id, issued_at, expires_at, pathway) VALUES (?,?,?,?,?,?)",
                       ("hash-burn-test", "exam-demo-2026", cand["id"], now, now + 30, "demo_info_simulation"))
        self.assertEqual(self.admin("PUT", f"/api/admin/demo-candidates/{cand['id']}", {"active": False})[0], 200)
        self.assertIsNotNone(self.db("SELECT used_at FROM demo_tokens WHERE token_hash = 'hash-burn-test'")[0][0])


class RestartPersistenceTests(unittest.TestCase):
    def start(self, tmp, port):
        env = {k: v for k, v in os.environ.items() if not k.startswith("EXAM_SAFE_")}
        env.update(EXAM_SAFE_DB=str(Path(tmp) / "r.sqlite3"), EXAM_SAFE_PHOTO_DIR=str(Path(tmp) / "p"),
                   EXAM_SAFE_LEGACY_IMAGE_DIR=str(Path(tmp) / "none"), EXAM_SAFE_HOST="127.0.0.1", EXAM_SAFE_PORT=str(port))
        env.update(ts.auth_env())
        process = subprocess.Popen([sys.executable, str(ROOT / "server.py")], cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True)
        for line in iter(process.stdout.readline, ""):
            if line.startswith("LISTENING"):
                return process
        raise RuntimeError("server did not start")

    def stop(self, process):
        process.terminate()
        process.wait(timeout=10)
        process.stdout.close()

    def call(self, port, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(method, path, json.dumps(body) if body is not None else None,
                     {"Content-Type": "application/json", **(headers or {})})
        response = conn.getresponse()
        data = response.read()
        result = (response.status, dict(response.getheaders()), json.loads(data) if data[:1] == b"{" else data)
        conn.close()
        return result

    def test_sessions_survive_restart_and_expired_ones_stay_dead(self):
        with tempfile.TemporaryDirectory() as tmp:
            port = ts.reserve_port()
            process = self.start(tmp, port)
            try:
                _, headers, _ = self.call(port, "POST", "/api/login", {"role": "admin", "username": "admin-demo", "password": ts.ADMIN_PASSWORD})
                cookie = headers["Set-Cookie"].split(";")[0]
                _, _, kiosk = self.call(port, "POST", "/api/kiosk/session", {})
            finally:
                self.stop(process)
            process = self.start(tmp, port)
            try:
                self.assertTrue(self.call(port, "GET", "/api/session", headers={"Cookie": cookie})[2]["logged_in"])
                self.assertEqual(self.call(port, "GET", "/api/admin/session", headers={"Cookie": cookie})[0], 200)
            finally:
                self.stop(process)
            with sqlite3.connect(Path(tmp) / "r.sqlite3") as db:  # time passes while the server is down
                db.execute("UPDATE session_store SET expires_at = ?", (time.time() - 5,))
                data = db.execute("SELECT session_key, data FROM session_store WHERE namespace = 'kiosk'").fetchall()
                for key, blob in data:
                    item = json.loads(blob)
                    item["last_activity"] = time.time() - 100000
                    db.execute("UPDATE session_store SET data = ?, expires_at = ? WHERE session_key = ?",
                               (json.dumps(item), time.time() + 100, key))
            process = self.start(tmp, port)
            try:
                self.assertFalse(self.call(port, "GET", "/api/session", headers={"Cookie": cookie})[2]["logged_in"])
                self.assertEqual(self.call(port, "GET", "/api/admin/session", headers={"Cookie": cookie})[0], 401)
                status, _, _ = self.call(port, "POST", "/api/kiosk/candidate", {"kiosk_session_id": kiosk["kiosk_session_id"],
                                         "exam_id": "exam-demo-2026", "candidate_number": "2026-0012", "mode": "self_demo"})
                self.assertIn(status, (404, 410))
            finally:
                self.stop(process)

    def test_logout_invalidates_session_on_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            port = ts.reserve_port()
            process = self.start(tmp, port)
            try:
                _, headers, body = self.call(port, "POST", "/api/login", {"role": "admin", "username": "admin-demo", "password": ts.ADMIN_PASSWORD})
                cookie = headers["Set-Cookie"].split(";")[0]
                self.assertEqual(self.call(port, "POST", "/api/logout", {}, {"Cookie": cookie})[0], 403)  # needs CSRF token
                self.assertEqual(self.call(port, "POST", "/api/logout", {}, {"Cookie": cookie, "X-CSRF-Token": body["csrf_token"]})[0], 200)
                self.assertEqual(self.call(port, "GET", "/api/admin/session", headers={"Cookie": cookie})[0], 401)
            finally:
                self.stop(process)


if __name__ == "__main__":
    unittest.main()
