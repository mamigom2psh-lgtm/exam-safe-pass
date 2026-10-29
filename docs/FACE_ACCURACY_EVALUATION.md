# 얼굴 비교 정확도 평가 체계

## 거리·정규화 (검증됨)
Human 3.3.6 `match.distance` = round(100·25·Σdiff²)/100 (임베딩 L2 정규화 없음), `similarity` = clamp(round((1−√d/100−0.2)/0.6, 2), 0, 1). 동일 벡터 거리 0을 자동 테스트와 오프라인 평가에서 확인했습니다.

## Facemesh 정렬
- 모델: `vendor/human/models/facemesh.{json,bin}` (공식 npm 패키지 @vladmandic/human 3.3.6 사본, MIT, SHA-256은 `MODEL-SOURCES.txt`).
- 키오스크: 같은 Human 인스턴스에서 등록 사진과 각 카메라 프레임에 **동일하게** mesh+회전 정렬을 적용한 "참고 측정"을 추가로 수행하고 정렬 전/후 거리 개선·악화 횟수와 중앙 변화량을 진단 화면에 표시합니다. 기본(판정) 경로는 정렬 꺼짐 그대로이며, 정렬 결과는 서버로 전송되지 않고 판정에 쓰이지 않습니다. 정렬 측정 실패·시간 부족 시 기본 경로 결과만 사용합니다.
- 오프라인(node-wasm, 사진 3장) 예비 실행: 정렬 후 거리 변화는 쌍 3개 중 2개 개선, 1개 악화(중앙 −17.6). 표본이 너무 작아 **효과 결론 없음**. 실제 카메라 프레임 효과는 수동 검증 대상입니다.

## 오프라인 FAR/FRR 평가 도구
`tools/evaluate_faces.js` — 데이터셋(`<dir>/<사람>/<이미지>`)은 **저장소 밖**에 두어야 하며(저장소 안이면 실행 거부), 출력은 거리 통계와 컷오프별 FAR/FRR뿐입니다(이미지·임베딩 미저장).
```
NODE_PATH=<@tensorflow/tfjs-{core,converter,backend-wasm,backend-cpu}, jpeg-js, pngjs 설치 경로> node tools/evaluate_faces.js <dataset-dir> [report.json]
```
- 오류율은 클래스당 30개 미만이면 `reportable:false`, `recommendedThreshold`는 항상 null. 컷오프 격자는 탐색용이며 권고값이 아닙니다.
- 이 도구용 의존성은 저장소 manifest에 추가하지 않았습니다(임시 설치).
- 현재 확정 가능한 성능·임계값: 없음. 자동 승인 임계값은 설정하지 않습니다.
