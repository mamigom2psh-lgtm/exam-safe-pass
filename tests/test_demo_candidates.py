import http.client
import json
import sqlite3
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

import demo_candidates as dc
import test_scenarios as ts

COMPUTER, HISTORY = "demo-computer", "demo-korean-history"


def make_png(width=320, height=320, shade=100):
    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    rows = b"".join(b"\x00" + bytes([shade, (shade * 2) % 256, 200]) * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def pixels(data: bytes) -> bytes:
    """Stored photos are re-encoded by the server, so compare decoded pixels rather than raw bytes."""
    from PIL import Image
    import io
    return Image.open(io.BytesIO(data)).convert("RGB").tobytes()


class RegistryBase(unittest.TestCase):
    setUpClass = classmethod(ts.ServerScenarios.setUpClass.__func__)
    tearDownClass = classmethod(ts.ServerScenarios.tearDownClass.__func__)
    setUp = ts.ServerScenarios.setUp
    counter = 100

    def raw(self, method, path, body=None, headers=None, binary=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = dict(headers or {})
        data = binary
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        content = response.read()
        result = (response.status, dict(response.getheaders()), content)
        conn.close()
        return result

    @property
    def port(self):
        return type(self).port

    def admin(self, method, path, body=None, binary=None):
        status, headers, content = self.raw(method, path, body, {"X-Demo-Role": "admin", "X-Demo-Key": ts.ADMIN_KEY}, binary)
        try:
            return status, json.loads(content) if content[:1] in (b"{", b"[") else content
        except ValueError:
            return status, content

    def create(self, name="테스트", exam=COMPUTER, grade="2급", code="990101-1", number=None, **extra):
        type(self).counter += 1
        number = number or f"2027-{type(self).counter:04d}"
        status, body = self.admin("POST", "/api/admin/demo-candidates", {
            "name": name, "number": number, "virtual_code": code, "exam_ref": exam, "grade": grade,
            "round": "테스트 회차", **extra})
        self.assertEqual(status, 201, body)
        return body["candidate"]

    def upload(self, candidate_id, shade=100, **size):
        return self.admin("POST", f"/api/admin/demo-candidates/{candidate_id}/photo", binary=make_png(shade=shade, **size))

    def kiosk(self, number, mode="self_demo"):
        status, body = ts.DemoApi.request(self.api, "POST", "/api/kiosk/session", {})
        sid = body["kiosk_session_id"]
        status, record = self.api.lookup_kiosk_candidate(sid, number=number, mode=mode)
        return sid, status, record

    def db(self, query, *args):
        with sqlite3.connect(self.db_path) as db:
            return db.execute(query, args).fetchall()


class AdminAndSeedTests(RegistryBase):
    def test_registry_api_requires_server_side_admin(self):
        self.assertEqual(self.raw("GET", "/api/admin/demo-candidates")[0], 403)
        self.assertEqual(self.raw("GET", "/api/admin/demo-candidates", headers={"X-Demo-Role": "proctor", "X-Demo-Key": ts.PROCTOR_KEY})[0], 403)
        self.assertEqual(self.raw("GET", "/api/admin/demo-candidates", headers={"X-Demo-Role": "admin", "X-Demo-Key": "wrong"})[0], 403)
        self.assertEqual(self.raw("POST", "/api/admin/demo-candidates", {"name": "x"})[0], 403)
        self.assertEqual(self.raw("DELETE", "/api/admin/demo-candidates/candidate-001?confirm=2026-0012")[0], 403)
        status, _, page = self.raw("GET", "/demo-candidates")
        self.assertEqual(status, 401)  # unauthenticated: only the data-free login form is returned
        self.assertNotIn("candidate-001".encode(), page)
        status, body = self.admin("GET", "/api/admin/demo-candidates")
        self.assertEqual(status, 200)
        self.assertNotIn("980320", json.dumps(body))  # virtual codes are masked in the list

    def test_cookie_session_writes_need_csrf_token(self):
        status, headers, content = self.raw("POST", "/api/login", {"role": "admin", "username": "admin-demo", "password": ts.ADMIN_PASSWORD})
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"].split(";")[0]
        token = json.loads(content)["csrf_token"]
        payload = {"name": "씨에스", "number": "2027-9001", "virtual_code": "990101-1", "exam_ref": COMPUTER, "grade": "1급"}
        self.assertEqual(self.raw("POST", "/api/admin/demo-candidates", payload, {"Cookie": cookie})[0], 403)
        self.assertEqual(self.raw("POST", "/api/admin/demo-candidates", payload, {"Cookie": cookie, "X-CSRF-Token": "bad"})[0], 403)
        self.assertEqual(self.raw("GET", "/api/admin/demo-candidates", headers={"Cookie": cookie})[0], 200)
        self.assertEqual(self.raw("POST", "/api/admin/demo-candidates", payload, {"Cookie": cookie, "X-CSRF-Token": token})[0], 201)

    def test_legacy_2026_0012_is_migrated_and_preserved(self):
        status, body = self.admin("GET", "/api/admin/demo-candidates")
        legacy = {c["number"]: c for c in body["candidates"]}["2026-0012"]
        self.assertEqual((legacy["id"], legacy["name"], legacy["exam_name"], legacy["grade"]), ("candidate-001", "박세현", "컴퓨터활용능력", "2급"))
        self.assertEqual(self.admin("POST", "/api/admin/demo-candidates/seed", {})[1]["already_present"].count("2026-0012"), 1)
        self.assertEqual(self.admin("POST", "/api/admin/demo-candidates/seed", {})[1]["created"], [])  # idempotent

    def test_seed_registers_five_virtual_candidates_without_photos_and_reports_conflicts(self):
        status, result = self.admin("POST", "/api/admin/demo-candidates/seed", {})
        self.assertEqual(status, 200, result)
        self.assertFalse(result["photos_assigned"])
        status, body = self.admin("GET", "/api/admin/demo-candidates")
        by_number = {c["number"]: c for c in body["candidates"]}
        expected = {"2026-0012": ("박세현", "컴퓨터활용능력", "2급"), "2026-0013": ("김하늘", "한국사능력검정시험", "심화"),
                    "2026-0014": ("이준호", "정보처리기사", "해당 없음"), "2026-0015": ("최서연", "워드프로세서", "단일등급"),
                    "2026-0016": ("정민수", "컴퓨터활용능력", "1급")}
        for number, (name, exam, grade) in expected.items():
            self.assertEqual((by_number[number]["name"], by_number[number]["exam_name"], by_number[number]["grade"]), (name, exam, grade))
            if number != "2026-0012":
                self.assertFalse(by_number[number]["has_photo"])
        # a conflicting value is reported to the admin and never silently overwritten
        self.admin("PUT", f"/api/admin/demo-candidates/{by_number['2026-0013']['id']}", {"name": "다른이름"})
        status, result = self.admin("POST", "/api/admin/demo-candidates/seed", {})
        self.assertEqual(result["conflicts"], [{"number": "2026-0013", "differing_fields": ["name"]}])
        self.assertTrue(result["needs_admin_confirmation"])
        self.assertEqual(self.admin("GET", f"/api/admin/demo-candidates/{by_number['2026-0013']['id']}")[1]["candidate"]["name"], "다른이름")
        status, result = self.admin("POST", "/api/admin/demo-candidates/seed", {"overwrite_numbers": ["2026-0013"]})
        self.assertEqual(result["overwritten"], ["2026-0013"])

    def test_duplicate_number_is_rejected_and_existing_record_not_overwritten(self):
        first = self.create(name="원본", number="2027-7001")
        status, body = self.admin("POST", "/api/admin/demo-candidates", {
            "name": "덮어쓰기", "number": "2027-7001", "virtual_code": "990101-1", "exam_ref": COMPUTER, "grade": "1급"})
        self.assertEqual(status, 409)
        self.assertEqual(self.admin("GET", f"/api/admin/demo-candidates/{first['id']}")[1]["candidate"]["name"], "원본")
        other = self.create(number="2027-7002")
        self.assertEqual(self.admin("PUT", f"/api/admin/demo-candidates/{other['id']}", {"number": "2027-7001"})[0], 409)
        self.assertNotEqual(first["id"], other["id"])

    def test_field_validation_and_real_resident_numbers_are_refused(self):
        base = {"name": "검증", "number": "2027-7100", "virtual_code": "990101-1", "exam_ref": COMPUTER, "grade": "2급"}
        bad = [{"virtual_code": "9901011234567"}, {"virtual_code": "990101-5"}, {"virtual_code": "991301-1"}, {"virtual_code": ""},
               {"number": "7100"}, {"name": ""}, {"name": "x" * 31}, {"exam_ref": "nope"}, {"grade": "심화"}, {"exam_ref": HISTORY, "grade": "2급"}]
        for change in bad:
            with self.subTest(change=change):
                self.assertEqual(self.admin("POST", "/api/admin/demo-candidates", {**base, **change})[0], 400)

    def test_exam_catalog_separates_kind_name_grade_and_is_admin_managed(self):
        status, body = self.admin("GET", "/api/admin/demo-exams")
        exams = {e["name"]: e for e in body["exams"]}
        self.assertEqual(exams["컴퓨터활용능력"]["grades"], ["1급", "2급"])
        self.assertEqual(exams["한국사능력검정시험"]["grades"], ["심화", "기본"])
        self.assertEqual(exams["정보처리기사"]["grades"], ["해당 없음"])
        self.assertEqual(exams["워드프로세서"]["grades"], ["단일등급"])
        self.assertEqual(exams["전기기능사"]["grades"], ["해당 없음"])
        self.assertEqual(exams["한식조리기능사"]["grades"], ["해당 없음"])
        self.assertIn("연동", body["notice"])
        self.assertEqual(self.raw("POST", "/api/admin/demo-exams", {"kind": "k", "name": "n", "grades": ["a"]})[0], 403)
        status, created = self.admin("POST", "/api/admin/demo-exams", {"kind": "테스트", "name": "신규시험", "grades": ["A", "B"]})
        self.assertEqual(status, 201)
        self.assertEqual(self.admin("POST", "/api/admin/demo-exams", {"kind": "테스트", "name": "신규시험", "grades": ["A"]})[0], 409)
        cand = self.create(exam=created["exam"]["id"], grade="B")
        status, _ = self.admin("PUT", f"/api/admin/demo-exams/{created['exam']['id']}", {"grades": ["A"]})
        self.assertEqual(status, 409)  # grade still used by a candidate
        self.assertEqual(self.admin("PUT", f"/api/admin/demo-exams/{created['exam']['id']}", {"grades": ["A", "B", "C"]})[0], 200)
        self.assertTrue(cand["id"])

    def test_delete_requires_number_confirmation_and_backs_up_photo(self):
        cand = self.create(number="2027-7200")
        self.assertEqual(self.upload(cand["id"])[0], 200)
        self.assertEqual(self.admin("DELETE", f"/api/admin/demo-candidates/{cand['id']}")[0], 400)
        self.assertEqual(self.admin("DELETE", f"/api/admin/demo-candidates/{cand['id']}?confirm=2027-0000")[0], 400)
        photo_dir = Path(self.temp_dir.name) / "demo_photos"
        before = len(list((photo_dir / "backup").glob("*.bak"))) if (photo_dir / "backup").exists() else 0
        self.assertEqual(self.admin("DELETE", f"/api/admin/demo-candidates/{cand['id']}?confirm=2027-7200")[0], 200)
        self.assertEqual(len(list((photo_dir / "backup").glob("*.bak"))), before + 1)  # backed up, not destroyed
        self.assertEqual(self.admin("GET", f"/api/admin/demo-candidates/{cand['id']}")[0], 404)

    def test_reset_requires_confirmation_phrase_and_keeps_legacy_records(self):
        self.create(number="2027-7300")
        self.assertEqual(self.admin("POST", "/api/admin/demo-candidates/reset", {})[0], 400)
        self.assertEqual(self.admin("POST", "/api/admin/demo-candidates/reset", {"confirm": "yes"})[0], 400)
        self.assertEqual(self.admin("POST", "/api/admin/demo-candidates/reset", {"confirm": dc.RESET_CONFIRMATION})[0], 200)
        numbers = [c["number"] for c in self.admin("GET", "/api/admin/demo-candidates")[1]["candidates"]]
        self.assertEqual(sorted(numbers), ["2026-0012", "2026-0099"])


class PhotoTests(RegistryBase):
    def test_upload_validates_real_format_size_and_resolution(self):
        cand = self.create()
        self.assertEqual(self.upload(cand["id"])[0], 200)
        self.assertEqual(self.admin("POST", f"/api/admin/demo-candidates/{cand['id']}/photo", binary=b"not an image at all " * 20)[0], 415)
        self.assertEqual(self.admin("POST", f"/api/admin/demo-candidates/{cand['id']}/photo", binary=b"<script>alert(1)</script>" * 5)[0], 415)
        self.assertEqual(self.upload(cand["id"], width=64, height=64)[0], 400)  # too small
        self.assertEqual(self.upload("missing-id")[0], 404)
        self.assertEqual(self.raw("POST", f"/api/admin/demo-candidates/{cand['id']}/photo", binary=make_png())[0], 403)
        with self.assertRaises(dc.RegistryError):
            dc.inspect_image(make_png()[:-20])  # truncated PNG

    def test_oversize_upload_is_rejected(self):
        cand = self.create()
        big = make_png() + b"\0" * (dc.MAX_PHOTO_BYTES + 1)
        try:  # the server answers 413 before reading the body, so the client may see a reset instead
            status, _ = self.admin("POST", f"/api/admin/demo-candidates/{cand['id']}/photo", binary=big)
            self.assertEqual(status, 413)
        except (BrokenPipeError, ConnectionResetError):
            pass
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(dc, "PHOTO_DIR", Path(tmp)):
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.executescript(dc.SCHEMA)
            connection.execute("INSERT INTO demo_candidates (id, number, number_key, name, created_at, updated_at) VALUES ('c','2027-0001','20270001','n','t','t')")
            with self.assertRaises(dc.RegistryError) as raised:
                dc.set_photo(connection, "c", big)
            self.assertEqual(raised.exception.status, 413)
            self.assertFalse(list(Path(tmp).glob("*.png")))
        self.assertEqual(self.admin("GET", f"/api/admin/demo-candidates/{cand['id']}")[1]["candidate"]["has_photo"], False)

    def test_photos_use_random_server_names_outside_public_folders_and_replacement_is_backed_up(self):
        cand = self.create(number="2027-7400")
        self.upload(cand["id"], shade=30)
        photo_dir = Path(self.temp_dir.name) / "demo_photos"
        name = self.db("SELECT photo_file FROM demo_candidates WHERE id = ?", cand["id"])[0][0]
        self.assertRegex(name, r"^[0-9a-f]{32}\.png$")
        self.assertNotIn("7400", name)
        self.assertTrue((photo_dir / name).is_file())
        self.assertEqual((photo_dir / name).stat().st_mode & 0o077, 0)
        self.assertFalse(Path(dc.ROOT / "images" / name).exists())
        self.upload(cand["id"], shade=200)
        new_name = self.db("SELECT photo_file FROM demo_candidates WHERE id = ?", cand["id"])[0][0]
        self.assertNotEqual(name, new_name)
        self.assertFalse((photo_dir / name).exists())
        self.assertTrue(list((photo_dir / "backup").glob(f"{name}.*.bak")))

    def test_photo_access_is_limited_and_never_cached(self):
        a, b = self.create(number="2027-7500"), self.create(number="2027-7501")
        self.upload(a["id"], shade=20), self.upload(b["id"], shade=220)
        url = f"/api/candidate-photo/{a['id']}"
        self.assertEqual(self.raw("GET", url)[0], 403)  # anonymous
        status, headers, content = self.raw("GET", url, headers={"X-Demo-Role": "admin", "X-Demo-Key": ts.ADMIN_KEY})
        self.assertEqual((status, headers["Content-Type"]), (200, "image/png"))
        self.assertEqual(pixels(content), pixels(make_png(shade=20)))
        self.assertIn("no-store", headers["Cache-Control"])
        self.assertEqual(self.raw("GET", url, headers={"X-Demo-Role": "candidate", "X-Demo-Key": ts.CANDIDATE_KEY})[0], 403)
        self.assertEqual(self.raw("GET", "/images/2026-0012-registration.jpg")[0], 404)  # no public static photos
        self.assertEqual(self.raw("GET", f"/api/candidate-photo/..%2F..%2Fserver.py", headers={"X-Demo-Role": "admin", "X-Demo-Key": ts.ADMIN_KEY})[0], 404)

    def test_kiosk_session_can_only_read_its_own_candidates_photo(self):
        a, b = self.create(number="2027-7600"), self.create(number="2027-7601")
        self.upload(a["id"], shade=20), self.upload(b["id"], shade=220)
        sid, status, record = self.kiosk("2027-7600")
        self.assertEqual(status, 200, record)
        self.assertEqual(record["registered_photo_url"], f"/api/candidate-photo/{a['id']}")
        own = self.raw("GET", record["registered_photo_url"], headers={"X-Kiosk-Session": sid})
        self.assertEqual(own[0], 200)
        self.assertEqual(pixels(own[2]), pixels(make_png(shade=20)))
        other = self.raw("GET", f"/api/candidate-photo/{b['id']}", headers={"X-Kiosk-Session": sid})
        self.assertEqual(other[0], 403)  # B's photo can never be mixed into A's session
        self.assertEqual(self.raw("GET", record["registered_photo_url"], headers={"X-Kiosk-Session": "forged"})[0], 403)
        fresh = ts.DemoApi.request(self.api, "POST", "/api/kiosk/session", {})[1]["kiosk_session_id"]
        self.assertEqual(self.raw("GET", record["registered_photo_url"], headers={"X-Kiosk-Session": fresh})[0], 403)

    def test_candidate_without_photo_is_handled_safely(self):
        cand = self.create(number="2027-7700")
        sid, status, record = self.kiosk("2027-7700")
        self.assertEqual(status, 200)
        self.assertIsNone(record["registered_photo_url"])
        self.assertEqual(self.raw("GET", f"/api/candidate-photo/{cand['id']}", headers={"X-Demo-Role": "admin", "X-Demo-Key": ts.ADMIN_KEY})[0], 404)

    def test_legacy_photo_is_copied_not_moved_and_never_assigned_to_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            legacy_dir = tmp / "images"
            legacy_dir.mkdir()
            (legacy_dir / "2026-0012-registration.jpg").write_bytes(make_png(shade=77))  # content-sniffed, extension ignored
            (legacy_dir / "unrelated.png").write_bytes(make_png(shade=11))
            with mock.patch.object(dc, "PHOTO_DIR", tmp / "photos"), mock.patch.object(dc, "LEGACY_IMAGE_DIR", legacy_dir):
                connection = sqlite3.connect(tmp / "m.sqlite3")
                connection.row_factory = sqlite3.Row
                dc.init_schema(connection)
                dc.init_schema(connection)  # idempotent
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM demo_candidates").fetchone()[0], 2)
                row = connection.execute("SELECT * FROM demo_candidates WHERE id = 'candidate-001'").fetchone()
                self.assertEqual((row["name"], row["number"]), ("박세현", "2026-0012"))
                self.assertEqual(pixels((tmp / "photos" / row["photo_file"]).read_bytes()), pixels(make_png(shade=77)))
                self.assertTrue((legacy_dir / "2026-0012-registration.jpg").exists())
                self.assertIsNone(connection.execute("SELECT photo_file FROM demo_candidates WHERE id = 'candidate-002'").fetchone()[0])
                connection.close()


class KioskIntegrationTests(RegistryBase):
    def info(self, sid, code, exam, grade):
        return self.api.request("POST", "/api/kiosk/self-demo/verify-demo-info",
                                {"kiosk_session_id": sid, "virtual_code": code, "exam": exam, "grade": grade})

    def issue(self, sid):
        return self.api.request("POST", "/api/kiosk/demo-auto-issue",
                                {"kiosk_session_id": sid, "demo_simulation": True, "scenario": "demo_info_simulation"})

    def test_each_candidate_is_looked_up_and_verified_against_own_server_record(self):
        people = [("김가", "990415-2", HISTORY, "심화"), ("이나", "010822-3", "demo-engineer-info", "해당 없음"),
                  ("최다", "020711-4", "demo-wordprocessor", "단일등급"), ("정라", "970903-1", COMPUTER, "1급"),
                  ("박마", "980320-2", COMPUTER, "2급")]
        made = [self.create(name=n, code=c, exam=e, grade=g, number=f"2027-{8000 + i}") for i, (n, c, e, g) in enumerate(people)]
        for i, (cand, (name, code, exam, grade)) in enumerate(zip(made, people)):
            with self.subTest(name=name):
                sid, status, record = self.kiosk(f"2027-{8000 + i}")
                self.assertEqual((status, record["candidate_name"], record["candidate_id"]), (200, name, cand["id"]))
                self.assertNotIn(code, json.dumps(record))  # registered code/exam never revealed before the check
                self.assertNotIn("exam_name", record)
                # another candidate's code / exam / grade is a mismatch
                wrong = people[(i + 1) % 5]
                self.assertEqual(self.info(sid, wrong[1], wrong[2], wrong[3])[0], 403)
                self.assertEqual(self.issue(sid)[0], 403)
                status, _ = self.info(sid, code, exam, grade)
                self.assertEqual(status, 200)
                status, qr = self.issue(sid)
                self.assertEqual(status, 200, qr)
                self.assertEqual((qr["candidate_name"], qr["candidate_number"], qr["exam_grade"]), (name, f"2027-{8000 + i}", grade))
                self.assertIn(self.db("SELECT name FROM demo_exams WHERE id = ?", exam)[0][0], qr["exam_name"])
                for personal in (name, f"2027-{8000 + i}", code, grade):
                    self.assertNotIn(personal, qr["demo_token"])  # token carries no personal data
                self.assertTrue(qr["simulation"] and not qr["real_authentication"] and qr["expires_in"] == 30)

    def test_right_exam_with_wrong_grade_or_other_candidates_code_is_rejected(self):
        cand = self.create(name="급수", code="990415-2", exam=COMPUTER, grade="1급", number="2027-8100")
        sid, status, _ = self.kiosk("2027-8100")
        self.assertEqual(self.info(sid, "990415-2", COMPUTER, "2급")[0], 403)  # exam matches, grade differs
        self.assertEqual(self.info(sid, "980320-2", COMPUTER, "1급")[0], 403)  # another person's code
        self.assertEqual(self.info(sid, "990415-2", HISTORY, "심화")[0], 403)
        self.assertEqual(self.issue(sid)[0], 403)
        self.assertEqual(self.info(sid, "990415-2", COMPUTER, "1급")[0], 200)
        self.assertTrue(cand["id"])

    def test_session_of_a_cannot_issue_a_qr_for_b(self):
        a = self.create(name="에이", code="990101-1", number="2027-8200")
        b = self.create(name="비", code="990202-2", number="2027-8201")
        sid, _, _ = self.kiosk("2027-8200")
        self.info(sid, "990101-1", COMPUTER, "2급")
        for field in ("candidate_id", "candidate_number", "exam_id"):
            with self.subTest(field=field):
                status, _ = self.api.request("POST", "/api/kiosk/demo-auto-issue", {
                    "kiosk_session_id": sid, "demo_simulation": True, "scenario": "demo_info_simulation", field: b["id"]})
                self.assertIn(status, (200, 400, 409))
                if status == 200:
                    break
        self.assertEqual(self.db("SELECT DISTINCT candidate_id FROM demo_tokens"), [(a["id"],)])
        # B's own session cannot reuse A's verified state
        sid_b, _, _ = self.kiosk("2027-8201")
        self.assertEqual(self.issue(sid_b)[0], 403)
        self.assertEqual(self.info(sid_b, "990101-1", COMPUTER, "2급")[0], 403)

    def test_inactive_unknown_and_deleted_candidates_get_the_same_safe_error(self):
        cand = self.create(number="2027-8300")
        self.assertEqual(self.admin("PUT", f"/api/admin/demo-candidates/{cand['id']}", {"active": False})[0], 200)
        inactive = self.kiosk("2027-8300")
        unknown = self.kiosk("2027-8999")
        self.assertEqual((inactive[1], unknown[1]), (404, 404))
        self.assertEqual(inactive[2], unknown[2])  # no oracle for deactivated vs missing
        self.assertEqual(self.admin("PUT", f"/api/admin/demo-candidates/{cand['id']}", {"active": True})[0], 200)
        self.assertEqual(self.kiosk("2027-8300")[1], 200)
        self.assertEqual(self.admin("DELETE", f"/api/admin/demo-candidates/{cand['id']}?confirm=2027-8300")[0], 200)
        self.assertEqual(self.kiosk("2027-8300")[1], 404)

    def test_deactivation_mid_session_blocks_qr_issue(self):
        cand = self.create(number="2027-8400")
        sid, _, _ = self.kiosk("2027-8400")
        self.info(sid, "990101-1", COMPUTER, "2급")
        self.admin("PUT", f"/api/admin/demo-candidates/{cand['id']}", {"active": False})
        self.assertIn(self.issue(sid)[0], (409, 410))  # session invalidated: safe 4xx, never 500
        self.assertEqual(self.db("SELECT COUNT(*) FROM demo_tokens WHERE candidate_id = ?", cand["id"]), [(0,)])

    def test_registry_qr_expiry_reuse_and_operational_rejection(self):
        self.create(number="2027-8500")
        sid, _, _ = self.kiosk("2027-8500")
        self.info(sid, "990101-1", COMPUTER, "2급")
        status, qr = self.issue(sid)
        self.assertEqual(status, 200)
        tokens, admissions = self.db("SELECT COUNT(*) FROM tokens")[0][0], self.db("SELECT COUNT(*) FROM admissions")[0][0]
        for path in ("/api/token/inspect", "/api/admit"):
            status, _ = self.api.request("POST", path, {"token": qr["demo_token"], "exam_id": ts.EXAM_ID}, "proctor", ts.PROCTOR_KEY)
            self.assertNotEqual(status, 200, path)
        self.assertEqual((self.db("SELECT COUNT(*) FROM tokens")[0][0], self.db("SELECT COUNT(*) FROM admissions")[0][0]), (tokens, admissions))
        self.assertEqual(self.api.kiosk_demo_entry(sid, qr["demo_token"])[0], 200)
        self.assertEqual(self.api.kiosk_demo_entry(sid, qr["demo_token"])[0], 409)  # reuse / duplicate entry
        self.create(number="2027-8501")
        sid2, _, _ = self.kiosk("2027-8501")
        self.info(sid2, "990101-1", COMPUTER, "2급")
        status, qr2 = self.issue(sid2)
        self.db("UPDATE demo_tokens SET expires_at = 1 WHERE token_hash IS NOT NULL")
        with sqlite3.connect(self.db_path) as db:
            db.execute("UPDATE demo_tokens SET expires_at = 1")
        self.assertEqual(self.api.kiosk_demo_entry(sid2, qr2["demo_token"])[0], 409)

    def test_audit_log_masks_names_codes_and_photos(self):
        cand = self.create(name="비밀이름", code="991231-1", number="2027-8600")
        self.upload(cand["id"])
        text = " ".join(" ".join(map(str, r)) for r in self.db("SELECT event, details, actor, candidate_id FROM events"))
        self.assertIn("DEMO 응시자 관리", text)
        for secret in ("비밀이름", "991231", "2027-8600", "demo_photos"):
            self.assertNotIn(secret, text)

    def test_reference_photos_differ_per_candidate_for_analysis(self):
        a, b = self.create(number="2027-8700"), self.create(number="2027-8701")
        self.upload(a["id"], shade=10), self.upload(b["id"], shade=240)
        photos = []
        for number in ("2027-8700", "2027-8701"):
            sid, _, record = self.kiosk(number)
            photos.append(self.raw("GET", record["registered_photo_url"], headers={"X-Kiosk-Session": sid})[2])
        self.assertNotEqual(photos[0], photos[1])


if __name__ == "__main__":
    unittest.main()
