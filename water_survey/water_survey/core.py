import collections
import datetime as dt
import math
import threading
import time
from .river_navigation import RiverZigzag, heading_error

R = 6378137.0
ACTIVE = ('running', 'dwelling')
SETTING_BOUNDS=dict(cell_m=(1,100),radius_m=(1,20),dwell_s=(3,120),throttle=(1,30),
                    obstacle_m=(.2,30),zigzag_deg=(0,80))
LIVE_SETTINGS=set(SETTING_BOUNDS)-{'cell_m'}


def checked_settings(current,changes,live=False):
    if not isinstance(changes,dict):raise ValueError('설정 형식 오류')
    opts=current.copy()
    for k,v in changes.items():
        if k=='sim_speed':continue
        if k not in SETTING_BOUNDS or (live and k not in LIVE_SETTINGS):
            raise ValueError('주행 중 격자 크기는 변경할 수 없습니다.' if k=='cell_m' else '알 수 없는 설정')
        n=float(v);lo,hi=SETTING_BOUNDS[k]
        if not math.isfinite(n) or not lo<=n<=hi:raise ValueError(f'{k} 범위: {lo}~{hi}')
        opts[k]=n
    return opts


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


def coverage_plan(area, size):
    sw = point(area['south'], area['west'])
    ne = point(area['north'], area['east'])
    if ne['lat'] <= sw['lat'] or ne['lon'] <= sw['lon']:
        raise ValueError('직사각형의 서로 다른 두 꼭짓점을 지정하세요.')
    w, h = xy(ne, sw)
    if min(w, h) < .5 or distance(sw, ne) > 2000:
        raise ValueError('조사 범위는 폭·높이 0.5m 이상, 대각선 2km 이내입니다.')
    cols, rows = math.ceil(w/size-1e-8), math.ceil(h/size-1e-8)
    if cols*rows > 2500:
        raise ValueError('최대 2500칸입니다. 범위를 줄이거나 격자를 크게 설정하세요.')
    cells = [(x, y) for y in range(rows) for x in (range(cols) if y % 2 == 0 else reversed(range(cols)))]
    route = [latlon((x*size+min((x+1)*size, w))/2,
                    (y*size+min((y+1)*size, h))/2, sw) for x, y in cells]
    return dict(origin=sw, width=w, height=h, cols=cols, rows=rows,
                targets=[f'{x},{y}' for x, y in cells]), route


