# 확인 결과 — 2026-11-04

## 자동 확인

`PYTHONPATH=water_survey python -m unittest discover -s water_survey/tests -v`의 13개 항목 통과. `node --check water_survey/water_survey/static/app.js` 통과.

## 선박에서 확인

- 별도 `/home/ubuntu/water_survey_ws`에서 ROS 2 패키지 빌드 성공.
- `water-survey-indoor` 활성 상태와 부팅 자동 시작 확인. 서버는 :8000에서 동작하며 실제 WL118D283 / V403 수신 확인.
- 완료된 실내 조사 `2026-11-04T075237466Z_f6f19f`: 경유지 1~8 도착 이벤트와 완료 이벤트 확인.
- 실제 DO 417개, 위치 결합 417개. 모든 위치는 가상 GPS이며 실제 하천 조사 결과가 아니다. 추진 명령 발행 0회.
- 해당 조사 내보내기 ZIP의 samples.csv, events.jsonl, mission.json, samples.geojson, grid.geojson, route.geojson, grid.csv, map.svg, timeseries.svg, report.html 확인.
- 기존 독립 do_receiver.py 프로세스는 센서 연결 중복 점유를 해소하기 위해 종료하고 관리 서비스의 수신기로 전환했다. 기존 CSV 기록은 보존했다.
- field 서비스는 설치했으며 부팅 자동 시작과 실제 실행은 활성화하지 않았다. 수상용 launch는 설치된 기존 선박 브링업을 포함한다.

## 실행 범위

조사 상태는 선박 프로세스에 있고 브라우저는 상태를 재수신한다. SSH나 WebSocket 연결이 끊겨도 진행 중 조사를 취소하지 않는 동작을 자동 확인했다. 프로세스 재시작 후 진행 중 조사 복구·자동 출발은 제공하지 않는다. 파일은 남고 새 대기 상태로 시작한다.

실제 GPS·IMU를 이용한 수상 주행, 조향 부호·서보 중앙·ESC 및 MCU 배선은 물리적으로 확인하지 않았다. 현재 인터페이스는 단일 조향과 단일 추진 명령이다. 좌우 독립 스러스터와 동일하다고 가정하지 않는다. 장애물 회피 및 오염원 자동 역추적은 구현 범위에 포함되지 않는다.
