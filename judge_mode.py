"""Judge-only (공모전 심사위원 전용) deployment mode.

Enabled with EXAM_SAFE_JUDGE_MODE=1. In this mode the server is a closed, fictional sandbox:

* one shared judge password (from an environment variable, never from code) opens a short-lived session,
* every page and API except /healthz and the login page needs that session,
* administrator, proctor and operational routes are not reachable at all,
* the registry holds only the five virtual candidates with generated cartoon avatars - no real photos,
* nothing here can mark a person as verified; QRs stay DEMO-only (30 s, single use, "not valid for entry").
"""
from __future__ import annotations

import io
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import auth_config
import demo_candidates

MIN_PASSWORD_LENGTH = 12
DEFAULT_SESSION_SECONDS = 2 * 60 * 60
ROLE = "judge"
PUBLIC_GET_PATHS = ("/healthz", "/judge-login")
LOGIN_PATH = "/api/judge/login"
DEFAULT_DATABASE_NAME = "exam_safe_pass.sqlite3"

# Every other path (admin registry, /demo-candidates, /demo-scenarios, operational token/approve/inspect APIs,
# /api/demo/*, /api/login ...) is intentionally absent and therefore denied even for a logged-in judge.
_GET_EXACT = {"/", "/kiosk", "/face_similarity.js", "/api/judge/session"}
_GET_PREFIXES = ("/vendor/", "/api/candidate-photo/", "/api/kiosk/")
_POST_EXACT = {"/api/judge/logout", "/api/judge/reset"}
_POST_PREFIXES = ("/api/kiosk/",)

_FILL = [
    ("#cfe3f7", "#2f6db5"), ("#f7e0cf", "#c0602a"), ("#dff3d9", "#3d8a2e"),
    ("#ecdcf6", "#7a3fa8"), ("#fbf0c4", "#b08a00"),
]


@dataclass
class JudgeConfig:
    password_hash: str
    session_seconds: int
    client_max_failures: int
    global_max_failures: int
    window_seconds: int
    trust_proxy: bool


def enabled(environ: dict[str, str]) -> bool:
    return environ.get("EXAM_SAFE_JUDGE_MODE", "").strip() == "1"


def load_config(environ: dict[str, str], *, database: Path, photo_dir: Path, root: Path) -> JudgeConfig:
    """Fail closed: refuse to start without a strong judge password or when pointed at development data."""
    problems: list[str] = []
    stored = environ.get("EXAM_SAFE_JUDGE_PASSWORD_HASH", "").strip()
    plain = environ.get("EXAM_SAFE_JUDGE_PASSWORD", "")
    if stored:
        if auth_config.parse_hash(stored) is None:
            problems.append("EXAM_SAFE_JUDGE_PASSWORD_HASH 형식이 올바르지 않습니다(scrypt$...).")
    elif plain:
        if len(plain) < MIN_PASSWORD_LENGTH:
            problems.append(f"EXAM_SAFE_JUDGE_PASSWORD 는 {MIN_PASSWORD_LENGTH}자 이상이어야 합니다.")
        elif plain.lower() in auth_config.KNOWN_WEAK_PASSWORDS or plain.strip() != plain:
            problems.append("EXAM_SAFE_JUDGE_PASSWORD 가 약하거나 알려진 기본값입니다.")
        else:
            stored = auth_config.hash_password(plain)  # only the hash is kept in memory
    else:
        problems.append("EXAM_SAFE_JUDGE_PASSWORD(또는 EXAM_SAFE_JUDGE_PASSWORD_HASH)가 설정되지 않았습니다.")

    if database.resolve() == (root / DEFAULT_DATABASE_NAME).resolve():
        problems.append("심사용 모드는 개발용 DB(exam_safe_pass.sqlite3)를 사용할 수 없습니다. EXAM_SAFE_DB 를 임시 경로로 지정하세요.")
    for protected in (root / "images", root / "demo_photos"):
        if photo_dir.resolve() == protected.resolve():
            problems.append("심사용 모드는 개발용 사진 폴더를 사용할 수 없습니다. EXAM_SAFE_PHOTO_DIR 를 임시 경로로 지정하세요.")
    if problems:
        raise auth_config.ConfigError("\n".join(f"- {item}" for item in problems))
    return JudgeConfig(
        password_hash=stored,
        session_seconds=max(300, int(environ.get("EXAM_SAFE_JUDGE_SESSION_SECONDS", DEFAULT_SESSION_SECONDS))),
        client_max_failures=int(environ.get("EXAM_SAFE_LOGIN_MAX_FAILURES", "5")),
        global_max_failures=int(environ.get("EXAM_SAFE_JUDGE_GLOBAL_MAX_FAILURES", "40")),
        window_seconds=int(environ.get("EXAM_SAFE_LOGIN_WINDOW_SECONDS", "300")),
        trust_proxy=environ.get("EXAM_SAFE_TRUST_PROXY", "") == "1",
    )


