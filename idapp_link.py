"""ID·DEMO app link: one-time pairing codes and short-lived DEMO ID-QR tokens.

Flow: kiosk asks for a pairing code (bound to its server session) -> the phone app redeems the code once for a
30 s single-use DEMO token -> the kiosk submits the scanned token -> the server marks the session as
"idapp_demo_checked". That check is DEMO-only: it never creates `verified`, an entry QR or an admission.

Only HMAC digests (keyed with a server-side secret from the environment) are stored; neither the pairing code, the
token nor the kiosk session id is kept in clear text. The token carries no personal data and no long-lived key.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3

PREFIX = "DEMO-ONLY:IDAPP:"
PAIRING_TTL_SECONDS = 60
TOKEN_TTL_SECONDS = 30
CHECK_VALID_SECONDS = 300
MIN_SECRET_LENGTH = 32
CODE_LENGTH = 8
# No 0/O/1/I/L: the code is read from a kiosk screen and typed on a phone.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_RE = re.compile(rf"[{CODE_ALPHABET}]{{{CODE_LENGTH}}}")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{40,64}")

SCHEMA = """
CREATE TABLE IF NOT EXISTS idapp_pairings (
    code_hash TEXT PRIMARY KEY,
    session_hash TEXT NOT NULL,
    exam_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    issued_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL
);
CREATE TABLE IF NOT EXISTS idapp_tokens (
    token_hash TEXT PRIMARY KEY,
    session_hash TEXT NOT NULL,
    exam_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    issued_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL
);
"""


def secret_from_env(environ: dict[str, str]) -> bytes | None:
    """The link is disabled (fail closed) unless a strong server-side secret is configured."""
    value = environ.get("EXAM_SAFE_IDAPP_SECRET", "").strip()
    if len(value) < MIN_SECRET_LENGTH or len(set(value)) < 8:
        return None
    return value.encode("utf-8")


def init_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


def _digest(secret: bytes, kind: str, value: str) -> str:
    return hmac.new(secret, f"{kind}|{value}".encode("utf-8"), hashlib.sha256).hexdigest()


def normalize_code(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    code = re.sub(r"[\s-]", "", value).upper()
    return code if _CODE_RE.fullmatch(code) else None


def create_pairing(connection: sqlite3.Connection, secret: bytes, session_id: str, exam_id: str,
                   candidate_id: str, now: float) -> str:
    session_hash = _digest(secret, "session", session_id)
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    connection.execute("BEGIN IMMEDIATE")
    purge(connection, now)
    # A new code supersedes every unused older code/token of the same kiosk session.
    for table in ("idapp_pairings", "idapp_tokens"):
        connection.execute(f"UPDATE {table} SET used_at = ? WHERE session_hash = ? AND used_at IS NULL",
                           (now, session_hash))
    connection.execute(
        "INSERT INTO idapp_pairings (code_hash, session_hash, exam_id, candidate_id, issued_at, expires_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (_digest(secret, "code", code), session_hash, exam_id, candidate_id, now, now + PAIRING_TTL_SECONDS),
    )
    connection.commit()
    return code


def redeem_pairing(connection: sqlite3.Connection, secret: bytes, code_value: object, now: float,
                   candidate_active) -> tuple[str, str | None]:
    """Returns (status, token). status: ok | invalid | expired | used | candidate_inactive."""
    code = normalize_code(code_value)
    if code is None:
        return "invalid", None
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute("SELECT * FROM idapp_pairings WHERE code_hash = ?",
                                 (_digest(secret, "code", code),)).fetchone()
        if row is None:
            return "invalid", None
        if row["used_at"] is not None:
            return "used", None
        # Consume first: an expired or orphaned code can never be retried.
        connection.execute("UPDATE idapp_pairings SET used_at = ? WHERE code_hash = ? AND used_at IS NULL",
                           (now, row["code_hash"]))
        if row["expires_at"] <= now:
            return "expired", None
        if not candidate_active(row["exam_id"], row["candidate_id"]):
            return "candidate_inactive", None
        token = f"{PREFIX}{secrets.token_urlsafe(32)}"
        connection.execute(
            "INSERT INTO idapp_tokens (token_hash, session_hash, exam_id, candidate_id, issued_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (_digest(secret, "token", token), row["session_hash"], row["exam_id"], row["candidate_id"], now,
             now + TOKEN_TTL_SECONDS),
        )
        return "ok", token
    finally:
        connection.commit()


def verify_token(connection: sqlite3.Connection, secret: bytes, session_id: str, exam_id: str,
                 candidate_id: str, payload: object, now: float, candidate_active) -> str:
    """status: ok | invalid | expired | used | mismatch | candidate_inactive. Only `ok` consumes the token."""
    if not isinstance(payload, str) or not payload.startswith(PREFIX) or not _TOKEN_RE.fullmatch(payload[len(PREFIX):]):
        return "invalid"
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute("SELECT * FROM idapp_tokens WHERE token_hash = ?",
                                 (_digest(secret, "token", payload),)).fetchone()
        if row is None:
            return "invalid"
        same_session = hmac.compare_digest(row["session_hash"], _digest(secret, "session", session_id))
        if not same_session or row["exam_id"] != exam_id or row["candidate_id"] != candidate_id:
            return "mismatch"
        if row["used_at"] is not None:
            return "used"
        if row["expires_at"] <= now:
            return "expired"
        if not candidate_active(exam_id, candidate_id):
            return "candidate_inactive"
        updated = connection.execute("UPDATE idapp_tokens SET used_at = ? WHERE token_hash = ? AND used_at IS NULL",
                                     (now, row["token_hash"]))
        return "ok" if updated.rowcount == 1 else "used"
    finally:
        connection.commit()


def revoke_inactive(connection: sqlite3.Connection, active_candidate_ids: set[str], now: float) -> None:
    marks = ",".join("?" for _ in active_candidate_ids)
    clause = f"AND candidate_id NOT IN ({marks})" if active_candidate_ids else ""
    for table in ("idapp_pairings", "idapp_tokens"):
        connection.execute(f"UPDATE {table} SET used_at = ? WHERE used_at IS NULL {clause}",
                           (now, *active_candidate_ids))


def purge(connection: sqlite3.Connection, now: float, keep_seconds: int = 3600) -> None:
    for table in ("idapp_pairings", "idapp_tokens"):
        connection.execute(f"DELETE FROM {table} WHERE expires_at < ?", (now - keep_seconds,))


def clear(connection: sqlite3.Connection) -> None:
    for table in ("idapp_pairings", "idapp_tokens"):
        connection.execute(f"DELETE FROM {table}")
