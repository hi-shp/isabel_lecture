import asyncio
import contextlib
import json
import math
import os
from pathlib import Path
import signal
import threading
import time
import uuid

from aiohttp import web
from .core import Survey, utc, ACTIVE
from .recording import Recorder, message_data
from .do_protocol import open_sensor, FrameParser, exchange, decode_snapshot
from .bringup import BringupManager
from .river_navigation import forward_scan


class SensorWorker(threading.Thread):
    def __init__(self, survey, root, address):
        super().__init__(daemon=True)
        self.survey, self.root, self.address = survey, Path(root), address
        self.stop_event = threading.Event()
        self.sock = None

    def run(self):
        rawdir=self.root/'sensor_raw'; rawdir.mkdir(parents=True, exist_ok=True)
        while not self.stop_event.is_set():
            try:
                self.survey.do_status='센서 연결 시도 중'
                self.sock, _ = open_sensor(self.address, 2)
                parser=FrameParser()
                name=utc().replace(':','').replace('.','')+'.jsonl'
                with self.sock as sock, (rawdir/name).open('a', encoding='utf-8') as raw:
                    sensor=exchange(sock,parser,'N','n',raw)[0][2:11].rstrip(b'\0').decode('ascii')
                    if not sensor.lower().startswith('wl118d'):
                        raise ValueError('Unexpected sensor identity: '+sensor)
                    firmware=exchange(sock,parser,'V','v',raw)[0][2:11].rstrip(b'\0').decode('ascii')
                    while not self.stop_event.is_set():
                        start=time.monotonic()
                        body,sent,received,latency=exchange(sock,parser,'S','stu',raw)
                        value,_=decode_snapshot(body)
                        self.survey.add_do(value,sent,received,latency,body.hex(),sensor,firmware)
                        self.stop_event.wait(max(0,1-(time.monotonic()-start)))
            except Exception as exc:
                self.survey.do_status='연결 끊김 · 자동 재시도: '+str(exc)[:180]
                print(self.survey.do_status, flush=True)
                self.stop_event.wait(3)
            finally:
                self.sock=None

    def stop(self):
        self.stop_event.set()
        if self.sock:
            with contextlib.suppress(OSError):
                self.sock.shutdown(2)


