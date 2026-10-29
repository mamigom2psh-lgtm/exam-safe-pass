import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ExistingPrototypeRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = (ROOT / "index.html").read_text(encoding="utf-8")

    def test_existing_four_tabs_and_panels_remain(self):
        for tab in ("candidate", "proctor", "security", "admin"):
            with self.subTest(tab=tab):
                self.assertIn(f'data-tab="{tab}"', self.page)
                self.assertIn(f'id="{tab}"', self.page)

    def test_candidate_issue_and_reset_controls_remain(self):
        for element_id in ("issue", "reset", "candidateStatus"):
            with self.subTest(element_id=element_id):
                self.assertIn(f'id="{element_id}"', self.page)
        self.assertIn("모의 인증 및 QR 발급", self.page)
        self.assertIn("초기화", self.page)
        self.assertIn('id="tokenQr"', self.page)
        self.assertIn('id="tokenCountdown"', self.page)
        self.assertIn('id="mockAuthOutcome"', self.page)

    def test_manual_proctor_review_remains(self):
        self.assertIn('id="approve"', self.page)
        self.assertIn('id="reject"', self.page)
        self.assertIn('id="face"', self.page)
        self.assertIn('id="rosterMatch"', self.page)
        self.assertIn('value="match"', self.page)
        self.assertIn('value="mismatch"', self.page)
        self.assertIn("명부", self.page)
        self.assertIn('id="startScanner"', self.page)
        self.assertIn('id="manualToken"', self.page)
        self.assertIn('id="verifyToken"', self.page)

    def test_photo_scenarios_and_images_remain(self):
        for scenario in ("normal", "impostor", "alternate"):
            self.assertIn(f'value="{scenario}"', self.page)
        for image in ("candidate.jpg", "candidate2.png", "impostor.png"):
            with self.subTest(image=image):
                self.assertTrue((ROOT / "images" / image).is_file())
                self.assertIn(image, self.page)

    def test_existing_security_cases_remain(self):
        for scenario in ("other", "expired", "replay", "duplicate", "offline"):
            with self.subTest(scenario=scenario):
                self.assertIn(f'data-test="{scenario}"', self.page)

    def test_admin_dashboard_and_event_table_remain(self):
        for element_id in ("countAdmitted", "countBlocked", "logs"):
            with self.subTest(element_id=element_id):
                self.assertIn(f'id="{element_id}"', self.page)
        self.assertIn("관리자 대시보드", self.page)

    def test_scanner_and_manual_fallback_guidance_remain(self):
        for guidance in (
            "카메라 권한이 거부되었거나 브라우저 정책으로 차단되었습니다.",
            "아직 QR을 인식하지 못했습니다.",
            "수동 토큰 입력",
        ):
            with self.subTest(guidance=guidance):
                self.assertIn(guidance, self.page)

    def test_qr_scan_has_local_decoder_and_image_upload_fallback(self):
        self.assertIn('<script src="/vendor/qr-decoder.min.js"></script>', self.page)
        self.assertIn('id="qrImageFile"', self.page)
        self.assertIn("navigator.mediaDevices.getUserMedia", self.page)
        self.assertIn("window.isSecureContext", self.page)
        self.assertIn("useNativeBarcodeDetector", self.page)
        self.assertIn("const qrDecoderReady=", self.page)
        scanner_start = self.page.index("async function startQrScanner()")
        decoder_gate = self.page.index("if(!qrDecoderReady)", scanner_start)
        camera_request = self.page.index("navigator.mediaDevices.getUserMedia", scanner_start)
        self.assertLess(decoder_gate, camera_request)
        self.assertIn("카메라 스캔은 시작하지 않습니다.", self.page)
        self.assertIn("decodeQrFromSource(image,'image')", self.page)
        self.assertIn("decodeQrFromSource(canvas,'canvas')", self.page)
        self.assertIn("getTracks().forEach(track=>track.stop())", self.page)
        decoder = (ROOT / "vendor" / "qr-decoder-entry.js").read_text()
        self.assertIn("decodeFromCanvas", decoder)
        self.assertIn("decodeFromImageElement", decoder)
        self.assertNotRegex(self.page, r"<script[^>]+src=[\"']https?://")
        self.assertNotRegex(self.page, r"console\.(?:log|debug|info)")

    def test_qr_decoder_bundle_and_license_notices_are_present(self):
        self.assertTrue((ROOT / "vendor" / "qr-decoder.min.js").is_file())
        for license_file in (
            "ZXING-BROWSER-LICENSE.txt",
            "ZXING-LIBRARY-LICENSE.txt",
            "ZXING-TEXT-ENCODING-LICENSE.txt",
            "TS-CUSTOM-ERROR-LICENSE.txt",
        ):
            with self.subTest(license_file=license_file):
                self.assertTrue((ROOT / "vendor" / license_file).is_file())

    def test_every_demo_panel_labels_mock_auth_status(self):
        for panel_id in ("candidate", "proctor", "supervisor", "security", "admin"):
            start = self.page.index(f'id="{panel_id}"')
            panel = self.page[start:self.page.index("</section>", start)]
            self.assertIn("공모전 시제품 · 실제 모바일신분증 인증 아님", panel)
            self.assertIn("실제 신원인증 아님", panel)

    def test_plus_authentication_choices_and_nonstorage_copy_are_present(self):
        for method in ("mobile", "physical", "exception"):
            with self.subTest(method=method):
                self.assertIn(f'data-method="{method}"', self.page)
        for control in (
            "physicalDocumentType",
            "physicalDocumentMatch",
            "physicalPhotoMatch",
            "physicalFaceMatch",
            "submitPhysical",
            "holdIdentity",
            "loginSupervisor",
            "approveException",
            "exceptionApplication",
            "exceptionWitnesses",
        ):
            with self.subTest(control=control):
                self.assertIn(f'id="{control}"', self.page)
        self.assertIn("신분증 사진 및 민감한 개인정보는 저장하지 않습니다.", self.page)
        self.assertIn("실제 신원인증 아님", self.page)
        self.assertIn("공식 기관의 승인/절차 없이 실제 입실을 허용하지 않습니다.", self.page)

    def test_existing_proctor_screen_has_role_gated_kiosk_review(self):
        for element_id in ("refreshKioskRequests", "kioskRequests", "kioskRequestStatus"):
            with self.subTest(element_id=element_id):
                self.assertIn(f'id="{element_id}"', self.page)
        self.assertIn("/api/kiosk/physical-review", self.page)
        self.assertIn("/api/kiosk/requests", self.page)
        self.assertIn("/api/kiosk/refresh-qr", self.page)
        kiosk = (ROOT / "kiosk.html").read_text(encoding="utf-8")
        for wording in (
            "셀프 인증 시작",
            "감독관 인증",
            "관리자 도움 요청",
            "수험번호 조회",
            "얼굴 유사도 및 위조 탐지 PoC",
            "얼굴 비교 결과",
            "Human Antispoof PoC",
            "DEMO 전용 QR · 실제 입실 불가",
            "세션 종료",
            "30초",
            "모의 입실 승인으로 진행",
        ):
            with self.subTest(wording=wording):
                self.assertIn(wording, kiosk)
        self.assertIn("setTimeout", kiosk)
        self.assertIn("/api/kiosk/end", kiosk)
        self.assertNotIn("/api/kiosk/demo-qr", kiosk)
        self.assertIn("qrDataUrl", kiosk)
        self.assertIn("data:${mimeType};base64,${base64}", kiosk)
        self.assertIn("/api/kiosk/self-demo/refresh-qr", kiosk)
        self.assertIn('id="reissueSelfQr"', kiosk)
        self.assertIn("/api/kiosk/self-demo/complete", kiosk)
        self.assertIn("/api/kiosk/self-demo/issue-ticket", kiosk)

    def test_kiosk_self_demo_and_proctor_modes_are_separated(self):
        kiosk = (ROOT / "kiosk.html").read_text(encoding="utf-8")
        for control in ("selfModeButton", "proctorModeButton", "facePreview", "registeredFacePhoto", "cameraContinue", "completeSelfDemo"):
            with self.subTest(control=control):
                self.assertIn(f'id="{control}"', kiosk)
        for control in ("selfDemoSuccess", "selfDemoFailure", "selfDemoUncertain", "selfDemoMismatch", "previewDemoQr"):
            self.assertNotIn(f'id="{control}"', kiosk)
        for text in ("얼굴 비교 참고값", "본인/타인을 판정하지 않았습니다", "인증 상태: ${browserState.state}", "서버는 실제 본인 여부나 실재성을 검증하지 않습니다.", "실제 입실 기록은 생성되지 않았습니다."):
            self.assertIn(text, kiosk)
        self.assertIn("navigator.mediaDevices.getUserMedia", kiosk)
        self.assertIn("getTracks().forEach(track=>track.stop())", kiosk)
        self.assertIn("model.detect(reference)", kiosk)
        self.assertIn("model.detect(frame)", kiosk)
        self.assertIn("ExamFaceSimilarity.evaluate", kiosk)
        self.assertIn("ExamFaceSimilarity.evaluateLiveness", kiosk)
        self.assertIn("MULTI_FRAME_COUNT=8", kiosk)
        self.assertIn("MULTI_FRAME_INTERVAL_MS=350", kiosk)
        self.assertIn("ANALYSIS_TIMEOUT_MS=15000", kiosk)
        self.assertIn("awaitAnalysisStep", kiosk)
        self.assertIn("분석 시간 초과", kiosk)
        self.assertIn("평균·통합 판정에 사용하지 않으며", kiosk)
        self.assertIn("antispoof:{enabled:true,modelPath:'antispoof.json'", kiosk)
        self.assertIn("capturedLivenessResult='liveness_unavailable'", kiosk)
        self.assertIn("얼굴 유사도 및 위조 탐지 PoC (DEMO)", kiosk)
        self.assertIn("참고용 얼굴 유사도 분석 결과 (DEMO)", kiosk)
        self.assertIn("aria-busy", kiosk)
        self.assertIn("cameraErrorMessage(error.name)", kiosk)
        self.assertIn("비교가 완료되기 전에는 결과 단계로 진행되지 않습니다.", kiosk)
        self.assertIn("comparison.status==='reference_only'", kiosk)
        self.assertNotIn("/api/kiosk/self-auth", kiosk)
        self.assertIn("demo_outcome:'success'", kiosk)
        analysis = kiosk[kiosk.index("$('cameraContinue').onclick"):kiosk.index("$('completeSelfDemo').onclick")]
        self.assertLess(analysis.index("model.detect(reference)"), analysis.index("showScreen('selfResultScreen')"))
        self.assertIn("showScreen('selfResultScreen')", analysis[analysis.index("finally"):])
        self.assertIn("얼굴 비교 분석에 실패했습니다. 이는 타인이라는 판정이 아닙니다.", analysis)
        self.assertIn("cameraContinue').disabled=false", analysis)
        demo_request = kiosk[kiosk.index("$('completeSelfDemo').onclick"):kiosk.index("$('retryFaceCapture').onclick")]
        self.assertIn("demo_outcome:'success'", demo_request)
        self.assertNotRegex(demo_request, r"(?:face_score|face_similarity|face_match|captured_face_result)")
        self.assertNotIn("issue-ticket", analysis)
        self.assertIn("sessionStorage.setItem(SESSION_KEY,kioskSessionId)", kiosk)
        self.assertNotIn('id="residentNumber"', kiosk)
        self.assertNotIn('name="residentNumber"', kiosk)

    def test_antispoof_model_is_local_and_fail_closed(self):
        kiosk = (ROOT / "kiosk.html").read_text(encoding="utf-8")
        server = (ROOT / "server.py").read_text(encoding="utf-8")
        model_dir = ROOT / "vendor" / "human" / "models"
        self.assertTrue((model_dir / "antispoof.json").is_file())
        self.assertTrue((model_dir / "antispoof.bin").is_file())
        self.assertTrue((model_dir / "MODEL-SOURCES.txt").is_file())
        self.assertIn('"human/models/antispoof.json"', server)
        self.assertIn('"human/models/antispoof.bin"', server)
        self.assertIn("liveness_unavailable", kiosk)
        self.assertIn("livenessResult", kiosk)
        self.assertNotIn("face_score", kiosk)
        self.assertNotRegex(kiosk, r"<script[^>]+src=[\"']https?://")

    def test_antispoof_diagnostics_report_real_runtime_steps_without_persisting_images(self):
        kiosk = (ROOT / "kiosk.html").read_text(encoding="utf-8")
        for element_id in (
            "antiSpoofDiagnostics",
            "diagManifest",
            "diagWeights",
            "diagLoad",
            "diagModel",
            "diagFrame",
            "diagMultiFrame",
            "diagFaceCount",
            "diagReferenceInput",
            "diagReferenceBox",
            "diagReferenceEmbedding",
            "diagLiveInput",
            "diagLiveBox",
            "diagLiveEmbedding",
            "diagDistance",
            "diagFormula",
            "diagScore",
            "diagVerdict",
        ):
            with self.subTest(element_id=element_id):
                self.assertIn(f'id="{element_id}"', kiosk)
        self.assertIn("fetch(manifestUrl", kiosk)
        self.assertIn("fetch(weightUrl", kiosk)
        self.assertIn("faceModel.load().then", kiosk)
        self.assertIn("!stats?.loaded", kiosk)
        self.assertIn("!antiSpoofModel?.executor", kiosk)
        self.assertIn("model.detect(frame)", kiosk)
        self.assertIn("model.match.distance", kiosk)
        self.assertIn("formatFaceDiagnostics", kiosk)
        self.assertIn("embeddingDimension", kiosk)
        self.assertIn("distance=round(100×25×Σ(diff²))/100", kiosk)
        self.assertIn("similarity×100", kiosk)
        self.assertIn("detectedFaces[0]?.real", kiosk)
        self.assertIn("Number.isFinite(livenessSummary.scoreMin)", kiosk)
        self.assertIn("score:`점수 없음 / ${ANTISPOOF_THRESHOLD.toFixed(2)}`", kiosk)
        self.assertIn("개발자 진단 · DEMO 전용", kiosk)
        self.assertIn("if(currentMode!=='self_demo')return", kiosk)
        self.assertIn("얼굴 이미지, 특징값, 응시자 정보는 표시하거나 저장하지 않습니다.", kiosk)
        self.assertNotIn("localStorage.setItem", kiosk)
        self.assertNotIn("sessionStorage.setItem('antiSpoof", kiosk)

    def test_official_adapter_requires_signed_evidence_and_server_policy(self):
        provider = (ROOT / "mobile_id_provider.py").read_text(encoding="utf-8")
        server = (ROOT / "server.py").read_text(encoding="utf-8")
        for field in (
            "verification_source",
            "signing_key_id",
            "signature_valid",
            "request_id",
            "transaction_id",
            "issued_at",
            "expires_at",
            "candidate_id",
            "VerifiedLivenessEvidence",
            "model_version",
            "liveness_evidence",
        ):
            with self.subTest(field=field):
                self.assertIn(field, provider)
        self.assertIn("MobileIdAuthResult", provider)
        self.assertIn("OfficialMobileIdProvider", provider)
        self.assertIn("mobile_id_auth_transaction", server)
        self.assertIn("signature_valid", server)
        self.assertIn("evidence_candidate_id", server)
        self.assertIn("liveness_signature_valid", server)
        self.assertIn("liveness_score", server)
        self.assertIn("liveness_model_version", server)
        self.assertIn("OFFICIAL_MOBILE_ID_PROVIDER: OfficialMobileIdProvider | None = None", server)
        self.assertIn("INSTITUTION_ADMISSION_POLICY_APPROVED = False", server)

    def test_kiosk_outcome_selector_is_admin_only(self):
        admin_page = self.page
        kiosk = (ROOT / "kiosk.html").read_text(encoding="utf-8")
        admin_section = admin_page[admin_page.index('id="admin"'):]
        self.assertIn('id="adminKioskTestPanel" hidden', admin_section)
        self.assertIn('id="kioskTestOutcome"', admin_section)
        for outcome in ("success", "failure", "uncertain", "mismatch"):
            self.assertIn(f'<option value="{outcome}"', admin_section)
            self.assertNotIn(f'id="selfDemo{outcome.title()}"', kiosk)
        self.assertIn("관리자 테스트 실행", admin_section)
        self.assertNotIn("시연용 QR 미리보기", kiosk)

    def test_kiosk_demo_ticket_print_is_separate_and_print_friendly(self):
        kiosk = (ROOT / "kiosk.html").read_text(encoding="utf-8")
        for text in ("화면으로 QR 보기", "QR 입실권 출력", "window.print()", "A4", "영수증형 80mm"):
            self.assertIn(text, kiosk)
        for element_id in ("ticketPrintArea", "ticketQrImage", "ticketIssuedAt", "ticketExpiresAt", "ticketCandidateName", "ticketCandidateNumber"):
            self.assertIn(f'id="{element_id}"', kiosk)
        self.assertIn("@media print", kiosk)
        self.assertIn("body *{visibility:hidden!important}", kiosk)
        self.assertIn("재출력은 같은 유효 QR을 인쇄하며 새 토큰 발급은 하지 않습니다.", (ROOT / "README.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
