# QR 디코드 간헐 실패 분석

- 증상: `test_kiosk_demo_qr_is_valid_png_but_never_an_entry_token`이 약 5~7% 확률로 ChecksumException/NotFoundException.
- 격리: 같은 PNG를 zxing-cpp로 200/200 디코드, segno로 만든 QR도 zxing-js에서 같은 비율 실패, 마스크·박스 크기·오류정정 수준을 바꿔도 사라지지 않음. PURE_BARCODE 힌트 사용 시 0/400 실패 → QR 비트 행렬은 항상 유효, 실패는 zxing-js 검출 단계.
- 조치: 테스트 디코더를 PURE_BARCODE로 전환(깨끗한 서버 생성 PNG에 적합). 결정적 시드 150개 페이로드로 100% 일치 검증, 검출 경로 실패율은 15% 상한으로 감시.
- 한계: 브라우저 스캐너가 같은 검출기를 쓰므로 일부 인쇄 QR은 재스캔/수동 입력이 필요할 수 있음. 실제 프린터·스캐너 검증은 수행하지 않음.
