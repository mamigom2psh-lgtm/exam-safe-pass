"""Headless-browser regression for the kiosk result -> QR transitions.

Camera and Human models are not exercised: the test jumps to the result screen the
same way analysis finishes. Skipped (reported, not hidden) if Playwright/Chromium is absent.
"""
import sqlite3
import unittest

import test_scenarios as ts

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

PROCTOR_PASSWORD = "proctor-demo-123"


@unittest.skipIf(sync_playwright is None, "playwright is not installed")
class KioskBrowserFlowTests(unittest.TestCase):
    setUpClass = classmethod(ts.ServerScenarios.setUpClass.__func__)
    tearDownClass = classmethod(ts.ServerScenarios.tearDownClass.__func__)
    setUp = ts.ServerScenarios.setUp

    def open_result_screen(self, browser):
        page = browser.new_context().new_page()
        self.errors = []
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.goto(self.api.base_url + "/kiosk")
        page.click("#selfModeButton")
        page.fill("#candidateNumber", "20260012")
        page.click("#lookupButton")
        page.wait_for_function("currentScreenId === 'selfCameraScreen'", timeout=5000)
        page.evaluate("showScreen('selfResultScreen')")
        return page

    def assert_demo_qr_shown(self, page, simulation):
        page.wait_for_function("currentScreenId === 'qrScreen'", timeout=8000)
        page.wait_for_selector("#entryQr", state="visible", timeout=5000)
        self.assertRegex(page.inner_text("#qrCountdown"), r"유효시간 \d+초")
        self.assertTrue(page.is_visible("#printTicketButton"))
        label = page.inner_text("#qrLabel")
        self.assertIn("DEMO 전용 QR · 실제 입실 불가", label)
        self.assertEqual("자동 발급 시연" in label, simulation)
        self.assertIn("공모전 시제품 · 실제 입실 불가", page.text_content("#ticketDemoFoot"))
        self.assertEqual(self.errors, [])

    def db_count(self, table):
        with sqlite3.connect(self.db_path) as db:
            return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_a_supervisor_request_then_inline_approval_shows_demo_qr(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = self.open_result_screen(browser)
            self.assertNotIn("본인이 아닙니다", page.content())
            page.click("#faceHelp")
            page.wait_for_function("currentScreenId === 'proctorWaitScreen'", timeout=5000)
            self.assertEqual(self.db_count("tokens"), 0)
            page.fill("#ipPass", "wrong-password")
            page.click("#inlineProctorForm button[type=submit]")
            page.wait_for_selector("#inlineProctorStatus:not([hidden])")
            self.assertIn("승인되지 않았습니다", page.inner_text("#inlineProctorStatus"))
            self.assertEqual(self.db_count("demo_tokens"), 0)
            page.fill("#ipPass", PROCTOR_PASSWORD)
            page.click("#inlineProctorForm button[type=submit]")
            self.assert_demo_qr_shown(page, simulation=False)
            self.assertEqual(self.db_count("tokens"), 0)  # operational QR untouched
            browser.close()

    def test_b_auto_issue_demo_asks_for_server_login_then_shows_simulation_qr(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = self.open_result_screen(browser)
            page.click("#autoIssueDemo")
            page.wait_for_selector("#simStatus:not([hidden])")
            self.assertIn("로그인이 필요", page.inner_text("#simStatus"))
            self.assertTrue(page.is_visible("#simLoginForm"))
            self.assertEqual(self.db_count("demo_tokens"), 0)
            page.fill("#simPass", PROCTOR_PASSWORD)
            page.click("#simLoginForm button[type=submit]")
            self.assert_demo_qr_shown(page, simulation=True)
            self.assertEqual(self.db_count("tokens"), 0)
            browser.close()

    def test_rejected_review_never_shows_qr(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = self.open_result_screen(browser)
            page.click("#faceHelp")
            page.wait_for_function("currentScreenId === 'proctorWaitScreen'", timeout=5000)
            page.fill("#ipPass", PROCTOR_PASSWORD)
            page.select_option("#ipResult", "suspected_proxy")
            page.click("#inlineProctorForm button[type=submit]")
            page.wait_for_selector("#inlineProctorStatus:not([hidden])")
            page.wait_for_timeout(1500)
            self.assertFalse(page.is_visible("#entryQr"))
            self.assertFalse(page.is_visible("#printTicketButton"))
            self.assertEqual(self.db_count("demo_tokens"), 0)
            browser.close()


if __name__ == "__main__":
    unittest.main()
