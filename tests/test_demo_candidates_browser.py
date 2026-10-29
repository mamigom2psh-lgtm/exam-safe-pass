"""Headless-browser checks for the admin candidate screen and the kiosk flow with a per-candidate photo.

Camera and Human models are not exercised. Skipped (reported) if Playwright/Chromium is absent.
"""
import tempfile
import unittest
from pathlib import Path

import test_scenarios as ts
from tests.test_demo_candidates import make_png

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None


@unittest.skipIf(sync_playwright is None, "playwright is not installed")
class DemoCandidatesBrowserTests(unittest.TestCase):
    setUpClass = classmethod(ts.ServerScenarios.setUpClass.__func__)
    tearDownClass = classmethod(ts.ServerScenarios.tearDownClass.__func__)
    setUp = ts.ServerScenarios.setUp

    def admin_page(self, browser):
        page = browser.new_context().new_page()
        self.errors = []
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.goto(self.api.base_url + "/demo-candidates")
        self.assertTrue(page.is_hidden("#adminArea"))  # nothing is shown before the server accepts the login
        page.fill("#adminUser", "admin-demo")
        page.fill("#adminPass", "wrong")
        page.click("#loginForm button[type=submit]")
        page.wait_for_selector("#status.bad")
        self.assertTrue(page.is_hidden("#adminArea"))
        page.fill("#adminPass", "admin-demo-123")
        page.click("#loginForm button[type=submit]")
        page.wait_for_selector("#adminArea:not([hidden])")
        return page

    def test_admin_adds_candidate_with_cascading_exam_fields_and_uploads_photo(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = self.admin_page(browser)
            page.click("#seedButton")
            page.wait_for_function("document.getElementById('status').textContent.includes('시드 등록 완료')")
            rows = page.inner_text("#candidateRows")
            for name in ("김하늘", "이준호", "최서연", "정민수", "박세현"):
                self.assertIn(name, rows)
            page.fill("#fName", "브라우저")
            page.fill("#fNumber", "2027-9500")
            page.fill("#fCode", "990101-1")
            page.select_option("#fKind", label="역사·교양 검정")
            self.assertEqual(page.inner_text("#fExam").strip(), "한국사능력검정시험")
            self.assertEqual(page.inner_text("#fGrade").split(), ["심화", "기본"])
            page.select_option("#fKind", label="사무·IT 자격")
            page.select_option("#fExam", label="컴퓨터활용능력")
            self.assertEqual(page.inner_text("#fGrade").split(), ["1급", "2급"])
            page.select_option("#fGrade", "1급")
            page.click("#saveButton")
            page.wait_for_function("document.getElementById('candidateRows').textContent.includes('2027-9500')")
            page.fill("#fNumber", "2027-9500")  # duplicate must be refused by the server
            page.fill("#fName", "중복")
            page.fill("#fCode", "990101-1")
            page.click("#saveButton")
            page.wait_for_selector("#status.bad")
            self.assertIn("이미 등록된 수험번호", page.inner_text("#status"))
            self.assertNotIn("중복", page.inner_text("#candidateRows"))
            with tempfile.TemporaryDirectory() as tmp:
                png = Path(tmp) / "x.png"
                png.write_bytes(make_png())
                row = page.locator("#candidateRows tr", has_text="2027-9500")
                with page.expect_file_chooser() as chooser:
                    row.get_by_role("button", name="사진").click()
                chooser.value.set_files(str(png))
            page.wait_for_selector("#candidateRows tr:has-text('2027-9500') img.thumb")
            page.wait_for_function("document.querySelector('#candidateRows img.thumb').naturalWidth > 0")
            self.assertEqual(self.errors, [])
            browser.close()

    def test_kiosk_loads_only_the_selected_candidates_photo_as_in_memory_blob(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = self.admin_page(browser)
            page.click("#seedButton")
            page.wait_for_function("document.getElementById('status').textContent.includes('시드 등록 완료')")
            with tempfile.TemporaryDirectory() as tmp:
                png = Path(tmp) / "a.png"
                png.write_bytes(make_png(shade=60))
                with page.expect_file_chooser() as chooser:
                    page.locator("#candidateRows tr", has_text="2026-0013").get_by_role("button", name="사진").click()
                chooser.value.set_files(str(png))
            page.wait_for_selector("#candidateRows tr:has-text('2026-0013') img.thumb")
            kiosk = browser.new_context().new_page()
            kiosk.goto(self.api.base_url + "/kiosk")
            kiosk.click("#selfModeButton")
            kiosk.fill("#candidateNumber", "20260013")
            kiosk.click("#lookupButton")
            kiosk.wait_for_function("currentScreenId === 'selfCameraScreen'", timeout=5000)
            self.assertTrue(kiosk.evaluate("document.getElementById('registeredFacePhoto').src.startsWith('blob:')"))
            self.assertGreater(kiosk.evaluate("document.getElementById('registeredFacePhoto').naturalWidth"), 0)
            self.assertIn("김하늘", kiosk.inner_text("#candidateInfo"))
            kiosk2 = browser.new_context().new_page()  # candidate without a photo: clear message, no stale image
            kiosk2.goto(self.api.base_url + "/kiosk")
            kiosk2.click("#selfModeButton")
            kiosk2.fill("#candidateNumber", "20260014")
            kiosk2.click("#lookupButton")
            kiosk2.wait_for_function("currentScreenId === 'selfCameraScreen'", timeout=5000)
            self.assertIn("등록 사진이 없어", kiosk2.inner_text("#cameraStatus"))
            self.assertTrue(kiosk2.is_hidden("#registeredFacePhoto"))
            browser.close()


if __name__ == "__main__":
    unittest.main()
