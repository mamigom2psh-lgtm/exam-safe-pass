import time
import unittest

from mobile_id_provider import (
    MobileIdAuthResult,
    MobileIdProvider,
    MockMobileIdProvider,
    OfficialMobileIdProvider,
    VerifiedLivenessEvidence,
    VerifiedMobileIdEvidence,
)


class FixtureOfficialProvider(OfficialMobileIdProvider):
    name = "official-test"

    def __init__(self, result: MobileIdAuthResult) -> None:
        self.result = result
        self.request_id: str | None = None

    def authenticate(
        self,
        exam_id: str,
        candidate_id: str,
        request_id: str,
    ) -> MobileIdAuthResult:
        self.request_id = request_id
        return self.result


class MockMobileIdProviderTests(unittest.TestCase):
    def setUp(self):
        self.provider = MockMobileIdProvider()

    def test_mock_provider_implements_provider_interface(self):
        self.assertIsInstance(self.provider, MobileIdProvider)

    def test_success_is_explicitly_a_mock_result(self):
        result = self.provider.authenticate("exam-demo-2026", "candidate-001")
        self.assertTrue(result.success)
        self.assertEqual(result.provider, "mock")
        self.assertIn("모의", result.message)

    def test_failure_scenario_does_not_authenticate(self):
        result = MockMobileIdProvider("failure").authenticate(
            "exam-demo-2026", "candidate-001"
        )
        self.assertFalse(result.success)
        self.assertEqual(result.provider, "mock")


class OfficialMobileIdAdapterTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.evidence = VerifiedMobileIdEvidence(
            verification_source="approved-mobile-id-verifier",
            signing_key_id="test-key-id",
            signature_valid=True,
            request_id="server-request-123",
            transaction_id="provider-transaction-123",
            issued_at=self.now - 1,
            expires_at=self.now + 60,
            candidate_id="candidate-001",
        )
        self.result = MobileIdAuthResult(
            success=True,
            provider="official-test",
            message="verified",
            official=True,
            candidate_match=True,
            institution_policy_approved=True,
            verified_candidate_id="candidate-001",
            evidence=self.evidence,
            liveness_evidence=VerifiedLivenessEvidence(
                verification_source="approved-liveness-verifier",
                model_version="validated-server-model-1",
                signature_valid=True,
                request_id="server-request-123",
                transaction_id="liveness-transaction-123",
                issued_at=self.now - 1,
                expires_at=self.now + 60,
                candidate_id="candidate-001",
                score=0.91,
                threshold=0.8,
            ),
        )

    def test_operational_result_requires_signed_current_and_bound_evidence(self):
        self.assertTrue(
            self.result.permits_operational_admission_for(
                "server-request-123",
                "candidate-001",
                self.now,
            )
        )

    def test_official_adapter_receives_server_request_binding(self):
        provider = FixtureOfficialProvider(self.result)
        self.assertIsInstance(provider, MobileIdProvider)
        self.assertIsInstance(provider, OfficialMobileIdProvider)
        result = provider.authenticate(
            "exam-demo-2026",
            "candidate-001",
            "server-request-123",
        )
        self.assertEqual(provider.request_id, "server-request-123")
        self.assertIs(result, self.result)

    def test_unbound_expired_unsigned_or_unmatched_evidence_fails_closed(self):
        self.assertFalse(
            self.result.permits_operational_admission_for(
                "different-request",
                "candidate-001",
                self.now,
            )
        )
        self.assertFalse(
            self.result.permits_operational_admission_for(
                "server-request-123",
                "candidate-001",
                self.now + 61,
            )
        )
        self.assertFalse(
            MobileIdAuthResult(
                **{
                    **self.result.__dict__,
                    "evidence": VerifiedMobileIdEvidence(
                        **{
                            **self.evidence.__dict__,
                            "signature_valid": False,
                        }
                    ),
                }
            ).permits_operational_admission_for(
                "server-request-123",
                "candidate-001",
                self.now,
            )
        )
        self.assertFalse(
            MobileIdAuthResult(
                **{
                    **self.result.__dict__,
                    "verified_candidate_id": "candidate-002",
                }
            ).permits_operational_admission_for(
                "server-request-123",
                "candidate-001",
                self.now,
            )
        )

    def test_bad_expiry_types_are_rejected_without_trusting_provider_data(self):
        evidence = VerifiedMobileIdEvidence(
            **{**self.evidence.__dict__, "expires_at": "never"}
        )
        self.assertFalse(evidence.is_valid_for("server-request-123", self.now))

    def test_operational_result_requires_bound_liveness_evidence_and_pass_score(self):
        liveness = self.result.liveness_evidence
        self.assertIsNotNone(liveness)
        self.assertTrue(
            liveness.is_valid_for("server-request-123", "candidate-001", self.now)
        )
        self.assertFalse(
            liveness.is_valid_for("server-request-123", "candidate-002", self.now)
        )
        low_score = VerifiedLivenessEvidence(
            **{**liveness.__dict__, "score": 0.79}
        )
        self.assertFalse(
            low_score.is_valid_for("server-request-123", "candidate-001", self.now)
        )
        unsigned = VerifiedLivenessEvidence(
            **{**liveness.__dict__, "signature_valid": False}
        )
        self.assertFalse(
            unsigned.is_valid_for("server-request-123", "candidate-001", self.now)
        )
        missing_liveness = MobileIdAuthResult(
            **{**self.result.__dict__, "liveness_evidence": None}
        )
        self.assertFalse(
            missing_liveness.permits_operational_admission_for(
                "server-request-123",
                "candidate-001",
                self.now,
            )
        )


if __name__ == "__main__":
    unittest.main()
