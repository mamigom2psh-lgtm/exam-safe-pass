# 구현 상태 (남은 8개 기능)

제외: 공식 모바일신분증 provider 연동, 모바일신분증-수험정보 공식 연결 검증. 운영 인증은 계속 fail-closed이며 모의 연동으로 대체하지 않았습니다.
**이 프로토타입은 실제 얼굴 위조 방지 성능이나 운영 신원확인을 검증하지 않았습니다.**

| # | 기능 | 상태 | 비고 |
|---|---|---|---|
| 1 | 사진·동영상 위조 탐지 | **부분 구현** | 오판정 사례 기록, PAD 참고용 고정(`evaluatePadEvidence`), 도입 기준 문서화. 추가 PAD 모델은 라이선스·검증 자료 미확인으로 **도입하지 않음** |
| 2 | 얼굴 비교 정확도 | **부분 구현** | 동일 이미지·유사/타인 거리 테스트, 임계값 준비도 게이트(`assessThresholdReadiness`), 진단 화면(raw distance, box, 입력 크기, 정렬 상태). Facemesh 정렬은 측정 전용이며 표본 부족으로 성공 임계값·헤어스타일 강건성은 확정하지 않음 |
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
1. 실제 브라우저·카메라: `/kiosk` 셀프 DEMO → 얼굴 비교 참고값, 미검증 PAD 참고 결과, 별도 `inconclusive` 인증 상태와 감독관 확인 버튼이 구분 표시되는지 확인. 낮은 얼굴/PAD 점수도 타인·위조 확정 문구가 아니며 QR이 자동 발급되지 않는지 확인.
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


## 시험장 통합 시뮬레이션 · 공모전 시연 모드
구현: `demo_scenarios.py`(메모리 샌드박스), `demo_scenarios.html`(`/demo-scenarios`, 감독관 로그인 필요), `server.py` 라우트(`/api/demo/scenario/*`, `/api/proctor/overview`), `kiosk.html` 단계 표시.

| 영역 | 판정 | 근거·한계 |
|---|---|---|
| 입실 시뮬레이션 | **부분 구현** | 조회→촬영→참고 분석→감독관 확인→DEMO QR→입실 단계 표시. 키오스크는 기존 실제 흐름에 단계 표시만 추가, 시나리오 화면은 사전 설정(가상) 값. 실제 인증 아님 |
| 부정행위 대응 | **부분 구현** | 위조 의심·QR 만료·재사용·중복 입실 시연. 결과는 `preset_demo`로 실제 모델 판정과 분리, 부정행위 확정 아님 |
| 감독관 통합 관리 | **부분 구현** | 서버 권한 검사, 대기 건수·확인 이벤트(응시자 마스킹)·DEMO 입실 수. 승인/거부 감사 기록은 기존 키오스크 감독관 API가 DB에 기록 |
| 장애 복구 | **부분 구현** | 카메라·네트워크·모델·인쇄 실패 주입과 재시도/감독관 경로, 자동 승인 없음, 재출력은 새 토큰 미발급. 실제 장치 장애는 미검증 |
| 시연 모드 | **부분 구현** | 6개 시나리오, 시나리오별 재시작·전체 초기화, 모든 화면·QR에 '공모전 시제품 · 실제 입실 불가'. 운영 DB·인증 API·감사 기록 미사용(테스트로 행 수 불변 확인) |

시나리오 QR은 `DEMO-ONLY:SCENARIO:` 토큰이며 운영·DEMO 토큰 검사 API에서 통과하지 않습니다.

### 수동 검증 항목 (미수행)
1. `/demo-scenarios`에서 감독관 로그인 → 6개 시나리오를 각각 끝까지 진행, 상단 배너 확인.
2. 위조 의심: 서류·얼굴 대조 중 하나라도 불일치 시 승인되지 않고 QR이 없는지 확인.
3. 만료: QR 발급 후 '+150초' → 입실 시도 차단. 재사용·중복 입실 차단.
4. 장애 복구: 4종 장애 주입 → 재시도/감독관 이동, 인쇄 실패 후 같은 QR 재출력.
5. '전체 초기화' 후 이벤트 로그가 비고 기존 DEMO 입실 현황이 그대로인지 확인.
6. 실제 카메라·프린터(A4/80mm)·키보드 Tab·고대비·큰 글씨 확인. 키오스크 상단 단계 표시 확인.

## 실제 얼굴 분석 경로 vs 자동 발급 시연 경로
| 구분 | 실제 얼굴 분석 경로 | 자동 발급 시연(DEMO) |
|---|---|---|
| 점수 사용 | 참고용 표시, 상태 inconclusive | 얼굴 결과와 무관(사전 설정) |
| QR 발급 | 감독관 확인 요청(`/api/kiosk/self-demo/request-review`) → 서버 검증된 proctor 승인 → DEMO QR | `/api/kiosk/demo-auto-issue`, proctor 권한 서버 검증, simulation 표시 |
| 공통 | 30초 만료·1회 사용·재사용/중복 입실 차단, '공모전 시제품 · 실제 입실 불가', 운영 QR/입실 API 차단 | 동일 |

