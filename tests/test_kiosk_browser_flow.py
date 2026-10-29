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

PROCTOR_PASSWORD = ts.PROCTOR_PASSWORD


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

    def assert_form_layout(self, page):
        for width, height in ((1280, 800), (390, 844), (320, 640)):
            page.set_viewport_size({"width": width, "height": height})
            boxes = page.evaluate("""() => ['virtualCode','demoExam','demoGrade'].map(id => {
                const e = document.getElementById(id), r = e.getBoundingClientRect(), p = e.parentElement.getBoundingClientRect();
                return {id, top: r.top, bottom: r.bottom, w: r.width, pw: p.width, right: r.right, sw: e.scrollWidth, cw: e.clientWidth};
            })""")
            code, exam, grade = boxes
            self.assertLessEqual(code["bottom"], exam["top"], (width, boxes))  # separate rows
            self.assertLessEqual(exam["bottom"], grade["top"], (width, boxes))
            for box in boxes:
                self.assertAlmostEqual(box["w"], box["pw"], delta=1, msg=(width, box))  # 100% of its row
                self.assertLessEqual(box["right"], width, (width, box))
                self.assertLessEqual(box["sw"], box["cw"] + 1, (width, box))  # text not clipped
            overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
            self.assertLessEqual(overflow, 1, width)
        page.set_viewport_size({"width": 1280, "height": 800})

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

    def test_score_gate_then_demo_info_then_simulation_qr(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = self.open_result_screen(browser)
            for score, expected in ((0.499, False), (0.5, True), (0.66, True)):
                page.evaluate("s => applyDemoScreenGate({status:'reference_only', score:s})", score)
                self.assertEqual(page.is_visible("#goDemoInfo"), expected, score)
                self.assertEqual(page.is_visible("#demoInfoGateBelow"), not expected, score)
            for bad in ("{status:'reference_only'}", "{status:'reference_only', score:NaN}", "{status:'face_missing', score:0.9}"):
                page.evaluate(f"applyDemoScreenGate({bad})")
                self.assertFalse(page.is_visible("#goDemoInfo"), bad)
            page.evaluate("applyDemoScreenGate({status:'reference_only', score:0.58})")
            page.click("#goDemoInfo")
            page.wait_for_function("currentScreenId === 'demoInfoScreen'")
            self.assertIn("컴퓨터활용능력", page.inner_text("#demoExam"))
            self.assertEqual(page.eval_on_selector("#demoExam", "e => e.selectedOptions[0].textContent"), "컴퓨터활용능력")
            self.assertEqual(page.inner_text("#demoGrade").split(), ["1급", "2급"])
            self.assertEqual(page.eval_on_selector("#demoGrade", "e => e.value"), "2급")
            page.select_option("#demoExam", label="한국사능력검정시험")
            self.assertEqual(page.inner_text("#demoGrade").split(), ["심화", "기본"])  # only that exam's grades
            page.select_option("#demoExam", label="컴퓨터활용능력")
            self.assertEqual(page.eval_on_selector("#demoGrade", "e => e.value"), "2급")
            self.assert_form_layout(page)
            for code, fragment in (("", "입력해"), ("98032", "형식"), ("990101-1", "일치하지 않습니다")):
                page.fill("#virtualCode", code)
                page.click("#demoInfoSubmit")
                page.wait_for_function("f => document.getElementById('demoInfoStatus').textContent.includes(f)", arg=fragment)
                self.assertEqual(page.evaluate("currentScreenId"), "demoInfoScreen")
            self.assertEqual(self.db_count("demo_tokens"), 0)
            page.fill("#virtualCode", "980320-2")
            page.click("#demoInfoSubmit")
            self.assert_demo_qr_shown(page, simulation=True)
            self.assertIn("공식 신분 인증 아님", page.inner_text("#qrLabel"))
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