class Dashboard:
    def __init__(self, survey, recorder, static, bringup=None, manual_publish=None, neutral_publish=None):
        self.survey, self.recorder, self.static = survey, recorder, Path(static)
        self.clients={}
        self.bringup = bringup or BringupManager()
        self.manual_publish = manual_publish
        self.neutral_publish = neutral_publish
        self.last_stop = None
        self.control_revision = 0
        self.lifecycle_busy = False
        self.publish_count=0
        self.app=web.Application(client_max_size=100_000, middlewares=[self.errors])
        self.app.add_routes([web.get('/',self.index),web.get('/ws',self.websocket),
                             web.get('/api/state',self.state),web.get('/api/missions',self.missions),
                             web.get('/api/export/{mission}',self.export),web.post('/api/{action}',self.action)])
        self.app.router.add_static('/static/',self.static)
        self.app.on_startup.append(self.startup)
        self.app.on_cleanup.append(self.cleanup)

    @web.middleware
    async def errors(self, request, handler):
        if request.method == 'POST' or request.path == '/ws':
            if request.headers.get('Origin') not in ('http://'+request.host,'https://'+request.host):
                return web.json_response({'error':'같은 대시보드 화면에서 요청하세요.'},status=403)
        try:
            return await handler(request)
        except (ValueError, KeyError, TypeError) as exc:
            return web.json_response({'error':str(exc)},status=400)

    async def startup(self, app):
        self.pump=asyncio.create_task(self.broadcast())
        self.bringup_poll=asyncio.create_task(self.poll_bringup())

    async def poll_bringup(self):
        while True:
            await self.bringup.refresh()
            await asyncio.sleep(2)

    async def cleanup(self, app):
        self.survey.stop('대시보드 서버 종료')
        self.pump.cancel()
        self.bringup_poll.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.pump
        with contextlib.suppress(asyncio.CancelledError):
            await self.bringup_poll
        for ws in list(self.clients.values()):
            await ws.close()

    async def index(self,request):
        return web.FileResponse(self.static/'index.html',headers={'Cache-Control':'no-cache'})

    def data(self):
        with self.survey.lock:
            return dict(self.survey.snapshot(), hardware_publish_count=self.publish_count,
                        bringup=self.bringup.state, lifecycle_busy=self.lifecycle_busy,last_stop=self.last_stop,
                        control_revision=self.control_revision)

    def end_motion(self, reason='사용자가 주행을 종료했습니다.'):
        # Same lock as control snapshot + publish: an old nonzero command cannot
        # be published AFTER the immediate neutral command.
        with self.survey.lock:
            self.control_revision+=1
            self.survey.command.update(servo=90.,throttle=0.)
            published=False;publish_error=None;record_error=None
            # Send zero before filesystem finalisation, which may be slow.
            try:published=bool(self.neutral_publish and self.neutral_publish())
            except Exception as exc:publish_error=str(exc)[:180]
            try:self.survey.stop(reason,neutral=True)
            except OSError as exc:record_error=str(exc)[:180]
            self.last_stop=dict(utc=utc(),neutral_requested=True,neutral_published=published,
                actuator_acknowledged=False,publish_error=publish_error,record_error=record_error)

    async def state(self,request):
        return web.json_response(self.data())

    async def missions(self,request):
        return web.json_response(self.recorder.folders())

    async def export(self,request):
        data=await asyncio.to_thread(self.recorder.bundle,request.match_info['mission'])
        return web.Response(body=data,content_type='application/zip',headers={'Content-Disposition':'attachment; filename="water-survey.zip"'})

    async def action(self,request):
        action=request.match_info['action']
        # Stop is always accepted from this origin, including expired WS clients
        # and a request arriving during a longer bringup operation.
        if action == 'stop':
            self.end_motion()
            return web.json_response(self.data())
        data=await request.json()
        client=data.get('client')
        if client not in self.clients:
            raise ValueError('대시보드 연결이 만료됐습니다. 새로고침하세요.')
        if self.lifecycle_busy and action != 'stop':
            raise ValueError('브링업 요청을 처리하고 있습니다.')
        if action == 'live-update':
            with self.survey.lock:
                revision=data.get('control_revision')
                if type(revision) is not int or revision!=self.control_revision or data.get('mission_id')!=self.survey.mission_id:
                    raise ValueError('주행 상태가 바뀌었습니다. 최신 상태에서 다시 변경하세요.')
                try:self.survey.live_update(data.get('settings',{}),data.get('waypoint'),data.get('plan_revision'))
                except OSError as exc:
                    self.end_motion('설정 변경 기록 저장 실패로 정지')
                    raise ValueError('기록 저장 실패로 주행을 종료했습니다.') from exc
        elif action == 'configure':
            self.survey.configure(data['mode'],data['route'],data.get('settings',{}),
                                  data.get('pattern', 'waypoints'),data.get('area'))
        elif action == 'virtual':
            self.survey.set_virtual(data['lat'],data['lon'])
            if self.manual_publish:
                self.manual_publish(float(data['lat']), float(data['lon']))
        elif action == 'reset-position':
            self.survey.reset_position()
        elif action == 'new-map':
            self.survey.new_map()
        elif action in ('bringup-start', 'bringup-stop'):
            if action == 'bringup-start' and self.survey.state in ('running', 'dwelling'):
                raise ValueError('조사를 정지한 뒤 브링업을 시작하세요.')
            self.lifecycle_busy = True
            try:
                if action == 'bringup-stop':
                    self.end_motion('브링업 종료 요청')
                    await asyncio.sleep(2.2)  # Keep the base alive until zero-throttle messages are sent.
                await self.bringup.command(action.split('-')[1])
            finally:
                self.lifecycle_busy = False
        elif action in ('start','start-waypoints','start-autonomous'):
            with self.survey.lock:
                revision=data.get('control_revision',0)
                if type(revision) is not int or revision!=self.control_revision:
                    raise ValueError('종료 전에 전송된 시작 요청입니다. 화면 상태를 확인하고 다시 시작하세요.')
                navigation='river_zigzag' if action=='start-autonomous' else 'waypoint'
                self.survey.start(client,data.get('armed') is True,navigation=navigation)
        else:
            raise ValueError('알 수 없는 요청')
        return web.json_response(self.data())

    async def websocket(self,request):
        ws=web.WebSocketResponse(heartbeat=3,max_msg_size=2048)
        await ws.prepare(request); key=uuid.uuid4().hex; self.clients[key]=ws
        await ws.send_json({'type':'hello','client':key,'control_revision':self.control_revision})
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT and msg.data == 'heartbeat':
                    self.survey.pulse(key)
                elif msg.type == web.WSMsgType.TEXT:
                    try:
                        ping=json.loads(msg.data)
                        stamp=ping.get('sent')
                        if ping.get('type') == 'ping' and isinstance(stamp,(int,float)) and math.isfinite(stamp):
                            await ws.send_json({'type':'pong','sent':stamp})
                    except (ValueError, TypeError, AttributeError):
                        pass
        finally:
            self.clients.pop(key,None)
        return ws

    async def broadcast(self):
        while True:
            try:await asyncio.to_thread(self.recorder.sync)
            except OSError as exc:self.end_motion('기록 저장 오류로 주행 종료: '+str(exc)[:120])
            for key,ws in list(self.clients.items()):
                try:
                    await asyncio.wait_for(ws.send_json({'type':'state','data':self.data()}),1)
                except Exception:
                    await ws.close()
            await asyncio.sleep(.5)


