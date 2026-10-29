import unittest

import demo_scenarios as ds

APPROVE = {"decision": "approve", "reason": "normal", "document_match": True, "live_face_match": True}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def fake_qr(token):
    return "QR-" + str(len(token))


class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.sb = ds.ScenarioSandbox(qr_factory=fake_qr, clock=self.clock)

    def run_to_review(self, scenario="normal_entry"):
        run = self.sb.start(scenario)
        rid = run["run_id"]
        for action in ("lookup", "capture", "analyze"):
            run = self.sb.act(rid, action)
        self.assertEqual(run["state"], "review")
        return rid

    def status_of(self, rid, action, params=None):
        try:
            self.sb.act(rid, action, params)
        except ds.ScenarioError as exc:
            return exc.status
        return 200

    def test_every_response_is_demo_only(self):
        run = self.sb.start("normal_entry")
        self.assertTrue(run["demo_only"])
        self.assertFalse(run["real_authentication"])
        self.assertEqual(run["banner"], "공모전 시제품 · 실제 입실 불가")
        for item in self.sb.list_scenarios():
            self.assertIn("실제 입실 불가", item["banner"])

    def test_normal_flow_enters_once(self):
        rid = self.run_to_review()
        self.assertEqual(self.sb.act(rid, "review", APPROVE)["state"], "approved")
        issued = self.sb.act(rid, "issue_qr")
        self.assertTrue(issued["qr_issued"])
        self.assertEqual(self.sb.act(rid, "enter")["state"], "entered")
        self.assertEqual(self.status_of(rid, "enter"), 409)

    def test_analysis_is_preset_and_not_authentication(self):
        rid = self.run_to_review()
        analysis = self.sb.act(rid, "review", APPROVE)["analysis"]
        self.assertEqual(analysis["source"], "preset_demo")
        self.assertFalse(analysis["real_model"])
        self.assertFalse(analysis["is_authentication"])

    def test_no_qr_without_review_approval(self):
        rid = self.run_to_review()
        self.assertEqual(self.status_of(rid, "issue_qr"), 409)
        reject = {"decision": "reject", "reason": "suspected_proxy", "document_match": False, "live_face_match": False}
        self.assertEqual(self.sb.act(rid, "review", reject)["state"], "rejected")
        self.assertEqual(self.status_of(rid, "issue_qr"), 409)

    def test_approve_without_matches_is_downgraded(self):
        for doc, live in ((True, False), (False, True), (False, False)):
            rid = self.run_to_review()
            params = dict(APPROVE, document_match=doc, live_face_match=live)
            state = self.sb.act(rid, "review", params)["state"]
            self.assertNotEqual(state, "approved")
            self.assertEqual(self.status_of(rid, "issue_qr"), 409)

    def test_suspicious_preset_requires_normal_reason(self):
        rid = self.run_to_review("spoof_suspected")
        params = dict(APPROVE, reason="identity_unverifiable")
        self.assertNotEqual(self.sb.act(rid, "review", params)["state"], "approved")

    def test_duplicate_issue_is_blocked_without_new_token(self):
        rid = self.run_to_review()
        self.sb.act(rid, "review", APPROVE)
        self.assertTrue(self.sb.act(rid, "issue_qr")["qr_issued"])
        digest = self.sb._runs[rid]["qr"]["hash"]
        self.assertEqual(self.status_of(rid, "issue_qr"), 409)
        self.assertTrue(self.sb.act(rid, "reprint")["qr_issued"])
        self.assertEqual(self.sb._runs[rid]["qr"]["hash"], digest)
        accepted = [e for e in self.sb.events() if e["event"] == "DEMO QR 발급" and e["outcome"] == "accepted"]
        self.assertEqual(len(accepted), 1)

    def test_qr_token_not_stored_in_plaintext(self):
        rid = self.run_to_review()
        self.sb.act(rid, "review", APPROVE)
        self.sb.act(rid, "issue_qr")
        self.assertNotIn("DEMO-ONLY", repr(self.sb._runs[rid]["qr"]))

    def test_expiry_blocks_entry(self):
        rid = self.run_to_review("expired_qr")
        self.sb.act(rid, "review", APPROVE)
        self.sb.act(rid, "issue_qr")
        self.sb.act(rid, "advance_time", {"seconds": ds.QR_TTL_SECONDS + 5})
        self.assertEqual(self.status_of(rid, "enter"), 410)

    def test_time_shift_bounds(self):
        rid = self.run_to_review()
        for seconds in (0, -5, ds.MAX_TIME_SHIFT_SECONDS + 1, "9", True, None):
            self.assertEqual(self.status_of(rid, "advance_time", {"seconds": seconds}), 400, seconds)

    def test_forged_browser_fields_rejected(self):
        rid = self.run_to_review()
        for field in ("face_match", "liveness", "score", "verified", "state", "approved"):
            self.assertEqual(self.status_of(rid, "review", dict(APPROVE, **{field: True})), 400, field)
        self.assertEqual(self.status_of(rid, "issue_qr", {"face_match": True}), 400)
        self.assertEqual(self.status_of(rid, "bogus"), 400)
        self.assertEqual(self.status_of(rid, "enter", "x"), 400)

    def test_unknown_run_and_scenario(self):
        self.assertEqual(self.status_of("nope", "lookup"), 404)
        with self.assertRaises(ds.ScenarioError) as ctx:
            self.sb.start("operational")
        self.assertEqual(ctx.exception.status, 400)

    def test_failure_recovery_never_auto_approves(self):
        run = self.sb.start("failure_recovery")
        rid = run["run_id"]
        self.sb.act(rid, "lookup")
        run = self.sb.act(rid, "fail", {"kind": "camera_denied"})
        self.assertEqual(run["state"], "recovery_needed")
        self.assertEqual(self.status_of(rid, "capture"), 409)
        run = self.sb.act(rid, "recover", {"choice": "retry"})
        self.assertEqual(run["state"], "capture")
        self.sb.act(rid, "fail", {"kind": "network"})
        run = self.sb.act(rid, "recover", {"choice": "supervisor"})
        self.assertEqual(run["state"], "supervisor_pending")
        self.assertEqual(self.status_of(rid, "issue_qr"), 409)
        self.assertFalse(run["qr_issued"])

    def test_print_failure_reprints_same_qr(self):
        rid = self.run_to_review("failure_recovery")
        self.assertEqual(self.status_of(rid, "fail", {"kind": "print_failure"}), 409)
        self.sb.act(rid, "review", APPROVE)
        self.sb.act(rid, "issue_qr")
        self.sb.act(rid, "fail", {"kind": "print_failure"})
        digest = self.sb._runs[rid]["qr"]["hash"]
        self.sb.act(rid, "reprint")
        self.assertEqual(self.sb._runs[rid]["qr"]["hash"], digest)
        self.assertEqual(self.status_of(rid, "issue_qr"), 409)

    def test_restart_and_reset_clear_state(self):
        rid = self.run_to_review()
        self.sb.act(rid, "review", APPROVE)
        self.sb.act(rid, "issue_qr")
        self.sb.act(rid, "enter")
        new_rid = self.sb.start("normal_entry")["run_id"]
        self.assertNotEqual(rid, new_rid)
        self.assertEqual(self.status_of(rid, "enter"), 404)
        self.sb.act(new_rid, "lookup")
        self.sb.reset()
        self.assertEqual(self.sb.events(), [])
        self.assertEqual(self.status_of(new_rid, "capture"), 404)
        self.assertEqual(self.sb._admitted, set())

    def test_events_are_isolated_domain_and_free_of_secrets(self):
        rid = self.run_to_review()
        self.sb.act(rid, "review", APPROVE)
        self.sb.act(rid, "issue_qr")
        events = self.sb.events()
        self.assertTrue(events)
        for event in events:
            self.assertEqual(event["domain"], "demo_scenario")
            self.assertNotIn("DEMO-ONLY", repr(event))

    def test_reused_qr_scenario_blocks_second_entry(self):
        rid = self.run_to_review("reused_qr")
        self.sb.act(rid, "review", APPROVE)
        self.sb.act(rid, "issue_qr")
        self.sb.act(rid, "enter")
        self.assertEqual(self.status_of(rid, "enter"), 409)


if __name__ == "__main__":
    unittest.main()
