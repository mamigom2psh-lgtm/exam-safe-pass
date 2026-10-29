"""Verification state model that keeps DEMO outcomes apart from operational ones.

The inputs are deliberately server-side facts only. Browser face similarity,
liveness scores and similar client assertions are not parameters, so they cannot
influence the resulting state.
"""

from __future__ import annotations

import re
from typing import Any

STATES = ("pending", "verified", "rejected", "inconclusive")
DOMAINS = ("demo", "operational")

_PENDING_SESSION_STATES = {"start", "awaiting_self_auth", "not_found"}
_DEMO_SUCCESS_SESSION_STATES = {"demo_authenticated", "demo_qr_ready", "demo_completed"}
_REJECTED_SESSION_STATES = {"rejected"}
_INCONCLUSIVE_SESSION_STATES = {
    "awaiting_proctor",
    "supervisor_pending",
    "expired",
    "verification_expired",
    "duplicate",
}


def resolve_state(
    domain: str,
    *,
    server_outcome: str | None = None,
    trusted_evidence_valid: bool = False,
    institution_policy_approved: bool = False,
) -> dict[str, Any]:
    """Return the verification state for a domain from server-side facts only.

    Operational ``verified`` needs both a trusted server-side evidence check and
    an approved institution policy. Without them the result can never be
    ``verified``.
    """
    if domain not in DOMAINS:
        return _result("demo", "inconclusive")
    if domain == "demo":
        # A DEMO success is a staged scenario, never a real authentication.
        state = {
            "success": "verified",
            "failure": "rejected",
            "mismatch": "rejected",
            "uncertain": "inconclusive",
        }.get(server_outcome or "", "pending")
        return _result("demo", state)
    if server_outcome in {"failure", "mismatch"}:
        return _result("operational", "rejected")
    if trusted_evidence_valid and institution_policy_approved and server_outcome == "success":
        return _result("operational", "verified")
    if server_outcome is None and not trusted_evidence_valid:
        return _result("operational", "pending")
    return _result("operational", "inconclusive")


def describe_session(session: dict[str, Any]) -> dict[str, Any]:
    """Map a kiosk session to a verification summary without trusting the browser."""
    state = session.get("state")
    if state in _DEMO_SUCCESS_SESSION_STATES:
        return resolve_state("demo", server_outcome="success")
    if state in _REJECTED_SESSION_STATES:
        return resolve_state("demo", server_outcome="failure")
    if state in _INCONCLUSIVE_SESSION_STATES:
        return resolve_state("demo", server_outcome="uncertain")
    if state in _PENDING_SESSION_STATES:
        return resolve_state("demo")
    # Operational QR states never become verified without the trusted provider path.
    return resolve_state("operational", server_outcome="uncertain")


def _result(domain: str, state: str) -> dict[str, Any]:
    return {
        "domain": domain,
        "state": state,
        "real_authentication": domain == "operational" and state == "verified",
        "supervisor_required": state in {"rejected", "inconclusive"},
    }


_SENSITIVE_PATTERNS = (
    (re.compile(r"\b\d{6}\s*-?\s*[1-8]\d{6}\b"), "[REDACTED-RRN]"),
    (re.compile(r"data:image/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=]+", re.I), "[REDACTED-IMAGE]"),
    (re.compile(r"[A-Za-z0-9+/=_-]{200,}"), "[REDACTED-BLOB]"),
    (re.compile(r"(?:-?\d+\.\d+\s*,\s*){15,}-?\d+\.\d+"), "[REDACTED-VECTOR]"),
)
MAX_DETAILS_LENGTH = 500


def redact_details(text: str) -> str:
    """Remove identifiers, image data and biometric vectors from audit text."""
    for pattern, replacement in _SENSITIVE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text[:MAX_DETAILS_LENGTH]
