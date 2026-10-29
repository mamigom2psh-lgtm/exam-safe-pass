#!/usr/bin/env python3
"""End-to-end API scenarios for the demo server and its QR output."""

import base64
import hashlib
import io
import json
import os
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parent
QR_DECODE_SCRIPT = """
const zxing = require('@zxing/library');
const { PNG } = require('pngjs');
// Server PNGs are clean, axis-aligned QR images. PURE_BARCODE skips zxing-js's
// finder-pattern detector, which fails ~7% of clean v4 QRs (see docs/QR_DECODE_ANALYSIS.md).
const chunks = [];
process.stdin.on('data', chunk => chunks.push(chunk));
process.stdin.on('end', () => {
    try {
        const png = PNG.sync.read(Buffer.concat(chunks));
        const luminance = new Uint8ClampedArray(png.width * png.height);
        for (let y = 0; y < png.height; y++) {
            for (let x = 0; x < png.width; x++) {
                const index = (y * png.width + x) * 4;
                const r = png.data[index];
                const g = png.data[index + 1];
                const b = png.data[index + 2];
                luminance[y * png.width + x] = (r * 299 + g * 587 + b * 114) / 1000;
            }
        }
        const bitmap = new zxing.BinaryBitmap(
            new zxing.HybridBinarizer(
                new zxing.RGBLuminanceSource(luminance, png.width, png.height)
            )
        );
        process.stdout.write(new zxing.QRCodeReader().decode(bitmap, new Map([[zxing.DecodeHintType.PURE_BARCODE, true]])).getText());
    } catch (error) {
        process.stderr.write(String(error && error.stack ? error.stack : error));
        process.exit(1);
    }
});
"""
PROCTOR_KEY = "local-test-proctor-key"
ADMIN_KEY = "local-test-admin-key"
SUPERVISOR_KEY = "local-test-supervisor-key"
EXAM_ID = "exam-demo-2026"
CAN_1 = "candidate-001"
CAN_2 = "candidate-002"


def reserve_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def decode_qr_png(qr_png):
    qr_bytes = base64.b64decode(qr_png, validate=True)
    if not qr_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        raise AssertionError("QR payload is not a valid PNG image.")
    decoded = subprocess.run(
        ["node", "-e", QR_DECODE_SCRIPT],
        cwd=ROOT,
        input=qr_bytes,
        capture_output=True,
        check=False,
        timeout=10,
    )
    if decoded.returncode:
        raise AssertionError(
            f"QR decoder failed: {decoded.stderr.decode('utf-8', errors='replace')}"
        )
    return decoded.stdout.decode("utf-8")


class DemoApi:
    def __init__(self, base_url):
        self.base_url = base_url

    def request(self, method, path, payload=None, role=None, key=None):
        data = None if payload is None else json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if role:
            headers.update({"X-Demo-Role": role, "X-Demo-Key": key})
        request = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            status = error.code
            body = json.loads(error.read())
            error.close()
            return status, body

    def issue(self, candidate_id=CAN_1):
        auth_status, auth = self.authenticate(candidate_id)
        if auth_status != 200 or not auth["success"]:
            raise AssertionError(f"mock authentication failed: {auth}")
        return self.request(
            "POST",
            "/api/demo/token",
            {
                "exam_id": EXAM_ID,
                "candidate_id": candidate_id,
                "auth_session_id": auth["auth_session_id"],
            },
        )

    def authenticate(self, candidate_id=CAN_1, demo_outcome="success"):
        return self.request(
            "POST",
            "/api/mobile-id/authenticate",
            {
                "exam_id": EXAM_ID,
                "candidate_id": candidate_id,
                "demo_outcome": demo_outcome,
            },
        )

    def issue_with_auth(self, candidate_id, auth_session_id):
        return self.request(
            "POST",
            "/api/demo/token",
            {
                "exam_id": EXAM_ID,
                "candidate_id": candidate_id,
                "auth_session_id": auth_session_id,
            },
        )

    def approve(self, token, candidate_id=CAN_1, face_match=True, roster_match=True):
        return self.request(
            "POST",
            "/api/demo/approve",
            {
                "exam_id": EXAM_ID,
                "candidate_id": candidate_id,
                "token": token,
                "roster_match": roster_match,
                "face_match": face_match,
            },
            "proctor",
            PROCTOR_KEY,
        )

    def inspect(self, token, exam_id=EXAM_ID, role="proctor", key=PROCTOR_KEY):
        return self.request(
            "POST",
            "/api/demo/token/inspect",
            {"token": token, "exam_id": exam_id},
            role,
            key,
        )

    def decide_physical(
        self,
        *,
        document_type="resident_card",
        decision="approve",
        document_match=True,
        roster_photo_match=True,
        live_face_match=True,
        reason="identity_unverifiable",
        role="proctor",
        key=PROCTOR_KEY,
    ):
        return self.request(
            "POST",
            "/api/physical-id/decision",
            {
                "exam_id": EXAM_ID,
                "candidate_id": CAN_1,
                "document_type": document_type,
                "decision": decision,
                "reason": reason,
                "document_match": document_match,
                "roster_photo_match": roster_photo_match,
                "live_face_match": live_face_match,
            },
            role,
            key,
        )

    def decide_exception(
        self, *, decision="approve", checks=(True, True, True, True), role="supervisor", key=SUPERVISOR_KEY
    ):
        return self.request(
            "POST",
            "/api/exception/decision",
            {
                "exam_id": EXAM_ID,
                "candidate_id": CAN_1,
                "procedure_id": "dual_staff_application_review",
                "decision": decision,
                "application_source_verified": checks[0],
                "roster_photo_match": checks[1],
                "two_staff_witnesses": checks[2],
                "supervisor_confirmed": checks[3],
            },
            role,
            key,
        )

    def create_kiosk_session(self):
        return self.request("POST", "/api/kiosk/session", {})

    def lookup_kiosk_candidate(self, session_id, number="2026-0012", mode="proctor"):
        return self.request(
            "POST",
            "/api/kiosk/candidate",
            {
                "kiosk_session_id": session_id,
                "exam_id": EXAM_ID,
                "candidate_number": number,
                "mode": mode,
            },
        )

    def kiosk_self_auth(self, session_id, demo_outcome="success", role="admin", key=ADMIN_KEY):
        return self.request(
            "POST",
            "/api/kiosk/self-auth",
            {
                "kiosk_session_id": session_id,
                "demo_outcome": demo_outcome,
                "demo_simulation": True,
            },
            role,
            key,
        )

    def kiosk_self_demo_complete(self, session_id):
        status, result = self.kiosk_self_demo_authenticate(session_id)
        if status != 200:
            return status, result
        return self.kiosk_self_demo_issue_ticket(session_id, "screen")

    def kiosk_self_demo_authenticate(self, session_id, outcome="success", demo_simulation=True):
        return self.request(
            "POST",
            "/api/kiosk/self-demo/complete",
            {
                "kiosk_session_id": session_id,
                "demo_outcome": outcome,
                "demo_simulation": demo_simulation,
            },
        )

    def kiosk_self_demo_issue_ticket(self, session_id, delivery):
        return self.request(
            "POST",
            "/api/kiosk/self-demo/issue-ticket",
            {"kiosk_session_id": session_id, "delivery": delivery},
        )

    def kiosk_demo_entry(self, session_id, demo_token):
        payload = {"demo_token": demo_token}
        if session_id is not None:
            payload["kiosk_session_id"] = session_id
        return self.request(
            "POST",
            "/api/kiosk/demo-entry",
            payload,
        )

    def kiosk_status(self, session_id):
        return self.request(
            "POST", "/api/kiosk/status", {"kiosk_session_id": session_id}
        )

    def kiosk_demo_qr(self, session_id):
        return self.request(
            "POST", "/api/kiosk/demo-qr", {"kiosk_session_id": session_id}
        )

    def refresh_self_demo_qr(self, session_id):
        return self.request(
            "POST",
            "/api/kiosk/self-demo/refresh-qr",
            {"kiosk_session_id": session_id},
        )

    def kiosk_review(
        self,
        session_id,
        *,
        document_type="resident_card",
        decision="approve",
        reason="normal",
        document_match=True,
        live_face_match=True,
        role="proctor",
        key=PROCTOR_KEY,
    ):
        return self.request(
            "POST",
            "/api/kiosk/physical-review",
            {
                "kiosk_session_id": session_id,
                "document_type": document_type,
                "decision": decision,
                "reason": reason,
                "document_match": document_match,
                "live_face_match": live_face_match,
            },
            role,
            key,
        )

    def refresh_kiosk_qr(self, session_id, *, role="proctor", key=PROCTOR_KEY):
        return self.request(
            "POST",
            "/api/kiosk/refresh-qr",
            {"kiosk_session_id": session_id},
            role,
            key,
        )


