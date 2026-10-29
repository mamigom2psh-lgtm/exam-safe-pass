#!/usr/bin/env python3
"""Demo HTTP server for Exam Safe Pass with SQLite-backed token verification."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

import qrcode

from mobile_id_provider import (
    MobileIdAuthResult,
    MobileIdProvider,
    MockMobileIdProvider,
    OfficialMobileIdProvider,
)
from self_identity_provider import MockSelfIdentityProvider
import demo_scenarios
import verification_policy


ROOT = Path(__file__).resolve().parent
DATABASE = Path(os.environ.get("EXAM_SAFE_DB", ROOT / "exam_safe_pass.sqlite3"))
TOKEN_TTL_SECONDS = 30
AUTH_PROOF_TTL_SECONDS = 300
KIOSK_PRINT_QR_TTL_SECONDS = int(
    os.environ.get("EXAM_SAFE_KIOSK_PRINT_QR_TTL_SECONDS", "900")
)
if KIOSK_PRINT_QR_TTL_SECONDS < 30:
    raise RuntimeError("EXAM_SAFE_KIOSK_PRINT_QR_TTL_SECONDS must be at least 30")
KIOSK_BROWSER_ASSERTION_FIELDS = frozenset({
    "face_score", "face_similarity", "face_match", "captured_face_result",
    "face_comparison_result", "face_verified", "authentication_success",
    "real_authentication", "liveness", "score", "verified",
})
EXAM_ID = "exam-demo-2026"
ROSTER = {
    ("exam-demo-2026", "candidate-001"): {"name": "박세현", "number": "2026-0012"},
    ("exam-demo-2026", "candidate-002"): {"name": "이모의", "number": "2026-0099"},
}
# Preset VIRTUAL DEMO data. These are not real personal data; the code is a fictional birth-date/gender
# code used only by the contest simulation and is never accepted as a real resident number.
DEMO_EXAMS = {
    "demo-computer-2": "컴퓨터활용능력 2급",
    "demo-mock-2026": "2026 모의 자격시험",
}
DEMO_CANDIDATE_PROFILES = {
    ("exam-demo-2026", "candidate-001"): {"virtual_code": "980320-2", "exam_id": "demo-computer-2"},
}
DEMO_DEFAULT_EXAM_ID = "demo-computer-2"  # pre-selected in the UI; the server still compares on submit
DEMO_INFO_MAX_ATTEMPTS = 5
# Server-side permission for the preset DEMO simulation. It never enables operational issuance.
DEMO_SIMULATION_ENABLED = os.environ.get("EXAM_SAFE_DEMO_SIMULATION", "1") != "0"
VIRTUAL_CODE_PATTERN = re.compile(r"^(\d{2})(\d{2})(\d{2})-([1-4])$")
IMAGE_FILES = {
    "2026-0012-registration.jpg",
    "candidate2.png",
    "impostor.png",
}
VENDOR_FILES = {
    "qr-decoder.min.js",
    "ZXING-BROWSER-LICENSE.txt",
    "ZXING-LIBRARY-LICENSE.txt",
    "ZXING-TEXT-ENCODING-LICENSE.txt",
    "TS-CUSTOM-ERROR-LICENSE.txt",
    "human/human.bundle.js",
    "human/LICENSE",
    "human/MODEL-README.md",
    "human/models/blazeface.json",
    "human/models/blazeface.bin",
    "human/models/faceres.json",
    "human/models/faceres.bin",
    "human/models/facemesh.json",
    "human/models/facemesh.bin",
    "human/models/antispoof.json",
    "human/models/antispoof.bin",
}
DEMO_ACCOUNTS = {
    "candidate": {
        "username": "candidate-demo",
        "password": "candidate-demo-123",
        "candidate_id": "candidate-001",
    },
    "proctor": {
        "username": "proctor-demo",
        "password": "proctor-demo-123",
    },
    "admin": {
        "username": "admin-demo",
        "password": "admin-demo-123",
    },
    "supervisor": {
        "username": "supervisor-demo",
        "password": "supervisor-demo-123",
    },
}
MOCK_ALLOWED_ID_TYPES = {"resident_card", "driver_license", "passport"}
MOCK_EXCEPTION_PROCEDURES = {"dual_staff_application_review"}
SESSION_TTL_SECONDS = 3600
EVENT_RETENTION_DAYS = int(os.environ.get("EXAM_SAFE_EVENT_RETENTION_DAYS", "30"))
KIOSK_SESSION_TTL_SECONDS = 180
DEMO_MOBILE_ID_PROVIDER_FACTORY: Callable[[str], MobileIdProvider] = MockMobileIdProvider
OFFICIAL_MOBILE_ID_PROVIDER: OfficialMobileIdProvider | None = None
INSTITUTION_ADMISSION_POLICY_APPROVED = False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect_db():
    connection = sqlite3.connect(DATABASE, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def purge_old_events(connection: sqlite3.Connection, now: datetime | None = None) -> int:
    """Delete audit events older than the retention period; returns rows removed."""
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=EVENT_RETENTION_DAYS)).isoformat(
        timespec="seconds"
    )
    return connection.execute("DELETE FROM events WHERE occurred_at < ?", (cutoff,)).rowcount


def initialize_db() -> None:
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    with connect_db() as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS tokens (
                token_hash TEXT PRIMARY KEY,
                exam_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                issued_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                used_at REAL
            );
            CREATE TABLE IF NOT EXISTS mobile_id_auth (
                auth_id INTEGER PRIMARY KEY AUTOINCREMENT,
                auth_session_hash TEXT UNIQUE,
                exam_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                outcome TEXT NOT NULL,
                authenticated_at REAL NOT NULL,
                expires_at REAL,
                token_issued_at REAL,
                invalidated_at REAL,
                official INTEGER NOT NULL DEFAULT 0,
                candidate_match INTEGER,
                policy_approved INTEGER NOT NULL DEFAULT 0,
                verification_source TEXT,
                signing_key_id TEXT,
                signature_valid INTEGER NOT NULL DEFAULT 0,
                transaction_id TEXT,
                request_id TEXT,
                evidence_candidate_id TEXT,
                evidence_issued_at REAL,
                liveness_source TEXT,
                liveness_model_version TEXT,
                liveness_signature_valid INTEGER NOT NULL DEFAULT 0,
                liveness_transaction_id TEXT,
                liveness_candidate_id TEXT,
                liveness_issued_at REAL,
                liveness_expires_at REAL,
                liveness_score REAL,
                liveness_threshold REAL
            );
            CREATE TABLE IF NOT EXISTS admissions (
                exam_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                admitted_at TEXT NOT NULL,
                PRIMARY KEY (exam_id, candidate_id)
            );
            CREATE TABLE IF NOT EXISTS demo_tokens (
                token_hash TEXT PRIMARY KEY,
                exam_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                pathway TEXT NOT NULL,
                issued_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                used_at REAL
            );
            CREATE TABLE IF NOT EXISTS demo_mobile_id_auth (
                auth_id INTEGER PRIMARY KEY AUTOINCREMENT,
                auth_session_hash TEXT UNIQUE,
                exam_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                outcome TEXT NOT NULL,
                authenticated_at REAL NOT NULL,
                expires_at REAL,
                token_issued_at REAL,
                invalidated_at REAL
            );
            CREATE TABLE IF NOT EXISTS demo_admissions (
                exam_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                admitted_at TEXT NOT NULL,
                PRIMARY KEY (exam_id, candidate_id)
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at TEXT NOT NULL,
                event TEXT NOT NULL,
                outcome TEXT NOT NULL,
                exam_id TEXT,
                candidate_id TEXT,
                actor TEXT NOT NULL,
                details TEXT NOT NULL
            );
            """
        )
        event_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(events)")
        }
        for column in ("domain", "auth_path"):
            if column not in event_columns:
                connection.execute(
                    f"ALTER TABLE events ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                )
        token_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(tokens)")
        }
        if "auth_session_hash" not in token_columns:
            connection.execute("ALTER TABLE tokens ADD COLUMN auth_session_hash TEXT")
        mobile_auth_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(mobile_id_auth)")
        }
        for column, definition in (
            ("official", "INTEGER NOT NULL DEFAULT 0"),
            ("candidate_match", "INTEGER"),
            ("policy_approved", "INTEGER NOT NULL DEFAULT 0"),
            ("verification_source", "TEXT"),
            ("signing_key_id", "TEXT"),
            ("signature_valid", "INTEGER NOT NULL DEFAULT 0"),
            ("transaction_id", "TEXT"),
            ("request_id", "TEXT"),
            ("evidence_candidate_id", "TEXT"),
            ("evidence_issued_at", "REAL"),
            ("liveness_source", "TEXT"),
            ("liveness_model_version", "TEXT"),
            ("liveness_signature_valid", "INTEGER NOT NULL DEFAULT 0"),
            ("liveness_transaction_id", "TEXT"),
            ("liveness_candidate_id", "TEXT"),
            ("liveness_issued_at", "REAL"),
            ("liveness_expires_at", "REAL"),
            ("liveness_score", "REAL"),
            ("liveness_threshold", "REAL"),
        ):
            if column not in mobile_auth_columns:
                connection.execute(
                    f"ALTER TABLE mobile_id_auth ADD COLUMN {column} {definition}"
                )
        purge_old_events(connection)
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS mobile_id_auth_transaction
            ON mobile_id_auth (verification_source, transaction_id)
            WHERE transaction_id IS NOT NULL
            """
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS mobile_id_auth_liveness_transaction
            ON mobile_id_auth (liveness_source, liveness_transaction_id)
            WHERE liveness_transaction_id IS NOT NULL
            """
        )


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def operational_identity_ready() -> bool:
    return (
        OFFICIAL_MOBILE_ID_PROVIDER is not None
        and INSTITUTION_ADMISSION_POLICY_APPROVED is True
    )


def has_operational_auth_proof(
    connection: sqlite3.Connection,
    auth_session_hash: str | None,
    exam_id: str,
    candidate_id: str,
    now: float,
    *,
    require_issued: bool = True,
) -> bool:
    if not auth_session_hash or not operational_identity_ready():
        return False
    expected_provider = getattr(OFFICIAL_MOBILE_ID_PROVIDER, "name", None)
    if not isinstance(expected_provider, str) or not expected_provider:
        return False
    auth = connection.execute(
        """
        SELECT * FROM mobile_id_auth
        WHERE auth_session_hash = ? AND exam_id = ? AND candidate_id = ?
        """,
        (auth_session_hash, exam_id, candidate_id),
    ).fetchone()
    return bool(
        auth
        and auth["outcome"] == "success"
        and auth["provider"] == expected_provider
        and auth["official"] == 1
        and auth["candidate_match"] == 1
        and auth["policy_approved"] == 1
        and auth["signature_valid"] == 1
        and bool(auth["verification_source"])
        and bool(auth["signing_key_id"])
        and bool(auth["transaction_id"])
        and auth["request_id"] is not None
        and auth["evidence_candidate_id"] == candidate_id
        and auth["evidence_issued_at"] is not None
        and auth["evidence_issued_at"] <= now
        and auth["liveness_signature_valid"] == 1
        and bool(auth["liveness_source"])
        and bool(auth["liveness_model_version"])
        and bool(auth["liveness_transaction_id"])
        and auth["liveness_candidate_id"] == candidate_id
        and auth["liveness_issued_at"] is not None
        and auth["liveness_issued_at"] <= now
        and auth["liveness_expires_at"] is not None
        and auth["liveness_expires_at"] > now
        and auth["liveness_score"] is not None
        and auth["liveness_threshold"] is not None
        and 0 <= auth["liveness_threshold"]
        and auth["liveness_threshold"] <= auth["liveness_score"]
        and auth["liveness_score"] <= 1
        and auth["expires_at"] is not None
        and auth["expires_at"] > now
        and auth["expires_at"] > auth["evidence_issued_at"]
        and auth["expires_at"] <= auth["liveness_expires_at"]
        and auth["liveness_expires_at"] > auth["liveness_issued_at"]
        and auth["invalidated_at"] is None
        and (
            auth["token_issued_at"] is not None
            if require_issued
            else auth["token_issued_at"] is None
        )
    )


