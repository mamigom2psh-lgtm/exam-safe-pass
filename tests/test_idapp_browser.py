"""Headless check of the kiosk ID·DEMO app link screen (pairing code -> app token -> manual QR text -> DEMO check).

Real phone, camera QR scanning and the Android app are NOT exercised.
"""
import json
import tempfile
import unittest
import urllib.request

from tests.test_judge_mode import JudgeServer, PASSWORD
from tests.test_idapp_link import SECRET

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None


@unittest.skipIf(sync_playwright is None, "playwright is not installed")
class IdappKioskBrowserTests(unittest.TestCase):
    def test_pairing_code_screen_and_manual_qr_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = JudgeServer(tmp, EXAM_SAFE_IDAPP_SECRET=SECRET)
            base = f"http://127.0.0.1:{server.port}"
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch()
                    page = browser.new_context(viewport={"width": 375, "height": 667}).new_page()
                    errors = []
                    page.on("pageerror", lambda e: errors.append(str(e)))
                    page.goto(base + "/kiosk")
                    page.fill("#judgePass", PASSWORD)
                    page.click("#loginForm button[type=submit]")
                    page.wait_for_url("**/kiosk")
                    page.wait_for_selector("#judgeBanner:not([hidden])")
                    page.click("#selfModeButton")
                    page.fill("#candidateNumber", "2026-0013")
                    page.click("#lookupButton")
                    page.wait_for_selector("#skipCameraSim", state="visible", timeout=15000)
                    page.evaluate("showScreen('idappScreen')")
                    page.click("#idappNewCode")
                    page.wait_for_function("/^[A-Z2-9]{8}$/.test(document.getElementById('idappCode').textContent)")
                    code = page.inner_text("#idappCode")
                    page.fill("#idappManual", "DEMO-ONLY:IDAPP:" + "a" * 43)
                    page.click("#idappManualSubmit")
                    page.wait_for_selector("#idappStatus.bad")
                    request = urllib.request.Request(
                        base + "/api/idapp/token", data=json.dumps({"pairing_code": code}).encode(),
                        headers={"Content-Type": "application/json"}, method="POST")
                    token = json.load(urllib.request.urlopen(request))["qr_payload"]
                    page.fill("#idappManual", token)
                    page.click("#idappManualSubmit")
                    page.wait_for_selector("#idappStatus.good")
                    self.assertIn("공식 신분 인증이나 입실 승인이 아닙니다", page.inner_text("#idappStatus"))
                    self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
                    self.assertEqual(errors, [])
                    browser.close()
            finally:
                server.stop()


if __name__ == "__main__":
    unittest.main()