class ServerScenarios(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.temp_dir.name) / "scenario.sqlite3"
        cls.port = reserve_port()
        env = os.environ.copy()
        env.update(
            {
                "EXAM_SAFE_DB": str(cls.db_path),
                "EXAM_SAFE_PHOTO_DIR": str(Path(cls.temp_dir.name) / "demo_photos"),
                "EXAM_SAFE_LEGACY_IMAGE_DIR": str(Path(cls.temp_dir.name) / "no_legacy_images"),
                "EXAM_SAFE_HOST": "127.0.0.1",
                "EXAM_SAFE_PORT": str(cls.port),
                "EXAM_SAFE_PROCTOR_KEY": PROCTOR_KEY,
                "EXAM_SAFE_ADMIN_KEY": ADMIN_KEY,
                "EXAM_SAFE_SUPERVISOR_KEY": SUPERVISOR_KEY,
                "EXAM_SAFE_KIOSK_PRINT_QR_TTL_SECONDS": "900",
            }
        )
        env.update(getattr(cls, "extra_env", {}))
        cls.process = subprocess.Popen(
            [sys.executable, str(ROOT / "server.py")],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        ready = False
        for line in iter(cls.process.stdout.readline, ""):
            if line.startswith("LISTENING "):
                ready = True
                break
        if not ready:
            output, _ = cls.process.communicate(timeout=5)
            raise RuntimeError(f"server did not start:\n{output}")
        cls.api = DemoApi(f"http://127.0.0.1:{cls.port}")

    def setUp(self):
        status, _ = self.api.request(
            "POST",
            "/api/demo/reset",
            {},
            "proctor",
            PROCTOR_KEY,
        )
        self.assertEqual(status, 200)

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        try:
            cls.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.process.kill()
            cls.process.wait(timeout=5)
        cls.process.stdout.close()
        cls.temp_dir.cleanup()

    def issue_token(self, candidate_id=CAN_1):
        status, response = self.api.issue(candidate_id)
        self.assertEqual(status, 201, response)
        return response

    def issue_token_value(self, candidate_id=CAN_1):
        return self.issue_token(candidate_id)["token"]

    def test_local_qr_decoder_and_license_assets_are_served(self):
        for asset, expected_type in (
            ("/vendor/qr-decoder.min.js", "text/javascript"),
            ("/vendor/ZXING-BROWSER-LICENSE.txt", "text/plain"),
        ):
            with self.subTest(asset=asset):
                with urllib.request.urlopen(self.api.base_url + asset, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertTrue(response.headers["Content-Type"].startswith(expected_type))
                    self.assertGreater(len(response.read()), 0)

        with self.assertRaises(urllib.error.HTTPError) as response:
            urllib.request.urlopen(self.api.base_url + "/vendor/unlisted.js", timeout=5)
        self.assertEqual(response.exception.code, 404)
        response.exception.close()

    def test_kiosk_page_and_candidate_lookup_are_available_without_authentication(self):
        with urllib.request.urlopen(self.api.base_url + "/kiosk", timeout=5) as response:
            page = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertIn("실제 신원확인 또는 실제 입실 허가가 아닙니다.", page)
            self.assertIn("셀프 인증 시작", page)
            self.assertIn("감독관 인증", page)
            self.assertIn("관리자 도움 요청", page)

        with urllib.request.urlopen(
            self.api.base_url + "/vendor/human/human.bundle.js", timeout=5
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "text/javascript")
            self.assertIn(b"HumanBundle", response.read(500))

        with urllib.request.urlopen(
            self.api.base_url + "/vendor/human/models/faceres.json", timeout=5
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "application/json")
            model_manifest = response.read()
            self.assertIn(b"modelTopology", model_manifest)

        with urllib.request.urlopen(
            self.api.base_url + "/vendor/human/models/antispoof.json", timeout=5
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "application/json")
            model_manifest = response.read()
            self.assertIn(b'"generatedBy"', model_manifest)
            self.assertIn(b"fake-face-detection", model_manifest)

        with urllib.request.urlopen(
            self.api.base_url + "/vendor/human/models/antispoof.bin", timeout=5
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "application/octet-stream")
            self.assertEqual(len(response.read()), 853098)

        status, created = self.api.create_kiosk_session()
        self.assertEqual(status, 201)
        status, candidate = self.api.lookup_kiosk_candidate(created["kiosk_session_id"])
        self.assertEqual(status, 200, candidate)
        self.assertEqual(candidate["candidate_number"], "2026-0012")
        self.assertIsNone(candidate["registered_photo_url"])  # no photo uploaded in the test registry; never a public /images/ URL
        self.assertEqual(candidate["status"], "awaiting_proctor")
        self.assertNotIn("authenticated", candidate)
        status, missing = self.api.lookup_kiosk_candidate(
            created["kiosk_session_id"], number="9999-9999"
        )
        self.assertEqual(status, 409, missing)

    def test_kiosk_full_flow_requires_proctor_and_records_final_entry(self):
        _, created = self.api.create_kiosk_session()
        kiosk_session_id = created["kiosk_session_id"]
        status, candidate = self.api.lookup_kiosk_candidate(kiosk_session_id)
        self.assertEqual(status, 200, candidate)
        self.assertEqual(candidate["status"], "awaiting_proctor")

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            initial_token_count = connection.execute(
                "SELECT COUNT(*) FROM tokens WHERE exam_id = ? AND candidate_id = ?",
                (EXAM_ID, CAN_1),
            ).fetchone()[0]
        status, denied = self.api.kiosk_review(kiosk_session_id, role="candidate", key="wrong")
        self.assertEqual(status, 403, denied)
        self.assertNotIn("qr_png", self.api.kiosk_status(kiosk_session_id)[1])
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM tokens WHERE exam_id = ? AND candidate_id = ?",
                    (EXAM_ID, CAN_1),
                ).fetchone()[0],
                initial_token_count,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM admissions WHERE exam_id = ? AND candidate_id = ?",
                    (EXAM_ID, CAN_1),
                ).fetchone()[0],
                0,
            )

        status, reviewed = self.api.kiosk_review(kiosk_session_id)
        self.assertEqual(status, 200, reviewed)
        self.assertEqual(reviewed["state"], "demo_qr_ready")
        status, kiosk_status = self.api.kiosk_status(kiosk_session_id)
        self.assertEqual(status, 200, kiosk_status)
        self.assertGreaterEqual(kiosk_status["expires_in"], 1)
        self.assertLessEqual(kiosk_status["expires_in"], 30)

        token = decode_qr_png(kiosk_status["qr_png"])
        self.assertTrue(token)
        self.assertNotIn("박세현", token)
        self.assertNotIn("2026-0012", token)
        self.assertNotIn(CAN_1, token)

        status, approved = self.api.kiosk_demo_entry(kiosk_session_id, token)
        self.assertEqual(status, 200, approved)
        self.assertTrue(approved["demo_admitted"])
        self.assertFalse(approved["real_admission"])
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            token_row = connection.execute(
                "SELECT used_at FROM demo_tokens WHERE token_hash = ?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()
            admission = connection.execute(
                "SELECT COUNT(*) FROM demo_admissions WHERE exam_id = ? AND candidate_id = ?",
                (EXAM_ID, CAN_1),
            ).fetchone()[0]
            audit = connection.execute(
                "SELECT actor, outcome FROM events WHERE event = '키오스크 DEMO 모의 입실' ORDER BY event_id DESC LIMIT 1"
            ).fetchone()
        self.assertIsNotNone(token_row[0])
        self.assertEqual(admission, 1)
        self.assertEqual(audit, ("kiosk_demo", "demo_admitted"))
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM admissions WHERE exam_id = ? AND candidate_id = ?",
                    (EXAM_ID, CAN_1),
                ).fetchone()[0],
                0,
            )

        status, replay = self.api.kiosk_demo_entry(kiosk_session_id, token)
        self.assertEqual(status, 409)
        self.assertFalse(replay["demo_admitted"])
        status, duplicate = self.api.lookup_kiosk_candidate(
            self.api.create_kiosk_session()[1]["kiosk_session_id"]
        )
        self.assertEqual(status, 409)
        self.assertIn("이미 입실", duplicate["error"])

    def test_kiosk_demo_qr_is_valid_png_but_never_an_entry_token(self):
        _, created = self.api.create_kiosk_session()
        session_id = created["kiosk_session_id"]
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            initial_token_count = connection.execute(
                "SELECT COUNT(*) FROM demo_tokens WHERE exam_id = ? AND candidate_id = ?",
                (EXAM_ID, CAN_1),
            ).fetchone()[0]
        status, preview = self.api.kiosk_demo_qr(session_id)
        self.assertEqual(status, 200, preview)
        self.assertTrue(preview["demo_only"])
        self.assertEqual(preview["qr_mime_type"], "image/png")
        png = base64.b64decode(preview["qr_png"], validate=True)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        token = decode_qr_png(preview["qr_png"])
        self.assertTrue(token.startswith("DEMO-ONLY:"))
        self.assertNotIn(CAN_1, token)

        status, inspection = self.api.inspect(token)
        self.assertEqual(status, 409)
        self.assertFalse(inspection["valid"])
        self.assertIn("DEMO 전용", inspection["error"])
        status, approval = self.api.approve(token, candidate_id=CAN_1)
        self.assertEqual(status, 409)
        self.assertFalse(approval["admitted"])
        self.assertIn("DEMO", approval["error"])

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM demo_tokens WHERE exam_id = ? AND candidate_id = ?",
                    (EXAM_ID, CAN_1),
                ).fetchone()[0],
                initial_token_count,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM admissions WHERE exam_id = ? AND candidate_id = ?",
                    (EXAM_ID, CAN_1),
                ).fetchone()[0],
                0,
            )

    def test_kiosk_self_demo_qr_and_simulated_entry_never_create_real_admission(self):
        _, created = self.api.create_kiosk_session()
        session_id = created["kiosk_session_id"]
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            initial_token_count = connection.execute(
                "SELECT COUNT(*) FROM tokens WHERE exam_id = ? AND candidate_id = ?",
                (EXAM_ID, CAN_1),
            ).fetchone()[0]
            initial_demo_token_count = connection.execute(
                "SELECT COUNT(*) FROM demo_tokens WHERE pathway = 'self_demo'"
            ).fetchone()[0]
        status, candidate = self.api.lookup_kiosk_candidate(
            session_id, mode="self_demo"
        )
        self.assertEqual(status, 200, candidate)
        self.assertEqual(candidate["status"], "awaiting_self_auth")
        status, pending = self.api.kiosk_status(session_id)
        self.assertEqual(status, 200)
        self.assertEqual(pending["state"], "awaiting_self_auth")
        self.assertNotIn("qr_png", pending)

        status, auth = self.api.kiosk_self_demo_complete(session_id)
        self.assertEqual(status, 200, auth)
        self.assertEqual(auth["state"], "demo_qr_ready")
        status, qr = self.api.kiosk_status(session_id)
        self.assertEqual(status, 200, qr)
        self.assertTrue(qr["demo_only"])
        self.assertEqual(qr["qr_mime_type"], "image/png")
        self.assertGreaterEqual(qr["expires_in"], 1)
        self.assertLessEqual(qr["expires_in"], 30)
        token = decode_qr_png(qr["qr_png"])
        self.assertTrue(token.startswith("DEMO-ONLY:SELF:"))
        self.assertNotIn("2026-0012", token)
        self.assertNotIn("박세현", token)
        self.assertEqual(qr["demo_token"], token)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            demo_token = connection.execute(
                "SELECT token_hash, expires_at FROM demo_tokens WHERE token_hash = ?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()
            self.assertEqual(demo_token[0], hashlib.sha256(token.encode()).hexdigest())
            self.assertLessEqual(demo_token[1], time.time() + 30)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM tokens WHERE exam_id = ? AND candidate_id = ?",
                    (EXAM_ID, CAN_1),
                ).fetchone()[0],
                initial_token_count,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM demo_tokens WHERE pathway = 'self_demo'",
                ).fetchone()[0],
                initial_demo_token_count + 1,
            )

        status, inspected = self.api.inspect(token)
        self.assertEqual(status, 409)
        self.assertFalse(inspected["valid"])
        status, approved = self.api.approve(token, candidate_id=CAN_1)
        self.assertEqual(status, 409)
        self.assertFalse(approved["admitted"])
        status, wrong_token = self.api.kiosk_demo_entry(session_id, "DEMO-ONLY:SELF:wrong")
        self.assertEqual(status, 409)
        self.assertFalse(wrong_token["demo_admitted"])

        status, simulated = self.api.kiosk_demo_entry(session_id, token)
        self.assertEqual(status, 200, simulated)
        self.assertTrue(simulated["demo_admitted"])
        self.assertFalse(simulated["real_admission"])
        status, replay = self.api.kiosk_demo_entry(session_id, token)
        self.assertEqual(status, 409)
        self.assertFalse(replay["demo_admitted"])
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM admissions").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM demo_admissions").fetchone()[0],
                1,
            )
            event = connection.execute(
                "SELECT actor, outcome, details FROM events WHERE event = '키오스크 DEMO 모의 입실' AND outcome = 'demo_admitted' ORDER BY event_id DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(event[0:2], ("kiosk_demo", "demo_admitted"))
            self.assertIn("simulated only", event[2])

        _, second_session = self.api.create_kiosk_session()
        status, duplicate = self.api.lookup_kiosk_candidate(
            second_session["kiosk_session_id"], mode="self_demo"
        )
        self.assertEqual(status, 409)
        self.assertIn("이미 입실", duplicate["error"])

    def test_kiosk_self_demo_qr_refresh_invalidates_previous_token_and_remains_demo_only(self):
        _, created = self.api.create_kiosk_session()
        session_id = created["kiosk_session_id"]
        self.assertEqual(
            self.api.lookup_kiosk_candidate(session_id, mode="self_demo")[0], 200
        )
        self.assertEqual(self.api.kiosk_self_auth(session_id, "success")[0], 200)
        status, original = self.api.kiosk_status(session_id)
        self.assertEqual(status, 200, original)
        old_token = original["demo_token"]

        status, refreshed = self.api.refresh_self_demo_qr(session_id)
        self.assertEqual(status, 200, refreshed)
        self.assertTrue(refreshed["demo_only"])
        self.assertEqual(refreshed["qr_mime_type"], "image/png")
        self.assertTrue(
            base64.b64decode(refreshed["qr_png"], validate=True).startswith(
                b"\x89PNG\r\n\x1a\n"
            )
        )
        new_token = refreshed["demo_token"]
        self.assertTrue(new_token.startswith("DEMO-ONLY:SELF:"))
        self.assertNotEqual(old_token, new_token)
        status, rejected = self.api.kiosk_demo_entry(session_id, old_token)
        self.assertEqual(status, 409)
        self.assertFalse(rejected["demo_admitted"])

        status, simulated = self.api.kiosk_demo_entry(session_id, new_token)
        self.assertEqual(status, 200, simulated)
        self.assertTrue(simulated["demo_admitted"])
        self.assertFalse(simulated["real_admission"])
        status, real_inspection = self.api.inspect(new_token)
        self.assertEqual(status, 409)
        self.assertFalse(real_inspection["valid"])
        status, real_approval = self.api.approve(new_token, candidate_id=CAN_1)
        self.assertEqual(status, 409)
        self.assertFalse(real_approval["admitted"])
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM admissions").fetchone()[0],
                0,
            )

    def test_expired_self_demo_qr_can_be_reissued_only_while_mock_auth_is_valid(self):
        _, created = self.api.create_kiosk_session()
        session_id = created["kiosk_session_id"]
        self.assertEqual(
            self.api.lookup_kiosk_candidate(session_id, mode="self_demo")[0], 200
        )
        self.assertEqual(self.api.kiosk_self_auth(session_id, "success")[0], 200)
        status, issued = self.api.kiosk_status(session_id)
        self.assertEqual(status, 200, issued)
        token_hash = hashlib.sha256(issued["demo_token"].encode()).hexdigest()
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE demo_tokens SET expires_at = 0 WHERE token_hash = ?",
                (token_hash,),
            )

        status, expired = self.api.kiosk_status(session_id)
        self.assertEqual(status, 200, expired)
        self.assertEqual(expired["state"], "expired")
        self.assertTrue(expired["demo_reissue_allowed"])
        status, refreshed = self.api.refresh_self_demo_qr(session_id)
        self.assertEqual(status, 200, refreshed)
        self.assertGreaterEqual(refreshed["expires_in"], 1)
        self.assertLessEqual(refreshed["expires_in"], 30)
        token = decode_qr_png(refreshed["qr_png"])
        self.assertTrue(token.startswith("DEMO-ONLY:SELF:"))

    def test_kiosk_self_demo_failures_and_mismatch_fall_back_to_proctor(self):
        for outcome in ("failure", "uncertain", "mismatch"):
            with self.subTest(outcome=outcome):
                _, created = self.api.create_kiosk_session()
                session_id = created["kiosk_session_id"]
                status, _ = self.api.lookup_kiosk_candidate(
                    session_id, mode="self_demo"
                )
                self.assertEqual(status, 200)
                status, result = self.api.kiosk_self_auth(session_id, outcome)
                self.assertEqual(status, 200, result)
                self.assertEqual(result["state"], "awaiting_proctor")
                status, current = self.api.kiosk_status(session_id)
                self.assertEqual(status, 200)
                self.assertEqual(current["state"], "awaiting_proctor")
                self.assertNotIn("qr_png", current)
                status, requests = self.api.request(
                    "GET", "/api/kiosk/requests", role="proctor", key=PROCTOR_KEY
                )
                self.assertEqual(status, 200)
                request = next(
                    row for row in requests["requests"]
                    if row["kiosk_session_id"] == session_id
                )
                self.assertEqual(request["authentication_mode"], "proctor_fallback")

                status, reviewed = self.api.kiosk_review(session_id)
                self.assertEqual(status, 200, reviewed)
                self.assertEqual(reviewed["state"], "demo_qr_ready")
                status, issued = self.api.kiosk_status(session_id)
                self.assertEqual(status, 200)
                self.assertTrue(issued["demo_only"])
                self.assertEqual(issued["qr_mime_type"], "image/png")
                self.assertTrue(
                    base64.b64decode(issued["qr_png"], validate=True).startswith(
                        b"\x89PNG\r\n\x1a\n"
                    )
                )
                with closing(sqlite3.connect(self.db_path)) as connection, connection:
                    token_count = connection.execute(
                        "SELECT COUNT(*) FROM demo_tokens WHERE exam_id = ? AND candidate_id = ?",
                        (EXAM_ID, CAN_1),
                    ).fetchone()[0]
                    audit = connection.execute(
                        "SELECT details FROM events WHERE event = '키오스크 DEMO QR 발급' ORDER BY event_id DESC LIMIT 1"
                    ).fetchone()[0]
                    self.assertGreater(token_count, 0)
                    self.assertIn("authentication_path=proctor_fallback", audit)

    def test_kiosk_self_auth_requires_explicit_demo_result(self):
        _, created = self.api.create_kiosk_session()
        session_id = created["kiosk_session_id"]
        self.assertEqual(
            self.api.lookup_kiosk_candidate(session_id, mode="self_demo")[0], 200
        )
        status, rejected = self.api.request(
            "POST",
            "/api/kiosk/self-auth",
            {"kiosk_session_id": session_id, "demo_outcome": "success"},
        )
        self.assertEqual(status, 403)
        status, rejected = self.api.request(
            "POST",
            "/api/kiosk/self-auth",
            {"kiosk_session_id": session_id, "demo_outcome": "success"},
            role="admin",
            key=ADMIN_KEY,
        )
        self.assertEqual(status, 400)
        self.assertIn("DEMO 모의 결과", rejected["error"])
        status, rejected = self.api.request(
            "POST",
            "/api/kiosk/self-demo/complete",
            {"kiosk_session_id": session_id, "demo_outcome": "failure"},
        )
        self.assertEqual(status, 400)
        self.assertIn("DEMO 모의 성공", rejected["error"])
        status, blocked = self.api.request(
            "POST",
            "/api/kiosk/self-demo/issue-ticket",
            {"kiosk_session_id": session_id, "delivery": "screen"},
        )
        self.assertEqual(status, 409)
        self.assertIn("유효한 DEMO 모의 인증", blocked["error"])
        status, state = self.api.kiosk_status(session_id)
        self.assertEqual(status, 200)
        self.assertEqual(state["state"], "awaiting_self_auth")

    def test_browser_face_claims_are_not_server_auth_and_demo_qr_stays_demo_only(self):
        _, created = self.api.create_kiosk_session()
        session_id = created["kiosk_session_id"]
        self.assertEqual(
            self.api.lookup_kiosk_candidate(session_id, mode="self_demo")[0], 200
        )

        status, denied = self.api.request(
            "POST",
            "/api/kiosk/self-demo/complete",
            {
                "kiosk_session_id": session_id,
                "face_match": True,
                "face_similarity": 0.99,
                "captured_face_result": "similar",
                "authentication_success": True,
            },
        )
        self.assertEqual(status, 400, denied)
        self.assertIn("서버 인증 근거", denied["error"])
        self.assertFalse(denied["real_authentication"])
        self.assertEqual(self.api.kiosk_status(session_id)[1]["state"], "awaiting_self_auth")

        status, denied = self.api.request(
            "POST",
            "/api/kiosk/self-demo/complete",
            {
                "kiosk_session_id": session_id,
                "demo_outcome": "success",
                "demo_simulation": True,
                "face_match": True,
                "face_similarity": 0.99,
                "real_authentication": True,
            },
        )
        self.assertEqual(status, 400, denied)
        self.assertIn("서버 인증 근거", denied["error"])
        self.assertEqual(self.api.kiosk_status(session_id)[1]["state"], "awaiting_self_auth")

        status, demo_auth = self.api.kiosk_self_demo_authenticate(session_id)
        self.assertEqual(status, 200, demo_auth)
        self.assertEqual(demo_auth["state"], "demo_authenticated")
        self.assertTrue(demo_auth["demo_only"])
        self.assertFalse(demo_auth["real_authentication"])
        status, ticket = self.api.kiosk_self_demo_issue_ticket(session_id, "screen")
        self.assertEqual(status, 200, ticket)
        self.assertTrue(ticket["demo_only"])
        self.assertTrue(ticket["demo_token"].startswith("DEMO-ONLY:SELF:"))

        status, denied_operational = self.api.request(
            "POST",
            "/api/token",
            {
                "exam_id": EXAM_ID,
                "candidate_id": CAN_1,
                "auth_session_id": "forged-by-client",
                "face_similarity": 1.0,
                "face_match": True,
                "authentication_success": True,
            },
        )
        self.assertEqual(status, 400, denied_operational)
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM admissions").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM demo_tokens").fetchone()[0],
                1,
            )

    def test_self_demo_print_ticket_requires_explicit_result_and_uses_server_ttl(self):
        _, created = self.api.create_kiosk_session()
        session_id = created["kiosk_session_id"]
        self.assertEqual(self.api.lookup_kiosk_candidate(session_id, mode="self_demo")[0], 200)

        status, blocked = self.api.kiosk_self_demo_issue_ticket(session_id, "print")
        self.assertEqual(status, 409)
        self.assertNotIn("qr_png", blocked)

        status, rejected = self.api.kiosk_self_demo_authenticate(
            session_id, outcome="success", demo_simulation=False
        )
        self.assertEqual(status, 400)
        status, authenticated = self.api.kiosk_self_demo_authenticate(session_id)
        self.assertEqual(status, 200, authenticated)
        self.assertEqual(authenticated["state"], "demo_authenticated")
        self.assertTrue(authenticated["demo_only"])
        self.assertFalse(authenticated["real_authentication"])

        status, ticket = self.api.kiosk_self_demo_issue_ticket(session_id, "print")
        self.assertEqual(status, 200, ticket)
        self.assertTrue(ticket["demo_only"])
        self.assertEqual(ticket["delivery"], "print")
        self.assertEqual(ticket["expires_in"], 900)
        self.assertEqual(ticket["candidate_name"], "박세현")
        self.assertEqual(ticket["candidate_number"], "2026-0012")
        token = decode_qr_png(ticket["qr_png"])
        self.assertTrue(token.startswith("DEMO-ONLY:SELF:"))
        self.assertNotIn("박세현", token)
        self.assertNotIn("2026-0012", token)

        status, current = self.api.kiosk_status(session_id)
        self.assertEqual(status, 200)
        self.assertEqual(current["delivery"], "print")
        self.assertNotIn("candidate_name", current)
        self.assertNotIn("candidate_number", current)
        self.assertEqual(current["expires_at"], ticket["expires_at"])
        status, rejected = self.api.inspect(token)
        self.assertEqual(status, 409)
        self.assertFalse(rejected["valid"])

        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            row = connection.execute(
                "SELECT expires_at, used_at FROM demo_tokens WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            self.assertGreaterEqual(row[0], time.time() + 899)
            self.assertIsNone(row[1])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM admissions").fetchone()[0], 0)

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE demo_tokens SET expires_at = 0 WHERE token_hash = ?",
                (token_hash,),
            )
        status, expired = self.api.kiosk_status(session_id)
        self.assertEqual(status, 200)
        self.assertEqual(expired["state"], "expired")
        status, refreshed = self.api.refresh_self_demo_qr(session_id)
        self.assertEqual(status, 200, refreshed)
        self.assertEqual(refreshed["expires_in"], 900)
        refreshed_token = decode_qr_png(refreshed["qr_png"])
        self.assertNotEqual(token, refreshed_token)
        status, replay = self.api.kiosk_demo_entry(session_id, token)
        self.assertEqual(status, 409)
        self.assertFalse(replay["demo_admitted"])
        self.assertEqual(self.api.request(
            "POST", "/api/kiosk/end", {"kiosk_session_id": session_id}
        )[0], 200)
        status, simulated = self.api.kiosk_demo_entry(None, refreshed_token)
        self.assertEqual(status, 200, simulated)
        self.assertTrue(simulated["demo_admitted"])
        self.assertFalse(simulated["real_admission"])
        status, replay = self.api.kiosk_demo_entry(None, refreshed_token)
        self.assertEqual(status, 409)
        self.assertFalse(replay["demo_admitted"])
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM admissions").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM demo_admissions").fetchone()[0], 1)

    def test_kiosk_self_demo_qr_expiration_is_server_enforced(self):
        _, created = self.api.create_kiosk_session()
        session_id = created["kiosk_session_id"]
        self.assertEqual(
            self.api.lookup_kiosk_candidate(session_id, mode="self_demo")[0], 200
        )
        self.assertEqual(self.api.kiosk_self_auth(session_id, "success")[0], 200)
        status, qr = self.api.kiosk_status(session_id)
        self.assertEqual(status, 200)
        token = qr["demo_token"]
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE demo_tokens SET expires_at = 0 WHERE token_hash = ?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            )
        status, expired = self.api.kiosk_status(session_id)
        self.assertEqual(status, 200)
        self.assertEqual(expired["state"], "expired")
        self.assertNotIn("qr_png", expired)
        status, rejected = self.api.kiosk_demo_entry(session_id, token)
        self.assertEqual(status, 409)
        self.assertFalse(rejected["demo_admitted"])

    def test_kiosk_proctor_mode_lookup_and_demo_qr_stay_in_demo_storage(self):
        _, created = self.api.create_kiosk_session()
        session_id = created["kiosk_session_id"]
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            initial_token_count = connection.execute(
                "SELECT COUNT(*) FROM demo_tokens WHERE exam_id = ? AND candidate_id = ?",
                (EXAM_ID, CAN_1),
            ).fetchone()[0]
        status, candidate = self.api.lookup_kiosk_candidate(
            session_id, mode="proctor"
        )
        self.assertEqual(status, 200, candidate)
        self.assertEqual(candidate["status"], "awaiting_proctor")
        self.assertEqual(candidate["authentication_mode"], "proctor")
        self.assertEqual(self.api.kiosk_review(session_id)[0], 200)
        status, issued = self.api.kiosk_status(session_id)
        self.assertEqual(status, 200)
        self.assertTrue(issued["demo_only"])
        self.assertTrue(issued["demo_token"].startswith("DEMO-ONLY:KIOSK-PROCTOR:"))
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM demo_tokens WHERE exam_id = ? AND candidate_id = ?",
                    (EXAM_ID, CAN_1),
                ).fetchone()[0],
                initial_token_count + 1,
            )

    def test_kiosk_proctor_review_is_role_protected_and_issues_private_30_second_qr(self):
        _, created = self.api.create_kiosk_session()
        session_id = created["kiosk_session_id"]
        self.assertEqual(self.api.lookup_kiosk_candidate(session_id)[0], 200)
        status, _ = self.api.request("GET", "/api/kiosk/requests")
        self.assertEqual(status, 403)
        status, requests = self.api.request(
            "GET", "/api/kiosk/requests", role="proctor", key=PROCTOR_KEY
        )
        self.assertEqual(status, 200)
        self.assertIsNone(requests["requests"][0]["photo_url"])
        status, _ = self.api.kiosk_review(session_id, role="supervisor", key=SUPERVISOR_KEY)
        self.assertEqual(status, 403)
        status, response = self.api.kiosk_review(session_id)
        self.assertEqual(status, 200, response)
        self.assertEqual(response["state"], "demo_qr_ready")
        status, qr = self.api.kiosk_status(session_id)
        self.assertEqual(status, 200, qr)
        self.assertEqual(qr["state"], "demo_qr_ready")
        self.assertEqual(qr["qr_mime_type"], "image/png")
        self.assertTrue(base64.b64decode(qr["qr_png"], validate=True).startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreaterEqual(qr["expires_in"], 1)
        self.assertLessEqual(qr["expires_in"], 30)
        self.assertNotIn("candidate_name", qr)
        self.assertNotIn("candidate_number", qr)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            token = connection.execute(
                "SELECT token_hash, expires_at FROM demo_tokens WHERE exam_id = ? AND candidate_id = ? ORDER BY issued_at DESC LIMIT 1",
                (EXAM_ID, CAN_1),
            ).fetchone()
            self.assertEqual(len(token[0]), 64)
            self.assertLessEqual(token[1], time.time() + 30)
            connection.execute(
                "UPDATE demo_tokens SET expires_at = 0 WHERE token_hash = ?", (token[0],)
            )
        status, expired = self.api.kiosk_status(session_id)
        self.assertEqual(status, 200)
        self.assertEqual(expired["state"], "expired")
        self.assertNotIn("qr_png", expired)

    def test_kiosk_qr_refresh_requires_proctor_expired_qr_and_valid_identity_approval(self):
        _, created = self.api.create_kiosk_session()
        session_id = created["kiosk_session_id"]
        self.assertEqual(self.api.lookup_kiosk_candidate(session_id)[0], 200)

        status, response = self.api.request(
            "POST", "/api/kiosk/refresh-qr", {"kiosk_session_id": session_id}
        )
        self.assertEqual(status, 403)
        status, response = self.api.refresh_kiosk_qr(
            session_id, role="supervisor", key=SUPERVISOR_KEY
        )
        self.assertEqual(status, 403)
        status, response = self.api.refresh_kiosk_qr(session_id)
        self.assertEqual(status, 409)

        self.assertEqual(self.api.kiosk_review(session_id)[0], 200)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            previous_hash = connection.execute(
                "SELECT token_hash FROM demo_tokens WHERE exam_id = ? AND candidate_id = ? AND pathway = 'kiosk_proctor_demo' ORDER BY issued_at DESC LIMIT 1",
                (EXAM_ID, CAN_1),
            ).fetchone()[0]
            connection.execute(
                "UPDATE demo_tokens SET expires_at = 0 WHERE token_hash = ?",
                (previous_hash,),
            )
        status, expired = self.api.kiosk_status(session_id)
        self.assertEqual(status, 200)
        self.assertEqual(expired["state"], "expired")
        status, refreshed = self.api.refresh_kiosk_qr(session_id)
        self.assertEqual(status, 200, refreshed)
        self.assertEqual(refreshed["state"], "demo_qr_ready")
        status, qr = self.api.kiosk_status(session_id)
        self.assertEqual(status, 200, qr)
        self.assertEqual(qr["state"], "demo_qr_ready")
        self.assertTrue(qr["demo_only"])
        qr_bytes = base64.b64decode(qr["qr_png"], validate=True)
        self.assertNotIn(b"2026-0012", qr_bytes)
        self.assertNotIn("박세현".encode("utf-8"), qr_bytes)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            previous = connection.execute(
                "SELECT used_at FROM demo_tokens WHERE token_hash = ?", (previous_hash,)
            ).fetchone()
            latest = connection.execute(
                "SELECT token_hash, expires_at, used_at FROM demo_tokens WHERE exam_id = ? AND candidate_id = ? AND pathway = 'kiosk_proctor_demo' ORDER BY issued_at DESC LIMIT 1",
                (EXAM_ID, CAN_1),
            ).fetchone()
            self.assertIsNotNone(previous[0])
            self.assertNotEqual(previous_hash, latest[0])
            self.assertIsNone(latest[2])
            self.assertLessEqual(latest[1], time.time() + 30)

    def test_kiosk_qr_refresh_rejects_still_valid_and_already_used_tokens(self):
        _, created = self.api.create_kiosk_session()
        session_id = created["kiosk_session_id"]
        self.assertEqual(self.api.lookup_kiosk_candidate(session_id)[0], 200)
        self.assertEqual(self.api.kiosk_review(session_id)[0], 200)
        status, response = self.api.refresh_kiosk_qr(session_id)
        self.assertEqual(status, 409)
        self.assertIn("아직 유효", response["error"])

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            token_hash = connection.execute(
                "SELECT token_hash FROM demo_tokens WHERE exam_id = ? AND candidate_id = ? AND pathway = 'kiosk_proctor_demo' ORDER BY issued_at DESC LIMIT 1",
                (EXAM_ID, CAN_1),
            ).fetchone()[0]
            connection.execute(
                "UPDATE demo_tokens SET expires_at = 0, used_at = ? WHERE token_hash = ?",
                (time.time(), token_hash),
            )
        status, response = self.api.refresh_kiosk_qr(session_id)
        self.assertEqual(status, 409)
        self.assertIn("이미 사용", response["error"])

    def test_kiosk_hides_qr_after_entry_gate_consumes_token(self):
        _, created = self.api.create_kiosk_session()
        session_id = created["kiosk_session_id"]
        self.assertEqual(self.api.lookup_kiosk_candidate(session_id)[0], 200)
        self.assertEqual(self.api.kiosk_review(session_id)[0], 200)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            token_hash = connection.execute(
                "SELECT token_hash FROM demo_tokens WHERE exam_id = ? AND candidate_id = ? AND pathway = 'kiosk_proctor_demo' ORDER BY issued_at DESC LIMIT 1",
                (EXAM_ID, CAN_1),
            ).fetchone()[0]
            connection.execute(
                "UPDATE demo_tokens SET used_at = ? WHERE token_hash = ?",
                (time.time(), token_hash),
            )
        status, result = self.api.kiosk_status(session_id)
        self.assertEqual(status, 200)
        self.assertEqual(result["state"], "demo_completed")
        self.assertNotIn("qr_png", result)

    def test_kiosk_help_request_is_recorded_without_identity_details(self):
        _, created = self.api.create_kiosk_session()
        session_id = created["kiosk_session_id"]
        status, result = self.api.request(
            "POST", "/api/kiosk/help", {"kiosk_session_id": session_id}
        )
        self.assertEqual(status, 200, result)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            event = connection.execute(
                "SELECT event, outcome, details FROM events ORDER BY event_id DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(event[:2], ("키오스크 관리자 도움 요청", "pending"))
            self.assertNotIn("2026-0012", event[2])

    def test_kiosk_mismatch_proxy_and_unverifiable_cases_do_not_issue_qr(self):
        cases = (
            ("document_mismatch", "reject", "resident_card", False, False, "rejected", "키오스크 신분증 불일치"),
            ("suspected_proxy", "reject", "resident_card", True, False, "rejected", "키오스크 대리응시 의심"),
            ("identity_unverifiable", "hold", "resident_card", False, False, "supervisor_pending", "키오스크 책임자 확인 대기"),
            ("no_document", "hold", "none", False, False, "supervisor_pending", "키오스크 책임자 확인 대기"),
            ("identity_unverifiable", "approve", "resident_card", True, True, "supervisor_pending", "키오스크 책임자 확인 대기"),
            ("normal", "approve", "resident_card", False, False, "supervisor_pending", "키오스크 책임자 확인 대기"),
        )
        for reason, decision, document_type, document_match, live_face_match, expected_state, expected_event in cases:
            with self.subTest(reason=reason):
                _, created = self.api.create_kiosk_session()
                session_id = created["kiosk_session_id"]
                self.assertEqual(self.api.lookup_kiosk_candidate(session_id)[0], 200)
                status, result = self.api.kiosk_review(
                    session_id,
                    document_type=document_type,
                    decision=decision,
                    reason=reason,
                    document_match=document_match,
                    live_face_match=live_face_match,
                )
                self.assertEqual(status, 200, result)
                self.assertEqual(result["state"], expected_state)
                self.assertNotIn("qr_png", self.api.kiosk_status(session_id)[1])
                with closing(sqlite3.connect(self.db_path)) as connection, connection:
                    event = connection.execute(
                        "SELECT event FROM events ORDER BY event_id DESC LIMIT 1"
                    ).fetchone()[0]
                    self.assertEqual(event, expected_event)

    def test_kiosk_end_clears_session_and_invalidates_unspent_qr(self):
        _, created = self.api.create_kiosk_session()
        session_id = created["kiosk_session_id"]
        self.assertEqual(self.api.lookup_kiosk_candidate(session_id)[0], 200)
        self.assertEqual(self.api.kiosk_review(session_id)[0], 200)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            token_hash = connection.execute(
                "SELECT token_hash FROM demo_tokens WHERE exam_id = ? AND candidate_id = ? AND pathway = 'kiosk_proctor_demo' ORDER BY issued_at DESC LIMIT 1",
                (EXAM_ID, CAN_1),
            ).fetchone()[0]
        status, ended = self.api.request(
            "POST", "/api/kiosk/end", {"kiosk_session_id": session_id}
        )
        self.assertEqual(status, 200, ended)
        self.assertEqual(self.api.kiosk_status(session_id)[0], 410)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            used_at = connection.execute(
                "SELECT used_at FROM demo_tokens WHERE token_hash = ?", (token_hash,)
            ).fetchone()[0]
            self.assertIsNotNone(used_at)

    def test_kiosk_end_invalidates_self_demo_qr(self):
        _, created = self.api.create_kiosk_session()
        session_id = created["kiosk_session_id"]
        self.assertEqual(
            self.api.lookup_kiosk_candidate(session_id, mode="self_demo")[0], 200
        )
        self.assertEqual(self.api.kiosk_self_auth(session_id, "success")[0], 200)
        _, qr = self.api.kiosk_status(session_id)
        token_hash = hashlib.sha256(qr["demo_token"].encode()).hexdigest()
        status, ended = self.api.request(
            "POST", "/api/kiosk/end", {"kiosk_session_id": session_id}
        )
        self.assertEqual(status, 200, ended)
        self.assertEqual(self.api.kiosk_status(session_id)[0], 410)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            used_at = connection.execute(
                "SELECT used_at FROM demo_tokens WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()[0]
            self.assertIsNotNone(used_at)
            audit = connection.execute(
                "SELECT details FROM events WHERE event = '키오스크 세션 종료' ORDER BY event_id DESC LIMIT 1"
            ).fetchone()[0]
            self.assertIn("authentication_path=self_demo", audit)

    def test_kiosk_rejects_duplicate_admission_before_and_during_proctor_review(self):
        token = self.issue_token_value()
        self.assertEqual(self.api.approve(token)[0], 200)

        _, created = self.api.create_kiosk_session()
        session_id = created["kiosk_session_id"]
        status, response = self.api.lookup_kiosk_candidate(session_id)
        self.assertEqual(status, 409)
        self.assertIn("이미 입실", response["error"])

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("DELETE FROM admissions WHERE exam_id = ? AND candidate_id = ?", (EXAM_ID, CAN_1))
            connection.execute("DELETE FROM demo_admissions WHERE exam_id = ? AND candidate_id = ?", (EXAM_ID, CAN_1))
        _, created = self.api.create_kiosk_session()
        session_id = created["kiosk_session_id"]
        self.assertEqual(self.api.lookup_kiosk_candidate(session_id)[0], 200)
        token = self.issue_token_value()
        self.assertEqual(self.api.approve(token)[0], 200)
        status, response = self.api.kiosk_review(session_id)
        self.assertEqual(status, 409)
        self.assertIn("이미 입실", response["error"])
        self.assertEqual(self.api.kiosk_status(session_id)[1]["state"], "duplicate")

    def test_physical_id_approval_is_allowed_only_by_mock_policy_and_one_time(self):
        status, response = self.api.decide_physical()
        self.assertEqual(status, 200, response)
        self.assertTrue(response["admitted"])
        self.assertTrue(response["demo_admitted"])
        self.assertFalse(response["real_admission"])
        duplicate_status, duplicate = self.api.decide_physical()
        self.assertEqual(duplicate_status, 409)
        self.assertFalse(duplicate["admitted"])
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM demo_admissions WHERE exam_id = ? AND candidate_id = ?",
                    (EXAM_ID, CAN_1),
                ).fetchone()[0],
                1,
            )

    def test_unlisted_document_type_is_rejected_and_logged_without_sensitive_data(self):
        payload = {
            "exam_id": EXAM_ID,
            "candidate_id": CAN_1,
            "document_type": "other",
            "decision": "approve",
            "reason": "identity_unverifiable",
            "document_match": True,
            "roster_photo_match": True,
            "live_face_match": True,
            "document_number": "FAKE-DO-NOT-STORE",
            "birth_date": "1900-01-01",
            "document_photo": "FAKE-IMAGE-DO-NOT-STORE",
        }
        status, response = self.api.request("POST", "/api/physical-id/decision", payload, "proctor", PROCTOR_KEY)
        self.assertEqual(status, 403)
        self.assertFalse(response["admitted"])
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            event = connection.execute(
                "SELECT event, outcome, details FROM events ORDER BY event_id DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(event[:2], ("신분증 불일치", "rejected"))
            stored = " ".join(str(value) for value in event)
            self.assertNotIn("FAKE-DO-NOT-STORE", stored)
            self.assertNotIn("1900-01-01", stored)
            self.assertNotIn("FAKE-IMAGE", stored)

    def test_suspected_proxy_and_document_mismatch_are_rejected(self):
        status, response = self.api.decide_physical(live_face_match=False)
        self.assertEqual(status, 403)
        self.assertIn("대조 불일치", response["error"])
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            event = connection.execute(
                "SELECT event, outcome FROM events ORDER BY event_id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(event, ("대리응시 의심", "rejected"))

        status, response = self.api.decide_physical(document_match=False, reason="document_mismatch")
        self.assertEqual(status, 403)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            event = connection.execute(
                "SELECT event, outcome FROM events ORDER BY event_id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(event, ("신분증 불일치", "rejected"))

    def test_unverifiable_identity_is_not_approved(self):
        status, response = self.api.decide_physical(
            document_match=False, roster_photo_match=False, live_face_match=False
        )
        self.assertEqual(status, 403)
        self.assertFalse(response["admitted"])
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            event = connection.execute(
                "SELECT event, outcome FROM events ORDER BY event_id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(event, ("신원확인 불가", "rejected"))

    def test_missing_phone_and_document_is_pending_not_admitted(self):
        status, response = self.api.request(
            "POST",
            "/api/physical-id/decision",
            {
                "exam_id": EXAM_ID,
                "candidate_id": CAN_1,
                "document_type": "none",
                "decision": "hold",
                "reason": "no_phone_or_document",
                "document_match": False,
                "roster_photo_match": False,
                "live_face_match": False,
            },
            "proctor",
            PROCTOR_KEY,
        )
        self.assertEqual(status, 200)
        self.assertFalse(response["admitted"])
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            self.assertEqual(
                connection.execute(
                    "SELECT event, outcome FROM events ORDER BY event_id DESC LIMIT 1"
                ).fetchone(),
                ("신원확인 보류", "pending"),
            )
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM admissions").fetchone()[0], 0)

    def test_exception_requires_supervisor_role_and_complete_preapproved_procedure(self):
        status, _ = self.api.decide_physical(role="supervisor", key=SUPERVISOR_KEY)
        self.assertEqual(status, 403)
        status, _ = self.api.decide_exception(role="proctor", key=PROCTOR_KEY)
        self.assertEqual(status, 403)
        status, response = self.api.decide_exception(checks=(False, True, False, True))
        self.assertEqual(status, 403)
        self.assertFalse(response["admitted"])
        status, response = self.api.decide_exception()
        self.assertEqual(status, 200, response)
        self.assertTrue(response["admitted"])
        self.assertTrue(response["demo_admitted"])
        self.assertFalse(response["real_admission"])
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            self.assertEqual(
                connection.execute(
                    "SELECT actor, outcome FROM events WHERE event = '책임자 예외 DEMO 모의 입실' ORDER BY event_id DESC LIMIT 1"
                ).fetchone(),
                ("supervisor", "demo_admitted"),
            )

    def test_mobile_id_demo_issues_demo_qr_and_inspection_does_not_consume(self):
        issued = self.issue_token()
        self.assertTrue(issued["demo_only"])
        self.assertTrue(issued["token"].startswith("DEMO-ONLY:MOBILE:"))
        qr_bytes = base64.b64decode(issued["qr_png"], validate=True)
        self.assertTrue(qr_bytes.startswith(b"\x89PNG\r\n\x1a\n"))
        with Image.open(io.BytesIO(qr_bytes)) as qr_image:
            self.assertGreaterEqual(qr_image.width, 200)
            self.assertEqual(qr_image.width, qr_image.height)
            qr_image.verify()
        status, result = self.api.inspect(issued["token"])
        self.assertEqual(status, 200, result)
        self.assertTrue(result["valid"])
        self.assertTrue(result["demo_only"])
        self.assertEqual(result["candidate_id"], CAN_1)
        status, approved = self.api.approve(issued["token"])
        self.assertEqual(status, 200, approved)
        self.assertTrue(approved["demo_admitted"])
        self.assertFalse(approved["real_admission"])
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM admissions").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM demo_admissions").fetchone()[0], 1)

    def test_mock_auth_failure_does_not_issue_qr_and_is_logged(self):
        status, result = self.api.authenticate(demo_outcome="failure")
        self.assertEqual(status, 200)
        self.assertFalse(result["success"])
        self.assertIsNone(result["auth_session_id"])
        self.assertFalse(result["official"])
        self.assertEqual(
            self.api.request(
                "POST",
                "/api/token",
                {"exam_id": EXAM_ID, "candidate_id": CAN_1},
            )[0],
            400,
        )
        self.assertEqual(self.api.issue_with_auth(CAN_1, "")[0], 400)
        _, dashboard = self.api.request("GET", "/api/dashboard", role="admin", key=ADMIN_KEY)
        self.assertTrue(
            any(
                event["event"] == "모의 모바일신분증 인증" and event["outcome"] == "blocked"
                for event in dashboard["events"]
            )
        )

    def test_successful_mock_auth_proof_is_one_time(self):
        status, result = self.api.authenticate()
        self.assertEqual(status, 200)
        self.assertTrue(result["success"])
        self.assertEqual(result["provider"], "mock")
        self.assertFalse(result["official"])
        with closing(sqlite3.connect(self.db_path)) as db:
            stored_hash = db.execute(
                "SELECT auth_session_hash FROM demo_mobile_id_auth ORDER BY auth_id DESC LIMIT 1"
            ).fetchone()[0]
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM mobile_id_auth").fetchone()[0],
                0,
            )
        self.assertEqual(stored_hash, hashlib.sha256(result["auth_session_id"].encode()).hexdigest())
        self.assertNotEqual(stored_hash, result["auth_session_id"])
        self.assertEqual(self.api.issue_with_auth(CAN_1, result["auth_session_id"])[0], 201)
        status, response = self.api.issue_with_auth(CAN_1, result["auth_session_id"])
        self.assertEqual(status, 409)
        self.assertIn("이미", response["error"])

    def test_demo_auth_cannot_issue_or_admit_operational_qr(self):
        auth_status, auth = self.api.authenticate()
        self.assertEqual(auth_status, 200)
        self.assertTrue(auth["success"])
        operation_status, operation = self.api.request(
            "POST",
            "/api/token",
            {
                "exam_id": EXAM_ID,
                "candidate_id": CAN_1,
                "auth_session_id": auth["auth_session_id"],
            },
        )
        self.assertEqual(operation_status, 503, operation)
        self.assertFalse(operation.get("demo_only", True))

        status, issued = self.api.issue_with_auth(CAN_1, auth["auth_session_id"])
        self.assertEqual(status, 201, issued)
        self.assertTrue(issued["demo_only"])
        token = issued["token"]
        self.assertTrue(token.startswith("DEMO-ONLY:MOBILE:"))
        status, inspected = self.api.request(
            "POST",
            "/api/token/inspect",
            {"exam_id": EXAM_ID, "token": token},
            "proctor",
            PROCTOR_KEY,
        )
        self.assertEqual(status, 409)
        self.assertFalse(inspected["valid"])
        status, approval = self.api.request(
            "POST",
            "/api/approve",
            {
                "exam_id": EXAM_ID,
                "candidate_id": CAN_1,
                "token": token,
                "roster_match": True,
                "face_match": True,
            },
            "proctor",
            PROCTOR_KEY,
        )
        self.assertEqual(status, 409)
        self.assertFalse(approval["admitted"])
        status, demo_approval = self.api.approve(token)
        self.assertEqual(status, 200, demo_approval)
        self.assertTrue(demo_approval["demo_admitted"])
        self.assertFalse(demo_approval["real_admission"])
        status, replay = self.api.approve(token)
        self.assertEqual(status, 409)
        self.assertFalse(replay["demo_admitted"])
        with closing(sqlite3.connect(self.db_path)) as db, db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM admissions").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM demo_admissions").fetchone()[0], 1)

    def test_official_auth_and_operational_approval_fail_closed_without_provider(self):
        status, auth = self.api.request(
            "POST",
            "/api/mobile-id/official-authenticate",
            {"exam_id": EXAM_ID, "candidate_id": CAN_1},
        )
        self.assertEqual(status, 503, auth)
        self.assertFalse(auth["success"])

        status, issued = self.api.request(
            "POST",
            "/api/token",
            {
                "exam_id": EXAM_ID,
                "candidate_id": CAN_1,
                "auth_session_id": "forged-or-demo-auth",
            },
        )
        self.assertEqual(status, 503, issued)

        fake_token = "synthetic-operational-token"
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute(
                """
                INSERT INTO tokens
                    (token_hash, exam_id, candidate_id, issued_at, expires_at, auth_session_hash)
                VALUES (?, ?, ?, ?, ?, NULL)
                """,
                (
                    hashlib.sha256(fake_token.encode()).hexdigest(),
                    EXAM_ID,
                    CAN_1,
                    time.time(),
                    time.time() + 30,
                ),
            )
        status, inspected = self.api.request(
            "POST",
            "/api/token/inspect",
            {"exam_id": EXAM_ID, "token": fake_token},
            "proctor",
            PROCTOR_KEY,
        )
        self.assertEqual(status, 503)
        self.assertFalse(inspected["valid"])
        status, approval = self.api.request(
            "POST",
            "/api/approve",
            {
                "exam_id": EXAM_ID,
                "candidate_id": CAN_1,
                "token": fake_token,
                "roster_match": True,
                "face_match": True,
            },
            "proctor",
            PROCTOR_KEY,
        )
        self.assertEqual(status, 503)
        self.assertFalse(approval["admitted"])
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM admissions").fetchone()[0], 0)

    def test_new_mock_auth_invalidates_previous_unspent_proof(self):
        _, earlier = self.api.authenticate()
        self.assertTrue(earlier["success"])
        _, failed = self.api.authenticate(demo_outcome="failure")
        self.assertFalse(failed["success"])
        status, response = self.api.issue_with_auth(CAN_1, earlier["auth_session_id"])
        self.assertEqual(status, 409)
        self.assertIn("무효화", response["error"])

    def test_inspection_blocks_wrong_exam_expired_reuse_and_admitted_candidate(self):
        issued = self.issue_token()
        status, response = self.api.inspect(issued["token"], exam_id="another-exam")
        self.assertEqual(status, 409)
        self.assertIn("시험", response["error"])
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute(
                "UPDATE demo_tokens SET expires_at = 0 WHERE token_hash = ?",
                (hashlib.sha256(issued["token"].encode()).hexdigest(),),
            )
        status, response = self.api.inspect(issued["token"])
        self.assertEqual(status, 409)
        self.assertIn("만료", response["error"])

        fresh = self.issue_token_value()
        self.assertEqual(self.api.approve(fresh)[0], 200)
        auth_status, auth = self.api.authenticate()
        self.assertEqual(auth_status, 200)
        status, response = self.api.issue_with_auth(CAN_1, auth["auth_session_id"])
        self.assertEqual(status, 409)
        self.assertIn("입실", response["error"])

    def test_normal_approval(self):
        token = self.issue_token_value()
        status, response = self.api.approve(token, face_match=True)
        self.assertEqual(status, 200, response)
        self.assertTrue(response["demo_admitted"])
        self.assertFalse(response["real_admission"])
        dashboard = self.api.request(
            "GET",
            "/api/dashboard",
            role="admin",
            key=ADMIN_KEY,
        )[1]
        self.assertEqual(dashboard["admitted_count"], 0)

    def test_expired_token_is_blocked(self):
        token = self.issue_token_value()
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute(
                "UPDATE demo_tokens SET expires_at = 0 WHERE token_hash = ?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            )
        status, response = self.api.approve(token)
        self.assertEqual(status, 409)
        self.assertIn("만료", response["error"])

    def test_reuse_is_blocked(self):
        token = self.issue_token_value()
        self.assertEqual(self.api.approve(token)[0], 200)
        status, response = self.api.approve(token)
        self.assertEqual(status, 409)
        self.assertIn("사용", response["error"])

    def test_concurrent_duplicate_requests_only_admit_once(self):
        token = self.issue_token_value()
        barrier = threading.Barrier(3)
        results = []

        def submit():
            barrier.wait()
            results.append(self.api.approve(token))

        workers = [threading.Thread(target=submit) for _ in range(2)]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(timeout=10)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(sorted(status for status, _ in results), [200, 409])

    def test_another_candidate_cannot_use_token(self):
        token = self.issue_token_value(CAN_1)
        status, response = self.api.approve(token, candidate_id=CAN_2)
        self.assertEqual(status, 409)
        self.assertIn("일치", response["error"])

    def test_face_mismatch_blocks_and_consumes_token(self):
        token = self.issue_token_value()
        status, response = self.api.approve(token, face_match=False)
        self.assertEqual(status, 403)
        self.assertFalse(response["admitted"])
        status, response = self.api.approve(token, face_match=True)
        self.assertEqual(status, 409)

    def test_roster_mismatch_blocks_approval(self):
        token = self.issue_token_value()
        status, response = self.api.approve(token, roster_match=False)
        self.assertEqual(status, 403)
        self.assertIn("명부", response["error"])

    def test_role_separation_and_server_event_log(self):
        token = self.issue_token_value()
        self.assertEqual(
            self.api.request(
                "POST",
                "/api/approve",
                {
                    "exam_id": EXAM_ID,
                    "candidate_id": CAN_1,
                    "token": token,
                    "roster_match": True,
                    "face_match": True,
                },
                "admin",
                ADMIN_KEY,
            )[0],
            403,
        )
        status, dashboard = self.api.request(
            "GET", "/api/dashboard", role="admin", key=ADMIN_KEY
        )
        self.assertEqual(status, 200)
        self.assertTrue(any(event["event"] == "모의 모바일신분증 인증" for event in dashboard["events"]))
        self.assertTrue(any(event["event"] == "DEMO QR 토큰 발급" for event in dashboard["events"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