def write_event(
    connection: sqlite3.Connection,
    event: str,
    outcome: str,
    *,
    exam_id: str | None = None,
    candidate_id: str | None = None,
    actor: str = "candidate",
    details: str = "",
) -> None:
    safe_details = verification_policy.redact_details(details)
    auth_path_match = re.search(r"authentication_path=([a-z_]+)", safe_details)
    auth_path = auth_path_match.group(1) if auth_path_match else next(
        (
            path_name
            for keyword, path_name in (
                ("모바일신분증", "mobile_id"),
                ("감독관", "proctor"),
                ("현장", "proctor"),
                ("키오스크", "kiosk"),
            )
            if keyword in event
        ),
        "",
    )
    # DEMO records and operational records are tagged so they can be queried separately.
    domain = "demo" if re.search(r"demo", f"{event} {safe_details}", re.I) else "operational"
    connection.execute(
        """
        INSERT INTO events
            (occurred_at, event, outcome, exam_id, candidate_id, actor, details,
             domain, auth_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            utc_now(),
            event,
            outcome,
            exam_id,
            candidate_id,
            actor,
            safe_details,
            domain,
            auth_path,
        ),
    )


class DemoServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "ExamSafePassDemo/3.0"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[http] {self.address_string()} {fmt % args}", flush=True)

    def _send_json(
        self,
        status: int,
        payload: dict[str, object],
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if extra_headers:
            for header_name, header_value in extra_headers.items():
                self.send_header(header_name, header_value)
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._send_json(404, {"error": "파일을 찾을 수 없습니다."})
            return
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".jpg": "image/jpeg",
            ".png": "image/png",
            ".js": "text/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".txt": "text/plain; charset=utf-8",
        }.get(path.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, object]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("요청 크기가 올바르지 않습니다.") from exc
        if length < 1 or length > 65536:
            raise ValueError("요청 본문은 1~65536바이트여야 합니다.")
        try:
            value = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("JSON 요청 본문이 올바르지 않습니다.") from exc
        if not isinstance(value, dict):
            raise ValueError("JSON 객체를 보내야 합니다.")
        return value

    def _session_id(self) -> str:
        cookie_header = self.headers.get("Cookie", "")
        if not cookie_header:
            return ""
        cookies = SimpleCookie()
        cookies.load(cookie_header)
        session_cookie = cookies.get("demo_session_id")
        return session_cookie.value if session_cookie else ""

    def _current_session_role(self) -> str | None:
        session_id = self._session_id()
        if not session_id:
            return None
        session = self.server.sessions.get(session_id)
        if not session:
            return None
        if session.get("expires_at", 0) <= time.time():
            self.server.sessions.pop(session_id, None)
            return None
        return session.get("role")

    def _require_role(self, role: str) -> bool:
        session_role = self._current_session_role()
        if session_role == role:
            return True
        expected = self.server.role_keys.get(role)
        supplied = self.headers.get("X-Demo-Key", "")
        supplied_role = self.headers.get("X-Demo-Role", "")
        if expected and supplied_role == role and hmac.compare_digest(supplied, expected):
            return True
        with connect_db() as connection:
            write_event(
                connection,
                "권한 확인",
                "blocked",
                actor=supplied_role or (session_role or "unknown"),
                details=f"{role} 권한이 필요합니다.",
            )
        self._send_json(403, {"error": "시연 권한이 없습니다."})
        return False

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send_file(ROOT / "index.html")
            return
        if path == "/kiosk":
            self._send_file(ROOT / "kiosk.html")
            return
        if path == "/demo-scenarios":
            self._send_file(ROOT / "demo_scenarios.html")
            return
        if path == "/face_similarity.js":
            self._send_file(ROOT / "face_similarity.js")
            return
        if path.startswith("/images/"):
            image_name = path.removeprefix("/images/")
            if image_name in IMAGE_FILES:
                self._send_file(ROOT / "images" / image_name)
                return
            self._send_json(404, {"error": "이미지를 찾을 수 없습니다."})
            return
        if path.startswith("/vendor/"):
            vendor_name = path.removeprefix("/vendor/")
            if vendor_name in VENDOR_FILES:
                self._send_file(ROOT / "vendor" / vendor_name)
                return
            self._send_json(404, {"error": "라이브러리 파일을 찾을 수 없습니다."})
            return
        if path == "/api/dashboard":
            if not self._require_role("admin"):
                return
            query = parse_qs(urlparse(self.path).query)

            def first(name: str) -> str:
                return (query.get(name) or [""])[0].strip()

            conditions: list[str] = []
            parameters: list[object] = []
            domain_filter = first("domain")
            if domain_filter and domain_filter not in verification_policy.DOMAINS:
                self._send_json(400, {"error": "domain 필터는 demo 또는 operational만 허용됩니다."})
                return
            if domain_filter:
                conditions.append("domain = ?")
                parameters.append(domain_filter)
            for column in ("outcome", "auth_path", "actor"):
                value = first(column)
                if value and not re.fullmatch(r"[A-Za-z0-9_\-]{1,40}", value):
                    self._send_json(400, {"error": f"{column} 필터 형식이 올바르지 않습니다."})
                    return
                if value:
                    conditions.append(f"{column} = ?")
                    parameters.append(value)
            if first("event"):
                conditions.append("event LIKE ? ESCAPE '\\'")
                escaped = first("event").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                parameters.append(f"%{escaped}%")
            for name, operator in (("since", ">="), ("until", "<=")):
                if not first(name):
                    continue
                try:
                    moment = datetime.fromisoformat(first(name))
                except ValueError:
                    self._send_json(400, {"error": f"{name} 필터는 ISO 8601 형식이어야 합니다."})
                    return
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
                conditions.append(f"occurred_at {operator} ?")
                parameters.append(moment.astimezone(timezone.utc).isoformat(timespec="seconds"))
            try:
                limit = int(first("limit") or 100)
            except ValueError:
                self._send_json(400, {"error": "limit은 정수여야 합니다."})
                return
            if not 1 <= limit <= 500:
                self._send_json(400, {"error": "limit은 1~500 사이여야 합니다."})
                return
            where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            with connect_db() as connection:
                admitted_count = connection.execute(
                    "SELECT COUNT(*) FROM admissions WHERE exam_id = ?", (EXAM_ID,)
                ).fetchone()[0]
                demo_admitted_count = connection.execute(
                    "SELECT COUNT(*) FROM demo_admissions WHERE exam_id = ?", (EXAM_ID,)
                ).fetchone()[0]
                blocked_count = connection.execute(
                    "SELECT COUNT(*) FROM events WHERE outcome IN ('blocked', 'rejected')"
                ).fetchone()[0]
                events = connection.execute(
                    f"""
                    SELECT occurred_at, event, outcome, exam_id, candidate_id, actor,
                           details, domain, auth_path
                    FROM events {where} ORDER BY event_id DESC LIMIT ?
                    """,
                    (*parameters, limit),
                ).fetchall()
            masked_events = []
            for row in events:
                item = dict(row)
                candidate = item.get("candidate_id")
                # Administrators see only the tail of the candidate identifier.
                item["candidate_id"] = f"***{candidate[-3:]}" if candidate else candidate
                masked_events.append(item)
            self._send_json(
                200,
                {
                    "candidate_count": len(ROSTER),
                    "admitted_count": admitted_count,
                    "demo_admitted_count": demo_admitted_count,
                    "blocked_count": blocked_count,
                    "events": masked_events,
                    "filters_applied": {"domain": domain_filter, "limit": limit},
                },
            )
            return
        if path == "/api/health":
            self._send_json(200, {"ok": True})
            return
        if path == "/api/session":
            session_role = self._current_session_role()
            self._send_json(
                200,
                {
                    "logged_in": session_role is not None,
                    "role": session_role or "guest",
                    "candidate_id": DEMO_ACCOUNTS.get(session_role, {}).get("candidate_id"),
                },
            )
            return
        if path == "/api/kiosk/requests":
            if not self._require_role("proctor"):
                return
            self._list_kiosk_requests()
            return
        if path == "/api/demo/scenarios":
            if self._require_role("proctor"):
                self._send_json(200, {"demo_only": True, "banner": demo_scenarios.BANNER,
                                      "scenarios": self.server.scenario_sandbox.list_scenarios()})
            return
        if path == "/api/demo/scenario/events":
            if self._require_role("proctor"):
                self._send_json(200, {"demo_only": True, "domain": "demo_scenario",
                                      "events": self.server.scenario_sandbox.events()})
            return
        if path == "/api/proctor/overview":
            if self._require_role("proctor"):
                self._proctor_overview()
            return
        self._send_json(404, {"error": "요청한 경로를 찾을 수 없습니다."})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/login":
            self._login()
            return
        if path == "/api/token":
            self._issue_token()
            return
        if path == "/api/mobile-id/authenticate":
            self._authenticate_mobile_id()
            return
        if path == "/api/mobile-id/official-authenticate":
            self._authenticate_official_mobile_id()
            return
        if path == "/api/demo/token":
            self._issue_demo_token()
            return
        if path == "/api/demo/token/inspect":
            if self._require_role("proctor"):
                self._inspect_demo_token()
            return
        if path == "/api/demo/approve":
            if self._require_role("proctor"):
                self._approve_demo_token()
            return
        if path == "/api/token/inspect":
            if self._require_role("proctor"):
                self._inspect_token()
            return
        if path == "/api/approve":
            if self._require_role("proctor"):
                self._approve()
            return
        if path == "/api/physical-id/decision":
            if self._require_role("proctor"):
                self._decide_physical_id()
            return
        if path == "/api/exception/decision":
            if self._require_role("supervisor"):
                self._decide_exception()
            return
        if path == "/api/hold":
            if self._require_role("proctor"):
                self._hold()
            return
        if path in {"/api/demo/scenario/start", "/api/demo/scenario/act", "/api/demo/scenario/reset"}:
            if self._require_role("proctor"):
                self._scenario_action(path.rsplit("/", 1)[1])
            return
        if path == "/api/demo/reset":
            if self._require_role("proctor"):
                self._reset_demo()
            return
        if path == "/api/kiosk/session":
            self._create_kiosk_session()
            return
        if path == "/api/kiosk/candidate":
            self._lookup_kiosk_candidate()
            return
        if path == "/api/kiosk/self-auth":
            if self._require_role("admin"):
                self._authenticate_kiosk_self_demo()
            return
        if path == "/api/kiosk/self-demo/complete":
            self._authenticate_kiosk_self_demo(admin_test=False)
            return
        if path == "/api/kiosk/self-demo/issue-ticket":
            self._issue_kiosk_self_demo_ticket()
            return
        if path == "/api/kiosk/self-demo/request-review":
            self._kiosk_self_demo_request_review()
            return
        if path == "/api/kiosk/demo-auto-issue":
            self._kiosk_demo_auto_issue()
            return
        if path == "/api/kiosk/self-demo/verify-demo-info":
            self._kiosk_verify_demo_info()
            return
        if path == "/api/kiosk/self-demo/refresh-qr":
            self._refresh_kiosk_self_demo_qr()
            return
        if path == "/api/kiosk/demo-entry":
            self._approve_kiosk_demo_entry()
            return
        if path == "/api/kiosk/status":
            self._kiosk_status()
            return
        if path == "/api/kiosk/demo-qr":
            self._kiosk_demo_qr()
            return
        if path == "/api/kiosk/help":
            self._kiosk_help()
            return
        if path == "/api/kiosk/end":
            self._end_kiosk_session()
            return
        if path == "/api/kiosk/physical-review":
            if self._require_role("proctor"):
                self._review_kiosk_candidate()
            return
        if path == "/api/kiosk/refresh-qr":
            if self._require_role("proctor"):
                self._refresh_kiosk_qr()
            return
        self._send_json(404, {"error": "요청한 경로를 찾을 수 없습니다."})

    def _kiosk_session(self, session_id: object) -> dict[str, object] | None:
        if not isinstance(session_id, str) or not session_id:
            return None
        with self.server.kiosk_lock:
            session = self.server.kiosk_sessions.get(session_id)
            if not session:
                return None
            now = time.time()
            if now - session["last_activity"] > KIOSK_SESSION_TTL_SECONDS:
                self.server.kiosk_sessions.pop(session_id, None)
                return None
            session["last_activity"] = now
            return session

    def _create_kiosk_session(self) -> None:
        try:
            self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        self._purge_expired_kiosk_sessions()
        session_id = secrets.token_urlsafe(32)
        now = time.time()
        with self.server.kiosk_lock:
            self.server.kiosk_sessions[session_id] = {
                "created_at": now,
                "last_activity": now,
                "mode": None,
                "state": "start",
                "exam_id": None,
                "candidate_id": None,
                "token_hash": None,
                "qr_png": None,
                "token_issued_at": None,
                "token_expires_at": None,
                "identity_approved_until": None,
                "demo_token_hash": None,
                "demo_token": None,
                "demo_qr_png": None,
                "demo_expires_at": None,
                "demo_issued_at": None,
                "demo_delivery": None,
            }
        self._send_json(201, {"kiosk_session_id": session_id, "expires_in": KIOSK_SESSION_TTL_SECONDS})

    def _lookup_kiosk_candidate(self) -> None:
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        self._purge_expired_kiosk_sessions()
        session_id = payload.get("kiosk_session_id")
        exam_id = payload.get("exam_id")
        candidate_number = payload.get("candidate_number")
        mode = payload.get("mode", "proctor")
        if (
            not isinstance(session_id, str)
            or not isinstance(exam_id, str)
            or not isinstance(candidate_number, str)
            or not candidate_number.replace("-", "").isdigit()
            or not isinstance(mode, str)
            or mode not in {"proctor", "self_demo"}
        ):
            self._send_json(400, {"error": "키오스크 세션, 모드, 가상 수험번호를 확인해 주세요."})
            return
        with self.server.kiosk_lock:
            session = self.server.kiosk_sessions.get(session_id)
            if (
                not session
                or time.time() - session["last_activity"] > KIOSK_SESSION_TTL_SECONDS
            ):
                self.server.kiosk_sessions.pop(session_id, None)
                self._send_json(410, {"error": "키오스크 세션이 만료되었습니다. 처음 화면으로 돌아가세요."})
                return
            session["last_activity"] = time.time()
            if session["state"] != "start":
                self._send_json(409, {"error": "현재 키오스크 세션은 이미 사용 중입니다."})
                return
            candidate = next(
                (
                    (roster_candidate_id, roster_candidate)
                    for (roster_exam_id, roster_candidate_id), roster_candidate in ROSTER.items()
                    if roster_exam_id == exam_id
                    and roster_candidate["number"].replace("-", "") == candidate_number.replace("-", "")
                ),
                None,
            )
            if candidate is None:
                session["state"] = "not_found"
                with connect_db() as connection:
                    write_event(
                        connection,
                        "키오스크 응시자 조회",
                        "blocked",
                        exam_id=exam_id,
                        actor="kiosk",
                        details="가상 명부에서 수험번호를 찾을 수 없음",
                    )
                self._send_json(404, {"error": "가상 응시자 정보를 찾을 수 없습니다. 수험번호를 확인하거나 현장 직원을 불러 주세요."})
                return
            candidate_id, candidate_info = candidate
            with connect_db() as connection:
                admitted = connection.execute(
                    "SELECT 1 FROM admissions WHERE exam_id = ? AND candidate_id = ?",
                    (exam_id, candidate_id),
                ).fetchone()
                demo_admitted = connection.execute(
                    "SELECT 1 FROM demo_admissions WHERE exam_id = ? AND candidate_id = ?",
                    (exam_id, candidate_id),
                ).fetchone()
            if admitted or demo_admitted:
                session["state"] = "duplicate"
                self._send_json(409, {"error": "이미 입실 처리된 응시자입니다. 현장 직원에게 문의하세요."})
                return
            session["candidate_id"] = candidate_id
            session["demo_info_verified"] = False
            session["exam_id"] = exam_id
            session["mode"] = mode
            session["state"] = "awaiting_self_auth" if mode == "self_demo" else "awaiting_proctor"
            with connect_db() as connection:
                write_event(
                    connection,
                    "키오스크 응시자 조회",
                    "accepted",
                    exam_id=exam_id,
                    candidate_id=candidate_id,
                    actor="kiosk",
                    details=f"authentication_path={mode}; 수험번호는 명부 조회만 수행",
                )
        self._send_json(
            200,
            {
                "exam_id": exam_id,
                "candidate_id": candidate_id,
                "candidate_name": candidate_info["name"],
                "candidate_number": candidate_info["number"],
                "demo_exam_options": [{"id": key, "name": name} for key, name in DEMO_EXAMS.items()],
                "demo_default_exam_id": DEMO_DEFAULT_EXAM_ID,
                "registered_photo_url": (
                    "/images/2026-0012-registration.jpg"
                    if candidate_id == "candidate-001"
                    else "/images/candidate2.png"
                ),
                "status": "awaiting_self_auth" if mode == "self_demo" else "awaiting_proctor",
                "authentication_mode": mode,
                "message": (
                    "가상 응시자 조회 완료 · DEMO 모의인증과 카메라 미리보기는 실제 신원확인이 아닙니다."
                    if mode == "self_demo"
                    else "수험번호는 조회에만 사용합니다. 감독관 대면 확인이 완료되어야 인증됩니다."
                ),
            },
        )

    def _authenticate_kiosk_self_demo(self, admin_test: bool = True) -> None:
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        face_assertion_fields = {
            "face_score",
            "face_similarity",
            "face_match",
            "captured_face_result",
            "face_comparison_result",
            "face_verified",
            "authentication_success",
            "real_authentication",
        }
        if face_assertion_fields.intersection(payload):
            self._send_json(
                400,
                {
                    "error": (
                        "브라우저 얼굴 유사도 결과는 서버 인증 근거로 사용할 수 없습니다. "
                        "별도의 DEMO 모의 성공 선택만 허용됩니다."
                    ),
                    "demo_only": True,
                    "real_authentication": False,
                },
            )
            return
        session_id = payload.get("kiosk_session_id")
        outcome = payload.get("demo_outcome") if admin_test else "success"
        invalid_demo_input = (
            not isinstance(outcome, str)
            or outcome not in {"success", "failure", "uncertain", "mismatch"}
            or payload.get("demo_simulation") is not True
        ) if admin_test else (
            payload.get("demo_outcome") != "success"
            or payload.get("demo_simulation") is not True
        )
        if not isinstance(session_id, str) or invalid_demo_input:
            message = (
                "명시적인 DEMO 모의 결과를 선택해 주세요."
                if admin_test
                else "DEMO 모의 성공을 명시적으로 선택해야 합니다."
            )
            self._send_json(400, {"error": message})
            return
        session = self._kiosk_session(session_id)
        if not session:
            self._send_json(410, {"error": "키오스크 세션이 만료되었습니다."})
            return

        with self.server.kiosk_lock:
            if session.get("mode") != "self_demo" or session["state"] != "awaiting_self_auth":
                self._send_json(409, {"error": "셀프 DEMO 인증 대기 상태가 아닙니다."})
                return
            candidate_id = session["candidate_id"]
            exam_id = session["exam_id"]
            if not admin_test:
                now = time.time()
                with connect_db() as connection:
                    if connection.execute(
                        "SELECT 1 FROM admissions WHERE exam_id = ? AND candidate_id = ?",
                        (exam_id, candidate_id),
                    ).fetchone() or connection.execute(
                        "SELECT 1 FROM demo_admissions WHERE exam_id = ? AND candidate_id = ?",
                        (exam_id, candidate_id),
                    ).fetchone():
                        session["state"] = "duplicate"
                        self._send_json(409, {"error": "이미 입실 처리된 응시자입니다."})
                        return
                    session.update(
                        {
                            "state": "demo_authenticated",
                            "demo_auth_expires_at": now
                            + max(AUTH_PROOF_TTL_SECONDS, KIOSK_PRINT_QR_TTL_SECONDS),
                            "demo_delivery": None,
                        }
                    )
                    write_event(
                        connection,
                        "키오스크 셀프 DEMO 모의 인증",
                        "accepted",
                        exam_id=exam_id,
                        candidate_id=candidate_id,
                        actor="kiosk",
                        details="authentication_path=self_demo; explicit demo_outcome=success; browser face comparison is informational only; liveness unavailable; no real authentication",
                    )
                self._send_json(
                    200,
                    {
                        "state": "demo_authenticated",
                        "demo_only": True,
                        "real_authentication": False,
                        "message": "DEMO 모의 결과가 선택되었습니다. 실제 신원인증이 아닙니다.",
                    },
                )
                return
            result = MockSelfIdentityProvider(outcome).verify(exam_id, candidate_id)
            now = time.time()
            with connect_db() as connection:
                if outcome != "success":
                    session["mode"] = "proctor_fallback"
                    session["state"] = "awaiting_proctor"
                    event_outcome = "blocked" if outcome == "failure" else "pending"
                    write_event(
                        connection,
                        "키오스크 셀프 DEMO 신원확인",
                        event_outcome,
                        exam_id=exam_id,
                        candidate_id=candidate_id,
                        actor="admin" if admin_test else "kiosk",
                        details=f"authentication_path=self_demo; provider={result.provider}; demo_outcome={outcome}; admin_test={str(admin_test).lower()}; face comparison is informational only; liveness unavailable; supervisor fallback",
                    )
                    self._send_json(
                        200,
                        {
                            "state": "awaiting_proctor",
                            "authentication_mode": "proctor_fallback",
                            "message": f"{result.message} 감독관 확인으로 전환합니다.",
                            "demo_only": True,
                            "real_authentication": False,
                        },
                    )
                    return

                if connection.execute(
                    "SELECT 1 FROM admissions WHERE exam_id = ? AND candidate_id = ?",
                    (exam_id, candidate_id),
                ).fetchone() or connection.execute(
                    "SELECT 1 FROM demo_admissions WHERE exam_id = ? AND candidate_id = ?",
                    (exam_id, candidate_id),
                ).fetchone():
                    session["state"] = "duplicate"
                    write_event(
                        connection,
                        "키오스크 셀프 DEMO QR 발급",
                        "blocked",
                        exam_id=exam_id,
                        candidate_id=candidate_id,
                        actor="kiosk",
                        details="authentication_path=self_demo; duplicate admission",
                    )
                    self._send_json(409, {"error": "이미 입실 처리된 응시자입니다."})
                    return

                demo_token = f"DEMO-ONLY:SELF:{secrets.token_urlsafe(32)}"
                token_hash = token_digest(demo_token)
                expires_at = now + TOKEN_TTL_SECONDS
                qr_png = self._generate_qr_png(demo_token)
                connection.execute(
                    """
                    INSERT INTO demo_tokens
                        (token_hash, exam_id, candidate_id, pathway, issued_at, expires_at)
                    VALUES (?, ?, ?, 'self_demo', ?, ?)
                    """,
                    (token_hash, exam_id, candidate_id, now, expires_at),
                )
                session.update(
                    {
                        "state": "demo_qr_ready",
                        "demo_token_hash": token_hash,
                        "demo_token": demo_token,
                        "demo_qr_png": qr_png,
                        "demo_issued_at": now,
                        "demo_delivery": "screen",
                        "demo_expires_at": expires_at,
                        "demo_auth_expires_at": now + AUTH_PROOF_TTL_SECONDS,
                    }
                )
                write_event(
                    connection,
                    "키오스크 셀프 DEMO QR 발급",
                    "accepted",
                    exam_id=exam_id,
                    candidate_id=candidate_id,
                    actor="admin" if admin_test else "kiosk",
                    details=f"authentication_path=self_demo; explicit demo outcome; admin_test={str(admin_test).lower()}; face comparison informational only; liveness unavailable; 30-second QR; not eligible for real admission",
                )
        self._send_json(
            200,
            {
                "state": "demo_qr_ready",
                "authentication_mode": "self_demo",
                "message": "DEMO 모의 결과로 QR을 표시합니다. 실제 신원확인이나 입실 승인이 아닙니다.",
                "demo_only": True,
                "real_authentication": False,
            },
        )

    def _issue_kiosk_self_demo_ticket(self) -> None:
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        session = self._kiosk_session(payload.get("kiosk_session_id"))
        delivery = payload.get("delivery")
        if not session:
            self._send_json(410, {"error": "키오스크 세션이 만료되었습니다."})
            return
        if delivery not in {"screen", "print"}:
            self._send_json(400, {"error": "QR 표시 또는 출력 방식을 선택해 주세요."})
            return
        with self.server.kiosk_lock:
            if (
                session.get("mode") != "self_demo"
                or session["state"] != "demo_authenticated"
                or session.get("demo_auth_expires_at", 0) <= time.time()
            ):
                self._send_json(409, {"error": "유효한 DEMO 모의 인증이 없어 QR을 발급할 수 없습니다."})
                return
            now = time.time()
            candidate_id = session["candidate_id"]
            exam_id = session["exam_id"]
            with connect_db() as connection:
                if connection.execute(
                    "SELECT 1 FROM admissions WHERE exam_id = ? AND candidate_id = ?",
                    (exam_id, candidate_id),
                ).fetchone() or connection.execute(
                    "SELECT 1 FROM demo_admissions WHERE exam_id = ? AND candidate_id = ?",
                    (exam_id, candidate_id),
                ).fetchone():
                    session["state"] = "duplicate"
                    write_event(
                        connection,
                        "키오스크 셀프 DEMO QR 발급",
                        "blocked",
                        exam_id=exam_id,
                        candidate_id=candidate_id,
                        actor="kiosk",
                        details="authentication_path=self_demo; duplicate admission",
                    )
                    self._send_json(409, {"error": "이미 입실 처리된 응시자입니다."})
                    return
                demo_token = f"DEMO-ONLY:SELF:{secrets.token_urlsafe(32)}"
                token_hash = token_digest(demo_token)
                ttl = KIOSK_PRINT_QR_TTL_SECONDS if delivery == "print" else TOKEN_TTL_SECONDS
                expires_at = now + ttl
                qr_png = self._generate_qr_png(demo_token)
                connection.execute(
                    """
                    INSERT INTO demo_tokens
                        (token_hash, exam_id, candidate_id, pathway, issued_at, expires_at)
                    VALUES (?, ?, ?, 'self_demo', ?, ?)
                    """,
                    (token_hash, exam_id, candidate_id, now, expires_at),
                )
                session.update(
                    {
                        "state": "demo_qr_ready",
                        "demo_token_hash": token_hash,
                        "demo_token": demo_token,
                        "demo_qr_png": qr_png,
                        "demo_issued_at": now,
                        "demo_delivery": delivery,
                        "demo_expires_at": expires_at,
                        "demo_auth_expires_at": now + ttl + AUTH_PROOF_TTL_SECONDS,
                    }
                )
                write_event(
                    connection,
                    "키오스크 셀프 DEMO QR 입실권 발급",
                    "accepted",
                    exam_id=exam_id,
                    candidate_id=candidate_id,
                    actor="kiosk",
                    details=f"authentication_path=self_demo; delivery={delivery}; ttl={ttl}; one-time DEMO token; not eligible for real admission",
                )
        self._send_json(
            200,
            {
                "state": "demo_qr_ready",
                "qr_png": qr_png,
                "qr_mime_type": "image/png",
                "expires_in": ttl,
                "demo_only": True,
                "demo_token": demo_token,
                "delivery": delivery,
                "exam_name": "2026 모의 자격시험",
                "candidate_name": ROSTER[(exam_id, candidate_id)]["name"],
                "candidate_number": ROSTER[(exam_id, candidate_id)]["number"],
                "issued_at": datetime.fromtimestamp(now, timezone.utc).astimezone().isoformat(timespec="seconds"),
                "expires_at": datetime.fromtimestamp(expires_at, timezone.utc).astimezone().isoformat(timespec="seconds"),
            },
        )

    def _approve_kiosk_demo_entry(self) -> None:
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        session_id = payload.get("kiosk_session_id")
        demo_token = payload.get("demo_token")
        if (session_id is not None and not isinstance(session_id, str)) or not isinstance(demo_token, str):
            self._send_json(400, {"error": "DEMO QR 토큰 형식을 확인해 주세요."})
            return
        session = self._kiosk_session(session_id) if isinstance(session_id, str) else None
        token_hash = token_digest(demo_token)

        with self.server.kiosk_lock:
            if not (
                demo_token.startswith("DEMO-ONLY:SELF:")
                or demo_token.startswith("DEMO-ONLY:KIOSK-PROCTOR:")
            ) or (
                session is not None and (
                session.get("mode") not in {"self_demo", "proctor", "proctor_fallback"}
                or session["state"] not in {"demo_qr_ready", "qr_ready"}
                or not session.get("demo_token_hash")
                or not hmac.compare_digest(session["demo_token_hash"], token_hash)
                )
            ):
                self._send_json(409, {"error": "유효한 셀프 DEMO QR이 아닙니다.", "demo_admitted": False})
                return

            now = time.time()
            with connect_db() as connection:
                connection.execute("BEGIN IMMEDIATE")
                demo_row = connection.execute(
                    "SELECT * FROM demo_tokens WHERE token_hash = ?",
                    (token_hash,),
                ).fetchone()
                expected_pathway = (
                    "self_demo"
                    if demo_token.startswith("DEMO-ONLY:SELF:")
                    else "kiosk_proctor_demo"
                )
                if (
                    demo_row is None
                    or demo_row["pathway"] != expected_pathway
                    or (session is None and expected_pathway != "self_demo")
                    or (
                        session is not None
                        and expected_pathway == "kiosk_proctor_demo"
                        and session.get("mode") not in {"proctor", "proctor_fallback"}
                    )
                ):
                    self._send_json(409, {"error": "DEMO QR 기록을 찾을 수 없습니다.", "demo_admitted": False})
                    return
                if session is not None and (demo_row["exam_id"], demo_row["candidate_id"]) != (
                    session["exam_id"], session["candidate_id"],
                ):
                    self._send_json(409, {"error": "DEMO QR의 시험 또는 응시자 정보가 일치하지 않습니다.", "demo_admitted": False})
                    return
                if demo_row["expires_at"] <= now:
                    connection.execute(
                        "UPDATE demo_tokens SET used_at = COALESCE(used_at, ?) WHERE token_hash = ?",
                        (now, token_hash),
                    )
                    if session is not None:
                        session["state"] = "expired"
                        session["demo_token"] = None
                        session["demo_qr_png"] = None
                    write_event(
                        connection,
                        "키오스크 DEMO 모의 입실",
                        "blocked",
                        exam_id=demo_row["exam_id"],
                        candidate_id=demo_row["candidate_id"],
                        actor="kiosk_demo",
                        details="expired self_demo QR",
                    )
                    self._send_json(409, {"error": "DEMO QR이 만료되었습니다.", "demo_admitted": False})
                    return
                if demo_row["used_at"] is not None:
                    if session is not None:
                        session["state"] = "demo_completed"
                        session["demo_token"] = None
                        session["demo_qr_png"] = None
                    self._send_json(409, {"error": "이미 사용된 DEMO QR입니다.", "demo_admitted": False})
                    return
                if connection.execute(
                    "SELECT 1 FROM admissions WHERE exam_id = ? AND candidate_id = ?",
                    (demo_row["exam_id"], demo_row["candidate_id"]),
                ).fetchone() or connection.execute(
                    "SELECT 1 FROM demo_admissions WHERE exam_id = ? AND candidate_id = ?",
                    (demo_row["exam_id"], demo_row["candidate_id"]),
                ).fetchone():
                    connection.execute(
                        "UPDATE demo_tokens SET used_at = COALESCE(used_at, ?) WHERE token_hash = ?",
                        (now, token_hash),
                    )
                    if session is not None:
                        session["state"] = "duplicate"
                        session["demo_token"] = None
                        session["demo_qr_png"] = None
                    write_event(
                        connection,
                        "키오스크 DEMO 모의 입실",
                        "blocked",
                        exam_id=demo_row["exam_id"],
                        candidate_id=demo_row["candidate_id"],
                        actor="kiosk_demo",
                        details="duplicate actual or simulated admission",
                    )
                    self._send_json(409, {"error": "이미 입실 처리된 응시자입니다.", "demo_admitted": False})
                    return

                connection.execute(
                    "UPDATE demo_tokens SET used_at = ? WHERE token_hash = ? AND used_at IS NULL",
                    (now, token_hash),
                )
                connection.execute(
                    """
                    INSERT INTO demo_admissions (exam_id, candidate_id, admitted_at)
                    VALUES (?, ?, ?)
                    """,
                    (demo_row["exam_id"], demo_row["candidate_id"], utc_now()),
                )
                if session is not None:
                    session["state"] = "demo_completed"
                    session["demo_token"] = None
                    session["demo_qr_png"] = None
                write_event(
                    connection,
                    "키오스크 DEMO 모의 입실",
                    "demo_admitted",
                    exam_id=demo_row["exam_id"],
                    candidate_id=demo_row["candidate_id"],
                    actor="kiosk_demo",
                    details="simulated only; no real admissions row created",
                )
        self._send_json(
            200,
            {
                "state": "demo_completed",
                "demo_admitted": True,
                "real_admission": False,
                "message": "DEMO 모의 입실 시연을 완료했습니다. 실제 입실 기록은 생성되지 않았습니다.",
            },
        )

    def _refresh_kiosk_self_demo_qr(self) -> None:
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        session = self._kiosk_session(payload.get("kiosk_session_id"))
        if not session:
            self._send_json(410, {"error": "키오스크 세션이 만료되었습니다."})
            return

        with self.server.kiosk_lock:
            if (
                session.get("mode") != "self_demo"
                or session["state"] not in {"demo_qr_ready", "expired"}
                or not session.get("demo_token_hash")
                or session.get("demo_auth_expires_at", 0) <= time.time()
            ):
                self._send_json(409, {"error": "셀프 DEMO 인증이 없거나 유효기간이 끝나 재발급할 수 없습니다."})
                return

            now = time.time()
            old_token_hash = session["demo_token_hash"]
            with connect_db() as connection:
                connection.execute("BEGIN IMMEDIATE")
                old_token = connection.execute(
                    "SELECT * FROM demo_tokens WHERE token_hash = ?",
                    (old_token_hash,),
                ).fetchone()
                if (
                    old_token is None
                    or old_token["pathway"] != "self_demo"
                    or (old_token["exam_id"], old_token["candidate_id"])
                    != (session["exam_id"], session["candidate_id"])
                    or (
                        old_token["used_at"] is not None
                        and old_token["expires_at"] > now
                    )
                ):
                    self._send_json(409, {"error": "셀프 DEMO QR이 이미 사용되었거나 재발급할 수 없습니다."})
                    return

                if connection.execute(
                    "SELECT 1 FROM admissions WHERE exam_id = ? AND candidate_id = ?",
                    (old_token["exam_id"], old_token["candidate_id"]),
                ).fetchone() or connection.execute(
                    "SELECT 1 FROM demo_admissions WHERE exam_id = ? AND candidate_id = ?",
                    (old_token["exam_id"], old_token["candidate_id"]),
                ).fetchone():
                    self._send_json(409, {"error": "이미 입실 처리된 응시자입니다."})
                    return

                connection.execute(
                    "UPDATE demo_tokens SET used_at = ? WHERE token_hash = ? AND used_at IS NULL",
                    (now, old_token_hash),
                )
                demo_token = f"DEMO-ONLY:SELF:{secrets.token_urlsafe(32)}"
                token_hash = token_digest(demo_token)
                ttl = (
                    KIOSK_PRINT_QR_TTL_SECONDS
                    if session.get("demo_delivery") == "print"
                    else TOKEN_TTL_SECONDS
                )
                expires_at = now + ttl
                qr_png = self._generate_qr_png(demo_token)
                connection.execute(
                    """
                    INSERT INTO demo_tokens
                        (token_hash, exam_id, candidate_id, pathway, issued_at, expires_at)
                    VALUES (?, ?, ?, 'self_demo', ?, ?)
                    """,
                    (token_hash, session["exam_id"], session["candidate_id"], now, expires_at),
                )
                session.update(
                    {
                        "demo_token_hash": token_hash,
                        "demo_token": demo_token,
                        "demo_qr_png": qr_png,
                        "demo_issued_at": now,
                        "demo_expires_at": expires_at,
                    }
                )
                write_event(
                    connection,
                    "키오스크 셀프 DEMO QR 재발급",
                    "accepted",
                    exam_id=session["exam_id"],
                    candidate_id=session["candidate_id"],
                    actor="kiosk",
                    details=f"authentication_path=self_demo; prior QR invalidated; delivery={session.get('demo_delivery')}; ttl={ttl}; DEMO-only QR",
                )

        self._send_json(
            200,
            {
                "state": "demo_qr_ready",
                "qr_png": qr_png,
                "qr_mime_type": "image/png",
                "expires_in": max(0, math.ceil(expires_at - time.time())),
                "demo_only": True,
                "demo_token": demo_token,
                "delivery": session.get("demo_delivery"),
                "exam_name": "2026 모의 자격시험",
                "candidate_name": ROSTER[(session["exam_id"], session["candidate_id"])]["name"],
                "candidate_number": ROSTER[(session["exam_id"], session["candidate_id"])]["number"],
                "issued_at": datetime.fromtimestamp(now, timezone.utc).astimezone().isoformat(timespec="seconds"),
                "expires_at": datetime.fromtimestamp(expires_at, timezone.utc).astimezone().isoformat(timespec="seconds"),
            },
        )

    def _purge_expired_kiosk_sessions(self) -> None:
        now = time.time()
        with self.server.kiosk_lock:
            expired = [
                session_id
                for session_id, session in self.server.kiosk_sessions.items()
                if now - session["last_activity"] > KIOSK_SESSION_TTL_SECONDS
            ]
            sessions = [
                self.server.kiosk_sessions.pop(session_id)
                for session_id in expired
            ]
        token_hashes = [
            session["token_hash"]
            for session in sessions
            if session.get("token_hash")
        ]
        demo_token_hashes = [
            session["demo_token_hash"]
            for session in sessions
            if session.get("demo_token_hash")
            and not (
                session.get("demo_delivery") == "print"
                and session.get("demo_expires_at", 0) > now
            )
        ]
        if token_hashes or demo_token_hashes:
            with connect_db() as connection:
                if token_hashes:
                    connection.executemany(
                        "UPDATE tokens SET used_at = COALESCE(used_at, ?) WHERE token_hash = ?",
                        [(now, token_hash) for token_hash in token_hashes],
                    )
                if demo_token_hashes:
                    connection.executemany(
                        "UPDATE demo_tokens SET used_at = COALESCE(used_at, ?) WHERE token_hash = ?",
                        [(now, token_hash) for token_hash in demo_token_hashes],
                    )

    def _list_kiosk_requests(self) -> None:
        self._purge_expired_kiosk_sessions()
        now = time.time()
        with self.server.kiosk_lock:
            requests = []
            for session_id, session in self.server.kiosk_sessions.items():
                if (
                    session.get("identity_approved_until")
                    and session["identity_approved_until"] <= now
                ):
                    session["state"] = "verification_expired"
                    session["qr_png"] = None
                    if session.get("token_hash"):
                        with connect_db() as connection:
                            connection.execute(
                                "UPDATE tokens SET used_at = COALESCE(used_at, ?) WHERE token_hash = ?",
                                (now, session["token_hash"]),
                            )
                    if session.get("demo_token_hash"):
                        with connect_db() as connection:
                            connection.execute(
                                "UPDATE demo_tokens SET used_at = COALESCE(used_at, ?) WHERE token_hash = ?",
                                (now, session["demo_token_hash"]),
                            )
                if session["state"] not in {
                    "awaiting_proctor",
                    "qr_ready",
                    "demo_qr_ready",
                    "expired",
                }:
                    continue
                if session["state"] != "awaiting_proctor" and (
                    not session.get("identity_approved_until")
                    or session["identity_approved_until"] <= now
                ):
                    continue
                if session["state"] == "qr_ready":
                    with connect_db() as connection:
                        token = connection.execute(
                            "SELECT expires_at, used_at FROM tokens WHERE token_hash = ?",
                            (session["token_hash"],),
                        ).fetchone()
                    if token is None or token["used_at"] is not None:
                        session["state"] = "completed"
                        session["qr_png"] = None
                    elif token["expires_at"] <= now:
                        session["state"] = "expired"
                        session["qr_png"] = None
                        with connect_db() as connection:
                            connection.execute(
                                "UPDATE tokens SET used_at = COALESCE(used_at, ?) WHERE token_hash = ?",
                                (now, session["token_hash"]),
                            )
                elif session["state"] == "demo_qr_ready":
                    with connect_db() as connection:
                        token = connection.execute(
                            "SELECT expires_at, used_at FROM demo_tokens WHERE token_hash = ?",
                            (session["demo_token_hash"],),
                        ).fetchone()
                    if token is None or token["used_at"] is not None:
                        session["state"] = "demo_completed"
                        session["demo_qr_png"] = None
                        session["demo_token"] = None
                    elif token["expires_at"] <= now:
                        session["state"] = "expired"
                        session["demo_qr_png"] = None
                        session["demo_token"] = None
                        with connect_db() as connection:
                            connection.execute(
                                "UPDATE demo_tokens SET used_at = COALESCE(used_at, ?) WHERE token_hash = ?",
                                (now, session["demo_token_hash"]),
                            )
                candidate_id = session["candidate_id"]
                exam_id, candidate = next(
                    entry for entry in ROSTER.items() if entry[0][1] == candidate_id
                )
                request = {
                        "kiosk_session_id": session_id,
                        "exam_id": exam_id[0],
                        "candidate_id": candidate_id,
                        "candidate_name": candidate["name"],
                        "candidate_number": candidate["number"],
                        "authentication_mode": session.get("mode") or "proctor",
                        "photo_url": "/images/2026-0012-registration.jpg" if candidate_id == "candidate-001" else "/images/candidate2.png",
                        "expires_in": max(
                            0,
                            int(KIOSK_SESSION_TTL_SECONDS - (now - session["last_activity"])),
                        ),
                        "state": session["state"],
                    }
                if session["state"] in {"qr_ready", "demo_qr_ready", "expired"}:
                    request["verification_expires_in"] = max(
                        0, int(session["identity_approved_until"] - now)
                    )
                    expiry = session.get("demo_expires_at") or session.get("token_expires_at") or now
                    request["qr_expires_in"] = max(0, int(expiry - now))
                requests.append(request)
        self._send_json(200, {"requests": requests})

    def _kiosk_self_demo_request_review(self) -> None:
        """Hand a real-camera self DEMO session to the supervisor queue (no score is accepted)."""
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        if KIOSK_BROWSER_ASSERTION_FIELDS.intersection(payload):
            self._send_json(400, {"error": "브라우저 얼굴 분석 결과는 서버에서 사용하지 않습니다.", "demo_only": True, "real_authentication": False})
            return
        self._purge_expired_kiosk_sessions()
        session = self._kiosk_session(payload.get("kiosk_session_id"))
        if not session:
            self._send_json(410, {"error": "키오스크 세션이 만료되었습니다."})
            return
        with self.server.kiosk_lock:
            if session.get("mode") == "self_demo" and session["state"] == "awaiting_self_auth":
                session["mode"] = "proctor_fallback"
                session["state"] = "awaiting_proctor"
            elif session["state"] != "awaiting_proctor":
                self._send_json(409, {"error": "감독관 확인을 요청할 수 있는 상태가 아닙니다."})
                return
            session["last_activity"] = time.time()
            with connect_db() as connection:
                write_event(
                    connection,
                    "키오스크 감독관 확인 요청",
                    "pending",
                    exam_id=session["exam_id"],
                    candidate_id=session["candidate_id"],
                    actor="kiosk",
                    details="authentication_path=self_demo; real camera analysis is informational (inconclusive); supervisor review requested; DEMO",
                )
        self._send_json(
            200,
            {
                "state": "awaiting_proctor",
                "authentication_mode": "proctor_fallback",
                "demo_only": True,
                "real_authentication": False,
                "message": "감독관 확인을 요청했습니다. 감독관이 서버에서 승인하면 DEMO QR이 발급됩니다.",
            },
        )

    @staticmethod
    def _valid_virtual_code(value: object) -> bool:
        if not isinstance(value, str):
            return False
        match = VIRTUAL_CODE_PATTERN.match(value)
        if not match:
            return False
        yy, mm, dd = (int(part) for part in match.groups()[:3])
        for century in (1900, 2000):
            try:
                datetime(century + yy, mm, dd)
                return True
            except ValueError:
                continue
        return False

    def _kiosk_verify_demo_info(self) -> None:
        """Compare virtual DEMO info with server presets. Never trusts or stores browser scores or raw input."""
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        if KIOSK_BROWSER_ASSERTION_FIELDS.intersection(payload):
            self._send_json(400, {"error": "브라우저 얼굴 분석 결과는 서버에서 사용하지 않습니다.", "demo_only": True, "real_authentication": False})
            return
        self._purge_expired_kiosk_sessions()
        session = self._kiosk_session(payload.get("kiosk_session_id"))
        if not session:
            self._send_json(410, {"error": "키오스크 세션이 만료되었습니다."})
            return
        code, exam_ref = payload.get("virtual_code"), payload.get("exam")
        if not isinstance(code, str) or not code.strip():
            self._send_json(400, {"error": "가상 DEMO 생년월일·구분 코드를 입력해 주세요. 예: 980320-2", "field": "virtual_code"})
            return
        if not self._valid_virtual_code(code.strip()):
            self._send_json(400, {"error": "형식이 올바르지 않습니다. YYMMDD-N 형식의 유효한 가상 코드를 입력해 주세요. 예: 980320-2", "field": "virtual_code"})
            return
        if not isinstance(exam_ref, str) or not exam_ref.strip():
            self._send_json(400, {"error": "시험명을 선택하거나 입력해 주세요.", "field": "exam"})
            return
        with self.server.kiosk_lock:
            if session.get("mode") != "self_demo" or session["state"] != "awaiting_self_auth":
                self._send_json(409, {"error": "추가 DEMO 정보를 입력할 수 있는 상태가 아닙니다."})
                return
            attempts = int(session.get("demo_info_attempts", 0))
            if attempts >= DEMO_INFO_MAX_ATTEMPTS:
                self._send_json(429, {"error": "입력 시도 횟수를 초과했습니다. 감독관 확인을 요청해 주세요."})
                return
            profile = DEMO_CANDIDATE_PROFILES.get((session["exam_id"], session["candidate_id"]))
            exam_key = next(
                (key for key, name in DEMO_EXAMS.items() if exam_ref.strip() in (key, name)),
                None,
            )
            matched = bool(
                profile
                and exam_key
                and hmac.compare_digest(code.strip().encode(), profile["virtual_code"].encode())
                and exam_key == profile["exam_id"]
            )
            session["demo_info_attempts"] = attempts + 1
            session["demo_info_verified"] = matched
            with connect_db() as connection:
                write_event(
                    connection,
                    "키오스크 DEMO 추가 정보 확인",
                    "accepted" if matched else "rejected",
                    exam_id=session["exam_id"],
                    candidate_id=session["candidate_id"],
                    actor="kiosk",
                    details="authentication_path=self_demo; virtual DEMO info check only; not identity verification; input values not recorded",
                )
        if not matched:
            self._send_json(
                403,
                {
                    "error": "입력한 DEMO 정보가 사전 설정과 일치하지 않습니다. 다시 입력하거나 감독관 확인을 요청해 주세요.",
                    "attempts_left": max(0, DEMO_INFO_MAX_ATTEMPTS - attempts - 1),
                    "demo_only": True,
                    "real_authentication": False,
                },
            )
            return
        self._send_json(
            200,
            {
                "state": "demo_info_matched",
                "demo_only": True,
                "real_authentication": False,
                "message": "DEMO 정보가 사전 설정과 일치합니다. 이는 본인 인증이 아니며 DEMO 시뮬레이션 QR 발급만 가능합니다.",
            },
        )

    def _kiosk_demo_auto_issue(self) -> None:
        """Preset DEMO simulation: needs a server-verified proctor role, ignores any face result."""
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        if KIOSK_BROWSER_ASSERTION_FIELDS.intersection(payload):
            self._send_json(400, {"error": "자동 발급 시연은 얼굴 분석 결과를 받지 않습니다.", "demo_only": True, "real_authentication": False})
            return
        scenario = payload.get("scenario")
        if payload.get("demo_simulation") is not True or scenario not in ("preset_auto_issue", "demo_info_simulation"):
            self._send_json(400, {"error": "자동 발급 시연 시나리오를 명시해야 합니다."})
            return
        session = self._kiosk_session(payload.get("kiosk_session_id"))
        if not session:
            self._send_json(410, {"error": "키오스크 세션이 만료되었습니다."})
            return
        issue_actor = "proctor"
        if scenario == "demo_info_simulation":
            # Server-side gates: DEMO simulation permission + server-matched virtual DEMO info.
            if not DEMO_SIMULATION_ENABLED:
                self._send_json(403, {"error": "DEMO 시뮬레이션 권한이 서버에서 허용되지 않았습니다.", "demo_only": True})
                return
            if session.get("demo_info_verified") is not True:
                self._send_json(403, {"error": "서버가 확인한 DEMO 추가 정보 일치가 없어 QR을 발급할 수 없습니다.", "demo_only": True})
                return
            issue_actor = "kiosk"
        elif not self._require_role("proctor"):
            return
        with self.server.kiosk_lock:
            if session.get("mode") != "self_demo" or session["state"] != "awaiting_self_auth":
                self._send_json(409, {"error": "자동 발급 시연을 시작할 수 있는 상태가 아닙니다. 이미 발급되었거나 감독관 확인 중입니다."})
                return
            now = time.time()
            candidate_id = session["candidate_id"]
            exam_id = session["exam_id"]
            with connect_db() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if connection.execute(
                    """
                    SELECT 1 FROM admissions WHERE exam_id = ? AND candidate_id = ?
                    UNION ALL
                    SELECT 1 FROM demo_admissions WHERE exam_id = ? AND candidate_id = ?
                    LIMIT 1
                    """,
                    (exam_id, candidate_id, exam_id, candidate_id),
                ).fetchone():
                    session["state"] = "duplicate"
                    write_event(connection, "키오스크 자동 발급 시연 차단", "blocked", exam_id=exam_id,
                                candidate_id=candidate_id, actor=issue_actor, details="DEMO simulation; duplicate admission")
                    self._send_json(409, {"error": "이미 입실 처리된 응시자입니다."})
                    return
                demo_token = f"DEMO-ONLY:SELF:{secrets.token_urlsafe(32)}"
                expires_at = now + TOKEN_TTL_SECONDS
                qr_png = self._generate_qr_png(demo_token)
                connection.execute(
                    """
                    INSERT INTO demo_tokens (token_hash, exam_id, candidate_id, pathway, issued_at, expires_at)
                    VALUES (?, ?, ?, 'self_demo', ?, ?)
                    """,
                    (token_digest(demo_token), exam_id, candidate_id, now, expires_at),
                )
                session.update(
                    {
                        "state": "demo_qr_ready",
                        "demo_token_hash": token_digest(demo_token),
                        "demo_token": demo_token,
                        "demo_qr_png": qr_png,
                        "demo_issued_at": now,
                        "demo_delivery": "screen",
                        "demo_expires_at": expires_at,
                        "demo_simulation": True,
                        "demo_auth_expires_at": now + TOKEN_TTL_SECONDS + AUTH_PROOF_TTL_SECONDS,
                    }
                )
                write_event(
                    connection,
                    "키오스크 자동 발급 시연(DEMO)",
                    "accepted",
                    exam_id=exam_id,
                    candidate_id=candidate_id,
                    actor=issue_actor,
                    details=f"authentication_path=self_demo; preset simulation ({scenario}) independent of face analysis; DEMO-only one-time QR {TOKEN_TTL_SECONDS}s; not real authentication",
                )
        self._send_json(
            200,
            {
                "state": "demo_qr_ready",
                "qr_png": qr_png,
                "qr_mime_type": "image/png",
                "expires_in": TOKEN_TTL_SECONDS,
                "demo_only": True,
                "simulation": True,
                "real_authentication": False,
                "demo_token": demo_token,
                "delivery": "screen",
                "exam_name": "2026 모의 자격시험",
                "candidate_name": ROSTER[(exam_id, candidate_id)]["name"],
                "candidate_number": ROSTER[(exam_id, candidate_id)]["number"],
                "issued_at": datetime.fromtimestamp(now, timezone.utc).astimezone().isoformat(timespec="seconds"),
                "expires_at": datetime.fromtimestamp(expires_at, timezone.utc).astimezone().isoformat(timespec="seconds"),
            },
        )

    def _kiosk_status(self) -> None:
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        self._purge_expired_kiosk_sessions()
        session = self._kiosk_session(payload.get("kiosk_session_id"))
        if not session:
            self._send_json(410, {"error": "키오스크 세션이 만료되었습니다. 화면을 초기화하세요."})
            return
        now = time.time()
        with self.server.kiosk_lock:
            if session.get("identity_approved_until") and session["identity_approved_until"] <= now:
                session["state"] = "verification_expired"
                session["qr_png"] = None
                if session.get("token_hash"):
                    with connect_db() as connection:
                        connection.execute(
                            "UPDATE tokens SET used_at = COALESCE(used_at, ?) WHERE token_hash = ?",
                            (now, session["token_hash"]),
                        )
            elif session["state"] == "qr_ready":
                with connect_db() as connection:
                    token = connection.execute(
                        "SELECT expires_at, used_at FROM tokens WHERE token_hash = ?",
                        (session["token_hash"],),
                    ).fetchone()
                if token is None or token["used_at"] is not None:
                    session["state"] = "completed"
                    session["qr_png"] = None
                elif (
                    session["token_expires_at"] <= now
                    or token["expires_at"] <= now
                ):
                    session["state"] = "expired"
                    session["qr_png"] = None
                    with connect_db() as connection:
                        connection.execute(
                            "UPDATE tokens SET used_at = COALESCE(used_at, ?) WHERE token_hash = ?",
                            (now, session["token_hash"]),
                        )
            elif session["state"] == "demo_qr_ready":
                with connect_db() as connection:
                    demo_token = connection.execute(
                        "SELECT expires_at, used_at FROM demo_tokens WHERE token_hash = ?",
                        (session["demo_token_hash"],),
                    ).fetchone()
                if demo_token is None or demo_token["used_at"] is not None:
                    session["state"] = "demo_completed"
                    session["demo_token"] = None
                    session["demo_qr_png"] = None
                elif demo_token["expires_at"] <= now or session["demo_expires_at"] <= now:
                    session["state"] = "expired"
                    session["demo_token"] = None
                    session["demo_qr_png"] = None
                    with connect_db() as connection:
                        connection.execute(
                            "UPDATE demo_tokens SET used_at = COALESCE(used_at, ?) WHERE token_hash = ?",
                            (now, session["demo_token_hash"]),
                        )
                        write_event(
                            connection,
                            "키오스크 셀프 DEMO QR 만료",
                            "blocked",
                            exam_id=session["exam_id"],
                            candidate_id=session["candidate_id"],
                            actor="kiosk",
                            details="authentication_path=self_demo; QR expired",
                        )
            response: dict[str, object] = {
                "state": session["state"],
                "verification": verification_policy.describe_session(session),
            }
            if session.get("identity_approved_until"):
                response["verification_expires_in"] = max(
                    0, int(session["identity_approved_until"] - now)
                )
            if session["state"] == "qr_ready":
                response.update(
                    {
                        "qr_png": session["qr_png"],
                        "qr_mime_type": "image/png",
                        "expires_in": max(0, math.ceil(session["token_expires_at"] - now)),
                        "exam_name": "2026 모의 자격시험",
                        "issued_at": datetime.fromtimestamp(session["token_issued_at"], timezone.utc).astimezone().isoformat(timespec="seconds"),
                        "expires_at": datetime.fromtimestamp(session["token_expires_at"], timezone.utc).astimezone().isoformat(timespec="seconds"),
                    }
                )
            elif session["state"] == "demo_qr_ready":
                response.update(
                    {
                        "qr_png": session["demo_qr_png"],
                        "qr_mime_type": "image/png",
                        "expires_in": max(0, math.ceil(session["demo_expires_at"] - now)),
                        "demo_only": True,
                        "simulation": bool(session.get("demo_simulation")),
                        "demo_token": session["demo_token"],
                        "delivery": session.get("demo_delivery"),
                        "exam_name": "2026 모의 자격시험",
                        "issued_at": datetime.fromtimestamp(session["demo_issued_at"], timezone.utc).astimezone().isoformat(timespec="seconds"),
                        "expires_at": datetime.fromtimestamp(session["demo_expires_at"], timezone.utc).astimezone().isoformat(timespec="seconds"),
                    }
                )
            elif session["mode"] == "self_demo" and session.get("demo_auth_expires_at"):
                response["demo_reissue_allowed"] = (
                    session["demo_auth_expires_at"] > now
                    and session["state"] == "expired"
                )
        self._send_json(200, response)

    def _kiosk_help(self) -> None:
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        self._purge_expired_kiosk_sessions()
        session = self._kiosk_session(payload.get("kiosk_session_id"))
        if not session:
            self._send_json(410, {"error": "키오스크 세션이 만료되었습니다."})
            return
        with connect_db() as connection:
            write_event(
                connection,
                "키오스크 관리자 도움 요청",
                "pending",
                exam_id=EXAM_ID,
                candidate_id=session["candidate_id"],
                actor="kiosk",
                details="현장 관리자 도움 요청",
            )
        self._send_json(200, {"message": "현장 직원에게 도움을 요청했습니다."})

    def _end_kiosk_session(self) -> None:
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        session_id = payload.get("kiosk_session_id")
        if not isinstance(session_id, str) or not session_id:
            self._send_json(400, {"error": "키오스크 세션이 필요합니다."})
            return
        with self.server.kiosk_lock:
            session = self.server.kiosk_sessions.pop(session_id, None)
        if session:
            with connect_db() as connection:
                now = time.time()
                if session.get("token_hash"):
                    connection.execute(
                        "UPDATE tokens SET used_at = COALESCE(used_at, ?) WHERE token_hash = ?",
                        (now, session["token_hash"]),
                    )
                if session.get("demo_token_hash") and not (
                    session.get("demo_delivery") == "print"
                    and session.get("demo_expires_at", 0) > now
                ):
                    connection.execute(
                        "UPDATE demo_tokens SET used_at = COALESCE(used_at, ?) WHERE token_hash = ?",
                        (now, session["demo_token_hash"]),
                    )
                write_event(
                    connection,
                    "키오스크 세션 종료",
                    "info",
                    exam_id=session.get("exam_id") or EXAM_ID,
                    candidate_id=session.get("candidate_id"),
                    actor="kiosk",
                    details=(
                        f"authentication_path={session.get('mode')}; 세션 종료; "
                        + (
                            "인쇄용 DEMO QR은 만료 시각까지 유효"
                            if session.get("demo_delivery") == "print"
                            and session.get("demo_expires_at", 0) > now
                            else "미사용 QR 무효화"
                        )
                    ),
                )
        self._send_json(200, {"message": "키오스크 세션을 종료했습니다."})

    def _create_kiosk_demo_token(
        self,
        connection: sqlite3.Connection,
        candidate_id: str,
        now: float,
        pathway: str = "kiosk_proctor_demo",
    ) -> tuple[str, str, str, float]:
        token = f"DEMO-ONLY:KIOSK-PROCTOR:{secrets.token_urlsafe(32)}"
        expires_at = now + TOKEN_TTL_SECONDS
        qr_png = self._generate_qr_png(token)
        token_hash = token_digest(token)
        connection.execute(
            """
            INSERT INTO demo_tokens
                (token_hash, exam_id, candidate_id, pathway, issued_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (token_hash, EXAM_ID, candidate_id, pathway, now, expires_at),
        )
        return token_hash, token, qr_png, expires_at

    @staticmethod
    def _generate_qr_png(payload: str) -> str:
        qr = qrcode.QRCode(
            error_correction=qrcode.constants.ERROR_CORRECT_H,
            box_size=10,
            border=4,
        )
        qr.add_data(payload)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white")
        qr_buffer = io.BytesIO()
        image.save(qr_buffer, format="PNG")
        return base64.b64encode(qr_buffer.getvalue()).decode("ascii")

    def _kiosk_demo_qr(self) -> None:
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        session = self._kiosk_session(payload.get("kiosk_session_id"))
        if not session:
            self._send_json(410, {"error": "키오스크 세션이 만료되었습니다."})
            return
        demo_payload = f"DEMO-ONLY:{secrets.token_urlsafe(16)}"
        self._send_json(
            200,
            {
                "qr_png": self._generate_qr_png(demo_payload),
                "qr_mime_type": "image/png",
                "demo_only": True,
            },
        )

    def _refresh_kiosk_qr(self) -> None:
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        self._purge_expired_kiosk_sessions()
        session_id = payload.get("kiosk_session_id")
        if not isinstance(session_id, str):
            self._send_json(400, {"error": "키오스크 세션이 필요합니다."})
            return

        with self.server.kiosk_lock:
            session = self.server.kiosk_sessions.get(session_id)
            now = time.time()
            if not session or now - session["last_activity"] > KIOSK_SESSION_TTL_SECONDS:
                self.server.kiosk_sessions.pop(session_id, None)
                self._send_json(410, {"error": "키오스크 세션이 만료되었습니다."})
                return
            session["last_activity"] = now
            if not session.get("identity_approved_until"):
                self._send_json(409, {"error": "유효한 감독관 신원확인 승인 후 QR을 갱신할 수 있습니다."})
                return
            if session["identity_approved_until"] <= now:
                session["state"] = "verification_expired"
                session["qr_png"] = None
                if session.get("token_hash"):
                    with connect_db() as connection:
                        connection.execute(
                            "UPDATE tokens SET used_at = COALESCE(used_at, ?) WHERE token_hash = ?",
                            (now, session["token_hash"]),
                        )
                if session.get("demo_token_hash"):
                    with connect_db() as connection:
                        connection.execute(
                            "UPDATE demo_tokens SET used_at = COALESCE(used_at, ?) WHERE token_hash = ?",
                            (now, session["demo_token_hash"]),
                        )
                self._send_json(409, {"error": "신원확인 승인 유효기간이 만료되었습니다. 새 키오스크 접수와 감독관 확인이 필요합니다."})
                return
            if (
                session.get("mode") not in {"proctor", "proctor_fallback"}
                or session["state"] not in {"demo_qr_ready", "expired"}
                or not session.get("demo_token_hash")
            ):
                self._send_json(409, {"error": "유효한 감독관 신원확인 승인 후 QR을 갱신할 수 있습니다."})
                return

            with connect_db() as connection:
                connection.execute("BEGIN IMMEDIATE")
                previous = connection.execute(
                    """
                    SELECT expires_at, used_at, pathway FROM demo_tokens
                    WHERE token_hash = ? AND exam_id = ? AND candidate_id = ?
                    """,
                    (session["demo_token_hash"], EXAM_ID, session["candidate_id"]),
                ).fetchone()
                if previous is None or previous["pathway"] != "kiosk_proctor_demo":
                    session["state"] = "demo_completed"
                    session["demo_qr_png"] = None
                    self._send_json(409, {"error": "이전 QR이 이미 사용되었거나 유효하지 않습니다. 재접수가 필요합니다."})
                    return
                if previous["expires_at"] > now:
                    if previous["used_at"] is not None:
                        session["state"] = "demo_completed"
                        session["demo_qr_png"] = None
                        self._send_json(409, {"error": "이전 QR이 이미 사용되었습니다. 재접수가 필요합니다."})
                        return
                    self._send_json(409, {"error": "현재 QR이 아직 유효합니다. 만료 후에만 새 QR을 발급할 수 있습니다."})
                    return
                if previous["used_at"] is not None and session["state"] != "expired":
                    session["state"] = "demo_completed"
                    session["demo_qr_png"] = None
                    self._send_json(409, {"error": "이전 QR이 이미 사용되었습니다. 재접수가 필요합니다."})
                    return
                connection.execute(
                    "UPDATE demo_tokens SET used_at = COALESCE(used_at, ?) WHERE token_hash = ?",
                    (now, session["demo_token_hash"]),
                )
                candidate_id = session["candidate_id"]
                if connection.execute(
                    """
                    SELECT 1 FROM admissions WHERE exam_id = ? AND candidate_id = ?
                    UNION ALL
                    SELECT 1 FROM demo_admissions WHERE exam_id = ? AND candidate_id = ?
                    LIMIT 1
                    """,
                    (EXAM_ID, candidate_id, EXAM_ID, candidate_id),
                ).fetchone():
                    session["state"] = "duplicate"
                    session["demo_qr_png"] = None
                    write_event(
                        connection,
                        "키오스크 DEMO QR 재발급 차단",
                        "blocked",
                        exam_id=EXAM_ID,
                        candidate_id=candidate_id,
                        actor="proctor",
                        details="이미 입실 처리된 응시자",
                    )
                    self._send_json(409, {"error": "이미 입실 처리된 응시자입니다."})
                    return
                token_hash, demo_token, qr_png, expires_at = self._create_kiosk_demo_token(
                    connection, candidate_id, now
                )
                session.update(
                    {
                        "state": "demo_qr_ready",
                        "demo_token_hash": token_hash,
                        "demo_token": demo_token,
                        "demo_qr_png": qr_png,
                        "demo_issued_at": now,
                        "demo_expires_at": expires_at,
                        "demo_delivery": "screen",
                    }
                )
                write_event(
                    connection,
                    "키오스크 DEMO QR 재발급",
                    "accepted",
                    exam_id=EXAM_ID,
                    candidate_id=candidate_id,
                    actor="proctor",
                    details="이전 DEMO 토큰 폐기; 모의 감독관 대면 확인 후 30초 DEMO QR 재발급; 운영 admissions 금지",
                )
        self._send_json(
            200,
            {
                "state": "demo_qr_ready",
                "demo_only": True,
                "message": "이전 DEMO QR을 무효화하고 30초 유효한 새 DEMO QR을 발급했습니다.",
            },
        )

    def _review_kiosk_candidate(self) -> None:
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        self._purge_expired_kiosk_sessions()
        session_id = payload.get("kiosk_session_id")
        document_type = payload.get("document_type")
        decision = payload.get("decision")
        reason = payload.get("reason")
        document_match = payload.get("document_match")
        live_face_match = payload.get("live_face_match")
        if (
            not isinstance(session_id, str)
            or not isinstance(document_type, str)
            or document_type not in MOCK_ALLOWED_ID_TYPES | {"other", "none"}
            or not isinstance(decision, str)
            or decision not in {"approve", "reject", "hold"}
            or not isinstance(reason, str)
            or reason not in {"normal", "document_mismatch", "suspected_proxy", "identity_unverifiable", "no_document"}
            or (document_match is not True and document_match is not False)
            or (live_face_match is not True and live_face_match is not False)
        ):
            self._send_json(400, {"error": "신분증 종류와 현장 대조 결과를 선택해 주세요."})
            return
        with self.server.kiosk_lock:
            session = self.server.kiosk_sessions.get(session_id)
            if (
                not session
                or time.time() - session["last_activity"] > KIOSK_SESSION_TTL_SECONDS
            ):
                self.server.kiosk_sessions.pop(session_id, None)
                self._send_json(410, {"error": "키오스크 요청이 만료되었습니다."})
                return
            if session["state"] != "awaiting_proctor":
                self._send_json(409, {"error": "이미 처리되었거나 대기 중인 키오스크 요청이 아닙니다."})
                return
            if session.get("mode") not in {"proctor", "proctor_fallback"}:
                self._send_json(409, {"error": "감독관 확인 모드 요청이 아닙니다."})
                return
            candidate_id = session["candidate_id"]
            exam_id = session["exam_id"]
            session["last_activity"] = time.time()
            with connect_db() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if connection.execute(
                    """
                    SELECT 1 FROM admissions WHERE exam_id = ? AND candidate_id = ?
                    UNION ALL
                    SELECT 1 FROM demo_admissions WHERE exam_id = ? AND candidate_id = ?
                    LIMIT 1
                    """,
                    (exam_id, candidate_id, exam_id, candidate_id),
                ).fetchone():
                    session["state"] = "duplicate"
                    write_event(
                        connection,
                        "키오스크 입실 QR 발급",
                        "blocked",
                        exam_id=exam_id,
                        candidate_id=candidate_id,
                        actor="proctor",
                        details="이미 입실 처리된 응시자",
                    )
                    self._send_json(409, {"error": "이미 입실 처리된 응시자입니다."})
                    return
                if (
                    decision == "hold"
                    or reason in {"identity_unverifiable", "no_document"}
                    or document_type in {"other", "none"}
                    or (
                        decision == "approve"
                        and (not document_match or not live_face_match)
                    )
                ):
                    session["state"] = "supervisor_pending"
                    write_event(
                        connection,
                        "키오스크 책임자 확인 대기",
                        "pending",
                        exam_id=exam_id,
                        candidate_id=candidate_id,
                        actor="proctor",
                        details="허용 신분증 부재 또는 신원확인 불가; QR 미발급",
                    )
                    response = {
                        "state": "supervisor_pending",
                        "message": "신원확인을 보류하고 현장 책임자 확인을 요청했습니다. QR은 발급되지 않았습니다.",
                    }
                    status = 200
                elif decision == "reject" or reason in {"document_mismatch", "suspected_proxy"}:
                    session["state"] = "rejected"
                    if reason == "document_mismatch":
                        event = "키오스크 신분증 불일치"
                    elif reason == "suspected_proxy":
                        event = "키오스크 대리응시 의심"
                    else:
                        event = "키오스크 확인 거부"
                    write_event(
                        connection,
                        event,
                        "rejected",
                        exam_id=exam_id,
                        candidate_id=candidate_id,
                        actor="proctor",
                        details="감독관 수동 대조 거부; QR 미발급",
                    )
                    response = {"state": "rejected", "message": "확인 결과로 입실 QR을 발급하지 않았습니다."}
                    status = 200
                elif (
                    decision != "approve"
                    or reason != "normal"
                    or not document_match
                    or not live_face_match
                    or document_type not in MOCK_ALLOWED_ID_TYPES
                ):
                    session["state"] = "supervisor_pending"
                    write_event(
                        connection,
                        "키오스크 책임자 확인 대기",
                        "pending",
                        exam_id=exam_id,
                        candidate_id=candidate_id,
                        actor="proctor",
                        details="현장 대조 결과 확인 불가; QR 미발급",
                    )
                    response = {"state": "supervisor_pending", "message": "신원확인 보류 · 책임자 확인 대기 상태입니다. QR은 발급되지 않았습니다."}
                    status = 200
                else:
                    approved_at = time.time()
                    token_hash, demo_token, qr_png, expires_at = self._create_kiosk_demo_token(
                        connection, candidate_id, approved_at
                    )
                    session.update(
                        {
                            "state": "demo_qr_ready",
                            "demo_token_hash": token_hash,
                            "demo_token": demo_token,
                            "demo_qr_png": qr_png,
                            "demo_issued_at": approved_at,
                            "demo_expires_at": expires_at,
                            "demo_delivery": "screen",
                            "identity_approved_until": approved_at + KIOSK_SESSION_TTL_SECONDS,
                        }
                    )
                    write_event(
                        connection,
                        "키오스크 DEMO QR 발급",
                        "accepted",
                        exam_id=exam_id,
                        candidate_id=candidate_id,
                        actor="proctor",
                        details=f"authentication_path={session['mode']}; 모의 허용 신분증·현장 얼굴 확인 완료; DEMO-only 1회 QR 30초; admissions unchanged",
                    )
                    response = {
                        "state": "demo_qr_ready",
                        "demo_only": True,
                        "message": "모의 확인 완료 · 실제 입실 불가 DEMO QR을 발급했습니다.",
                    }
                    status = 200
        self._send_json(status, response)

    def _login(self) -> None:
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return

        role = payload.get("role")
        username = payload.get("username")
        password = payload.get("password")
        demo_key = payload.get("demo_key") or payload.get("key")

        if role not in DEMO_ACCOUNTS:
            self._send_json(400, {"error": "로그인 역할을 선택해 주세요."})
            return

        account = DEMO_ACCOUNTS[role]
        if (
            username is not None
            and username != account["username"]
            or password is not None
            and password != account["password"]
        ):
            self._send_json(401, {"error": "시연 계정 정보가 올바르지 않습니다."})
            return

        if username is None and password is None and demo_key is None:
            self._send_json(400, {"error": "시연 계정 또는 키를 입력해 주세요."})
            return

        expected_key = self.server.role_keys.get(role)
        if demo_key is not None and expected_key and not hmac.compare_digest(demo_key, expected_key):
            self._send_json(401, {"error": "시연 키가 올바르지 않습니다."})
            return

        session_id = uuid.uuid4().hex
        self.server.sessions[session_id] = {
            "role": role,
            "candidate_id": account.get("candidate_id"),
            "expires_at": time.time() + SESSION_TTL_SECONDS,
        }
        self._send_json(
            200,
            {
                "success": True,
                "role": role,
                "candidate_id": account.get("candidate_id"),
                "message": "데모 세션에 로그인했습니다.",
            },
            extra_headers={
                "Set-Cookie": (
                    f"demo_session_id={session_id}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL_SECONDS}"
                )
            },
        )

    def _issue_token(self) -> None:
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        face_assertion_fields = {
            "face_score",
            "face_similarity",
            "face_match",
            "captured_face_result",
            "face_comparison_result",
            "face_verified",
            "authentication_success",
            "real_authentication",
        }
        if face_assertion_fields.intersection(payload):
            self._send_json(
                400,
                {
                    "error": "브라우저 얼굴 유사도 결과는 서버 인증 근거로 사용할 수 없습니다. 공식 검증된 신원확인 결과만 운영 QR 발급에 사용됩니다.",
                    "demo_only": False,
                    "real_authentication": False,
                },
            )
            return
        exam_id = payload.get("exam_id")
        candidate_id = payload.get("candidate_id")
        auth_session_id = payload.get("auth_session_id")
        if (
            not isinstance(exam_id, str)
            or not isinstance(candidate_id, str)
            or not isinstance(auth_session_id, str)
            or not auth_session_id
            or (exam_id, candidate_id) not in ROSTER
        ):
            self._send_json(400, {"error": "공식 인증 세션과 명부 응시자 정보가 필요합니다."})
            return
        if not operational_identity_ready():
            with connect_db() as connection:
                write_event(
                    connection,
                    "운영 QR 토큰 발급",
                    "blocked",
                    exam_id=exam_id,
                    candidate_id=candidate_id,
                    actor="candidate",
                    details="공식 모바일신분증 provider 또는 주관기관 승인 정책이 설정되지 않음",
                )
            self._send_json(
                503,
                {
                    "error": "공식 신원확인 provider와 시험 주관기관 정책 승인이 없어 운영 QR을 발급할 수 없습니다.",
                    "demo_only": False,
                },
            )
            return

        token = secrets.token_urlsafe(32)
        qr_png = self._generate_qr_png(token)
        issued_at = time.time()
        expires_at = issued_at + TOKEN_TTL_SECONDS
        status = 201
        response: dict[str, object]
        with connect_db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            auth = connection.execute(
                """
                SELECT * FROM mobile_id_auth
                WHERE auth_session_hash = ? AND exam_id = ? AND candidate_id = ?
                """,
                (token_digest(auth_session_id), exam_id, candidate_id),
            ).fetchone()
            denial: str | None = None
            if auth is None or auth["outcome"] != "success":
                denial = "공식 모바일신분증 인증 기록이 없습니다."
            elif not has_operational_auth_proof(
                connection,
                token_digest(auth_session_id),
                exam_id,
                candidate_id,
                issued_at,
                require_issued=False,
            ):
                denial = "검증된 공식 provider 인증 또는 주관기관 정책 승인이 없습니다."
            elif auth["expires_at"] is None or auth["expires_at"] <= issued_at:
                denial = "공식 인증이 만료되었습니다. 다시 인증해 주세요."
            elif auth["invalidated_at"] is not None:
                denial = "이전 모의 인증은 무효화되었습니다. 다시 인증해 주세요."
            elif auth["token_issued_at"] is not None:
                denial = "이 모의 인증으로 이미 QR 토큰을 발급했습니다."
            elif connection.execute(
                """
                SELECT 1 FROM admissions WHERE exam_id = ? AND candidate_id = ?
                UNION ALL
                SELECT 1 FROM demo_admissions WHERE exam_id = ? AND candidate_id = ?
                LIMIT 1
                """,
                (exam_id, candidate_id, exam_id, candidate_id),
            ).fetchone():
                denial = "이미 입실 처리된 응시자입니다."

            if denial:
                write_event(
                    connection,
                    "QR 토큰 발급",
                    "blocked",
                    exam_id=exam_id,
                    candidate_id=candidate_id,
                    details=denial,
                )
                status = 409
                response = {"error": denial}
            else:
                connection.execute(
                    """
                    UPDATE mobile_id_auth SET token_issued_at = ?
                    WHERE auth_id = ? AND token_issued_at IS NULL
                    """,
                    (issued_at, auth["auth_id"]),
                )
                connection.execute(
                    """
                    INSERT INTO tokens
                        (token_hash, exam_id, candidate_id, issued_at, expires_at, auth_session_hash)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        token_digest(token),
                        exam_id,
                        candidate_id,
                        issued_at,
                        expires_at,
                        token_digest(auth_session_id),
                    ),
                )
                write_event(
                    connection,
                    "운영 QR 토큰 발급",
                    "accepted",
                    exam_id=exam_id,
                    candidate_id=candidate_id,
                    details=f"official_provider={auth['provider']}; institution_policy_approved=true; 만료 {TOKEN_TTL_SECONDS}초",
                )
                response = {
                    "token": token,
                    "expires_in": TOKEN_TTL_SECONDS,
                    "exam_id": exam_id,
                    "candidate_id": candidate_id,
                    "qr_png": qr_png,
                    "qr_mime_type": "image/png",
                }
        self._send_json(status, response)

    def _authenticate_mobile_id(self) -> None:
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        exam_id = payload.get("exam_id")
        candidate_id = payload.get("candidate_id")
        demo_outcome = payload.get("demo_outcome")
        if (
            not isinstance(exam_id, str)
            or not isinstance(candidate_id, str)
            or (exam_id, candidate_id) not in ROSTER
            or not isinstance(demo_outcome, str)
            or demo_outcome not in {"success", "failure"}
        ):
            self._send_json(400, {"error": "명부 응시자와 성공/실패 모의 인증 시나리오를 선택해 주세요."})
            return

        provider = DEMO_MOBILE_ID_PROVIDER_FACTORY(demo_outcome)
        result = provider.authenticate(exam_id, candidate_id)
        now = time.time()
        auth_session_id = secrets.token_urlsafe(32) if result.success else None
        expires_at = now + AUTH_PROOF_TTL_SECONDS if result.success else None
        with connect_db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE demo_mobile_id_auth SET invalidated_at = ?
                WHERE exam_id = ? AND candidate_id = ?
                    AND outcome = 'success'
                    AND token_issued_at IS NULL
                    AND invalidated_at IS NULL
                """,
                (now, exam_id, candidate_id),
            )
            connection.execute(
                """
                UPDATE demo_tokens SET used_at = COALESCE(used_at, ?)
                WHERE exam_id = ? AND candidate_id = ?
                    AND pathway = 'mobile_id_demo' AND used_at IS NULL
                """,
                (now, exam_id, candidate_id),
            )
            connection.execute(
                """
                INSERT INTO demo_mobile_id_auth
                    (auth_session_hash, exam_id, candidate_id, provider, outcome, authenticated_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token_digest(auth_session_id) if auth_session_id else None,
                    exam_id,
                    candidate_id,
                    result.provider,
                    "success" if result.success else "failure",
                    now,
                    expires_at,
                ),
            )
            write_event(
                connection,
                "모의 모바일신분증 인증",
                "accepted" if result.success else "blocked",
                exam_id=exam_id,
                candidate_id=candidate_id,
                actor="candidate",
                details=f"provider={result.provider}; demo_only=true; {result.message}",
            )
        self._send_json(
            200,
            {
                "success": result.success,
                "provider": result.provider,
                "message": result.message,
                "auth_session_id": auth_session_id,
                "expires_in": AUTH_PROOF_TTL_SECONDS if result.success else 0,
                "official": False,
                "demo_only": True,
            },
        )

    def _authenticate_official_mobile_id(self) -> None:
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        exam_id = payload.get("exam_id")
        candidate_id = payload.get("candidate_id")
        if (
            not isinstance(exam_id, str)
            or not isinstance(candidate_id, str)
            or (exam_id, candidate_id) not in ROSTER
        ):
            self._send_json(400, {"error": "시험 명부 응시자 정보가 필요합니다."})
            return
        if not operational_identity_ready():
            with connect_db() as connection:
                write_event(
                    connection,
                    "공식 모바일신분증 인증",
                    "blocked",
                    exam_id=exam_id,
                    candidate_id=candidate_id,
                    actor="candidate",
                    details="공식 mobile-ID provider 또는 시험 주관기관 승인 정책이 미설정",
                )
            self._send_json(
                503,
                {
                    "success": False,
                    "official": False,
                    "demo_only": False,
                    "error": "공식 모바일신분증 provider와 시험 주관기관 정책 승인이 설정되지 않았습니다.",
                },
            )
            return

        request_id = secrets.token_urlsafe(24)
        result = OFFICIAL_MOBILE_ID_PROVIDER.authenticate(
            exam_id,
            candidate_id,
            request_id,
        )
        if not isinstance(result, MobileIdAuthResult):
            raise TypeError("Official mobile-ID providers must return MobileIdAuthResult.")
        now = time.time()
        evidence = result.evidence
        liveness = result.liveness_evidence
        verified = (
            result.permits_operational_admission_for(
                request_id,
                candidate_id,
                now,
            )
            and INSTITUTION_ADMISSION_POLICY_APPROVED is True
            and result.provider == getattr(OFFICIAL_MOBILE_ID_PROVIDER, "name", None)
            and result.verified_candidate_id == candidate_id
            and result.candidate_match is True
        )
        expires_at = (
            min(
                evidence.expires_at,
                liveness.expires_at,
                now + AUTH_PROOF_TTL_SECONDS,
            )
            if verified and evidence and liveness
            else None
        )
        evidence_bound = bool(
            evidence
            and evidence.is_valid_for(request_id, now)
            and result.provider == getattr(OFFICIAL_MOBILE_ID_PROVIDER, "name", None)
        )
        liveness_bound = bool(
            liveness
            and liveness.is_valid_for(request_id, candidate_id, now)
            and result.provider == getattr(OFFICIAL_MOBILE_ID_PROVIDER, "name", None)
        )
        with connect_db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if evidence_bound and evidence:
                used_transaction = connection.execute(
                    """
                    SELECT 1 FROM mobile_id_auth
                    WHERE verification_source = ? AND transaction_id = ?
                    """,
                    (evidence.verification_source, evidence.transaction_id),
                ).fetchone()
                if used_transaction is not None:
                    verified = False
                    evidence_bound = False
                    expires_at = None
            if liveness_bound and liveness:
                used_liveness_transaction = connection.execute(
                    """
                    SELECT 1 FROM mobile_id_auth
                    WHERE liveness_source = ? AND liveness_transaction_id = ?
                    """,
                    (liveness.verification_source, liveness.transaction_id),
                ).fetchone()
                if used_liveness_transaction is not None:
                    verified = False
                    liveness_bound = False
                    expires_at = None
            auth_session_id = secrets.token_urlsafe(32) if verified else None
            connection.execute(
                """
                UPDATE mobile_id_auth SET invalidated_at = ?
                WHERE exam_id = ? AND candidate_id = ?
                    AND outcome = 'success'
                    AND token_issued_at IS NULL
                    AND invalidated_at IS NULL
                """,
                (now, exam_id, candidate_id),
            )
            connection.execute(
                """
                INSERT INTO mobile_id_auth
                    (auth_session_hash, exam_id, candidate_id, provider, outcome,
                     authenticated_at, expires_at, official, candidate_match, policy_approved,
                     verification_source, signing_key_id, signature_valid, transaction_id,
                     request_id, evidence_candidate_id, evidence_issued_at,
                     liveness_source, liveness_model_version, liveness_signature_valid,
                     liveness_transaction_id, liveness_candidate_id, liveness_issued_at,
                     liveness_expires_at, liveness_score, liveness_threshold)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token_digest(auth_session_id) if auth_session_id else None,
                    exam_id,
                    candidate_id,
                    result.provider,
                    "success" if verified else "failure",
                    now,
                    expires_at,
                    int(result.official),
                    None if result.candidate_match is None else int(result.candidate_match),
                    int(
                        result.institution_policy_approved
                        and INSTITUTION_ADMISSION_POLICY_APPROVED is True
                    ),
                    evidence.verification_source if evidence_bound and evidence else None,
                    evidence.signing_key_id if evidence_bound and evidence else None,
                    int(evidence_bound and evidence is not None and evidence.signature_valid),
                    evidence.transaction_id if evidence_bound and evidence else None,
                    request_id if evidence_bound else None,
                    evidence.candidate_id if evidence_bound and evidence else None,
                    evidence.issued_at if evidence_bound and evidence else None,
                    liveness.verification_source if liveness_bound and liveness else None,
                    liveness.model_version if liveness_bound and liveness else None,
                    int(liveness_bound and liveness is not None and liveness.signature_valid),
                    liveness.transaction_id if liveness_bound and liveness else None,
                    liveness.candidate_id if liveness_bound and liveness else None,
                    liveness.issued_at if liveness_bound and liveness else None,
                    liveness.expires_at if liveness_bound and liveness else None,
                    liveness.score if liveness_bound and liveness else None,
                    liveness.threshold if liveness_bound and liveness else None,
                ),
            )
            write_event(
                connection,
                "공식 모바일신분증 인증",
                "accepted" if verified else "blocked",
                exam_id=exam_id,
                candidate_id=candidate_id,
                actor="candidate",
                details=(
                    f"provider={result.provider}; official={str(result.official).lower()}; "
                    f"candidate_match={str(result.candidate_match is True and result.verified_candidate_id == candidate_id).lower()}; "
                    f"signature_verified={str(verified).lower()}; "
                    f"institution_policy_approved={str(INSTITUTION_ADMISSION_POLICY_APPROVED is True).lower()}"
                ),
            )
        self._send_json(
            200 if verified else 403,
            {
                "success": verified,
                "official": result.official,
                "demo_only": False,
                "message": (
                    "공식 모바일신분증 서명 검증이 완료되었습니다."
                    if verified
                    else "서명, 거래번호, 유효기간, 응시자 일치 여부를 검증하지 못했습니다."
                ),
                "auth_session_id": auth_session_id,
                "expires_in": AUTH_PROOF_TTL_SECONDS if verified else 0,
            },
        )

    def _issue_demo_token(self) -> None:
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        exam_id = payload.get("exam_id")
        candidate_id = payload.get("candidate_id")
        auth_session_id = payload.get("auth_session_id")
        if (
            not isinstance(exam_id, str)
            or not isinstance(candidate_id, str)
            or not isinstance(auth_session_id, str)
            or not auth_session_id
            or (exam_id, candidate_id) not in ROSTER
        ):
            self._send_json(400, {"error": "DEMO 인증 세션과 명부 응시자 정보가 필요합니다."})
            return

        now = time.time()
        demo_token = f"DEMO-ONLY:MOBILE:{secrets.token_urlsafe(32)}"
        token_hash = token_digest(demo_token)
        expires_at = now + TOKEN_TTL_SECONDS
        qr_png = self._generate_qr_png(demo_token)
        with connect_db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            auth = connection.execute(
                """
                SELECT * FROM demo_mobile_id_auth
                WHERE auth_session_hash = ? AND exam_id = ? AND candidate_id = ?
                """,
                (token_digest(auth_session_id), exam_id, candidate_id),
            ).fetchone()
            denial: str | None = None
            if auth is None or auth["outcome"] != "success":
                denial = "성공한 DEMO 모바일신분증 인증 기록이 없습니다."
            elif auth["expires_at"] is None or auth["expires_at"] <= now:
                denial = "DEMO 인증이 만료되었습니다. 다시 시연해 주세요."
            elif auth["invalidated_at"] is not None:
                denial = "이전 DEMO 인증은 무효화되었습니다."
            elif auth["token_issued_at"] is not None:
                denial = "이 DEMO 인증 결과는 이미 QR 발급에 사용되었습니다."
            elif connection.execute(
                """
                SELECT 1 FROM admissions WHERE exam_id = ? AND candidate_id = ?
                UNION ALL
                SELECT 1 FROM demo_admissions WHERE exam_id = ? AND candidate_id = ?
                LIMIT 1
                """,
                (exam_id, candidate_id, exam_id, candidate_id),
            ).fetchone():
                denial = "이미 입실 처리된 응시자입니다."

            if denial:
                write_event(
                    connection,
                    "DEMO QR 토큰 발급",
                    "blocked",
                    exam_id=exam_id,
                    candidate_id=candidate_id,
                    actor="candidate",
                    details=denial,
                )
                self._send_json(409, {"error": denial, "demo_only": True})
                return

            connection.execute(
                """
                UPDATE demo_mobile_id_auth SET token_issued_at = ?
                WHERE auth_id = ? AND token_issued_at IS NULL
                """,
                (now, auth["auth_id"]),
            )
            connection.execute(
                """
                INSERT INTO demo_tokens
                    (token_hash, exam_id, candidate_id, pathway, issued_at, expires_at)
                VALUES (?, ?, ?, 'mobile_id_demo', ?, ?)
                """,
                (token_hash, exam_id, candidate_id, now, expires_at),
            )
            write_event(
                connection,
                "DEMO QR 토큰 발급",
                "accepted",
                exam_id=exam_id,
                candidate_id=candidate_id,
                actor="candidate",
                details=f"pathway=mobile_id_demo; ttl={TOKEN_TTL_SECONDS}; operating admission disabled",
            )

        candidate = ROSTER[(exam_id, candidate_id)]
        self._send_json(
            201,
            {
                "token": demo_token,
                "expires_in": TOKEN_TTL_SECONDS,
                "exam_id": exam_id,
                "candidate_id": candidate_id,
                "candidate_name": candidate["name"],
                "candidate_number": candidate["number"],
                "exam_name": "2026 모의 자격시험",
                "issued_at": datetime.fromtimestamp(now, timezone.utc).astimezone().isoformat(timespec="seconds"),
                "expires_at": datetime.fromtimestamp(expires_at, timezone.utc).astimezone().isoformat(timespec="seconds"),
                "qr_png": qr_png,
                "qr_mime_type": "image/png",
                "demo_only": True,
            },
        )

    def _inspect_demo_token(self) -> None:
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        token = payload.get("token")
        exam_id = payload.get("exam_id")
        if not isinstance(token, str) or not token or not isinstance(exam_id, str):
            self._send_json(400, {"error": "DEMO 토큰과 시험 정보가 필요합니다."})
            return
        if not token.startswith("DEMO-ONLY:MOBILE:"):
            self._send_json(409, {"error": "DEMO 전용 QR은 이 확인 경로에서 사용할 수 없습니다.", "valid": False})
            return

        now = time.time()
        with connect_db() as connection:
            row = connection.execute(
                "SELECT * FROM demo_tokens WHERE token_hash = ? AND pathway = 'mobile_id_demo'",
                (token_digest(token),),
            ).fetchone()
            denial: str | None = None
            if row is None:
                denial = "유효하지 않은 DEMO 토큰입니다."
            elif row["exam_id"] != exam_id:
                denial = "토큰의 시험 정보가 일치하지 않습니다."
            elif row["expires_at"] <= now:
                denial = "DEMO 토큰이 만료되었습니다."
            elif row["used_at"] is not None:
                denial = "이미 사용된 DEMO 토큰입니다."
            elif connection.execute(
                """
                SELECT 1 FROM admissions WHERE exam_id = ? AND candidate_id = ?
                UNION ALL
                SELECT 1 FROM demo_admissions WHERE exam_id = ? AND candidate_id = ?
                LIMIT 1
                """,
                (row["exam_id"], row["candidate_id"], row["exam_id"], row["candidate_id"]),
            ).fetchone():
                denial = "이미 입실 처리된 응시자입니다."
            if denial:
                write_event(
                    connection,
                    "DEMO QR 토큰 확인",
                    "blocked",
                    exam_id=exam_id,
                    candidate_id=row["candidate_id"] if row else None,
                    actor="proctor",
                    details=denial,
                )
                self._send_json(409, {"error": denial, "valid": False, "demo_only": True})
                return
            candidate = ROSTER[(row["exam_id"], row["candidate_id"])]
            write_event(
                connection,
                "DEMO QR 토큰 확인",
                "accepted",
                exam_id=row["exam_id"],
                candidate_id=row["candidate_id"],
                actor="proctor",
                details="DEMO-only token; pending manual simulated decision",
            )
        self._send_json(
            200,
            {
                "valid": True,
                "demo_only": True,
                "real_admission": False,
                "exam_id": row["exam_id"],
                "candidate_id": row["candidate_id"],
                "candidate_name": candidate["name"],
                "candidate_number": candidate["number"],
                "expires_in": max(0, int(row["expires_at"] - now)),
            },
        )

    def _approve_demo_token(self) -> None:
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        exam_id = payload.get("exam_id")
        candidate_id = payload.get("candidate_id")
        token = payload.get("token")
        roster_match = payload.get("roster_match")
        face_match = payload.get("face_match")
        if (
            not isinstance(exam_id, str)
            or not isinstance(candidate_id, str)
            or not isinstance(token, str)
            or not token
            or (roster_match is not True and roster_match is not False)
            or (face_match is not True and face_match is not False)
        ):
            self._send_json(400, {"error": "시험, 응시자, DEMO 토큰 및 모의 대조 결과가 필요합니다."})
            return
        if not token.startswith("DEMO-ONLY:MOBILE:"):
            self._send_json(
                409,
                {
                    "error": "모바일신분증 DEMO QR만 이 모의 승인 API에서 사용할 수 있습니다.",
                    "demo_admitted": False,
                    "admitted": False,
                    "real_admission": False,
                },
            )
            return

        now = time.time()
        status = 200
        with connect_db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM demo_tokens WHERE token_hash = ? AND pathway = 'mobile_id_demo'",
                (token_digest(token),),
            ).fetchone()
            denial: str | None = None
            if row is None:
                denial = "유효하지 않은 DEMO 토큰입니다."
            elif (row["exam_id"], row["candidate_id"]) != (exam_id, candidate_id):
                denial = "토큰의 시험 또는 응시자 정보가 일치하지 않습니다."
            elif row["expires_at"] <= now:
                denial = "DEMO 토큰이 만료되었습니다."
            elif row["used_at"] is not None:
                denial = "이미 사용된 DEMO 토큰입니다."
            elif connection.execute(
                """
                SELECT 1 FROM admissions WHERE exam_id = ? AND candidate_id = ?
                UNION ALL
                SELECT 1 FROM demo_admissions WHERE exam_id = ? AND candidate_id = ?
                LIMIT 1
                """,
                (exam_id, candidate_id, exam_id, candidate_id),
            ).fetchone():
                denial = "이미 입실 처리된 응시자입니다."

            if denial:
                write_event(
                    connection,
                    "DEMO 감독관 입실 확인",
                    "blocked",
                    exam_id=exam_id,
                    candidate_id=candidate_id,
                    actor="proctor",
                    details=denial,
                )
                status = 409
                response = {
                    "error": denial,
                    "demo_admitted": False,
                    "admitted": False,
                    "real_admission": False,
                }
            else:
                connection.execute(
                    "UPDATE demo_tokens SET used_at = ? WHERE token_hash = ? AND used_at IS NULL",
                    (now, token_digest(token)),
                )
                if roster_match is True and face_match is True:
                    connection.execute(
                        """
                        INSERT INTO demo_admissions (exam_id, candidate_id, admitted_at)
                        VALUES (?, ?, ?)
                        """,
                        (exam_id, candidate_id, utc_now()),
                    )
                    write_event(
                        connection,
                        "DEMO 감독관 모의 입실 승인",
                        "demo_admitted",
                        exam_id=exam_id,
                        candidate_id=candidate_id,
                        actor="proctor",
                        details="DEMO-only token and simulated manual checks; admissions unchanged",
                    )
                    response = {
                        "message": "DEMO 모의 입실을 처리했습니다. 실제 입실 기록은 생성되지 않았습니다.",
                        "demo_admitted": True,
                        "real_admission": False,
                        "admitted": False,
                    }
                else:
                    write_event(
                        connection,
                        "DEMO 감독관 모의 입실",
                        "blocked",
                        exam_id=exam_id,
                        candidate_id=candidate_id,
                        actor="proctor",
                        details="simulated roster or face comparison mismatch; DEMO token consumed",
                    )
                    status = 403
                    response = {
                        "error": "모의 명부 확인 또는 얼굴 대조가 일치하지 않아 DEMO 입실을 차단했습니다.",
                        "demo_admitted": False,
                        "admitted": False,
                        "real_admission": False,
                    }
        self._send_json(status, response)

    def _inspect_token(self) -> None:
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        token = payload.get("token")
        exam_id = payload.get("exam_id")
        if not isinstance(token, str) or not token or not isinstance(exam_id, str):
            self._send_json(400, {"error": "토큰과 시험 정보가 필요합니다."})
            return
        if token.startswith("DEMO-ONLY:"):
            with connect_db() as connection:
                write_event(
                    connection,
                    "QR 토큰 확인",
                    "blocked",
                    exam_id=exam_id,
                    actor="proctor",
                    details="DEMO 전용 QR은 입실 확인에 사용할 수 없음",
                )
            self._send_json(409, {"error": "DEMO 전용 QR은 입실 확인에 사용할 수 없습니다.", "valid": False})
            return
        if not operational_identity_ready():
            with connect_db() as connection:
                write_event(
                    connection,
                    "운영 QR 토큰 확인",
                    "blocked",
                    exam_id=exam_id,
                    actor="proctor",
                    details="공식 mobile-ID provider 또는 시험 주관기관 승인 정책이 미설정",
                )
            self._send_json(
                503,
                {
                    "error": "공식 provider와 주관기관 정책 승인이 없어 운영 QR을 확인할 수 없습니다.",
                    "valid": False,
                },
            )
            return

        with connect_db() as connection:
            row = connection.execute(
                "SELECT * FROM tokens WHERE token_hash = ?", (token_digest(token),)
            ).fetchone()
            denial: str | None = None
            if row is None:
                denial = "유효하지 않은 토큰입니다."
            elif row["exam_id"] != exam_id:
                denial = "토큰의 시험 정보가 일치하지 않습니다."
            elif not has_operational_auth_proof(
                connection,
                row["auth_session_hash"],
                row["exam_id"],
                row["candidate_id"],
                time.time(),
            ):
                denial = "토큰에 유효한 공식 신원확인 및 기관 정책 승인 증명이 없습니다."
            elif row["expires_at"] <= time.time():
                denial = "토큰이 만료되었습니다."
            elif row["used_at"] is not None:
                denial = "이미 사용된 토큰입니다."
            elif (row["exam_id"], row["candidate_id"]) not in ROSTER:
                denial = "시험 명부에서 응시자를 확인할 수 없습니다."
            elif connection.execute(
                "SELECT 1 FROM admissions WHERE exam_id = ? AND candidate_id = ?",
                (row["exam_id"], row["candidate_id"]),
            ).fetchone():
                denial = "이미 입실 처리된 응시자입니다."

            if denial:
                write_event(
                    connection,
                    "QR 토큰 확인",
                    "blocked",
                    exam_id=exam_id,
                    actor="proctor",
                    details=denial,
                )
                status = 409
                response: dict[str, object] = {"error": denial, "valid": False}
            else:
                candidate = ROSTER[(row["exam_id"], row["candidate_id"])]
                write_event(
                    connection,
                    "QR 토큰 확인",
                    "accepted",
                    exam_id=row["exam_id"],
                    candidate_id=row["candidate_id"],
                    actor="proctor",
                    details="유효 토큰; 승인 전 수동 대조 대기",
                )
                status = 200
                response = {
                    "valid": True,
                    "exam_id": row["exam_id"],
                    "candidate_id": row["candidate_id"],
                    "candidate_name": candidate["name"],
                    "candidate_number": candidate["number"],
                    "expires_in": max(0, int(row["expires_at"] - time.time())),
                }
        self._send_json(status, response)

    def _approve(self) -> None:
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        exam_id = payload.get("exam_id")
        candidate_id = payload.get("candidate_id")
        token = payload.get("token")
        roster_match = payload.get("roster_match")
        face_match = payload.get("face_match")
        if (
            not isinstance(exam_id, str)
            or not isinstance(candidate_id, str)
            or not isinstance(token, str)
            or not token
            or (roster_match is not True and roster_match is not False)
            or (face_match is not True and face_match is not False)
        ):
            self._send_json(400, {"error": "시험, 응시자, 토큰, 명부 확인 및 얼굴 대조 결과가 필요합니다."})
            return
        if token.startswith("DEMO-ONLY:"):
            with connect_db() as connection:
                write_event(
                    connection,
                    "감독관 입실 확인",
                    "blocked",
                    exam_id=exam_id,
                    candidate_id=candidate_id,
                    actor="proctor",
                    details="DEMO 전용 QR은 입실 승인에 사용할 수 없음",
                )
            self._send_json(409, {"error": "DEMO 전용 QR은 입실 승인에 사용할 수 없습니다.", "admitted": False})
            return
        if not operational_identity_ready():
            with connect_db() as connection:
                write_event(
                    connection,
                    "운영 입실 승인",
                    "blocked",
                    exam_id=exam_id,
                    candidate_id=candidate_id,
                    actor="proctor",
                    details="공식 mobile-ID provider 또는 시험 주관기관 승인 정책이 미설정",
                )
            self._send_json(
                503,
                {
                    "error": "공식 provider 인증 및 주관기관 정책 승인이 없어 운영 입실을 승인할 수 없습니다.",
                    "admitted": False,
                    "real_admission": False,
                },
            )
            return

        status = 200
        response: dict[str, object]
        with connect_db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM tokens WHERE token_hash = ?", (token_digest(token),)
            ).fetchone()
            denial: str | None = None
            if row is None:
                denial = "유효하지 않은 토큰입니다."
            elif (row["exam_id"], row["candidate_id"]) != (exam_id, candidate_id):
                denial = "토큰의 시험 또는 응시자 정보가 일치하지 않습니다."
            elif not has_operational_auth_proof(
                connection,
                row["auth_session_hash"],
                row["exam_id"],
                row["candidate_id"],
                time.time(),
            ):
                denial = "토큰에 유효한 공식 신원확인 및 기관 정책 승인 증명이 없습니다."
            elif row["expires_at"] <= time.time():
                denial = "토큰이 만료되었습니다."
            elif row["used_at"] is not None:
                denial = "이미 사용된 토큰입니다."
            elif (exam_id, candidate_id) not in ROSTER:
                denial = "시험 명부에서 응시자를 확인할 수 없습니다."
            elif connection.execute(
                "SELECT 1 FROM admissions WHERE exam_id = ? AND candidate_id = ?",
                (exam_id, candidate_id),
            ).fetchone():
                denial = "이미 입실 처리된 응시자입니다."

            if denial:
                write_event(
                    connection,
                    "감독관 입실 확인",
                    "blocked",
                    exam_id=exam_id,
                    candidate_id=candidate_id,
                    actor="proctor",
                    details=denial,
                )
                status = 409
                response = {"error": denial, "admitted": False}
            else:
                # A valid token is consumed on the first proctor decision, including mismatch.
                connection.execute(
                    "UPDATE tokens SET used_at = ? WHERE token_hash = ? AND used_at IS NULL",
                    (time.time(), token_digest(token)),
                )
                if roster_match is not True:
                    denial = "감독관의 명부 확인이 일치하지 않아 입실을 차단했습니다."
                    write_event(
                        connection,
                        "감독관 입실 확인",
                        "blocked",
                        exam_id=exam_id,
                        candidate_id=candidate_id,
                        actor="proctor",
                        details=denial,
                    )
                    status = 403
                    response = {"error": denial, "admitted": False}
                elif face_match is not True:
                    denial = "감독관의 명부 확인 및 얼굴 대조가 일치하지 않아 입실을 차단했습니다."
                    write_event(
                        connection,
                        "감독관 입실 확인",
                        "blocked",
                        exam_id=exam_id,
                        candidate_id=candidate_id,
                        actor="proctor",
                        details=denial,
                    )
                    status = 403
                    response = {"error": denial, "admitted": False}
                else:
                    connection.execute(
                        """
                        INSERT INTO admissions (exam_id, candidate_id, admitted_at)
                        VALUES (?, ?, ?)
                        """,
                        (exam_id, candidate_id, utc_now()),
                    )
                    write_event(
                        connection,
                        "감독관 입실 승인",
                        "admitted",
                        exam_id=exam_id,
                        candidate_id=candidate_id,
                        actor="proctor",
                        details="명부 확인 및 수동 얼굴 대조 일치",
                    )
                    response = {"message": "입실이 승인되었습니다.", "admitted": True}
        self._send_json(status, response)

    def _decide_physical_id(self) -> None:
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        exam_id = payload.get("exam_id")
        candidate_id = payload.get("candidate_id")
        document_type = payload.get("document_type")
        decision = payload.get("decision")
        reason = payload.get("reason")
        document_match = payload.get("document_match")
        roster_photo_match = payload.get("roster_photo_match")
        live_face_match = payload.get("live_face_match")
        if (
            not isinstance(exam_id, str)
            or not isinstance(candidate_id, str)
            or (exam_id, candidate_id) not in ROSTER
            or not isinstance(document_type, str)
            or document_type not in MOCK_ALLOWED_ID_TYPES | {"other", "none"}
            or not isinstance(decision, str)
            or decision not in {"approve", "reject", "hold"}
            or (document_match is not True and document_match is not False)
            or (roster_photo_match is not True and roster_photo_match is not False)
            or (live_face_match is not True and live_face_match is not False)
            or not isinstance(reason, str)
            or reason not in {
                "document_mismatch",
                "suspected_proxy",
                "identity_unverifiable",
                "no_phone_or_document",
            }
        ):
            self._send_json(400, {"error": "허용된 모의 신분증 종류, 결과 및 세 가지 현장 대조 결과가 필요합니다."})
            return

        with connect_db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            denial: str | None = None
            if connection.execute(
                """
                SELECT 1 FROM admissions WHERE exam_id = ? AND candidate_id = ?
                UNION ALL
                SELECT 1 FROM demo_admissions WHERE exam_id = ? AND candidate_id = ?
                LIMIT 1
                """,
                (exam_id, candidate_id, exam_id, candidate_id),
            ).fetchone():
                denial = "이미 입실 처리된 응시자입니다."
                write_event(
                    connection,
                    "실물 신분증 입실 판정",
                    "blocked",
                    exam_id=exam_id,
                    candidate_id=candidate_id,
                    actor="proctor",
                    details=denial,
                )
                status = 409
                response = {"error": denial, "admitted": False}
            elif decision == "hold":
                write_event(
                    connection,
                    "신원확인 보류",
                    "pending",
                    exam_id=exam_id,
                    candidate_id=candidate_id,
                    actor="proctor",
                    details="휴대전화·허용 신분증 부재 또는 확인 불가; 자동 승인하지 않음",
                )
                status = 200
                response = {"message": "신원확인 보류 상태로 기록했습니다. 입실 승인되지 않았습니다.", "admitted": False}
            elif document_type not in MOCK_ALLOWED_ID_TYPES:
                write_event(
                    connection,
                    "신분증 불일치",
                    "rejected",
                    exam_id=exam_id,
                    candidate_id=candidate_id,
                    actor="proctor",
                    details="시험 주관기관 허용 여부를 확인할 수 없는 문서 종류; 모의 허용 목록 외",
                )
                status = 403
                response = {"error": "모의 허용 목록에 없는 신분증 종류입니다. 입실을 승인하지 않았습니다.", "admitted": False}
            elif decision == "reject" or not (
                document_match and roster_photo_match and live_face_match
            ):
                if reason == "suspected_proxy" or (
                    document_match and roster_photo_match and not live_face_match
                ):
                    event = "대리응시 의심"
                    details = "현장 얼굴 대조 불일치"
                elif reason == "document_mismatch":
                    event = "신분증 불일치"
                    details = "신분증과 응시자 현장 대조 불일치"
                else:
                    event = "신원확인 불가"
                    details = "필수 현장 대조 결과가 모두 일치하지 않음"
                write_event(
                    connection,
                    event,
                    "rejected",
                    exam_id=exam_id,
                    candidate_id=candidate_id,
                    actor="proctor",
                    details=details,
                )
                status = 403
                response = {"error": details + " · 입실을 승인하지 않았습니다.", "admitted": False}
            else:
                connection.execute(
                    "INSERT INTO demo_admissions (exam_id, candidate_id, admitted_at) VALUES (?, ?, ?)",
                    (exam_id, candidate_id, utc_now()),
                )
                write_event(
                    connection,
                    "실물 신분증 DEMO 모의 입실",
                    "demo_admitted",
                    exam_id=exam_id,
                    candidate_id=candidate_id,
                    actor="proctor",
                    details="모의 허용 조건 및 신분증·등록 사진·현장 얼굴 수동 대조 일치; admissions unchanged",
                )
                status = 200
                response = {
                    "message": "DEMO 모의 입실을 기록했습니다. 실제 입실 기록은 생성되지 않았습니다.",
                    "admitted": True,
                    "demo_admitted": True,
                    "real_admission": False,
                }
        self._send_json(status, response)

    def _decide_exception(self) -> None:
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        exam_id = payload.get("exam_id")
        candidate_id = payload.get("candidate_id")
        procedure_id = payload.get("procedure_id")
        decision = payload.get("decision")
        checks = (
            payload.get("application_source_verified"),
            payload.get("roster_photo_match"),
            payload.get("two_staff_witnesses"),
            payload.get("supervisor_confirmed"),
        )
        if (
            not isinstance(exam_id, str)
            or not isinstance(candidate_id, str)
            or (exam_id, candidate_id) not in ROSTER
            or not isinstance(procedure_id, str)
            or procedure_id not in MOCK_EXCEPTION_PROCEDURES
            or not isinstance(decision, str)
            or decision not in {"approve", "reject", "hold"}
            or any(value is not True and value is not False for value in checks)
        ):
            self._send_json(400, {"error": "사전 허용된 모의 절차, 결과 및 책임자 확인 항목이 필요합니다."})
            return

        with connect_db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            denial: str | None = None
            if connection.execute(
                """
                SELECT 1 FROM admissions WHERE exam_id = ? AND candidate_id = ?
                UNION ALL
                SELECT 1 FROM demo_admissions WHERE exam_id = ? AND candidate_id = ?
                LIMIT 1
                """,
                (exam_id, candidate_id, exam_id, candidate_id),
            ).fetchone():
                denial = "이미 입실 처리된 응시자입니다."
                write_event(
                    connection,
                    "책임자 예외 판정",
                    "blocked",
                    exam_id=exam_id,
                    candidate_id=candidate_id,
                    actor="supervisor",
                    details=denial,
                )
                status = 409
                response = {"error": denial, "admitted": False}
            elif decision == "hold":
                write_event(
                    connection,
                    "책임자 예외 확인 보류",
                    "pending",
                    exam_id=exam_id,
                    candidate_id=candidate_id,
                    actor="supervisor",
                    details="예외 절차가 완료되지 않아 신원확인 보류",
                )
                status = 200
                response = {"message": "신원확인 보류 상태로 기록했습니다. 입실 승인되지 않았습니다.", "admitted": False}
            elif decision == "reject" or not all(checks):
                write_event(
                    connection,
                    "책임자 예외 확인 거부",
                    "rejected",
                    exam_id=exam_id,
                    candidate_id=candidate_id,
                    actor="supervisor",
                    details="사전 허용된 대체 절차의 필수 확인 미완료",
                )
                status = 403
                response = {"error": "사전 허용된 대체 절차가 완료되지 않아 입실을 승인하지 않았습니다.", "admitted": False}
            else:
                connection.execute(
                    "INSERT INTO demo_admissions (exam_id, candidate_id, admitted_at) VALUES (?, ?, ?)",
                    (exam_id, candidate_id, utc_now()),
                )
                write_event(
                    connection,
                    "책임자 예외 DEMO 모의 입실",
                    "demo_admitted",
                    exam_id=exam_id,
                    candidate_id=candidate_id,
                    actor="supervisor",
                    details="시제품의 사전 허용 모의 절차 및 2인 확인; admissions unchanged",
                )
                status = 200
                response = {
                    "message": "DEMO 모의 예외 입실을 기록했습니다. 실제 입실 기록은 생성되지 않았습니다.",
                    "admitted": True,
                    "demo_admitted": True,
                    "real_admission": False,
                }
        self._send_json(status, response)

    def _hold(self) -> None:
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        exam_id = payload.get("exam_id")
        candidate_id = payload.get("candidate_id")
        if (
            not isinstance(exam_id, str)
            or not isinstance(candidate_id, str)
            or (exam_id, candidate_id) not in ROSTER
        ):
            self._send_json(400, {"error": "시험 명부 응시자 정보가 필요합니다."})
            return
        with connect_db() as connection:
            write_event(
                connection,
                "감독관 입실 보류",
                "blocked",
                exam_id=exam_id,
                candidate_id=candidate_id,
                actor="proctor",
                details="감독관이 입실 보류 처리",
            )
        self._send_json(200, {"message": "입실을 보류하고 서버 기록에 저장했습니다."})

    def _scenario_action(self, name: str) -> None:
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        sandbox = self.server.scenario_sandbox
        try:
            if name == "start":
                result = sandbox.start(payload.get("scenario"))
            elif name == "reset":
                result = sandbox.reset()
            else:
                result = sandbox.act(payload.get("run_id"), payload.get("action"), payload.get("params"))
        except demo_scenarios.ScenarioError as exc:
            self._send_json(exc.status, {"error": exc.message, "demo_only": True})
            return
        self._send_json(200, result)

    def _proctor_overview(self) -> None:
        with self.server.kiosk_lock:
            pending = sum(1 for item in self.server.kiosk_sessions.values()
                          if item.get("state") == "awaiting_proctor")
        with connect_db() as connection:
            demo_admitted = connection.execute(
                "SELECT COUNT(*) FROM demo_admissions WHERE exam_id = ?", (EXAM_ID,)
            ).fetchone()[0]
            rows = connection.execute(
                """
                SELECT occurred_at, event, outcome, candidate_id, details, domain
                FROM events WHERE actor = 'proctor' AND domain = 'demo' AND event LIKE '키오스크%'
                ORDER BY event_id DESC LIMIT 30
                """
            ).fetchall()
        reviews = []
        for row in rows:
            item = dict(row)
            candidate = item.get("candidate_id")
            item["candidate_id"] = f"***{candidate[-3:]}" if candidate else candidate
            reviews.append(item)
        self._send_json(200, {"demo_only": True, "banner": demo_scenarios.BANNER,
                              "pending_review_count": pending, "demo_admitted_count": demo_admitted,
                              "recent_reviews": reviews})

    def _reset_demo(self) -> None:
        try:
            self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        with connect_db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM admissions WHERE exam_id = ?", (EXAM_ID,))
            connection.execute("DELETE FROM demo_admissions WHERE exam_id = ?", (EXAM_ID,))
            connection.execute("DELETE FROM tokens WHERE exam_id = ?", (EXAM_ID,))
            connection.execute("DELETE FROM demo_tokens WHERE exam_id = ?", (EXAM_ID,))
            connection.execute("DELETE FROM mobile_id_auth WHERE exam_id = ?", (EXAM_ID,))
            connection.execute("DELETE FROM demo_mobile_id_auth WHERE exam_id = ?", (EXAM_ID,))
            write_event(
                connection,
                "시연 상태 초기화",
                "info",
                exam_id=EXAM_ID,
                actor="proctor",
                details="입실 상태 초기화; 토큰과 인증 기록 재설정; 감사 기록은 보존",
            )
        with self.server.kiosk_lock:
            self.server.kiosk_sessions.clear()
        self._send_json(200, {"message": "시연 상태를 초기화했습니다. 기록은 보존됩니다."})


