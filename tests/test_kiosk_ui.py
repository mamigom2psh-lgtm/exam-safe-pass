import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class KioskUiGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = (ROOT / "kiosk.html").read_text(encoding="utf-8")

    def test_paper_ticket_supports_a4_and_receipt_with_demo_notice(self):
        self.assertIn('<option value="a4">A4</option>', self.page)
        self.assertIn('value="receipt"', self.page)
        self.assertIn("@page receipt{size:80mm auto", self.page)
        self.assertIn("공모전 시제품 · 실제 입실 불가", self.page)

    def test_print_layout_repeats_demo_notice_and_hides_page_chrome(self):
        self.assertGreaterEqual(self.page.count("공모전 시제품 · 실제 입실 불가"), 2)
        self.assertIn('id="ticketDemoFoot"', self.page)
        self.assertIn('body[data-print-size="receipt"] .ticket-layout img{width:58mm}', self.page)
        self.assertIn('body[data-print-size="a4"] .ticket-demo', self.page)
        self.assertIn("break-inside:avoid", self.page)

    def test_model_load_always_awaits_load_promise(self):
        self.assertNotIn("if(faceModel)return faceModel;", self.page)

    def test_accessibility_controls_and_keyboard_skip_link(self):
        for needle in ('id="toggleLargeText"', 'id="toggleHighContrast"', 'class="skip-link"', "aria-pressed"):
            self.assertIn(needle, self.page)

    def test_api_calls_time_out_and_report_network_errors_without_success(self):
        self.assertIn("AbortController", self.page)
        self.assertIn("네트워크에 연결할 수 없습니다", self.page)
        self.assertIn("인증은 완료되지 않았습니다", self.page)

    def test_face_result_is_labelled_inconclusive_and_never_auto_issues_qr(self):
        self.assertIn("browserVerificationState", self.page)
        self.assertIn("감독관 확인이 필요합니다", self.page)
        self.assertIn("$('completeSelfDemo').hidden=true", self.page)

    def test_analysis_has_timeout_so_loading_cannot_hang(self):
        self.assertIn("ANALYSIS_TIMEOUT_MS", self.page)
        self.assertIn("분석 시간 초과", self.page)

    def test_admin_page_has_event_search_filters(self):
        admin = (ROOT / "index.html").read_text(encoding="utf-8")
        for element_id in ("filterDomain", "filterEvent", "filterAuthPath", "filterOutcome", "filterSince", "filterUntil"):
            self.assertIn(f'id="{element_id}"', admin)


if __name__ == "__main__":
    unittest.main()