class Survey:
    """Pure state machine. Virtual GPS never writes to ROS /gps/fix."""
    def __init__(self, hardware=False, clock=time.monotonic, recorder=None):
        self.lock = threading.RLock()
        self.clock, self.recorder, self.hardware = clock, recorder, hardware
        self.mode = 'real' if hardware else 'simulated'
        self.state = 'idle'
        self.route = []
        self.settings = dict(cell_m=5.0, radius_m=3.0, dwell_s=10.0, throttle=20.0,obstacle_m=2.,zigzag_deg=45.)
        self.plan_revision=0
        self.speed_m_s=None
        self.pattern, self.area, self.plan = 'waypoints', None, None
        self.dwell_sample = False
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
        self.do_range = None
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
        self.navigation = 'waypoint'
        self.zigzag = None
        self.guidance = None
        self.leg_origin = None
        self.scan = None
        self.scan_sequence = 0

    def position(self):
        return self.sim if self.mode == 'simulated' else self.gps

    def gps_update(self, lat, lon, valid, stamp, covariance=None):
        with self.lock:
            previous=self.gps
            try:
                p = point(lat, lon)
            except (ValueError, TypeError):
                valid = False
                p = {'lat': None, 'lon': None}
            self.gps = dict(p, valid=bool(valid), mono=self.clock(), stamp=stamp, received_utc=utc(), covariance=covariance)
            self.speed_m_s=None
            if valid and previous and previous['valid']:
                try:
                    elapsed=(dt.datetime.fromisoformat(stamp)-dt.datetime.fromisoformat(previous['stamp'])).total_seconds()
                    if .05<=elapsed<=5:self.speed_m_s=distance(previous,p)/elapsed
                except (ValueError,TypeError):pass
            if self.recorder and self.state in ACTIVE:
                self.recorder.telemetry('gps', self.gps)

    def imu_update(self, heading, stamp):
        if not math.isfinite(heading):
            return
        with self.lock:
            self.heading, self.imu_time = heading % 360, self.clock()
            if self.recorder and self.state in ACTIVE:
                self.recorder.telemetry('imu', dict(heading=self.heading, stamp=stamp, received_utc=utc()))

    def scan_update(self, observation, stamp, source_age_s=0.):
        with self.lock:
            if self.scan and self.scan['stamp']==stamp:return
            self.scan_sequence+=1
            self.scan=dict(observation,stamp=stamp,mono=self.clock()-max(0.,source_age_s),sequence=self.scan_sequence)
            self.classify_scan()
            if self.recorder and self.state in ACTIVE:
                self.recorder.telemetry('lidar',dict(self.scan,received_utc=utc()))

    def configure(self, mode, route, settings, pattern='waypoints', area=None):
        with self.lock:
            if self.state in ACTIVE:
                raise ValueError('먼저 정지한 뒤 경로 또는 모드를 바꾸세요.')
            if self.clock() < self.stop_until:
                raise ValueError('정지 명령 전송 중입니다. 2초 뒤에 변경하세요.')
            if mode not in ('real', 'simulated'):
                raise ValueError('알 수 없는 위치 모드')
            if pattern not in ('waypoints', 'coverage'):
                raise ValueError('알 수 없는 조사 방식')
            if not isinstance(route, list) or (pattern == 'waypoints' and len(route) > 100):
                raise ValueError('웨이포인트는 최대 100개입니다.')
            pts = [point(p['lat'], p['lon']) for p in route]
            if pts and any(distance(pts[0], p) > 2000 for p in pts):
                raise ValueError('한 조사 구역은 첫 지점에서 2km 이내로 지정하세요.')
            opts = checked_settings(self.settings,settings)
            plan = None
            if pattern == 'coverage':
                if not isinstance(area, dict):
                    raise ValueError('먼저 직사각형 조사 범위를 지정하세요.')
                area = {k: float(area[k]) for k in ('south', 'west', 'north', 'east')}
                plan, pts = coverage_plan(area, opts['cell_m'])
            self.mode, self.route, self.settings = mode, pts, opts
            self.pattern, self.area, self.plan = pattern, area if plan else None, plan
            self.armed = False
            self.plan_revision+=1
            self.classify_scan()

    def classify_scan(self):
        if self.scan:
            mean=self.scan.get('front_mean_m')
            self.scan['obstacle_m']=self.settings['obstacle_m']
            self.scan['obstacle']=bool(self.scan.get('valid') and mean is not None and mean<=self.settings['obstacle_m'])

    def live_update(self,settings,waypoint,revision):
        """Atomic edits that preserve arming, progress, cells and mission identity."""
        with self.lock:
            if type(revision) is not int or revision!=self.plan_revision:
                raise ValueError('다른 변경이 먼저 적용됐습니다. 최신 상태에서 다시 변경하세요.')
            opts=checked_settings(self.settings,settings,live=True)
            route=list(self.route);target_changed=False
            if waypoint is not None:
                if self.pattern!='waypoints':raise ValueError('직사각형 셀 경로는 드래그할 수 없습니다.')
                i=waypoint.get('index')
                if type(i) is not int or not 0<=i<len(route):raise ValueError('유효하지 않은 경유지 번호')
                p=point(waypoint['lat'],waypoint['lon']);route[i]=p
                if any(distance(route[0],v)>2000 for v in route):raise ValueError('한 조사 구역은 첫 지점에서 2km 이내로 지정하세요.')
                if self.position() and self.position()['valid'] and distance(self.position(),p)>2000:
                    raise ValueError('경유지는 현재 위치에서 2km 이내로 지정하세요.')
                target_changed=i==self.index
            old_settings=self.settings.copy();old_route=self.route
            if self.recorder and self.state in ACTIVE:
                # Save accepted intent before changing the active controller.
                self.recorder.event('live_update',dict(revision=self.plan_revision+1,waypoint=waypoint,
                    previous_settings=old_settings,settings=opts,previous_route=old_route,route=route))
                self.recorder.meta.update(settings=opts.copy(),route=route,plan_revision=self.plan_revision+1)
                self.recorder.sync(force=True)
            self.settings=opts;self.route=route;self.plan_revision+=1;self.classify_scan()
            if target_changed:
                self.arrivals=0;self.arrival_gps_time=None
                if self.state in ACTIVE:
                    self.state='running';self.dwell_start=None;self.dwell_sample=False
                if self.zigzag:self.zigzag.course=None
            elif old_settings['radius_m']!=opts['radius_m']:
                self.arrivals=0;self.arrival_gps_time=None
            if self.state in ACTIVE:self.tick()

    def set_virtual(self, lat, lon):
        with self.lock:
            if self.mode != 'simulated':
                raise ValueError('먼저 지도 위치 입력을 선택하세요.')
            self.sim = dict(point(lat, lon), valid=True, mono=self.clock(), stamp=utc(), received_utc=utc())
            if self.recorder and self.state in ACTIVE:
                self.recorder.telemetry('map_click',self.sim)

    def reset_position(self):
        with self.lock:
            if self.state in ACTIVE:
                raise ValueError('먼저 조사를 정지하세요.')
            self.sim = None
            self.mode = 'real' if self.hardware else 'simulated'

    def new_map(self):
        with self.lock:
            if self.state in ACTIVE or self.clock() < self.stop_until:
                raise ValueError('먼저 조사를 정지하고 정지 명령 전송을 기다리세요.')
            self.reset_position()
            self.state, self.reason, self.mission_id = 'idle', '', None
            self.route, self.cells, self.area, self.plan = [], {}, None, None
            self.pattern, self.origin, self.index = 'waypoints', None, 0
            self.navigation, self.zigzag, self.guidance, self.leg_origin = 'waypoint', None, None, None
            self.plan_revision+=1
            self.count, self.mapped = 0, 0
            self.do_range = None
            self.track.clear(); self.samples.clear()

    def cell_at(self, p):
        x, y = xy(p, self.origin)
        size = self.settings['cell_m']
        if self.plan and not (0 <= x < self.plan['width'] and 0 <= y < self.plan['height']):
            return None
        return math.floor(x/size), math.floor(y/size)

    def pulse(self, owner):
        with self.lock:
            if owner == self.owner:
                self.heartbeat = self.clock()

    def prerequisites(self, navigation=None):
        navigation = navigation or self.navigation
        now = self.clock()
        p = self.position()
        if not p or not p['valid']:
            return 'GPS 위치를 기다리고 있습니다.'
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
        if navigation == 'river_zigzag':
            if self.pattern != 'waypoints':return '중앙 경유지를 지정한 뒤 자율운항을 시작하세요.'
            if self.heading is None or now-self.imu_time > 2:return '자율운항용 IMU 방향을 기다리고 있습니다.'
            if not self.scan or now-self.scan['mono'] > 1:return '자율운항용 라이다 스캔이 없거나 1초 이상 지연됐습니다.'
            if not self.scan['valid']:return '라이다 정면 1° 구간의 유효 측정을 기다리고 있습니다.'
        return None

    def start(self, owner, armed=False, navigation='waypoint'):
        with self.lock:
            if navigation not in ('waypoint','river_zigzag'):raise ValueError('알 수 없는 주행 방식')
            if self.state in ACTIVE:
                raise ValueError('이미 조사가 진행 중입니다.')
            if self.clock()<self.stop_until:raise ValueError('중립 명령 전송 중입니다. 잠시 후 다시 시작하세요.')
            if not self.route:
                raise ValueError('지도에서 웨이포인트를 먼저 지정하세요.')
            reason = self.prerequisites(navigation)
            if reason:
                raise ValueError(reason)
            if self.mode == 'real' and armed is not True:
                raise ValueError('수상 주행 활성화를 확인하세요.')
            if distance(self.position(), self.route[0]) > 2000:
                raise ValueError('첫 웨이포인트가 현재 위치에서 2km 밖에 있습니다.')
            self.owner, self.heartbeat, self.armed = owner, self.clock(), self.mode == 'real'
            self.navigation = navigation
            self.zigzag = RiverZigzag() if navigation == 'river_zigzag' else None
            self.guidance = None
            self.leg_origin = point(self.position()['lat'],self.position()['lon'])
            self.state, self.reason, self.index = 'running', '', 0
            self.plan_revision+=1
            self.origin = self.plan['origin'] if self.plan else point(self.route[0]['lat'], self.route[0]['lon'])
            self.cells, self.count, self.mapped = {}, 0, 0
            self.do_range = None
            self.samples.clear()
            self.track.clear()
            self.dwell_start, self.arrivals, self.arrival_gps_time = None, 0, None
            if self.recorder:
                try:self.begin_recording(navigation)
                except OSError as exc:
                    self.armed=False;self.state='stopped'
                    self.reason='기록 파일을 열 수 없어 주행 시작을 취소했습니다.'
                    self.command.update(servo=90.,throttle=0.)
                    if self.hardware and self.mode=='real':self.stop_until=self.clock()+2
                    try:self.recorder.finish('failed',self.reason)
                    except OSError:pass
                    raise ValueError(self.reason+' '+str(exc)) from exc
            self.last_tick = self.clock()

    def begin_recording(self,navigation):
        self.mission_id = self.recorder.start(self.mode, self.route, self.settings, self.origin)
        self.recorder.meta.update(pattern=self.pattern, area=self.area, plan=self.plan,
            position_input='map_click' if self.mode == 'simulated' else 'gps',navigation=navigation,
            initial_position=self.position().copy(),initial_heading_deg=self.heading,
            initial_route=list(self.route),initial_settings=self.settings.copy(),plan_revision=self.plan_revision)
        if self.zigzag:
            self.recorder.meta['river_zigzag']=dict(offset_deg=self.settings['zigzag_deg'],obstacle_m=self.settings['obstacle_m'],trim_each_tail=.2,
                front_interval_deg=[-.5,.5],switch_on='new_front_obstacle',
                waypoint_transition='two fresh positions inside radius or past centreline cross-section')
        self.recorder.save_meta()

    def stop(self, reason='사용자가 정지했습니다.', complete=False, neutral=False):
        with self.lock:
            was_armed=self.armed
            was_active=self.state in ACTIVE
            if was_active:
                self.stop_until = self.clock()+2 if self.armed else -1e9
                self.state = 'complete' if complete else 'stopped'
                self.reason = reason
            self.command.update(throttle=0.0, servo=90.0)
            self.armed = False
            if (was_armed or neutral) and self.hardware and self.mode=='real':
                self.stop_until=max(self.stop_until,self.clock()+2)
            if was_active and self.recorder:
                self.recorder.telemetry('navigation',dict(received_utc=utc(),mono=self.clock(),
                    state=self.state,waypoint=self.index+1,navigation=self.navigation,
                    command=self.command,armed=False,reason=reason))
                self.recorder.finish(self.state, reason)

    def autonomous_command(self, p, target, dist, angle):
        """Centreline points guide continuous river progress, without a dwell."""
        gx,gy=xy(target,self.leg_origin);px,py=xy(p,self.leg_origin)
        passed=(gx*gx+gy*gy>=1 and px*gx+py*gy>=gx*gx+gy*gy)
        reached=dist<=self.settings['radius_m'] or passed
        if p['mono']!=self.arrival_gps_time:
            self.arrivals=self.arrivals+1 if reached else 0
            self.arrival_gps_time=p['mono']
        if self.arrivals>=2:
            if self.recorder:
                self.recorder.event('guidance_passed',dict(waypoint=self.index+1,
                    within_radius=dist<=self.settings['radius_m'],past_cross_section=passed))
            self.leg_origin=point(target['lat'],target['lon'])
            self.index+=1;self.arrivals=0;self.arrival_gps_time=p['mono']
            if self.index==len(self.route):
                self.stop('마지막 중앙 경유지 구간을 통과했습니다.',complete=True)
                return
            target=self.route[self.index];dist=distance(p,target);angle=bearing(p,target)
        self.guidance=self.zigzag.guide(angle,self.heading,self.index,self.scan,
            self.settings['zigzag_deg'],self.settings['obstacle_m'])
        self.command.update(distance=dist,bearing=angle,servo=max(30.,min(150.,
            90.+heading_error(self.guidance['course_deg'],self.heading))),
            throttle=self.settings['throttle'] if self.mode=='real' else 0.)
        if self.mode=='simulated':self.command['servo']=90.
        if self.recorder and self.guidance['event']:
            self.recorder.event(self.guidance['event'],dict(self.guidance,waypoint=self.index+1,
                scan_sequence=self.scan['sequence'],scan_stamp=self.scan['stamp']))

    def tick(self):
        with self.lock:
            self.control_step()
            if self.recorder and self.state in ACTIVE:
                self.recorder.telemetry('navigation',dict(received_utc=utc(),mono=self.clock(),
                    state=self.state,waypoint=self.index+1,navigation=self.navigation,
                    position=self.position(),heading_deg=self.heading,guidance=self.guidance,
                    command=self.command,armed=self.armed))

    def control_step(self):
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
            if self.navigation=='river_zigzag':
                self.autonomous_command(p,target,dist,angle)
                return
            if self.state == 'running':
                if self.mode == 'simulated':
                    self.command.update(servo=90.0, throttle=0.0)
                    arrived = dist <= self.settings['radius_m']
                else:
                    error = (angle-self.heading+180) % 360-180
                    self.command.update(servo=max(30.0, min(150.0, 90.0+error)), throttle=self.settings['throttle'])
                    if p['mono'] != self.arrival_gps_time:
                        self.arrivals = self.arrivals+1 if dist <= self.settings['radius_m'] else 0
                        self.arrival_gps_time = p['mono']
                    arrived = self.arrivals >= 2
                if self.plan:
                    cell = self.cell_at(p)
                    arrived = arrived and cell is not None and f'{cell[0]},{cell[1]}' == self.plan['targets'][self.index]
                if arrived:
                    self.state, self.dwell_start = 'dwelling', now
                    self.dwell_sample = False
                    self.command.update(servo=90.0, throttle=0.0)
                    if self.recorder:
                        self.recorder.event('arrived', {'waypoint': self.index+1})
            elif self.state == 'dwelling':
                self.command.update(servo=90.0, throttle=0.0)
                cell = self.cell_at(p) if self.plan else None
                outside_target = self.plan and (cell is None or f'{cell[0]},{cell[1]}' != self.plan['targets'][self.index])
                if outside_target or dist > self.settings['radius_m']*1.5:
                    self.state, self.arrivals, self.dwell_start = 'running', 0, None
                elif now-self.dwell_start >= self.settings['dwell_s'] and (not self.plan or self.dwell_sample):
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
            if self.do_range is None:
                self.do_range = [value, value]
            else:
                self.do_range = [min(self.do_range[0], value), max(self.do_range[1], value)]
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
                self.track.append([p['lat'], p['lon']])
            cell_position = self.cell_at(p) if position_ok else None
            if cell_position is not None:
                ix, iy = cell_position
                key = f'{ix},{iy}'
                cell = self.cells.setdefault(key, dict(ix=ix, iy=iy, count=0, total=0., min=value, max=value))
                cell.update(count=cell['count']+1, total=cell['total']+value,
                            min=min(value, cell['min']), max=max(value, cell['max']))
                self.mapped += 1
                if self.state == 'dwelling' and (not self.plan or key == self.plan['targets'][self.index]):
                    self.dwell_sample = True
                row.update(cell_x=ix, cell_y=iy)
            else:
                row.update(cell_x=None, cell_y=None)
            if self.recorder:
                self.recorder.sample(row)

    def snapshot(self):
        with self.lock:
            p = self.position()
            now = self.clock()
            values = [c['total']/c['count'] for c in self.cells.values()]
            scale = [min(values), max(values)] if values else self.do_range
            return dict(mode=self.mode, state=self.state, route=self.route, settings=self.settings,
                        plan_revision=self.plan_revision,
                        speed_m_s=self.speed_m_s if self.mode=='real' and self.gps and now-self.gps['mono']<=3 else None,
                        navigation=self.navigation,guidance=self.guidance,
                        lidar={k:v for k,v in self.scan.items() if k!='mono'} if self.scan else None,
                        lidar_age_s=now-self.scan['mono'] if self.scan else None,
                        autonomy_ready_reason=self.prerequisites('river_zigzag'),
                        color_scale=scale, do_range=self.do_range,
                        pattern=self.pattern, area=self.area, plan=self.plan,
                        coverage=dict(total=len(self.plan['targets']), observed=sum(k in self.cells for k in self.plan['targets'])) if self.plan else None,
                        position=p, heading=self.heading, imu_age_s=now-self.imu_time if self.heading is not None else None,
                        gps_age_s=now-self.gps['mono'] if self.gps else None,
                        do=self.do, do_age_s=now-self.do_time if self.do else None, do_status=self.do_status,
                        command=self.command, waypoint=self.index, reason=self.reason,
                        count=self.count, mapped=self.mapped, cells=[dict(c, mean=c['total']/c['count']) for c in self.cells.values()],
                        samples=list(self.samples), track=list(self.track), origin=self.origin,
                        hardware=self.hardware, armed=self.armed, ready_reason=self.prerequisites('waypoint'),
                        mission_id=self.mission_id, timestamp=utc())
