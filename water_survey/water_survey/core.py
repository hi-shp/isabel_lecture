import collections
import datetime as dt
import math
import threading
import time

R = 6378137.0
ACTIVE = ('running', 'dwelling')


def utc(epoch=None):
    return dt.datetime.fromtimestamp(time.time() if epoch is None else epoch, dt.timezone.utc).isoformat(timespec='milliseconds')


def point(lat, lon):
    lat, lon = float(lat), float(lon)
    if not (math.isfinite(lat) and math.isfinite(lon) and -85 <= lat <= 85 and -180 <= lon <= 180):
        raise ValueError('유효한 위도·경도를 입력하세요.')
    return {'lat': lat, 'lon': lon}


def distance(a, b):
    p, q = math.radians(a['lat']), math.radians(b['lat'])
    h = math.sin((q-p)/2)**2 + math.cos(p)*math.cos(q)*math.sin(math.radians(b['lon']-a['lon'])/2)**2
    return 2*R*math.asin(min(1, math.sqrt(h)))


def bearing(a, b):
    p, q, dl = math.radians(a['lat']), math.radians(b['lat']), math.radians(b['lon']-a['lon'])
    return math.degrees(math.atan2(math.sin(dl)*math.cos(q), math.cos(p)*math.sin(q)-math.sin(p)*math.cos(q)*math.cos(dl))) % 360


def xy(p, origin):
    return R*math.radians(p['lon']-origin['lon'])*math.cos(math.radians(origin['lat'])), R*math.radians(p['lat']-origin['lat'])


def latlon(x, y, origin):
    return point(origin['lat']+math.degrees(y/R), origin['lon']+math.degrees(x/(R*math.cos(math.radians(origin['lat'])))))