def main(args=None):
    import rclpy
    from rclpy.node import Node
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import NavSatFix, Imu, LaserScan
    from std_msgs.msg import Float64

    class Bridge(Node):
        def __init__(self):
            super().__init__('water_survey_dashboard')
            for name,value in [('allow_hardware',False),('port',8000),('data_dir','/home/ubuntu/water_survey_data'),
                               ('manage_bringup',False),('default_mode','simulated'),
                               ('sensor_address','22:61:F4:4C:10:58'),('scan_topic','/scan'),('scan_yaw_offset_deg',0.)]:
                self.declare_parameter(name,value)
            self.hardware=self.get_parameter('allow_hardware').value
            self.recorder=Recorder(self.get_parameter('data_dir').value)
            self.recorder.context=dict(ros_topics={'gps':'/gps/fix','imu':'/imu',
                'lidar':self.get_parameter('scan_topic').value,
                'steering':'/actuator/key/degree','throttle':'/actuator/thruster/percentage'},
                scan_yaw_offset_deg=self.get_parameter('scan_yaw_offset_deg').value,
                heading_convention='degrees clockwise from north; IMU ENU yaw converted as (90-yaw)%360',
                allow_hardware=self.hardware)
            self.survey=Survey(self.hardware,recorder=self.recorder)
            self.survey.mode=self.get_parameter('default_mode').value
            self.manual_fix=self.create_publisher(NavSatFix,'/water_survey/manual_fix',10)
            self.dashboard=Dashboard(self.survey,self.recorder,Path(__file__).parent/'static',
                BringupManager(self.get_parameter('manage_bringup').value),self.publish_manual,self.neutral_now)
            self.create_subscription(NavSatFix,'/gps/fix',self.gps,qos_profile_sensor_data)
            self.create_subscription(Imu,'/imu',self.imu,qos_profile_sensor_data)
            self.create_subscription(LaserScan,self.get_parameter('scan_topic').value,self.scan,qos_profile_sensor_data)
            # Indoor launch has no actuator publishers at all.
            self.steering=None
            self.throttle=None
            self.create_timer(.1,self.tick)
            self.create_timer(1,self.check_graph)
            self.worker=SensorWorker(self.survey,self.recorder.root,self.get_parameter('sensor_address').value)
            self.worker.start()

        def publish_manual(self,lat,lon):
            msg=NavSatFix()
            msg.header.stamp=self.get_clock().now().to_msg()
            msg.header.frame_id='map_click'
            msg.latitude, msg.longitude = lat, lon
            msg.status.status=0
            self.manual_fix.publish(msg)

        def stamp(self,msg):
            return utc(msg.header.stamp.sec+msg.header.stamp.nanosec/1e9)

        def gps(self,msg):
            self.record_message('gps_raw',msg)
            cov=list(msg.position_covariance)
            cov=[v if math.isfinite(v) else None for v in cov]
            self.survey.gps_update(msg.latitude,msg.longitude,msg.status.status>=0 and not (msg.latitude==0 and msg.longitude==0),self.stamp(msg),cov)

        def imu(self,msg):
            self.record_message('imu_raw',msg)
            q=msg.orientation
            vals=[q.x,q.y,q.z,q.w]
            norm=sum(v*v for v in vals)
            if msg.orientation_covariance[0] == -1 or not all(math.isfinite(v) for v in vals) or not .8<=norm<=1.2:
                return
            yaw=math.degrees(math.atan2(2*(q.w*q.z+q.x*q.y),1-2*(q.y*q.y+q.z*q.z)))
            self.survey.imu_update((90-yaw)%360,self.stamp(msg))

        def scan(self,msg):
            self.record_message('lidar_raw',msg)
            age=self.get_clock().now().nanoseconds/1e9-msg.header.stamp.sec-msg.header.stamp.nanosec/1e9
            try:
                observation=forward_scan(msg.ranges,msg.angle_min,msg.angle_increment,msg.range_min,
                                         msg.range_max,self.get_parameter('scan_yaw_offset_deg').value)
                if not -.25<=age<=1:observation['valid']=False
            except (ValueError,TypeError,OverflowError):
                observation=dict(valid=False,front_mean_m=None,obstacle=False)
            self.survey.scan_update(observation,self.stamp(msg),source_age_s=max(0.,age))

        def record_message(self,kind,msg):
            with self.survey.lock:
                if self.survey.state in ACTIVE:
                    try:self.recorder.telemetry(kind,dict(received_utc=utc(),
                        received_monotonic_ns=time.monotonic_ns(),message=message_data(msg)))
                    except OSError as exc:
                        self.dashboard.end_motion('기록 저장 오류로 주행 종료: '+str(exc)[:120])

        def neutral_now(self):
            with self.survey.lock:
                if not self.hardware or self.survey.mode!='real' or self.throttle is None:return False
                self.publish(dict(servo=90.,throttle=0.))
                return True

        def check_graph(self):
            if not self.hardware or self.throttle is None:
                return
            competing=False
            for topic in ('/actuator/key/degree','/actuator/thruster/percentage'):
                if any(info.node_name!=self.get_name() for info in self.get_publishers_info_by_topic(topic)):
                    competing=True
            self.survey.hardware_ready=not competing and self.throttle.get_subscription_count()>0 and self.steering.get_subscription_count()>0
            self.survey.hardware_reason='다른 추진 제어 프로그램이 실행 중입니다.' if competing else '추진기 수신 노드가 연결되지 않았습니다.'

        def tick(self):
            with self.survey.lock:
                self.control_tick()

        def control_tick(self):
            try:
                # No propulsion publishers exist while using map-click positions.
                if self.hardware and self.survey.mode == 'real' and self.throttle is None:
                    self.steering=self.create_publisher(Float64,'/actuator/key/degree',10)
                    self.throttle=self.create_publisher(Float64,'/actuator/thruster/percentage',10)
                elif self.survey.mode != 'real' and self.throttle is not None:
                    self.destroy_publisher(self.steering); self.destroy_publisher(self.throttle)
                    self.steering=None; self.throttle=None
                    self.survey.hardware_ready=False
                self.survey.tick()
                cmd=self.survey.output()
                if cmd is not None:
                    self.publish(cmd)
            except Exception as exc:
                self.get_logger().error(str(exc))
                self.dashboard.end_motion('제어 처리 오류로 정지')

        def publish(self,cmd):
            self.throttle.publish(Float64(data=float(cmd['throttle'])))
            self.steering.publish(Float64(data=float(cmd['servo'])))
            self.dashboard.publish_count+=1
            self.recorder.telemetry('commands',dict(received_utc=utc(),**cmd))

    rclpy.init(args=args)
    node=Bridge()
    thread=threading.Thread(target=rclpy.spin,args=(node,),daemon=True); thread.start()
    try:
        web.run_app(node.dashboard.app,host='0.0.0.0',port=node.get_parameter('port').value,handle_signals=True)
    finally:
        node.survey.stop('프로그램 종료')
        if node.hardware and node.survey.output() is not None and rclpy.ok():
            node.publish(dict(servo=90.,throttle=0.))
        node.worker.stop()
        if rclpy.ok():
            rclpy.shutdown()
        thread.join(timeout=2)
        node.destroy_node()


if __name__=='__main__':
    main()
