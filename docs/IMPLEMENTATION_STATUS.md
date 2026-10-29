# 구현 상태 (남은 8개 기능)

제외: 공식 모바일신분증 provider 연동, 모바일신분증-수험정보 공식 연결 검증. 운영 인증은 계속 fail-closed이며 모의 연동으로 대체하지 않았습니다.
**이 프로토타입은 실제 얼굴 위조 방지 성능이나 운영 신원확인을 검증하지 않았습니다.**

| # | 기능 | 상태 | 비고 |
|---|---|---|---|
| 1 | 사진·동영상 위조 탐지 | **부분 구현** | 오판정 사례 기록, PAD 참고용 고정(`evaluatePadEvidence`), 도입 기준 문서화. 추가 PAD 모델은 라이선스·검증 자료 미확인으로 **도입하지 않음** |
| 2 | 얼굴 비교 정확도 | **부분 구현** | 동일 이미지·유사/타인 거리 테스트, 임계값 준비도 게이트(`assessThresholdReadiness`), 기존 진단 화면(raw distance, box, 입력 크기, 정렬 상태). 표본 부족으로 성공 임계값은 정하지 않음. mesh·정렬은 모델 파일이 없어 미적용 |
| 3 | 인증 승인 조건 | **완료(DEMO 범위)** | `verification_policy.py`: pending/verified/rejected/inconclusive, DEMO·운영 분리, 운영 verified는 신뢰 증거+기관 정책 없이는 불가. 키오스크 상태 응답에 `verification` 포함 |
| 4 | 서버 측 검증 | **부분 구현** | 브라우저 face_match/liveness/score 거부, 위조 요청으로 운영 토큰·입실 불가 테스트, 기존 세션·만료·재사용 방지·감사 기록. 신뢰 가능한 외부 검증 서비스는 미연결이라 운영 인증은 차단 |
| 5 | 휴대전화 없는 응시자 | **완료(DEMO)** | 감독관 대면 확인 → DEMO 종이 QR(A4/80mm), '공모전 시제품 · 실제 입실 불가' 표시, 서버 만료·재사용·중복 차단(기존 테스트). 실제 프린터 출력은 미검증 |
| 6 | 개인정보 보호 | **완료(DEMO)** | 로그 마스킹/차단, 보존 기간 삭제, `.gitignore`, 점검 테스트, `docs/PRIVACY.md` |
| 7 | 접근성·장애 대응 | **부분 구현** | 큰 글씨/고대비(세션 한정), 건너뛰기 링크, API 10초 타임아웃·네트워크 오류 메시지, 기존 카메라/모델/검출/시간 초과 처리. 스크린리더·실기기 접근성 검증은 미수행 |
| 8 | 관리자 현황판 | **완료(DEMO)** | `/api/dashboard` 필터(domain, event, auth_path, outcome, since, until, limit), admin 서버 검증, 응시자 마스킹, DEMO/운영 구분 |

## 변경 파일
- 신규: `verification_policy.py`, `tests/test_server_hardening.py`, `tests/test_privacy.py`, `tests/test_kiosk_ui.py`, `tests/fixtures/recorded_field_cases.json`, `docs/*`
- 수정: `server.py`(이벤트 domain/auth_path 열, 로그 정제, 보존 삭제, 대시보드 필터, 상태 응답), `index.html`(관리자 필터), `kiosk.html`(접근성, 타임아웃, 인증 상태 안내), `face_similarity.js`, `tests/test_face_similarity.js`, `.gitignore`
- 백업: `*_pre_remaining8_backup.*`, `test_backups/*_pre_remaining8_backup.py`

## 자동 테스트
```sh
python3 -m pytest -q
python3 -m unittest discover
node --test tests/test_face_similarity.js
```
QR 디코드 간헐 실패: 원인은 서버 QR 생성이 아니라 zxing-js의 finder 검출 단계입니다(독립 디코더 zxing-cpp는 200/200 성공, 다른 인코더 segno도 동일 비율로 실패, PURE_BARCODE 모드는 0/400 실패). 테스트는 PURE_BARCODE로 정확한 페이로드 일치를 검증하고(`tests/test_qr_generation.py`), 검출 경로 실패율은 상한(15%)으로 감시합니다. 실패 은폐·재시도 없음. 브라우저 스캐너(zxing)도 같은 검출기를 쓰므로 일부 인쇄 QR은 재스캔이 필요할 수 있고, 수동 토큰 입력 경로가 있습니다. 자세한 내용: `docs/QR_DECODE_ANALYSIS.md`.

## 수동 검증 (미수행)
1. 실제 브라우저·카메라: `/kiosk` 셀프 DEMO → 결과 화면에 "인증 상태: inconclusive" 안내와 감독관 확인 버튼이 보이는지, QR이 자동 발급되지 않는지 확인.
2. 접근성: 큰 글씨/고대비 버튼, Tab 키 이동, 건너뛰기 링크.
3. 장애: 카메라 권한 거부, 서버 중지 후 요청 시 오류 메시지, 15초 분석 시간 초과.
4. 프린터: A4와 80mm 영수증 출력에서 '공모전 시제품 · 실제 입실 불가' 확인.
5. 관리자: `/` → 관리자 로그인 → 필터 검색, 응시자 끝 3자리만 표시되는지 확인.


## 얼굴 인증 기술 3종 (후속 개발)
| 항목 | 판정 | 근거 |
|---|---|---|
| Facemesh 정렬 | **부분 구현(참고 측정)** | 공식 3.3.6 facemesh 모델 추가(해시 고정), 등록 사진·프레임 동일 정렬, 전/후 거리 진단. 판정 경로는 미변경. 실제 카메라 효과는 미검증 |
| PAD 개선 | **부분 구현(평가 체계만)** | 평가 함수·기록 사례 테스트 완료. 검증 기준을 충족한 추가 PAD 모델이 없어 미통합. 위조 방지 성능은 검증되지 않음 |
| 정확도 평가 | **부분 구현** | `tools/evaluate_faces.js` FAR/FRR 도구와 함수 테스트 완료. 표본 부족으로 성능·임계값 확정 불가 |
