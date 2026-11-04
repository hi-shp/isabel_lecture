# Isabel 위치별 용존산소 조사

기존 `example` 예제와 별도로 ROS 2 `water_survey` 패키지를 추가했다. 지도에서 경유지를 지정하고 시작하면 순서대로 이동·관측하면서 실제 ScienceCube WL118DO 값을 위치와 결합한다. 지도와 하단 사각 격자는 관측한 칸의 DO 평균을 표시한다.

## 현재 상태

- 선박: Radxa ROCK 5A, Ubuntu 24.04, ROS 2 Jazzy. `mechaship_bringup`과 micro-ROS 환경은 선박에 별도 설치돼 있다.
- 실제 센서 `WL118D283`, 펌웨어 `V403`에서 수신 확인. 연결은 PC 모드의 Bluetooth Classic SPP 채널 2다.
- 실내 서비스는 선박에서 실행 중이며 부팅 자동 시작이 설정돼 있다. 가상 위치 시험은 실제 DO를 사용하지만 추진 토픽 발행자를 만들지 않는다.
- 수상 통합 브링업과 서비스도 제공한다. 실제 조향 방향·추진기 배선과 수상 주행은 아직 검증하지 않았다. 현재 확인된 제어 인터페이스는 조향 서보 하나와 추진 출력 하나이며, 좌우 독립 추진기는 해당 인터페이스 또는 추가 어댑터 확인이 필요하다.
- 사전 지정 경로에 따른 관측 기능이다. DO 하나로 배출원을 확정하거나 유동장을 따라 오염원을 자동 역추적하는 기능은 포함하지 않는다.

## 설치

기존 선박의 항법 코드를 덮어쓰지 않고 별도 작업공간에 설치한다. 아래 경로는 현재 선박의 `ubuntu` 계정 기준이다. 다른 계정이면 서비스 파일의 경로와 User도 수정해야 한다.

```bash
sudo apt-get install bluez python3-aiohttp
mkdir -p /home/ubuntu/water_survey_ws/src
cp -a water_survey /home/ubuntu/water_survey_ws/src/
source /opt/ros/jazzy/setup.bash
cd /home/ubuntu/water_survey_ws
colcon build --packages-select water_survey
source install/setup.bash
ros2 launch water_survey indoor.launch.py
```

`http://선박IP:8000`을 연다. 현재 선박 IP는 `192.168.80.78`이며 핫스팟 재연결 후 달라질 수 있다. 온라인 지도 배경은 인터넷이 필요하다. Leaflet 자체는 패키지에 포함해 격자·좌표·경로는 배경 타일이 없어도 동작한다.

## SSH와 무관하게 실행

```bash
sudo install -m 0644 /home/ubuntu/water_survey_ws/src/water_survey/deploy/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now water-survey-indoor
```

서비스는 선박에서 실행된다. SSH·브라우저·핫스팟 연결이 끊겨도 진행 중 조사와 파일 저장을 계속한다. 브라우저는 재연결 시 서버의 같은 조사 ID와 현재 진행 상태를 다시 받는다. 최근 600개 그래프와 3000개 위치를 화면으로 받고 전체 기록은 파일로 받는다. GPS·IMU·DO가 오래되면 수상 조사는 정지한다. 프로세스 재시작·전원 재부팅 후에는 새 대기 상태이며 진행 중 조사를 자동 출발시키지 않는다. 기존 기록은 디스크에 남는다.

## 실내 시험

1. 센서를 PC 모드로 켠다. 기존 `do_receiver.py`와 Science# 등 센서를 점유하는 다른 수신 프로그램은 종료한다.
2. 대시보드에서 DO 수신을 확인한다.
3. **가상 GPS 위치 찍기**를 누르고 지도에 위치를 찍는다.
4. **경유지 추가**를 누르고 가까운 점들을 순서대로 지정한다.
5. **순서대로 조사 시작**을 누른다. 가상 위치가 이동하고 실제 DO가 격자와 시간 그래프에 기록된다.
6. **현재 조사 파일 받기**로 ZIP을 받는다. 가상 위치는 `position_source=simulated`로 표시한다.

