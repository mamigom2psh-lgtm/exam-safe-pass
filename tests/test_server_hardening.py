import sqlite3
import unittest

import test_scenarios as ts
import verification_policy as vp
import server


class VerificationPolicyTests(unittest.TestCase):
    def test_operational_verified_requires_trusted_evidence_and_policy(self):
        for evidence in (False, True):
            for policy in (False, True):
                with self.subTest(evidence=evidence, policy=policy):
                    result = vp.resolve_state(
                        "operational",
                        server_outcome="success",
                        trusted_evidence_valid=evidence,
                        institution_policy_approved=policy,
                    )
                    self.assertEqual(result["state"] == "verified", evidence and policy)
                    self.assertEqual(result["real_authentication"], evidence and policy)

    def test_demo_success_is_never_real_authentication(self):
        result = vp.resolve_state("demo", server_outcome="success")
        self.assertEqual(result["state"], "verified")
        self.assertFalse(result["real_authentication"])

    def test_states_are_distinct_and_failures_need_supervisor(self):
        self.assertEqual(vp.resolve_state("demo")["state"], "pending")
        self.assertEqual(vp.resolve_state("demo", server_outcome="failure")["state"], "rejected")
        uncertain = vp.resolve_state("demo", server_outcome="uncertain")
        self.assertEqual(uncertain["state"], "inconclusive")
        self.assertTrue(uncertain["supervisor_required"])
        self.assertEqual(vp.resolve_state("operational")["state"], "pending")
        self.assertEqual(vp.resolve_state("unknown")["state"], "inconclusive")

    def test_resolver_has_no_browser_score_inputs(self):
        with self.assertRaises(TypeError):
            vp.resolve_state("operational", face_match=True)
        with self.assertRaises(TypeError):
            vp.resolve_state("operational", liveness_score=0.99)

    def test_session_without_trusted_provider_is_never_operationally_verified(self):
        for state in ("qr_ready", "completed", "anything"):
            self.assertNotEqual(
                vp.describe_session({"state": state})["state"], "verified", state
            )

    def test_audit_redaction_removes_identifiers_images_and_vectors(self):
        vector = ",".join(f"{i}.123456" for i in range(32))
        text = vp.redact_details(
            f"rrn 900101-1234567 img data:image/png;base64,{'A' * 50} emb {vector} {'B' * 300}"
        )
        self.assertNotIn("1234567", text)
        self.assertNotIn("base64", text)
        self.assertNotIn("0.123456,1.123456", text)
        self.assertNotIn("B" * 200, text)
        self.assertLessEqual(len(text), vp.MAX_DETAILS_LENGTH)

    def test_write_event_stores_only_redacted_details(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE events (event_id INTEGER PRIMARY KEY AUTOINCREMENT, occurred_at TEXT,"
            " event TEXT, outcome TEXT, exam_id TEXT, candidate_id TEXT, actor TEXT,"
            " details TEXT, domain TEXT, auth_path TEXT)"
        )
        server.write_event(
            connection, "DEMO 확인", "blocked", details="id 900101-1234567 authentication_path=self_demo"
        )
        row = connection.execute("SELECT * FROM events").fetchone()
        self.assertNotIn("1234567", row["details"])
        self.assertEqual((row["domain"], row["auth_path"]), ("demo", "self_demo"))


class RetentionTests(unittest.TestCase):
    def test_old_events_are_purged_and_recent_ones_kept(self):
        from datetime import datetime, timedelta, timezone

        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE events (occurred_at TEXT)")
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=server.EVENT_RETENTION_DAYS + 1)).isoformat(timespec="seconds")
        connection.execute("INSERT INTO events VALUES (?)", (old,))
        connection.execute("INSERT INTO events VALUES (?)", (now.isoformat(timespec="seconds"),))
        self.assertEqual(server.purge_old_events(connection, now), 1)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)


