"""Headless mobile-viewport walk through the judge login and the camera-free preset simulation.

Camera, Human models and printing are not exercised. Skipped (reported) if Playwright/Chromium is absent.
"""
import tempfile
import unittest

from tests.test_judge_mode import JudgeServer, PASSWORD

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None


@unittest.skipIf(sync_playwright is None, "playwright is not installed")
class JudgeBrowserTests(unittest.TestCase):
    def test_mobile_login_then_camera_free_demo_qr(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = JudgeServer(tmp)
            base = f"http://127.0.0.1:{server.port}"
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch()
                    page = browser.new_context(viewport={"width": 375, "height": 667}).new_page()
                    errors = []
                    page.on("pageerror", lambda e: errors.append(str(e)))
                    page.goto(base + "/kiosk")
                    self.assertTrue(page.url.endswith("/judge-login"))
                    self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
                    page.fill("#judgePass", "wrong-password-123")
                    page.click("#loginForm button[type=submit]")
                    page.wait_for_selector("#status:not([hidden])")
                    page.fill("#judgePass", PASSWORD)
                    page.click("#loginForm button[type=submit]")
                    page.wait_for_url("**/kiosk")
                    page.wait_for_selector("#judgeBanner:not([hidden])")
                    self.assertIn("실제 입실 불가", page.inner_text("#judgeBanner"))
                    page.click("#selfModeButton")
                    page.fill("#candidateNumber", "2026-0013")
                    page.click("#lookupButton")
                    page.wait_for_selector("#skipCameraSim", state="visible", timeout=15000)
                    page.click("#skipCameraSim")
                    page.wait_for_selector("#autoIssueDemo", state="visible", timeout=15000)
                    page.click("#autoIssueDemo")
                    page.wait_for_selector("#entryQr:not([hidden])", timeout=15000)
                    self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
                    self.assertEqual(errors, [])
                    browser.close()
            finally:
                server.stop()


if __name__ == "__main__":
    unittest.main()