## 수상 통합 브링업

기존 브링업과 기존 gps_map_node를 별도로 중복 실행하지 않는다. 새 통합 진입점은 다음과 같다.

```bash
# 서비스 방식: SSH를 종료해도 계속 실행
sudo systemctl start water-survey-field
# 또는 수동 실행 (서비스와 동시에 실행하지 않는다)
source /opt/ros/jazzy/setup.bash
source /home/ubuntu/ros2_ws/install/setup.bash
source /home/ubuntu/uros_ws/install/setup.bash
source /home/ubuntu/water_survey_ws/install/setup.bash
ros2 launch water_survey field.launch.py
```

두 서비스는 서로 배타적이다. field 서비스는 설치만 하며 자동 부팅 출발을 설정하지 않는다. 실제 GPS·IMU·DO와 추진 수신 노드가 준비되고 화면의 수상 주행 활성화를 확인해야 시작할 수 있다. 다른 추진 토픽 발행자가 있으면 시작을 막는다. 기본 추진 20%, 상한 30%, 도착 반경 3m, 지점 관측 10초다. 기존 MCU 펌웨어와 ESC 설정은 변경하지 않는다.

사용 토픽: `/gps/fix` (NavSatFix), `/imu` (Imu), `/actuator/key/degree` (Float64), `/actuator/thruster/percentage` (Float64). IMU는 ROS ENU 방향에 맞춰 북쪽 기준 방위로 변환한다. 설치 방향·방위 오프셋·실제 회전 방향은 현장에서 확인해야 한다.

## 파일과 재분석

기록 경로는 `/home/ubuntu/water_survey_data/`다. 조사별 폴더를 만들고 기록마다 CSV를 flush한다. 내보내기는 다음을 포함한다.

| 파일 | 내용 |
| --- | --- |
| `samples.csv` | DO, UTC 요청·수신 시간, GPS, 위치 출처, 경유지, IMU, 응답시간, 원본 응답 |
| `mission.json`, `events.jsonl` | 조사 조건, 경로, 상태, 도착·종료 이벤트 |
| `gps.jsonl`, `imu.jsonl`, `commands.jsonl` | 실제 GPS·IMU와 발행 명령; 해당 기록이 있을 때 생성 |
| `grid.csv`, `grid.geojson` | 관측 격자의 평균·최소·최대·개수와 도형 |
| `samples.geojson`, `route.geojson` | 측정 위치와 경유지 |
| `map.svg`, `timeseries.svg`, `report.html` | 확대 가능한 이미지와 브라우저에서 다시 열 수 있는 보고서 |

화면에서 격자·그래프 PNG 저장도 지원한다. 경로는 JSON으로 저장·불러온다. 지도 타일은 내보내기 ZIP에 포함하지 않는다. 재생성은 `python water_survey/scripts/plot_csv.py samples.csv --output figures`로 한다(matplotlib 필요).

DO 수신 시 최근 2초 이내 GPS를 결합하며, 시간은 선박 수신 시각이다. 센서 자체 측정 시각·프로브 반응 지연은 보정하지 않는다. 빈 격자는 미관측이며 색 범위 2~12 mg/L는 표시 기준이다. 수영강 현장에서는 수온·염분·조류·측정 깊이와 장착 위치를 함께 기록해야 한다. 센서 교정값은 변경하지 않는다.

자세한 운영 문서: [manual.html](water_survey/water_survey/static/manual.html) (대시보드의 사용 설명 버튼에서도 열 수 있음).

## 개발 확인

```bash
PYTHONPATH=water_survey python3 -m unittest discover -s water_survey/tests -v
```

13개 자동 확인 항목: 순차 경유·관측, 가상 모드 무출력, GPS/IMU/DO 지연 시 정지, 격자 평균, 내보내기, WebSocket 단절 후 조사 지속, 잘못된 요청 거부, 손상된 센서 프레임 거부. 실제 수상 주행 검증은 별도다.