def main() -> None:
    initialize_db()
    role_keys = {
        "candidate": os.environ.get("EXAM_SAFE_CANDIDATE_KEY") or "demo-candidate-key",
        "proctor": os.environ.get("EXAM_SAFE_PROCTOR_KEY") or "demo-proctor-key",
        "admin": os.environ.get("EXAM_SAFE_ADMIN_KEY") or "demo-admin-key",
        "supervisor": os.environ.get("EXAM_SAFE_SUPERVISOR_KEY") or "demo-supervisor-key",
    }
    host = os.environ.get("EXAM_SAFE_HOST", "127.0.0.1")
    port = int(os.environ.get("EXAM_SAFE_PORT", "8000"))
    server = DemoServer((host, port), RequestHandler)
    server.role_keys = role_keys
    server.sessions = {}
    server.kiosk_sessions = {}
    server.kiosk_lock = threading.Lock()
    server.scenario_sandbox = demo_scenarios.ScenarioSandbox(qr_factory=RequestHandler._generate_qr_png)
    print(f"시연 응시자 키: {role_keys['candidate']}", flush=True)
    print(f"시연 감독관 키: {role_keys['proctor']}", flush=True)
    print(f"시연 관리자 키: {role_keys['admin']}", flush=True)
    print(f"시연 현장 책임자 키: {role_keys['supervisor']}", flush=True)
    print(f"LISTENING http://{host}:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n서버를 종료합니다.", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