브라우저 수동 확인(미수행): 긴 머리/묶음 vs 단발 등록 촬영 시 진단 행(Facemesh 상태, 정렬 전/후 거리, crop, 판정 불가 사유), 감독관 승인→QR, 자동 발급 시연+로그인, 30초 만료, 휴대폰·인쇄 사진이 이전보다 더 자주 통과하지 않는지.

## 얼굴 분석 결과 안내 해석
- FaceRes의 50~53% 등은 보정되지 않은 참고 유사도이며 동일인 확률이 아닙니다. 검증된 동일인 임계값이 없으므로 낮은 점수도 `rejected`/불일치로 해석하지 않습니다.
- Human Antispoof의 점수와 0.50 PoC 기준은 참고값일 뿐, 인쇄·화면·재생 공격에 대한 검증이 없어 실재·위조를 확정하지 않습니다.
- 브라우저 분석 후 상태는 `inconclusive`입니다. 실제 경로는 감독관 확인을 요청하고, 서버가 감독관 권한과 결정을 확인한 경우에만 DEMO QR을 발급합니다. 운영 인증 및 운영 QR은 계속 차단됩니다.

## QR 발급 단계로 넘어가지 않는 문제 (원인과 수정)

- 원인: 서버 흐름은 정상이었으나 (1) 감독관 요청 후 키오스크에서 승인할 방법이 없어 별도 관리자 페이지가 필요했고, (2) 자동 발급 시연의 감독관 로그인 안내와 오류가 화면 하단에만 표시되어 멈춘 것처럼 보였습니다.
- 수정(kiosk.html): 감독관 대기 화면의 "이 키오스크에서 DEMO 승인" 패널(서버 `/api/login` + `/api/kiosk/physical-review`), 눈에 띄는 `#simStatus` 안내/오류, 세션 만료 안내. 얼굴·PAD 점수는 전송하지 않으며 상태는 계속 inconclusive입니다.
- 테스트: `tests/test_kiosk_browser_flow.py`(headless Chromium, 카메라는 결과 화면 이동으로 대체; Playwright 없으면 skip), `tests/test_kiosk_ui.py`.
- 미검증(수동): 실제 카메라 촬영, 실제 프린터(A4/80mm), 휴대전화 사진 PAD 성능.

## 50% 화면 전환 → 가상 DEMO 정보 → 시뮬레이션 QR

- 키오스크: 참고 유사도 ≥ 50.0%(`demoScreenGate`)이면 "추가 정보 입력" 버튼 표시. 점수 누락·NaN·얼굴 검출 실패는 닫힘. **화면 전환 기준일 뿐 본인 인증 기준이 아니며** 점수는 서버로 전송되지 않고 상태는 계속 inconclusive입니다.
- 서버: `/api/kiosk/self-demo/verify-demo-info`가 가상 코드(YYMMDD-N, 날짜 유효성)와 DEMO 시험 목록을 사전 설정값과 대조합니다(시도 5회 제한, 입력값 미저장·미기록). 일치 후에만 `demo-auto-issue`의 `demo_info_simulation` 시나리오가 서버 DEMO 시뮬레이션 권한(`EXAM_SAFE_DEMO_SIMULATION`, 기본 허용, `0`이면 차단)으로 30초·1회용 DEMO QR을 발급합니다. 브라우저 점수 필드는 400으로 거부됩니다.
- 감독관 승인 경로와 `preset_auto_issue`(감독관 로그인 필요)는 그대로입니다. 운영 QR·운영 입실은 계속 차단됩니다.
- 미검증: 실제 카메라 촬영 점수로 50% 전후 전환, 실제 프린터 출력.

- 추가 정보 입력 화면: 생년월일 코드와 시험명을 각각 한 줄(100% 너비)로 배치, 서버가 알려준 기본 시험(컴퓨터활용능력 2급) 선택. 헤드리스 Chromium(1280/390/320px)에서만 검증, 실제 모바일 기기는 미검증.

## 다중 응시자 DEMO 등록 (demo_candidates.py, /demo-candidates)

