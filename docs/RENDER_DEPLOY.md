# 심사위원 전용 Render DEMO 배포 안내

> 이 문서는 **공모전 심사용 시제품**을 Render 무료 플랜에 올리는 방법입니다. 가상 응시자 5명과 합성 프로필만 사용하며,
> 공식 모바일신분증 연동이 없고 QR은 **실제 입실에 사용할 수 없습니다**.
> 개발용 저장소(실제 얼굴 사진·DB·Git 이력이 있을 수 있는 곳)를 그대로 연결하지 말고, 아래 1단계의 **깨끗한 내보내기 저장소**만 사용하세요.

## 0. 구성 요약

| 항목 | 내용 |
|---|---|
| 서버 | `python3 server.py` (`EXAM_SAFE_JUDGE_MODE=1`), Render가 주는 `PORT`에 `0.0.0.0` 으로 바인딩 |
| 로그인 | `/judge-login` — 서버가 scrypt 해시로 비밀번호 검증, 세션 2시간(변경 가능), 로그인 실패 속도 제한 |
| 보호 범위 | `/kiosk` 와 모든 체험 API는 로그인 필요. 관리자·운영·`/demo-candidates`·`/api/admin/*` 는 로그인해도 차단 |
| 공개 경로 | `/healthz`(상태 `{"status":"ok"}`만 반환), `/judge-login` |
| 데이터 | 시작할 때마다 임시 DB(`/tmp/judge`)에 가상 응시자 5명 + 생성형 만화 아바타 자동 생성 |

## 1. 깨끗한 배포용 저장소 만들기 (내 컴퓨터/Codespaces에서)

```bash
cd /workspaces/exam-safe-pass
python3 tools/export_judge_repo.py /tmp/exam-safe-pass-judge --git-init
```

- 허용 목록(whitelist) 파일만 복사하고, 사진·DB·`.env`·백업·테스트·Git 이력은 포함하지 않습니다. 감사에 실패하면 중단됩니다.
- `--git-init` 은 **새 로컬 저장소 + 커밋 1개**만 만듭니다. 원격 등록과 push 는 자동으로 하지 않습니다.
- 개발 저장소의 Git 이력에는 과거 기본 계정 문자열이 남아 있으므로, 개발 저장소를 공개 저장소로 push/연결하지 마세요.

## 2. GitHub에 빈 공개(또는 비공개) 저장소 만들고 올리기 (직접 실행)

1. GitHub → **New repository** → 이름 예: `exam-safe-pass-judge` → README/ .gitignore 추가 **체크 해제** → **Create repository**.
2. 터미널에서(저장소 주소는 본인 것으로):
   ```bash
   cd /tmp/exam-safe-pass-judge
   git remote add origin https://github.com/<내계정>/exam-safe-pass-judge.git
   git push -u origin main
   ```
   (이 push 는 사용자가 직접 승인·실행합니다. 개발 저장소에서는 실행하지 않습니다.)

## 3. 심사용 비밀번호 정하기

- 12자 이상, 다른 곳에서 쓰지 않는 문자열(예: 무작위 단어 4개 조합). `changeme`, `password` 같은 값은 서버가 시작을 거부합니다.
- 코드·저장소·채팅에 적지 말고 Render 화면에만 입력합니다. 서버는 메모리에서 해시만 보관합니다.
- 해시로 넣고 싶다면: `python3 -c "import auth_config,getpass;print(auth_config.hash_password(getpass.getpass()))"` 로 만든 `scrypt$...` 값을 `EXAM_SAFE_JUDGE_PASSWORD_HASH` 에 입력하고 `EXAM_SAFE_JUDGE_PASSWORD` 는 지웁니다.

## 4. Render 화면에서 클릭 순서

1. https://dashboard.render.com 로그인 → 우측 상단 **New +** → **Blueprint** (또는 **Web Service**).
2. **Connect a repository** → GitHub 연결 허용 → 2단계에서 만든 `exam-safe-pass-judge` 선택 → **Connect**.
3. Blueprint 이름/브랜치(`main`) 확인. 화면에 `EXAM_SAFE_JUDGE_PASSWORD` 입력란이 나타나면 **3단계의 심사용 비밀번호를 직접 입력**합니다.
   - (Web Service 로 수동 생성할 때) Language **Python 3**, Build Command `pip install -r requirements.txt`,
     Start Command `python3 server.py`, Instance Type **Free**, **Health Check Path** `/healthz`, 아래 환경변수 입력.
4. **Apply**(또는 **Create Web Service**) 클릭 → 빌드·배포 로그에서 `LISTENING http://0.0.0.0:<PORT>` 확인.
5. 상태가 **Live** 가 되면 상단에 표시된 `https://<서비스이름>.onrender.com` 주소를 복사합니다.

