# ID·DEMO 앱 ↔ 안심패스 연동 (DEMO 전용)

> 공식 모바일신분증 연동이 **아닙니다**. 어떤 단계도 `verified`, 입실 승인, 운영 QR을 만들지 않습니다. 운영 인증은 계속 fail-closed입니다.

## 4단계 흐름
1. **페어링 코드** — 키오스크가 `POST /api/kiosk/idapp/pair`로 1회용 8자 코드(60초, 키오스크 세션에 서버에서 연결)를 표시.
2. **앱 QR** — 앱이 `POST /api/idapp/token`에 코드를 제출 → `DEMO-ONLY:IDAPP:<난수>` 토큰(30초, 1회용). 앱은 이 문자열을 QR로 그림.
3. **키오스크 검증** — 키오스크가 QR을 스캔해 `POST /api/kiosk/idapp/verify`. 서버가 만료·재사용·세션/응시자 연결·활성 상태 확인 → `idapp_demo_checked`(5분간 유효, 입실 권한 아님).
4. **감독관 승인** — 기존 절차. 앱 연동을 시작한 세션은 ID QR 확인 전에는 자동 DEMO 시뮬레이션·감독관 승인 모두 QR을 발급하지 않음(409). 발급되는 입실용 DEMO QR은 기존 30초·1회·중복 입실 차단 규칙 그대로.

명세: [openapi-idapp.yaml](openapi-idapp.yaml). 참고 클라이언트(앱 동작 재현): `python3 tools/idapp_app_simulator.py --base-url https://<host> --code <코드>`.

## 보안 설계
- 앱에 키 없음. 자격 = 1회용 페어링 코드. 서버 비밀 `EXAM_SAFE_IDAPP_SECRET`(32자 이상, 환경변수)은 코드/토큰/세션 ID의 HMAC 키로만 쓰이며 DB에는 해시만 저장. 미설정이면 연동 전체가 503(fail-closed).
- QR/토큰/코드에 이름·수험번호·생년월일·사진·장기 키 없음. 감사 로그에는 결과 코드만 기록.
- 코드 추측 방지: 실패 시 클라이언트별·전체 속도 제한(429). 코드는 한 번 조회되면(만료·오류 포함) 재사용 불가.
- 토큰은 발급한 키오스크 세션·응시자에만 유효. 다른 세션이 제출하면 403(토큰은 소모되지 않음). 응시자 비활성화/삭제/DEMO 초기화 시 미사용 코드·토큰 무효화.
- `IDAPP` 토큰은 운영 API·`demo-entry`·감독관 토큰 조회에서 모두 거부됨(테스트로 검증).
- 심사용(Render) 모드: `/api/idapp/info`·`/api/idapp/token`만 쿠키 없이 접근 가능, 키오스크용 엔드포인트는 심사용 로그인+CSRF 필요. 가상 응시자·합성 이미지만 사용.
- 환경 구분: `GET /api/idapp/info`의 `environment`(`judge-demo` / `development-demo`).

## 설정 (사용자가 직접)
- 개발: `.env` 또는 Codespaces Secrets에 `EXAM_SAFE_IDAPP_SECRET` 설정 후 서버 재시작(실행 중인 서버는 자동 종료/변경하지 않았음).
- Render: **기존 배포는 변경하지 않았습니다.** 연동을 켜려면 Render 대시보드 Environment에 `EXAM_SAFE_IDAPP_SECRET`(Generate/랜덤 값)을 직접 추가하고, 새 코드가 배포된 뒤에만 동작합니다.

## 구현 상태
| 항목 | 상태 |
|---|---|
| 서버 API(pair/token/verify/info), 만료·1회·세션 연결·속도 제한 | 완료(자동 테스트) |
| 키오스크 화면(코드 표시, 카메라/수동 QR 입력) | 완료(자동 헤드리스 테스트; 실제 카메라 스캔은 미검증) |
| OpenAPI 명세, 참고 클라이언트 | 완료 |
| 감독관 승인/입실 차단 유지, 운영 fail-closed | 완료(회귀 테스트) |
| Android 앱(1.0.2) 수정 | **미구현** — 원본 소스 없음(APK만 보유) |
| 실제 기기·카메라·Render 프록시·HTTPS 인증서 검증 | 미검증 |

## 앱 원본 소스 확보 후 할 일
1. 기존 화면/디자인 유지, 설정에 서버 주소(HTTPS만) 프로필 2개(개발/Render) 추가, `/api/idapp/info`로 환경 표시.
2. 「안심패스 연동 DEMO」 모드(기본 OFF): 코드 입력 → `/api/idapp/token` → 반환 문자열을 실제 QR로 렌더링, 30초 카운트다운·만료 시 재입력 안내, "DEMO · 실제 신분증 아님" 배지.
3. 오류/오프라인/타임아웃은 "연동 실패 · 인증되지 않음"만 표시. 앱에 키 하드코딩 금지, cleartext 비활성화.
4. 실기기에서 키오스크 카메라 스캔 시나리오 수동 검증.

## 수동 검증 절차(자동 테스트로 대체 불가)
1. `EXAM_SAFE_IDAPP_SECRET` 설정 후 서버 실행 → `/kiosk` 자체 DEMO → 수험번호 조회 → 카메라 없이 진행 → 「ID·DEMO 앱 연동 확인」.
2. 코드 표시 → (앱 미구현 시) `tools/idapp_app_simulator.py`로 토큰 받기 → 「QR 문자열 직접 입력」 또는 다른 화면에 QR로 띄워 카메라 스캔.
3. 30초 후 재사용/만료 오류, 연동 후에도 입실용 QR은 별도 발급인지 확인.