- 관리자 전용 화면에서 응시자 추가·조회·수정·비활성화·삭제, 수험번호 중복 거부, 가상 테스트 응시자 5명 시드(사진 자동 배정 없음, 충돌 시 덮어쓰지 않음).
- 시험 종류·시험명·급수·등급을 분리 관리(서버 관리 DEMO 목록, 실제 운영기관 연동 아님).
- 사진: 서버 검증(형식·크기·해상도), 임의 파일명, `demo_photos/`(gitignore), 인증된 경로로만 제공(no-store), 교체·삭제 시 `demo_photos/backup/`로 이동.
- 키오스크: 서버 세션에 연결된 응시자의 코드·시험명·급수만 대조. 얼굴 점수는 화면 전환용 참고값이며 verified를 만들지 않음. 운영 QR/입실은 계속 차단.
- 쓰기 API는 CSRF 토큰 필요. 감사 로그에는 이름·코드·사진을 기록하지 않음.
- 미검증: 실제 카메라·프린터·모바일·외부 브라우저. 서버 재시작 후 마이그레이션 적용.

## 보안·안정성 하드닝 (auth_config.py, session_store.py)

- 비밀정보: 하드코딩 계정/키 제거. 관리자 비밀번호 해시(scrypt)·키는 환경변수(Codespaces Secrets) 필수이며 없거나 과거 기본값/약한 값이면 서버가 시작되지 않음. `.env.example`은 변수 이름만 포함, `.env*`는 gitignore. 키는 로그에 출력하지 않음.
- 응시자 조회는 `RosterView`(DB 직접 조회)로 통일. 비활성화·삭제 시 해당 응시자의 키오스크/로그인 세션을 제거하고 미사용 DEMO/일반 토큰을 소진 처리. 사라진 응시자는 500 대신 409(재시작 안내).
- CSRF: 쿠키 세션 쓰기에 `X-CSRF-Token` 필수 + Origin 허용 목록(정확 일치, Host/X-Forwarded-Host 미신뢰) + Referer/Sec-Fetch-Site 보조 검사. 쿠키 HttpOnly·SameSite=Lax, HTTPS 프록시에서 Secure.
- 업로드: Pillow 전체 디코딩, 형식·해상도·픽셀·프레임·절단 검사, EXIF 제거 후 재인코딩, 임의 파일명, 비공개 폴더.
- 세션: SQLite 저장(세션 ID는 SHA-256만 저장, QR 토큰/이미지는 저장 안 함), 재시작 후에도 만료 시각 유지, 로그인 시 세션 ID 재발급(고정 방지), 로그아웃 서버 폐기.
- `/demo-candidates`는 관리자 쿠키 세션 없이는 401 + 데이터 없는 로그인 폼(no-store). 로그인 실패는 속도 제한.
- 한계: 세션 ID 외 Origin 없는 비브라우저 요청은 허용(쿠키 세션은 CSRF 토큰 필요), 세션 저장은 단일 프로세스 가정, 실제 브라우저(Codespaces 외부 주소)·프린터 미검증, 과거 Git 기록의 옛 기본값은 교체 필요.

## 심사위원 전용 Render DEMO 배포 준비 (judge_mode.py) — 준비 완료, 실제 배포는 하지 않음

- `EXAM_SAFE_JUDGE_MODE=1` 이면 닫힌 샌드박스로 동작: 심사용 비밀번호(환경변수, 12자 이상, 메모리에는 scrypt 해시만)로 로그인해야 `/kiosk` 및 체험 API 사용 가능. 공개 경로는 `/healthz`(상태만), `/judge-login`뿐.
- 관리자·운영·시나리오 경로(`/demo-candidates`, `/api/admin/*`, `/api/token*`, `/api/demo/*`, `/api/login` 등)는 로그인해도 403. 역할 계정/키가 없어 헤더 키 인증 불가.
- 가상 응시자 5명 + 생성형 만화 아바타만 사용(`images/`·개발 DB·`demo_photos/` 경로 사용 시 시작 거부). 시작 때마다 임시 DB에서 재생성.
- 세션 만료(기본 2시간), 로그인 실패 속도 제한(클라이언트별 + 전체), CSRF 토큰 + Origin 허용 목록(`RENDER_EXTERNAL_URL`, `EXAM_SAFE_ALLOWED_ORIGINS`).
- 카메라 없는 기본 체험: 「카메라 없이 사전 설정 시뮬레이션 체험」 → 「자동 발급 시연(DEMO)」. QR은 DEMO 전용(30초, 1회, 실제 입실 불가). 얼굴 점수는 인증/QR 근거가 아님.
- 배포 도구: `render.yaml`, `tools/export_judge_repo.py`(허용 목록 내보내기·감사·새 로컬 저장소 1커밋, push 없음), `tools/reset_judge_data.py`, `docs/RENDER_DEPLOY.md`, `docs/JUDGE_CHECKLIST.md`.
- 미검증: 실제 Render 배포, 외부 브라우저·스마트폰 실기기, 카메라·프린터, Render 프록시 뒤 클라이언트 IP 속도 제한. 속도 제한은 메모리 기반이라 재시작 시 초기화.