class HardeningScenarios(unittest.TestCase):
    setUpClass = classmethod(ts.ServerScenarios.setUpClass.__func__)
    tearDownClass = classmethod(ts.ServerScenarios.tearDownClass.__func__)
    setUp = ts.ServerScenarios.setUp

    def test_forged_browser_assertions_cannot_issue_operational_token_or_admission(self):
        forged = {
            "exam_id": ts.EXAM_ID,
            "candidate_id": ts.CAN_1,
            "face_match": True,
            "liveness": True,
            "face_similarity": 0.99,
            "score": 0.99,
            "auth_session_id": "forged-session",
            "authentication_success": True,
        }
        status, body = self.api.request("POST", "/api/token", forged)
        self.assertGreaterEqual(status, 400, body)
        self.assertNotIn("token", body)
        self.assertNotIn("qr_png", body)
        status, body = self.api.request("POST", "/api/approve", forged, "proctor", ts.PROCTOR_KEY)
        self.assertGreaterEqual(status, 400, body)
        status, body = self.api.request(
            "POST", "/api/mobile-id/official-authenticate", forged
        )
        self.assertGreaterEqual(status, 400, body)
        self.assertFalse(body.get("success", False))
        _, dashboard = self.api.request("GET", "/api/dashboard", role="admin", key=ts.ADMIN_KEY)
        self.assertEqual(dashboard["admitted_count"], 0)

    def test_kiosk_status_reports_demo_verification_separately(self):
        _, created = self.api.create_kiosk_session()
        session_id = created["kiosk_session_id"]
        self.api.lookup_kiosk_candidate(session_id, mode="self_demo")
        pending = self.api.kiosk_status(session_id)[1]["verification"]
        self.assertEqual((pending["domain"], pending["state"]), ("demo", "pending"))
        self.assertEqual(self.api.kiosk_self_demo_authenticate(session_id)[0], 200)
        verified = self.api.kiosk_status(session_id)[1]["verification"]
        self.assertEqual((verified["domain"], verified["state"]), ("demo", "verified"))
        self.assertFalse(verified["real_authentication"])

    def test_uncertain_self_demo_routes_to_supervisor(self):
        _, created = self.api.create_kiosk_session()
        session_id = created["kiosk_session_id"]
        self.api.lookup_kiosk_candidate(session_id, mode="self_demo")
        self.api.kiosk_self_auth(session_id, "uncertain")
        verification = self.api.kiosk_status(session_id)[1]["verification"]
        self.assertEqual(verification["state"], "inconclusive")
        self.assertTrue(verification["supervisor_required"])
        self.assertFalse(verification["real_authentication"])

    def test_dashboard_filters_separate_demo_and_mask_candidate(self):
        _, created = self.api.create_kiosk_session()
        session_id = created["kiosk_session_id"]
        self.api.lookup_kiosk_candidate(session_id, mode="self_demo")
        self.api.kiosk_self_demo_authenticate(session_id)
        self.api.request("POST", "/api/token", {"exam_id": ts.EXAM_ID, "candidate_id": ts.CAN_1})

        def query(qs):
            status, body = self.api.request(
                "GET", f"/api/dashboard?{qs}", role="admin", key=ts.ADMIN_KEY
            )
            self.assertEqual(status, 200, body)
            return body["events"]

        demo = query("domain=demo")
        self.assertTrue(demo)
        self.assertTrue(all(item["domain"] == "demo" for item in demo))
        self.assertTrue(
            all(item["candidate_id"] in (None, "") or item["candidate_id"].startswith("***") for item in demo)
        )
        self.assertTrue(all(item["domain"] == "operational" for item in query("domain=operational")))
        self.assertTrue(query("auth_path=self_demo"))
        self.assertEqual(query("event=ZZZ-no-such-event"), [])
        self.assertEqual(len(query("limit=1")), 1)
        self.assertEqual(query("since=2999-01-01T00:00:00"), [])

    def test_dashboard_rejects_invalid_filters(self):
        for qs in ("domain=prod", "limit=abc", "limit=0", "limit=9999", "since=yesterday",
                   "until=2026-13-45", "auth_path=a%27%3BDROP", "actor=x%20y"):
            with self.subTest(qs=qs):
                status, _ = self.api.request(
                    "GET", f"/api/dashboard?{qs}", role="admin", key=ts.ADMIN_KEY)
                self.assertEqual(status, 400)

    def test_dashboard_event_search_treats_wildcards_literally(self):
        status, body = self.api.request(
            "GET", "/api/dashboard?event=%25", role="admin", key=ts.ADMIN_KEY)
        self.assertEqual(status, 200)
        self.assertEqual(body["events"], [])

    def test_dashboard_requires_admin_role(self):
        status, _ = self.api.request("GET", "/api/dashboard")
        self.assertEqual(status, 403)
        status, _ = self.api.request(
            "GET", "/api/dashboard?domain=demo", role="proctor", key=ts.PROCTOR_KEY
        )
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
