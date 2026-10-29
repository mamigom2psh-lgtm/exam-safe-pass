# 위조 탐지(PAD) 평가 기록

## 현재 모델
Human 3.3.6 `antispoof` (브라우저 내 추론). 연구용 참고값이며 **인증 근거가 아닙니다.**

## 기록된 오판정 (실제 브라우저 테스트, 점수만 기록)
| 시나리오 | 점수 | 모델 판정 | 실제 |
|---|---|---|---|
| 휴대전화 본인 배경화면 | 0.69 | live_candidate | 화면 제시 공격 |
| 갤러리 본인 사진 | 0.49 | spoof_suspected | 화면 제시 공격 |
| 갤러리 타인 사진 | 0.88 | live_candidate | 화면 제시 공격 |

데이터는 `tests/fixtures/recorded_field_cases.json`에 있으며 이미지·특징값은 포함하지 않습니다.
세 사례 중 두 사례가 live_candidate로 통과했으므로 점수 평균, 프레임 합산, 임계값 조정만으로는 재촬영 공격을 막을 수 없습니다.

## 추가 PAD 도입 기준 (미도입)
다음을 모두 문서로 확인하기 전에는 모델을 번들에 추가하지 않습니다.
1. 라이선스: 모델 가중치와 학습 데이터의 상업/공모전 사용 가능 여부.
2. 모델 파일: 출처, 해시, 크기, TF.js/ONNX 웹 변환 가능 여부.
3. 브라우저 호환성: WebGL/WASM, 저사양 PC, 모바일 브라우저.
4. 검증 자료: 인쇄물·화면 재촬영·동영상 재생 공격에 대한 공개 평가 또는 자체 평가(APCER/BPCER), 가능하면 ISO/IEC 30107-3 기반 제3자 평가.
검토 대상 범주: (a) 공개 PAD 모델(예: MiniFASNet 계열) — 라이선스·학습 데이터 확인 필요, (b) 상용 PAD SDK/서비스 — 외부 서비스 계약과 서버 측 검증 연동 필요. 어느 쪽도 이번 작업에서 검증되지 않았고 **도입하지 않았습니다.**

## 코드상 보장
- `evaluatePadEvidence`는 어떤 점수에서도 `reference_only` / `verified:false`를 반환합니다.
- `browserVerificationState`는 `verified`/`rejected`를 만들 수 없고 `pending` 또는 `inconclusive`만 반환합니다.

## 추가 PAD 후보 검토 (조사 결과)
| 후보 | 확인된 사실 | 미확인/문제 | 결정 |
|---|---|---|---|
| Silent-Face-Anti-Spoofing(MiniFASNet) | GitHub 저장소 라이선스 Apache-2.0(API 확인), PyTorch 학습 코드 제공 | 공식 저장소에 ONNX/브라우저 런타임 없음(커뮤니티 변환본만), 학습 데이터 출처·재사용 권리 문서 없음, 인쇄/재생 공격 독립 검증 자료 미확인, onnxruntime-web 번들 필요 | **통합 안 함** |
| 상용 PAD SDK | 미조사(계약·라이선스 필요) | 모델 파일·성능 인증(예: ISO 30107-3 시험기관 보고서) 확인 불가 | **통합 안 함** |

검증 기준(출처·라이선스·모델 파일 해시·브라우저 실행·독립 검증 자료)을 충족한 모델이 없으므로 추가 모델은 통합하지 않았습니다. 웹 검색 요약의 TPR/FPR 수치는 출처 미검증이라 채택하지 않았습니다.

## 재현 가능한 평가 항목
`face_similarity.js`의 `evaluatePadSamples(samples, threshold)`는 공격 유형별(phone_screen, printed_photo, replay_video) APCER와 bona fide BPCER를 계산하고, 클래스당 30개 미만이면 `reportable:false`, 권고 임계값은 항상 null입니다. `tests/fixtures/recorded_field_cases.json`의 기록 사례(점수만)로 자동 테스트합니다.
- 기록 사례: 휴대전화 화면 공격 0.69·0.49·0.88, 실제 얼굴 0.74. 임계값 0.5에서는 공격 2건이 live로 통과하고, 0.75로 올리면 실제 얼굴(0.74)이 거부되는데도 공격 0.88은 여전히 통과합니다 → **임계값 조정으로 해결되지 않음**(테스트로 고정).
- 미시험 유형: 인쇄 사진, 재생 영상(실제 샘플 없음). 실제 얼굴·휴대전화·인쇄·영상을 구분한 수집은 수동 평가 항목입니다.
