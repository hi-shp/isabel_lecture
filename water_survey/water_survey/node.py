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
from .core import Survey, utc
from .recording import Recorder
from .do_protocol import open_sensor, FrameParser, exchange, decode_snapshot


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
    def __init__(self, survey, recorder, static):
        self.survey, self.recorder, self.static = survey, recorder, Path(static)
        self.clients={}
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

    async def cleanup(self, app):
        self.survey.stop('대시보드 서버 종료')
        self.pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.pump
        for ws in list(self.clients.values()):
            await ws.close()

    async def index(self,request):
        return web.FileResponse(self.static/'index.html',headers={'Cache-Control':'no-cache'})

    def data(self):
        return dict(self.survey.snapshot(), hardware_publish_count=self.publish_count)

    async def state(self,request):
        return web.json_response(self.data())

    async def missions(self,request):
        return web.json_response(self.recorder.folders())

    async def export(self,request):
        data=await asyncio.to_thread(self.recorder.bundle,request.match_info['mission'])
        return web.Response(body=data,content_type='application/zip',headers={'Content-Disposition':'attachment; filename="water-survey.zip"'})

    async def action(self,request):
        data=await request.json(); action=request.match_info['action']
        client=data.get('client')
        if client not in self.clients:
            raise ValueError('대시보드 연결이 만료됐습니다. 새로고침하세요.')
        if action == 'configure':
            self.survey.configure(data['mode'],data['route'],data.get('settings',{}))
        elif action == 'virtual':
            self.survey.set_virtual(data['lat'],data['lon'])
        elif action == 'start':
            self.survey.start(client,data.get('armed') is True)
        elif action == 'stop':
            self.survey.stop()
        else:
            raise ValueError('알 수 없는 요청')
        return web.json_response(self.data())

    async def websocket(self,request):
        ws=web.WebSocketResponse(heartbeat=3,max_msg_size=2048)
        await ws.prepare(request); key=uuid.uuid4().hex; self.clients[key]=ws
        await ws.send_json({'type':'hello','client':key})
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT and msg.data == 'heartbeat':
                    self.survey.pulse(key)
        finally:
            self.clients.pop(key,None)
        return ws

    async def broadcast(self):
        while True:
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
    from sensor_msgs.msg import NavSatFix, Imu
    from std_msgs.msg import Float64

    class Bridge(Node):
        def __init__(self):
            super().__init__('water_survey_dashboard')
            for name,value in [('allow_hardware',False),('port',8000),('data_dir','/home/ubuntu/water_survey_data'),
                               ('sensor_address','22:61:F4:4C:10:58')]:
                self.declare_parameter(name,value)
            self.hardware=self.get_parameter('allow_hardware').value
            self.recorder=Recorder(self.get_parameter('data_dir').value)
            self.survey=Survey(self.hardware,recorder=self.recorder)
            self.dashboard=Dashboard(self.survey,self.recorder,Path(__file__).parent/'static')
            self.create_subscription(NavSatFix,'/gps/fix',self.gps,qos_profile_sensor_data)
            self.create_subscription(Imu,'/imu',self.imu,qos_profile_sensor_data)
            # Indoor launch has no actuator publishers at all.
            if self.hardware:
                self.steering=self.create_publisher(Float64,'/actuator/key/degree',10)
                self.throttle=self.create_publisher(Float64,'/actuator/thruster/percentage',10)
            self.create_timer(.1,self.tick)
            self.create_timer(1,self.check_graph)
            self.worker=SensorWorker(self.survey,self.recorder.root,self.get_parameter('sensor_address').value)
            self.worker.start()

        def stamp(self,msg):
            return utc(msg.header.stamp.sec+msg.header.stamp.nanosec/1e9)

        def gps(self,msg):
            cov=list(msg.position_covariance)
            cov=[v if math.isfinite(v) else None for v in cov]
            self.survey.gps_update(msg.latitude,msg.longitude,msg.status.status>=0 and not (msg.latitude==0 and msg.longitude==0),self.stamp(msg),cov)

        def imu(self,msg):
            q=msg.orientation
            vals=[q.x,q.y,q.z,q.w]
            norm=sum(v*v for v in vals)
            if msg.orientation_covariance[0] == -1 or not all(math.isfinite(v) for v in vals) or not .8<=norm<=1.2:
                return
            yaw=math.degrees(math.atan2(2*(q.w*q.z+q.x*q.y),1-2*(q.y*q.y+q.z*q.z)))
            self.survey.imu_update((90-yaw)%360,self.stamp(msg))

        def check_graph(self):
            if not self.hardware:
                return
            competing=False
            for topic in ('/actuator/key/degree','/actuator/thruster/percentage'):
                if any(info.node_name!=self.get_name() for info in self.get_publishers_info_by_topic(topic)):
                    competing=True
            self.survey.hardware_ready=not competing and self.throttle.get_subscription_count()>0 and self.steering.get_subscription_count()>0
            self.survey.hardware_reason='다른 추진 제어 프로그램이 실행 중입니다.' if competing else '추진기 수신 노드가 연결되지 않았습니다.'

        def tick(self):
            try:
                self.survey.tick()
                cmd=self.survey.output()
                if cmd is not None:
                    self.publish(cmd)
            except Exception as exc:
                self.get_logger().error(str(exc))
                self.survey.stop('제어 처리 오류로 정지')
                if self.hardware and self.survey.output() is not None:
                    self.publish(dict(servo=90.,throttle=0.))

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
