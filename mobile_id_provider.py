"""Provider boundary for mobile identity authentication demonstrations."""

import math
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class VerifiedMobileIdEvidence:
    verification_source: str
    signing_key_id: str
    signature_valid: bool
    request_id: str
    transaction_id: str
    issued_at: float
    expires_at: float
    candidate_id: str

    def is_valid_for(self, request_id: str, now: float) -> bool:
        return (
            self.signature_valid is True
            and isinstance(self.verification_source, str)
            and bool(self.verification_source.strip())
            and isinstance(self.signing_key_id, str)
            and bool(self.signing_key_id.strip())
            and isinstance(self.transaction_id, str)
            and bool(self.transaction_id.strip())
            and isinstance(self.request_id, str)
            and self.request_id == request_id
            and isinstance(self.candidate_id, str)
            and bool(self.candidate_id.strip())
            and isinstance(self.issued_at, (int, float))
            and isinstance(self.expires_at, (int, float))
            and math.isfinite(self.issued_at)
            and math.isfinite(self.expires_at)
            and self.issued_at <= now
            and self.expires_at > now
            and self.expires_at > self.issued_at
        )


@dataclass(frozen=True)
class VerifiedLivenessEvidence:
    verification_source: str
    model_version: str
    signature_valid: bool
    request_id: str
    transaction_id: str
    issued_at: float
    expires_at: float
    candidate_id: str
    score: float
    threshold: float

    def is_valid_for(
        self,
        request_id: str,
        candidate_id: str,
        now: float,
    ) -> bool:
        return (
            self.signature_valid is True
            and isinstance(self.verification_source, str)
            and bool(self.verification_source.strip())
            and isinstance(self.model_version, str)
            and bool(self.model_version.strip())
            and self.request_id == request_id
            and self.transaction_id
            and self.candidate_id == candidate_id
            and isinstance(self.issued_at, (int, float))
            and isinstance(self.expires_at, (int, float))
            and isinstance(self.score, (int, float))
            and isinstance(self.threshold, (int, float))
            and math.isfinite(self.issued_at)
            and math.isfinite(self.expires_at)
            and math.isfinite(self.score)
            and math.isfinite(self.threshold)
            and 0 <= self.score <= 1
            and 0 <= self.threshold <= 1
            and self.score >= self.threshold
            and self.issued_at <= now
            and self.expires_at > now
            and self.expires_at > self.issued_at
        )


@dataclass(frozen=True)
class MobileIdAuthResult:
    success: bool
    provider: str
    message: str
    official: bool = False
    candidate_match: bool | None = None
    institution_policy_approved: bool = False
    verified_candidate_id: str | None = None
    evidence: VerifiedMobileIdEvidence | None = None
    liveness_evidence: VerifiedLivenessEvidence | None = None

    @property
    def permits_operational_admission(self) -> bool:
        return self.permits_operational_admission_for(
            self.evidence.request_id if self.evidence else "",
            self.evidence.candidate_id if self.evidence else "",
            time.time(),
        )

    def permits_operational_admission_for(
        self,
        request_id: str,
        candidate_id: str,
        now: float,
    ) -> bool:
        return (
            self.success
            and self.official
            and self.candidate_match is True
            and self.institution_policy_approved
            and self.provider != "mock"
            and self.evidence is not None
            and self.evidence.is_valid_for(request_id, now)
            and self.verified_candidate_id == self.evidence.candidate_id
            and self.evidence.candidate_id == candidate_id
            and self.liveness_evidence is not None
            and self.liveness_evidence.is_valid_for(
                request_id,
                candidate_id,
                now,
            )
        )


class MobileIdProvider(ABC):
    @abstractmethod
    def authenticate(
        self,
        exam_id: str,
        candidate_id: str,
    ) -> MobileIdAuthResult:
        """Return a provider result without exposing provider credentials."""


class OfficialMobileIdProvider(MobileIdProvider):
    """Server-side adapter contract for a future approved official integration."""

    @abstractmethod
    def authenticate(
        self,
        exam_id: str,
        candidate_id: str,
        request_id: str,
    ) -> MobileIdAuthResult:
        """Verify the signed provider response and bind it to this request."""


class MockMobileIdProvider(MobileIdProvider):
    name = "mock"

    def __init__(self, demo_outcome: str = "success") -> None:
        if demo_outcome not in {"success", "failure"}:
            raise ValueError("demo_outcome must be 'success' or 'failure'")
        self.demo_outcome = demo_outcome

    def authenticate(
        self,
        exam_id: str,
        candidate_id: str,
    ) -> MobileIdAuthResult:
        if self.demo_outcome == "failure":
            return MobileIdAuthResult(False, self.name, "모의 모바일신분증 인증 실패 시나리오입니다.")
        return MobileIdAuthResult(True, self.name, "모의 모바일신분증 인증 성공 시나리오입니다.")
