import unittest

from self_identity_provider import (
    MockSelfIdentityProvider,
    SelfIdentityResult,
    UnavailableSelfIdentityProvider,
)


class SelfIdentityProviderTests(unittest.TestCase):
    def test_demo_success_never_meets_real_admission_gate(self):
        result = MockSelfIdentityProvider("success").verify(
            "exam-demo-2026", "candidate-001"
        )
        self.assertEqual(result.status, "success")
        self.assertFalse(result.official)
        self.assertIsNone(result.candidate_match)
        self.assertFalse(result.institution_policy_approved)
        self.assertFalse(result.permits_automatic_admission)

    def test_unconfigured_provider_fails_closed(self):
        result = UnavailableSelfIdentityProvider().verify(
            "exam-demo-2026", "candidate-001"
        )
        self.assertEqual(result.status, "uncertain")
        self.assertFalse(result.permits_automatic_admission)

    def test_automatic_gate_requires_official_policy_and_candidate_match(self):
        complete = SelfIdentityResult(
            status="success",
            provider="approved-test-provider",
            official=True,
            candidate_match=True,
            institution_policy_approved=True,
            message="test result",
        )
        self.assertTrue(complete.permits_automatic_admission)
        for field in ("official", "candidate_match", "institution_policy_approved"):
            values = {
                "status": "success",
                "provider": "approved-test-provider",
                "official": True,
                "candidate_match": True,
                "institution_policy_approved": True,
                "message": "test result",
            }
            values[field] = False
            with self.subTest(field=field):
                self.assertFalse(SelfIdentityResult(**values).permits_automatic_admission)


if __name__ == "__main__":
    unittest.main()