### 환경변수 (render.yaml 이 자동 설정, 수동 생성 시에만 직접 입력)

| 이름 | 값 | 직접 입력? |
|---|---|---|
| `EXAM_SAFE_JUDGE_PASSWORD` | 3단계 심사용 비밀번호 | **예 (필수, 코드에 쓰지 않음)** |
| `EXAM_SAFE_JUDGE_MODE` | `1` | 아니오 |
| `EXAM_SAFE_COOKIE_SECURE` | `1` | 아니오 |
| `EXAM_SAFE_TRUST_PROXY` | `1` | 아니오 |
| `EXAM_SAFE_DB` | `/tmp/judge/exam_safe_pass_judge.sqlite3` | 아니오 |
| `EXAM_SAFE_PHOTO_DIR` | `/tmp/judge/photos` | 아니오 |
| `PYTHON_VERSION` | `3.12.3` | 아니오 |
| `EXAM_SAFE_ALLOWED_ORIGINS` | 사용자 지정 도메인을 쓸 때만 `https://내도메인` | 선택 |
| `EXAM_SAFE_JUDGE_SESSION_SECONDS` | 세션 길이(초, 기본 7200) | 선택 |

- `RENDER_EXTERNAL_URL` 은 Render 가 자동으로 넣으며, 서버가 이 주소를 신뢰하는 Origin 으로 사용합니다. 사용자 지정 도메인이면 `EXAM_SAFE_ALLOWED_ORIGINS` 도 설정하세요(Host/X-Forwarded-Host 헤더는 신뢰하지 않습니다).

## 5. 배포 후 확인

1. `https://<주소>/healthz` → `{"status":"ok"}`.
2. `https://<주소>/kiosk` → `/judge-login` 으로 이동하는지.
3. 비밀번호 입력 → 키오스크 화면 상단의 「심사용 DEMO」 배너 확인.
4. 체크리스트: [JUDGE_CHECKLIST.md](JUDGE_CHECKLIST.md).

## 6. 무료 플랜 특성과 데이터 초기화

- 15분 정도 접속이 없으면 서버가 잠들고, 다시 접속하면 30~60초 걸릴 수 있습니다(첫 접속 전에 미리 열어 두세요).
- 파일 시스템이 임시이므로 **서버가 재시작되면 DB·세션·QR 기록이 모두 사라지고** 가상 응시자 5명이 다시 만들어집니다. 심사위원은 다시 로그인하면 됩니다.
- 시연 중 초기화: 키오스크가 아닌 심사 세션에서 `POST /api/judge/reset` (입실 기록·토큰·키오스크 세션만 삭제, 응시자는 유지).
- 완전 초기화: Render **Manual Deploy → Restart**(또는 Deploy latest commit). 로컬에서는
  `EXAM_SAFE_JUDGE_MODE=1 EXAM_SAFE_DB=... EXAM_SAFE_PHOTO_DIR=... python3 tools/reset_judge_data.py --yes`.

## 7. 가상 응시자(전부 가상, 실제 개인정보 아님)

| 수험번호 | 이름 | 가상 코드 | 시험 | 급수 |
|---|---|---|---|---|
| 2026-0012 | 박세현 | 980320-2 | 컴퓨터활용능력 | 2급 |
| 2026-0013 | 김하늘 | 990415-2 | 한국사능력검정시험 | 심화 |
| 2026-0014 | 이준호 | 010822-3 | 정보처리기사 | 해당 없음 |
| 2026-0015 | 최서연 | 020711-4 | 워드프로세서 | 단일등급 |
| 2026-0016 | 정민수 | 970903-1 | 컴퓨터활용능력 | 1급 |

기본 체험(카메라 없음): 셀프 인증 시작 → 수험번호 입력·조회 → **카메라 없이 사전 설정 시뮬레이션 체험** → **자동 발급 시연(DEMO)** → QR(30초, 1회용, 「실제 입실 불가」 표시).

## 8. 한계와 주의

- 실제 얼굴 촬영은 선택 사항입니다. 등록 사진이 합성 만화 아바타이므로 얼굴 비교 값은 의미가 없는 참고용이며 인증이 아닙니다.
- 속도 제한은 서버 메모리에 있어 재시작하면 초기화됩니다. Render 프록시 뒤 클라이언트 IP 식별(`EXAM_SAFE_TRUST_PROXY=1`, 마지막 X-Forwarded-For 값 사용)은 실제 배포에서 확인이 필요합니다. 전체 실패 상한(기본 40회/5분)이 보조 방어입니다.
- 공식 모바일신분증 연동과 운영 인증은 구현하지 않았으며 운영 경로는 계속 차단됩니다.
