"""Provider boundary for future approved self-service identity verification."""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class SelfIdentityResult:
    status: str
    provider: str
    official: bool
    candidate_match: bool | None
    institution_policy_approved: bool
    message: str

    @property
    def permits_automatic_admission(self) -> bool:
        return (
            self.status == "success"
            and self.official
            and self.candidate_match is True
            and self.institution_policy_approved
        )


class SelfIdentityProvider(ABC):
    @abstractmethod
    def verify(self, exam_id: str, candidate_id: str) -> SelfIdentityResult:
        """Verify identity through an explicitly configured provider."""


class UnavailableSelfIdentityProvider(SelfIdentityProvider):
    def verify(self, exam_id: str, candidate_id: str) -> SelfIdentityResult:
        return SelfIdentityResult(
            status="uncertain",
            provider="unconfigured",
            official=False,
            candidate_match=None,
            institution_policy_approved=False,
            message="공식 신원확인 provider가 설정되지 않았습니다.",
        )


class MockSelfIdentityProvider(SelfIdentityProvider):
    """Produces explicitly simulated outcomes; never authorizes real admission."""

    def __init__(self, outcome: str) -> None:
        if outcome not in {"success", "failure", "uncertain", "mismatch"}:
            raise ValueError("unsupported self identity demo outcome")
        self.outcome = outcome

    def verify(self, exam_id: str, candidate_id: str) -> SelfIdentityResult:
        messages = {
            "success": "시연용 성공 선택이며 얼굴 비교나 실제 신원확인이 아닙니다.",
            "failure": "시연용 인증 실패 결과입니다.",
            "uncertain": "시연용 신원확인 불확실 결과입니다.",
            "mismatch": "시연용 응시자 불일치 결과입니다.",
        }
        return SelfIdentityResult(
            status=self.outcome,
            provider="demo-only",
            official=False,
            candidate_match=None,
            institution_policy_approved=False,
            message=messages[self.outcome],
        )
