"""Secret loading, password hashing, origin policy and login rate limiting for the DEMO server.

No credential has a built-in default. Admin secrets must come from the environment (Codespaces Secrets or an
untracked .env); the server refuses to start without them or with well-known placeholder values.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

ROLES = ("candidate", "proctor", "admin", "supervisor")
REQUIRED_ROLES = ("admin",)
MIN_KEY_LENGTH = 16
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1

# Values that shipped in earlier DEMO versions or are obvious placeholders; they are rejected at startup.
KNOWN_WEAK_PASSWORDS = (
    "admin-demo-123", "proctor-demo-123", "candidate-demo-123", "supervisor-demo-123",
    "password", "admin", "changeme", "12345678",
)
KNOWN_WEAK_KEYS = (
    "demo-admin-key", "demo-proctor-key", "demo-candidate-key", "demo-supervisor-key",
    "changeme", "change-me", "admin-demo-key",
)
CANDIDATE_ACCOUNT_ID = "candidate-001"


class ConfigError(Exception):
    """Startup configuration is missing or insecure."""


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def parse_hash(stored: str) -> tuple[int, int, int, bytes, bytes] | None:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return None
        return int(n), int(r), int(p), base64.b64decode(salt, validate=True), base64.b64decode(digest, validate=True)
    except (ValueError, TypeError):
        return None


def verify_password(password: str, stored: str) -> bool:
    parsed = parse_hash(stored or "")
    if parsed is None:
        return False
    n, r, p, salt, expected = parsed
    try:
        actual = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=len(expected),
                                maxmem=128 * n * r * 2 + 1024 * 1024)
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines from an untracked .env without overriding real environment variables."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name, value = name.strip(), value.strip()
        if " #" in value and not value.startswith(("'", '"')):
            value = value.split(" #", 1)[0].strip()
        value = value.strip("'\"")
        if name.startswith("EXAM_SAFE_") and value and name not in os.environ:
            os.environ[name] = value


@dataclass
class AuthConfig:
    accounts: dict[str, dict[str, str]] = field(default_factory=dict)
    role_keys: dict[str, str] = field(default_factory=dict)
    allowed_origins: frozenset[str] = frozenset()
    warnings: list[str] = field(default_factory=list)


def normalize_origin(value: str) -> str | None:
    """Return scheme://host[:port] in lower case, or None for anything that is not a plain http(s) origin."""
    parsed = urlparse(value.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    if parsed.path not in ("", "/") or parsed.params or parsed.query or parsed.fragment or parsed.username:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    default = 443 if parsed.scheme == "https" else 80
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"{parsed.scheme}://{host}" + (f":{port}" if port and port != default else "")


def trusted_origins(environ: dict[str, str], port: int) -> frozenset[str]:
    """Explicit allow-list. Host and X-Forwarded-Host request headers are never consulted."""
    origins: set[str] = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    for item in environ.get("EXAM_SAFE_ALLOWED_ORIGINS", "").split(","):
        if item.strip():
            normalized = normalize_origin(item)
            if normalized is None or "*" in item:
                raise ConfigError(f"EXAM_SAFE_ALLOWED_ORIGINS 항목이 올바른 origin(https://host[:port])이 아닙니다: {item.strip()!r}")
            origins.add(normalized)
    name = environ.get("CODESPACE_NAME", "")
    domain = environ.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "")
    if name and domain:
        origins.add(f"https://{name}-{port}.{domain}".lower())
    return frozenset(origins)


def origin_allowed(headers, allowed: frozenset[str]) -> bool:
    """CSRF origin policy for state-changing requests.

    * Origin present: must exactly match the allow-list ("null" and malformed values are rejected).
    * Origin absent but Referer present: the referer's origin must match.
    * Neither present: browsers always send Origin on cross-site writes, so this is a non-browser client; a
      browser hint of Sec-Fetch-Site: cross-site is still rejected. Cookie sessions additionally need the CSRF token.
    """
    origin = headers.get("Origin")
    if origin is not None:
        normalized = normalize_origin(origin)
        return normalized is not None and normalized in allowed
    referer = headers.get("Referer")
    if referer:
        normalized = normalize_origin(f"{urlparse(referer).scheme}://{urlparse(referer).netloc}")
        return normalized is not None and normalized in allowed
    return headers.get("Sec-Fetch-Site", "").lower() != "cross-site"


def _role_env(environ: dict[str, str], role: str, name: str) -> str:
    return environ.get(f"EXAM_SAFE_{role.upper()}_{name}", "").strip()


def load_auth_config(environ: dict[str, str], port: int) -> AuthConfig:
    config = AuthConfig()
    problems: list[str] = []
    for role in ROLES:
        password_hash = _role_env(environ, role, "PASSWORD_HASH")
        key = _role_env(environ, role, "KEY")
        username = _role_env(environ, role, "USERNAME") or f"{role}-demo"
        required = role in REQUIRED_ROLES
        if not password_hash and not key:
            if required:
                problems.append(
                    f"EXAM_SAFE_{role.upper()}_PASSWORD_HASH 와 EXAM_SAFE_{role.upper()}_KEY 가 설정되지 않았습니다."
                )
            continue
        if password_hash:
            if parse_hash(password_hash) is None:
                problems.append(f"EXAM_SAFE_{role.upper()}_PASSWORD_HASH 는 tools/generate_secrets.py 가 만든 scrypt 해시여야 합니다(평문 불가).")
                password_hash = ""
            elif any(verify_password(weak, password_hash) for weak in KNOWN_WEAK_PASSWORDS):
                problems.append(f"EXAM_SAFE_{role.upper()}_PASSWORD_HASH 가 기본/약한 비밀번호의 해시입니다. 새 비밀번호를 사용하세요.")
                password_hash = ""
        elif required:
            problems.append(f"EXAM_SAFE_{role.upper()}_PASSWORD_HASH 가 설정되지 않았습니다.")
        if key:
            if len(key) < MIN_KEY_LENGTH or key.lower() in KNOWN_WEAK_KEYS:
                problems.append(f"EXAM_SAFE_{role.upper()}_KEY 는 {MIN_KEY_LENGTH}자 이상의 무작위 값이어야 하며 기본값을 쓸 수 없습니다.")
                key = ""
            else:
                config.role_keys[role] = key
        elif required:
            problems.append(f"EXAM_SAFE_{role.upper()}_KEY 가 설정되지 않았습니다.")
        account = {"username": username, "password_hash": password_hash}
        if role == "candidate":
            account["candidate_id"] = CANDIDATE_ACCOUNT_ID
        config.accounts[role] = account
    if problems:
        raise ConfigError("\n".join(f"- {problem}" for problem in problems))
    config.allowed_origins = trusted_origins(environ, port)
    for role in ROLES:
        if role not in config.accounts:
            config.warnings.append(f"{role} 역할은 비밀정보가 없어 비활성화되었습니다.")
    return config


class LoginRateLimiter:
    """Sliding-window failure counter per key (client address + role)."""

    def __init__(self, max_failures: int = 5, window_seconds: int = 300) -> None:
        self.max_failures = max(1, max_failures)
        self.window = max(1, window_seconds)
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> list[float]:
        recent = [stamp for stamp in self._failures.get(key, []) if now - stamp < self.window]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return recent

    def retry_after(self, key: str) -> int:
        with self._lock:
            now = time.time()
            recent = self._recent(key, now)
            if len(recent) < self.max_failures:
                return 0
            return max(1, int(recent[0] + self.window - now) + 1)

    def record_failure(self, key: str) -> None:
        with self._lock:
            now = time.time()
            self._recent(key, now)
            self._failures.setdefault(key, []).append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