def render_origin(environ: dict[str, str]) -> str | None:
    """Render sets RENDER_EXTERNAL_URL for web services; it is an operator-controlled value, not a request header."""
    value = environ.get("RENDER_EXTERNAL_URL", "").strip()
    return auth_config.normalize_origin(value) if value else None


def route_allowed(method: str, path: str) -> bool:
    if method == "GET":
        return path in _GET_EXACT or path.startswith(_GET_PREFIXES)
    if method == "POST":
        return path in _POST_EXACT or path.startswith(_POST_PREFIXES)
    return False


def client_key(headers, address: str, trust_proxy: bool) -> str:
    """Last X-Forwarded-For hop (appended by the platform proxy) is used only when the operator trusts the proxy."""
    if trust_proxy:
        hops = [hop.strip() for hop in headers.get("X-Forwarded-For", "").split(",") if hop.strip()]
        if hops and re.fullmatch(r"[0-9a-fA-F:.]{3,45}", hops[-1]):
            return hops[-1]
    return address


def make_avatar(index: int) -> bytes:
    """Generated cartoon avatar (plain shapes). It is not a photograph and contains no biometric data."""
    from PIL import Image, ImageDraw

    background, accent = _FILL[index % len(_FILL)]
    image = Image.new("RGB", (400, 400), background)
    draw = ImageDraw.Draw(image)
    draw.ellipse((120, 70, 280, 250), fill="#f2d3b3", outline=accent, width=6)
    draw.pieslice((110, 50, 290, 200), 180, 360, fill=accent)
    draw.ellipse((165, 140, 185, 160), fill="#1d1d1d")
    draw.ellipse((215, 140, 235, 160), fill="#1d1d1d")
    draw.arc((165, 175, 235, 225), 20, 160, fill="#8a3b3b", width=5)
    draw.rounded_rectangle((90, 270, 310, 400), radius=50, fill=accent)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def seed_virtual_candidates(connection) -> dict:
    """Idempotently create the five virtual candidates and attach a generated avatar to each (when missing)."""
    result = demo_candidates.seed(connection)
    for index, (number, *_rest) in enumerate(demo_candidates.SEED_CANDIDATES):
        row = connection.execute(
            "SELECT id, photo_file FROM demo_candidates WHERE number_key = ?",
            (demo_candidates.normalize_number(number),),
        ).fetchone()
        if row and not row["photo_file"]:
            demo_candidates.set_photo(connection, row["id"], make_avatar(index))
    return result


def roster_for_banner() -> list[dict]:
    return [
        {"number": number, "name": name, "virtual_code": code}
        for number, name, code, _exam, _grade in demo_candidates.SEED_CANDIDATES
    ]


def new_session_tokens() -> tuple[str, str]:
    return secrets.token_urlsafe(32), secrets.token_urlsafe(24)


def wants_html(headers, path: str) -> bool:
    return not path.startswith("/api/") and "text/html" in headers.get("Accept", "")


def safe_path(raw: str) -> str:
    return urlparse(raw).path