class Survey:
    """Pure state machine. Virtual GPS never writes to ROS /gps/fix."""
    def __init__(self, hardware=False, clock=time.monotonic, recorder=None):
        self.lock = threading.RLock()
        self.clock, self.recorder, self.hardware = clock, recorder, hardware
        self.mode = 'real' if hardware else 'simulated'
        self.state = 'idle'
        self.route = []
        self.settings = dict(cell_m=5.0, radius_m=3.0, dwell_s=10.0, throttle=20.0, sim_speed=1.5)
        self.gps = None
        self.sim = None
        self.heading = None
        self.imu_time = -1e9
        self.do = None
        self.do_status = '센서 연결 대기'
        self.do_time = -1e9
        self.owner = None
        self.heartbeat = -1e9
        self.armed = False
        self.index = 0
        self.origin = None
        self.cells = {}
        self.samples = collections.deque(maxlen=600)
        self.track = collections.deque(maxlen=3000)
        self.count = 0
        self.mapped = 0
        self.reason = ''
        self.dwell_start = None
        self.arrivals = 0
        self.arrival_gps_time = None
        self.command = dict(servo=90.0, throttle=0.0, bearing=None, distance=None)
        self.hardware_ready = False
        self.hardware_reason = '추진 제어 노드 대기'
        self.stop_until = -1e9
        self.last_tick = self.clock()
        self.mission_id = None

    def position(self):
        return self.sim if self.mode == 'simulated' else self.gps

    def gps_update(self, lat, lon, valid, stamp, covariance=None):
        with self.lock:
            try:
                p = point(lat, lon)
            except (ValueError, TypeError):
                valid = False
                p = {'lat': None, 'lon': None}
            self.gps = dict(p, valid=bool(valid), mono=self.clock(), stamp=stamp, received_utc=utc(), covariance=covariance)
            if self.recorder and self.state in ACTIVE:
                self.recorder.telemetry('gps', self.gps)

    def imu_update(self, heading, stamp):
        if not math.isfinite(heading):
            return
        with self.lock:
            self.heading, self.imu_time = heading % 360, self.clock()
            if self.recorder and self.state in ACTIVE:
                self.recorder.telemetry('imu', dict(heading=self.heading, stamp=stamp, received_utc=utc()))

    def configure(self, mode, route, settings):
        with self.lock:
            if self.state in ACTIVE:
                raise ValueError('먼저 정지한 뒤 경로 또는 모드를 바꾸세요.')
            if self.clock() < self.stop_until:
                raise ValueError('정지 명령 전송 중입니다. 2초 뒤에 변경하세요.')
            if mode not in ('real', 'simulated'):
                raise ValueError('알 수 없는 위치 모드')
            if not isinstance(route, list) or len(route) > 100:
                raise ValueError('웨이포인트는 최대 100개입니다.')
            pts = [point(p['lat'], p['lon']) for p in route]
            if pts and any(distance(pts[0], p) > 2000 for p in pts):
                raise ValueError('한 조사 구역은 첫 지점에서 2km 이내로 지정하세요.')
            bounds = dict(cell_m=(1, 100), radius_m=(1, 20), dwell_s=(3, 120), throttle=(1, 30), sim_speed=(.2, 10))
            opts = self.settings.copy()
            for k, v in settings.items():
                if k not in bounds:
                    raise ValueError('알 수 없는 설정')
                n = float(v)
                if not math.isfinite(n) or not bounds[k][0] <= n <= bounds[k][1]:
                    raise ValueError(f'{k} 범위: {bounds[k][0]}~{bounds[k][1]}')
                opts[k] = n
            self.mode, self.route, self.settings = mode, pts, opts
            self.armed = False

    def set_virtual(self, lat, lon):
        with self.lock:
            if self.mode != 'simulated' or self.state in ACTIVE:
                raise ValueError('정지 상태의 가상 GPS 모드에서 지정하세요.')
            self.sim = dict(point(lat, lon), valid=True, mono=self.clock(), stamp=utc(), received_utc=utc())

    def pulse(self, owner):
        with self.lock:
            if owner == self.owner:
                self.heartbeat = self.clock()

    def prerequisites(self):
        now = self.clock()
        p = self.position()
        if not p or not p['valid']:
            return '유효한 GPS 위치가 없습니다. 실내에서는 가상 위치를 지정하세요.'
        if self.mode == 'real':
            if not self.hardware:
                return '실내 실행에서는 추진 명령이 비활성화되어 있습니다.'
            if now-p['mono'] > 3:
                return 'GPS 수신이 3초 이상 지연됐습니다.'
            if self.heading is None or now-self.imu_time > 2:
                return 'IMU 수신이 2초 이상 지연됐습니다.'
            if not self.hardware_ready:
                return self.hardware_reason
        if self.do is None or now-self.do_time > 5:
            return 'DO 센서의 새 측정값을 기다리고 있습니다.'
        return None

    def start(self, owner, armed=False):
        with self.lock:
            if self.state in ACTIVE:
                raise ValueError('이미 조사가 진행 중입니다.')
            if not self.route:
                raise ValueError('지도에서 웨이포인트를 먼저 지정하세요.')
            reason = self.prerequisites()
            if reason:
                raise ValueError(reason)
            if self.mode == 'real' and armed is not True:
                raise ValueError('수상 주행 활성화를 확인하세요.')
            if distance(self.position(), self.route[0]) > 2000:
                raise ValueError('첫 웨이포인트가 현재 위치에서 2km 밖에 있습니다.')
            self.owner, self.heartbeat, self.armed = owner, self.clock(), self.mode == 'real'
            self.state, self.reason, self.index = 'running', '', 0
            self.origin = point(self.route[0]['lat'], self.route[0]['lon'])
            self.cells, self.count, self.mapped = {}, 0, 0
            self.samples.clear()
            self.track.clear()
            self.dwell_start, self.arrivals, self.arrival_gps_time = None, 0, None
            if self.recorder:
                self.mission_id = self.recorder.start(self.mode, self.route, self.settings, self.origin)
            self.last_tick = self.clock()

    def stop(self, reason='사용자가 정지했습니다.', complete=False):
        with self.lock:
            if self.state in ACTIVE:
                self.stop_until = self.clock()+2 if self.armed else -1e9
                self.state = 'complete' if complete else 'stopped'
                self.reason = reason
                self.command.update(throttle=0.0, servo=90.0)
                if self.recorder:
                    self.recorder.finish(self.state, reason)
            self.armed = False

    def tick(self):
        with self.lock:
            now = self.clock()
            delta = max(0, min(.5, now-self.last_tick))
            self.last_tick = now
            if self.state not in ACTIVE:
                return
            reason = self.prerequisites()
            if reason:
                self.stop(reason)
                return
            p, target = self.position(), self.route[self.index]
            dist, angle = distance(p, target), bearing(p, target)
            self.command.update(distance=dist, bearing=angle)
            if self.state == 'running':
                if self.mode == 'simulated':
                    step = min(dist, self.settings['sim_speed']*delta)
                    if dist > .001:
                        self.sim = dict(point(p['lat']+(target['lat']-p['lat'])*step/dist,
                                              p['lon']+(target['lon']-p['lon'])*step/dist),
                                        mono=now, valid=True, stamp=utc(), received_utc=utc())
                    self.command.update(servo=90.0, throttle=0.0)
                    arrived = dist <= .3
                else:
                    error = (angle-self.heading+180) % 360-180
                    self.command.update(servo=max(30.0, min(150.0, 90.0+error)), throttle=self.settings['throttle'])
                    if p['mono'] != self.arrival_gps_time:
                        self.arrivals = self.arrivals+1 if dist <= self.settings['radius_m'] else 0
                        self.arrival_gps_time = p['mono']
                    arrived = self.arrivals >= 2
                if arrived:
                    self.state, self.dwell_start = 'dwelling', now
                    self.command.update(servo=90.0, throttle=0.0)
                    if self.recorder:
                        self.recorder.event('arrived', {'waypoint': self.index+1})
            elif self.state == 'dwelling':
                self.command.update(servo=90.0, throttle=0.0)
                if self.mode == 'real' and dist > self.settings['radius_m']*1.5:
                    self.state, self.arrivals, self.dwell_start = 'running', 0, None
                elif now-self.dwell_start >= self.settings['dwell_s']:
                    self.index += 1
                    if self.index == len(self.route):
                        self.stop('모든 웨이포인트의 관측을 마쳤습니다.', complete=True)
                    else:
                        self.state, self.arrivals, self.dwell_start = 'running', 0, None

    def output(self):
        with self.lock:
            if not self.hardware or self.mode != 'real':
                return None
            if self.armed and self.state in ACTIVE:
                return self.command.copy()
            if self.clock() < self.stop_until:
                return dict(servo=90.0, throttle=0.0)
            return None

    def add_do(self, value, sent, received, response_ms, raw_hex, sensor, firmware):
        if not math.isfinite(value) or not 0 <= value <= 50:
            raise ValueError('DO 값이 센서 범위를 벗어났습니다.')
        with self.lock:
            now = self.clock()
            self.do_time = now
            self.do_status = '연결됨'
            self.do = dict(value=value, received_utc=received, sensor=sensor, firmware=firmware)
            if self.state not in ACTIVE:
                return
            p = self.position()
            age = now-p['mono'] if p else None
            position_ok = bool(p and p['valid'] and (self.mode == 'simulated' or age <= 2))
            self.count += 1
            row = dict(sequence=self.count, request_utc=sent, received_utc=received, do_mg_l=round(value, 6),
                       lat=p['lat'] if position_ok else None, lon=p['lon'] if position_ok else None,
                       position_source=self.mode, do_source='sensor', gps_valid=position_ok,
                       gps_age_s=round(age, 4) if age is not None else None,
                       gps_stamp=p['stamp'] if p else None,
                       heading_deg=self.heading if self.mode == 'real' and now-self.imu_time <= 2 else None,
                       waypoint=self.index+1, phase=self.state, response_ms=response_ms,
                       sensor=sensor, firmware=firmware, payload_hex=raw_hex)
            self.samples.append(row)
            if position_ok:
                x, y = xy(p, self.origin)
                size = self.settings['cell_m']
                ix, iy = math.floor(x/size), math.floor(y/size)
                key = f'{ix},{iy}'
                cell = self.cells.setdefault(key, dict(ix=ix, iy=iy, count=0, total=0., min=value, max=value))
                cell.update(count=cell['count']+1, total=cell['total']+value,
                            min=min(value, cell['min']), max=max(value, cell['max']))
                self.mapped += 1
                self.track.append([p['lat'], p['lon']])
                row.update(cell_x=ix, cell_y=iy)
            else:
                row.update(cell_x=None, cell_y=None)
            if self.recorder:
                self.recorder.sample(row)

    def snapshot(self):
        with self.lock:
            p = self.position()
            now = self.clock()
            return dict(mode=self.mode, state=self.state, route=self.route, settings=self.settings,
                        position=p, heading=self.heading, imu_age_s=now-self.imu_time if self.heading is not None else None,
                        gps_age_s=now-self.gps['mono'] if self.gps else None,
                        do=self.do, do_age_s=now-self.do_time if self.do else None, do_status=self.do_status,
                        command=self.command, waypoint=self.index, reason=self.reason,
                        count=self.count, mapped=self.mapped, cells=[dict(c, mean=c['total']/c['count']) for c in self.cells.values()],
                        samples=list(self.samples), track=list(self.track), origin=self.origin,
                        hardware=self.hardware, armed=self.armed, ready_reason=self.prerequisites(),
                        mission_id=self.mission_id, timestamp=utc())
