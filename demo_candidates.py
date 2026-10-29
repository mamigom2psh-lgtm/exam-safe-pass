"""Multi-candidate DEMO registry: candidates, exam catalog and registration photos.

Everything here is VIRTUAL DEMO data. Virtual codes are fictional birth-date/gender codes and must never be real
resident registration numbers. Photos are stored outside any public folder under random names and are only
served by authenticated server routes. This module never logs names, codes, photos or embeddings.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PHOTO_DIR = Path(os.environ.get("EXAM_SAFE_PHOTO_DIR", ROOT / "demo_photos"))
LEGACY_IMAGE_DIR = Path(os.environ.get("EXAM_SAFE_LEGACY_IMAGE_DIR", ROOT / "images"))
MAX_PHOTO_BYTES = 5 * 1024 * 1024
MIN_PHOTO_SIDE, MAX_PHOTO_SIDE = 200, 6000
MAX_PHOTO_PIXELS = 25_000_000
NO_GRADE = "해당 없음"
RESET_CONFIRMATION = "RESET-DEMO-CANDIDATES"
LEGACY_EXAM_ID = "exam-demo-2026"

VIRTUAL_CODE_PATTERN = re.compile(r"^(\d{2})(\d{2})(\d{2})-([1-4])$")
NUMBER_PATTERN = re.compile(r"^\d{4}-\d{4}$")
PHOTO_NAME_PATTERN = re.compile(r"^[0-9a-f]{32}\.(jpg|png)$")
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

DEFAULT_EXAMS = [
    {"id": "demo-computer", "kind": "사무·IT 자격", "name": "컴퓨터활용능력", "grades": ["1급", "2급"], "default_grade": "2급"},
    {"id": "demo-korean-history", "kind": "역사·교양 검정", "name": "한국사능력검정시험", "grades": ["심화", "기본"]},
    {"id": "demo-engineer-info", "kind": "IT 기사", "name": "정보처리기사", "grades": [NO_GRADE]},
    {"id": "demo-wordprocessor", "kind": "사무·IT 자격", "name": "워드프로세서", "grades": ["단일등급"]},
    {"id": "demo-electric", "kind": "기능사", "name": "전기기능사", "grades": [NO_GRADE]},
    {"id": "demo-korean-cooking", "kind": "기능사", "name": "한식조리기능사", "grades": [NO_GRADE]},
]
DEFAULT_EXAM_ID = "demo-computer"

SEED_CANDIDATES = [
    ("2026-0012", "박세현", "980320-2", "demo-computer", "2급"),
    ("2026-0013", "김하늘", "990415-2", "demo-korean-history", "심화"),
    ("2026-0014", "이준호", "010822-3", "demo-engineer-info", NO_GRADE),
    ("2026-0015", "최서연", "020711-4", "demo-wordprocessor", "단일등급"),
    ("2026-0016", "정민수", "970903-1", "demo-computer", "1급"),
]
DEFAULT_ROUND = "2026년 DEMO 1회"

SCHEMA = """
CREATE TABLE IF NOT EXISTS demo_exams (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL UNIQUE,
    grades TEXT NOT NULL,
    default_grade TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS demo_candidates (
    id TEXT PRIMARY KEY,
    number TEXT NOT NULL,
    number_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    virtual_code TEXT,
    exam_ref TEXT REFERENCES demo_exams(id),
    grade TEXT,
    exam_round TEXT NOT NULL DEFAULT '',
    photo_file TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS demo_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class RegistryError(Exception):
    def __init__(self, status: int, message: str, field: str | None = None, extra: dict | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.field = field
        self.extra = extra or {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def valid_virtual_code(value: object) -> bool:
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


def mask_code(code: str | None) -> str:
    return f"{code[:2]}••••-{code[-1]}" if code and len(code) == 8 else ""


def normalize_number(number: str) -> str:
    return number.replace("-", "")


def _text(payload: dict, key: str, label: str, *, max_len: int = 40, required: bool = True) -> str | None:
    value = payload.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise RegistryError(400, f"{label}을(를) 입력해 주세요.", key)
        return None
    if not isinstance(value, str) or len(value.strip()) > max_len or CONTROL_CHARS.search(value):
        raise RegistryError(400, f"{label} 형식이 올바르지 않습니다.", key)
    return value.strip()


def init_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    now = _now()
    for exam in DEFAULT_EXAMS:
        connection.execute(
            "INSERT OR IGNORE INTO demo_exams (id, kind, name, grades, default_grade, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (exam["id"], exam["kind"], exam["name"], json.dumps(exam["grades"], ensure_ascii=False),
             exam.get("default_grade"), now, now),
        )
    migrate_legacy(connection)


def migrate_legacy(connection: sqlite3.Connection) -> None:
    """One-time, non-destructive migration of the two pre-existing DEMO candidates.

    Existing registrations are inserted only if absent and legacy image files are copied (never moved or deleted).
    """
    if connection.execute("SELECT 1 FROM demo_meta WHERE key = 'legacy_migrated'").fetchone():
        return
    now = _now()
    legacy = [
        ("candidate-001", "2026-0012", "박세현", "980320-2", "demo-computer", "2급", "2026-0012-registration.jpg"),
        ("candidate-002", "2026-0099", "이모의", None, None, None, "candidate2.png"),
    ]
    for cid, number, name, code, exam_ref, grade, legacy_file in legacy:
        if connection.execute("SELECT 1 FROM demo_candidates WHERE id = ? OR number_key = ?",
                              (cid, normalize_number(number))).fetchone():
            continue
        photo_file = _copy_legacy_photo(LEGACY_IMAGE_DIR / legacy_file)
        connection.execute(
            """INSERT INTO demo_candidates (id, number, number_key, name, virtual_code, exam_ref, grade, exam_round,
               photo_file, active, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,1,?,?)""",
            (cid, number, normalize_number(number), name, code, exam_ref, grade, DEFAULT_ROUND, photo_file, now, now),
        )
    connection.execute("INSERT OR REPLACE INTO demo_meta (key, value) VALUES ('legacy_migrated', ?)", (now,))


def _ensure_photo_dir() -> None:
    PHOTO_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    (PHOTO_DIR / "backup").mkdir(exist_ok=True, mode=0o700)


def _copy_legacy_photo(source: Path) -> str | None:
    if not source.is_file():
        return None
    data = source.read_bytes()
    try:
        ext = inspect_image(data)[0]
    except RegistryError:
        return None
    return _write_photo(data, ext)


def _write_photo(data: bytes, ext: str) -> str:
    _ensure_photo_dir()
    name = f"{secrets.token_hex(16)}.{ext}"
    fd = os.open(PHOTO_DIR / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    return name


def backup_photo(photo_file: str | None) -> None:
    """Local backup policy: replaced/removed photos are moved to demo_photos/backup/, never deleted."""
    if not photo_file or not PHOTO_NAME_PATTERN.match(photo_file):
        return
    source = PHOTO_DIR / photo_file
    if source.is_file():
        _ensure_photo_dir()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        shutil.move(str(source), str(PHOTO_DIR / "backup" / f"{photo_file}.{stamp}.bak"))


def inspect_image(data: bytes) -> tuple[str, int, int]:
    """Return (ext, width, height) from the real bytes; the extension/Content-Type is never trusted."""
    if len(data) < 32:
        raise RegistryError(400, "이미지 파일이 올바르지 않습니다.", "photo")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        if data[12:16] != b"IHDR" or b"IEND" not in data[-16:]:
            raise RegistryError(400, "PNG 파일이 손상되었거나 올바르지 않습니다.", "photo")
        width, height = struct.unpack(">II", data[16:24])
        ext = "png"
    elif data.startswith(b"\xff\xd8\xff"):
        if not data.rstrip(b"\x00").endswith(b"\xff\xd9"):
            raise RegistryError(400, "JPEG 파일이 손상되었거나 올바르지 않습니다.", "photo")
        width = height = 0
        pos = 2
        while pos + 9 < len(data):
            if data[pos] != 0xFF:
                pos += 1
                continue
            marker = data[pos + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
                pos += 1 if marker == 0xFF else 2
                continue
            length = struct.unpack(">H", data[pos + 2:pos + 4])[0]
            if marker in range(0xC0, 0xD0) and marker not in (0xC4, 0xC8, 0xCC):
                height, width = struct.unpack(">HH", data[pos + 5:pos + 9])
                break
            pos += 2 + length
        ext = "jpg"
    else:
        raise RegistryError(415, "JPG, JPEG, PNG 형식의 이미지만 등록할 수 있습니다.", "photo")
    if not (MIN_PHOTO_SIDE <= width <= MAX_PHOTO_SIDE and MIN_PHOTO_SIDE <= height <= MAX_PHOTO_SIDE) \
            or width * height > MAX_PHOTO_PIXELS:
        raise RegistryError(
            400, f"이미지 해상도는 한 변이 {MIN_PHOTO_SIDE}~{MAX_PHOTO_SIDE}px 범위여야 합니다.", "photo")
    return ext, width, height


# ---------------------------------------------------------------- exams

def _exam_row(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "kind": row["kind"], "name": row["name"], "grades": json.loads(row["grades"]),
            "default_grade": row["default_grade"] or json.loads(row["grades"])[0]}


def list_exams(connection: sqlite3.Connection) -> list[dict]:
    return [_exam_row(r) for r in connection.execute("SELECT * FROM demo_exams ORDER BY rowid")]


def get_exam(connection: sqlite3.Connection, exam_id: str) -> dict | None:
    row = connection.execute("SELECT * FROM demo_exams WHERE id = ?", (exam_id,)).fetchone()
    return _exam_row(row) if row else None


def _clean_grades(value: object) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > 10:
        raise RegistryError(400, "급수·등급을 1~10개 입력해 주세요. (없으면 '해당 없음')", "grades")
    grades = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item.strip()) > 20 or CONTROL_CHARS.search(item):
            raise RegistryError(400, "급수·등급 값이 올바르지 않습니다.", "grades")
        if item.strip() not in grades:
            grades.append(item.strip())
    return grades


def save_exam(connection: sqlite3.Connection, payload: dict, exam_id: str | None = None) -> dict:
    existing = get_exam(connection, exam_id) if exam_id else None
    if exam_id and not existing:
        raise RegistryError(404, "시험을 찾을 수 없습니다.")
    kind = _text(payload, "kind", "시험 종류", max_len=30) if "kind" in payload or not existing else existing["kind"]
    name = _text(payload, "name", "시험명", max_len=40) if "name" in payload or not existing else existing["name"]
    grades = _clean_grades(payload["grades"]) if "grades" in payload or not existing else existing["grades"]
    default_grade = payload.get("default_grade") or (existing or {}).get("default_grade") or grades[0]
    if default_grade not in grades:
        default_grade = grades[0]
    if connection.execute("SELECT 1 FROM demo_exams WHERE name = ? AND id IS NOT ?", (name, exam_id)).fetchone():
        raise RegistryError(409, "같은 이름의 시험이 이미 있습니다.", "name")
    if existing:
        removed = [g for g in existing["grades"] if g not in grades]
        for grade in removed:
            if connection.execute("SELECT 1 FROM demo_candidates WHERE exam_ref = ? AND grade = ?",
                                  (exam_id, grade)).fetchone():
                raise RegistryError(409, f"등록된 응시자가 사용 중인 급수·등급({grade})은 삭제할 수 없습니다.", "grades")
    now = _now()
    if existing:
        connection.execute("UPDATE demo_exams SET kind=?, name=?, grades=?, default_grade=?, updated_at=? WHERE id=?",
                           (kind, name, json.dumps(grades, ensure_ascii=False), default_grade, now, exam_id))
    else:
        exam_id = f"exam-{secrets.token_hex(4)}"
        connection.execute(
            "INSERT INTO demo_exams (id, kind, name, grades, default_grade, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (exam_id, kind, name, json.dumps(grades, ensure_ascii=False), default_grade, now, now))
    return get_exam(connection, exam_id)


# ---------------------------------------------------------------- candidates

def _public(row: sqlite3.Row, exam: dict | None) -> dict:
    return {
        "id": row["id"], "number": row["number"], "name": row["name"],
        "virtual_code_masked": mask_code(row["virtual_code"]),
        "exam_kind": exam["kind"] if exam else "", "exam_ref": row["exam_ref"],
        "exam_name": exam["name"] if exam else "", "grade": row["grade"] or "", "round": row["exam_round"],
        "has_photo": bool(row["photo_file"]), "active": bool(row["active"]),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def list_candidates(connection: sqlite3.Connection) -> list[dict]:
    exams = {e["id"]: e for e in list_exams(connection)}
    return [_public(r, exams.get(r["exam_ref"])) for r in connection.execute("SELECT * FROM demo_candidates ORDER BY number")]


def get_candidate(connection: sqlite3.Connection, candidate_id: str) -> dict | None:
    row = connection.execute("SELECT * FROM demo_candidates WHERE id = ?", (candidate_id,)).fetchone()
    return _public(row, get_exam(connection, row["exam_ref"]) if row["exam_ref"] else None) if row else None


def active_roster(connection: sqlite3.Connection) -> dict[str, dict]:
    return {r["id"]: {"name": r["name"], "number": r["number"]}
            for r in connection.execute("SELECT id, name, number FROM demo_candidates WHERE active = 1")}


def server_profile(connection: sqlite3.Connection, candidate_id: str) -> dict | None:
    """Server-side registration data used to compare kiosk input. Includes the virtual code; never sent to browsers."""
    row = connection.execute("SELECT * FROM demo_candidates WHERE id = ? AND active = 1", (candidate_id,)).fetchone()
    if not row:
        return None
    exam = get_exam(connection, row["exam_ref"]) if row["exam_ref"] else None
    return {"virtual_code": row["virtual_code"], "exam": exam, "grade": row["grade"],
            "round": row["exam_round"], "has_photo": bool(row["photo_file"])}


def _validated_fields(connection: sqlite3.Connection, payload: dict, existing: sqlite3.Row | None) -> dict:
    out: dict = {}

    def given(key: str) -> bool:
        return key in payload or existing is None

    if given("name"):
        out["name"] = _text(payload, "name", "이름", max_len=30)
    if given("number"):
        number = _text(payload, "number", "수험번호", max_len=9)
        if not NUMBER_PATTERN.match(number):
            raise RegistryError(400, "수험번호는 YYYY-NNNN 형식이어야 합니다. 예: 2026-0012", "number")
        out["number"], out["number_key"] = number, normalize_number(number)
    if given("virtual_code") and (existing is None or payload.get("virtual_code")):
        code = _text(payload, "virtual_code", "가상 DEMO 생년월일·구분 코드", max_len=8)
        if not valid_virtual_code(code):
            raise RegistryError(400, "가상 코드는 유효한 YYMMDD-N 형식이어야 합니다. (실제 주민등록번호 사용 금지)", "virtual_code")
        out["virtual_code"] = code
    if given("exam_ref") or given("grade"):
        exam_ref = payload.get("exam_ref", existing["exam_ref"] if existing else None)
        grade = payload.get("grade", existing["grade"] if existing else None)
        exam = get_exam(connection, exam_ref) if isinstance(exam_ref, str) else None
        if not exam:
            raise RegistryError(400, "등록된 DEMO 시험을 선택해 주세요.", "exam_ref")
        if grade not in exam["grades"]:
            raise RegistryError(400, f"선택한 시험에서 허용되는 급수·등급이 아닙니다. ({', '.join(exam['grades'])})", "grade")
        out["exam_ref"], out["grade"] = exam_ref, grade
    if given("round"):
        out["exam_round"] = _text(payload, "round", "시험 회차", max_len=30) if "round" in payload else DEFAULT_ROUND
    if "active" in payload:
        if not isinstance(payload["active"], bool):
            raise RegistryError(400, "등록 상태 값이 올바르지 않습니다.", "active")
        out["active"] = int(payload["active"])
    return out


def _check_number_unique(connection: sqlite3.Connection, number_key: str, own_id: str | None) -> None:
    if connection.execute("SELECT 1 FROM demo_candidates WHERE number_key = ? AND id IS NOT ?",
                          (number_key, own_id)).fetchone():
        raise RegistryError(409, "이미 등록된 수험번호입니다. 기존 응시자는 덮어쓰지 않습니다.", "number")


def create_candidate(connection: sqlite3.Connection, payload: dict) -> dict:
    fields = _validated_fields(connection, payload, None)
    _check_number_unique(connection, fields["number_key"], None)
    candidate_id = f"cand-{secrets.token_hex(6)}"
    now = _now()
    connection.execute(
        """INSERT INTO demo_candidates (id, number, number_key, name, virtual_code, exam_ref, grade, exam_round,
           active, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (candidate_id, fields["number"], fields["number_key"], fields["name"], fields["virtual_code"],
         fields["exam_ref"], fields["grade"], fields["exam_round"], fields.get("active", 1), now, now))
    return get_candidate(connection, candidate_id)


def update_candidate(connection: sqlite3.Connection, candidate_id: str, payload: dict) -> dict:
    row = connection.execute("SELECT * FROM demo_candidates WHERE id = ?", (candidate_id,)).fetchone()
    if not row:
        raise RegistryError(404, "응시자를 찾을 수 없습니다.")
    fields = _validated_fields(connection, payload, row)
    if "number_key" in fields:
        _check_number_unique(connection, fields["number_key"], candidate_id)
    if fields:
        fields["updated_at"] = _now()
        assignments = ", ".join(f"{column} = ?" for column in fields)
        connection.execute(f"UPDATE demo_candidates SET {assignments} WHERE id = ?", (*fields.values(), candidate_id))
    return get_candidate(connection, candidate_id)


def delete_candidate(connection: sqlite3.Connection, candidate_id: str, confirm_number: str | None) -> None:
    row = connection.execute("SELECT * FROM demo_candidates WHERE id = ?", (candidate_id,)).fetchone()
    if not row:
        raise RegistryError(404, "응시자를 찾을 수 없습니다.")
    if confirm_number != row["number"]:
        raise RegistryError(400, "삭제하려면 확인용으로 해당 수험번호를 정확히 입력해야 합니다.", "confirm")
    backup_photo(row["photo_file"])
    connection.execute("DELETE FROM demo_candidates WHERE id = ?", (candidate_id,))


def set_photo(connection: sqlite3.Connection, candidate_id: str, data: bytes) -> dict:
    row = connection.execute("SELECT * FROM demo_candidates WHERE id = ?", (candidate_id,)).fetchone()
    if not row:
        raise RegistryError(404, "응시자를 찾을 수 없습니다.")
    if len(data) > MAX_PHOTO_BYTES:
        raise RegistryError(413, "사진 파일은 5MB 이하여야 합니다.", "photo")
    ext, width, height = inspect_image(data)
    new_file = _write_photo(data, ext)
    backup_photo(row["photo_file"])
    connection.execute("UPDATE demo_candidates SET photo_file = ?, updated_at = ? WHERE id = ?",
                       (new_file, _now(), candidate_id))
    return {"has_photo": True, "width": width, "height": height, "format": ext}


def photo_path(connection: sqlite3.Connection, candidate_id: str) -> tuple[Path, str] | None:
    row = connection.execute("SELECT photo_file FROM demo_candidates WHERE id = ?", (candidate_id,)).fetchone()
    name = row["photo_file"] if row else None
    if not name or not PHOTO_NAME_PATTERN.match(name):
        return None
    path = PHOTO_DIR / name
    return (path, "image/png" if name.endswith(".png") else "image/jpeg") if path.is_file() else None


def seed(connection: sqlite3.Connection, overwrite_numbers: list[str] | None = None) -> dict:
    """Register the five virtual test candidates. Conflicting existing rows are reported, never silently replaced."""
    overwrite = set(overwrite_numbers or [])
    result = {"created": [], "already_present": [], "overwritten": [], "conflicts": []}
    for number, name, code, exam_ref, grade in SEED_CANDIDATES:
        row = connection.execute("SELECT * FROM demo_candidates WHERE number_key = ?", (normalize_number(number),)).fetchone()
        wanted = {"name": name, "virtual_code": code, "exam_ref": exam_ref, "grade": grade}
        if row is None:
            create_candidate(connection, {"number": number, "round": DEFAULT_ROUND, **wanted})
            result["created"].append(number)
            continue
        differing = [k for k, v in wanted.items() if row[k] != v]
        if not differing:
            result["already_present"].append(number)
        elif number in overwrite:
            update_candidate(connection, row["id"], wanted)
            result["overwritten"].append(number)
        else:
            result["conflicts"].append({"number": number, "differing_fields": differing})
    return result


def reset(connection: sqlite3.Connection, confirmation: object) -> dict:
    """Remove every candidate except the two migrated legacy ones. Photos are backed up, not deleted."""
    if confirmation != RESET_CONFIRMATION:
        raise RegistryError(400, f"초기화하려면 확인 문구 {RESET_CONFIRMATION}를 입력해야 합니다.", "confirm")
    removed = 0
    for row in connection.execute(
            "SELECT id, photo_file FROM demo_candidates WHERE id NOT IN ('candidate-001','candidate-002')").fetchall():
        backup_photo(row["photo_file"])
        connection.execute("DELETE FROM demo_candidates WHERE id = ?", (row["id"],))
        removed += 1
    return {"removed": removed, "kept": ["candidate-001", "candidate-002"]}
