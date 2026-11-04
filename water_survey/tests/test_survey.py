import asyncio
import base64
import csv
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zipfile

from water_survey.core import Survey, utc, latlon, distance, bearing
from water_survey.recording import Recorder
from water_survey.do_protocol import FrameParser, decode_snapshot
from water_survey.node import Dashboard

class Clock:
    def __init__(self): self.t=100.
    def __call__(self): return self.t
    def advance(self,dt): self.t+=dt

class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.clock=Clock(); self.rec=Recorder(self.tmp.name)
        self.s=Survey(True,self.clock,self.rec)
        self.p={'lat':35.1777,'lon':129.1198}
        self.sample=lambda v=8.5:self.s.add_do(v,utc(),utc(),25,'73','WL118D283','V403')

    def prepare_real(self):
        self.s.configure('real',[latlon(10,0,self.p)],{})
        self.s.gps_update(**{'lat':self.p['lat'],'lon':self.p['lon'],'valid':True,'stamp':utc()})
        self.s.imu_update(90,utc()); self.s.hardware_ready=True; self.sample()

    def tearDown(self):
        self.s.stop()

    def test_simulated_route_order_and_no_actuation(self):
        self.s.configure('simulated',[self.p,latlon(2,0,self.p)],{'dwell_s':3,'sim_speed':2})
        self.s.set_virtual(**self.p); self.sample(); self.s.start('owner')
        seen=[]
        for i in range(90):
            self.clock.advance(.1); self.s.pulse('owner'); self.sample(); self.s.tick()
            if self.s.state=='dwelling' and self.s.index not in seen: seen.append(self.s.index)
            self.assertIsNone(self.s.output())
        self.assertEqual(seen,[0,1]); self.assertEqual(self.s.state,'complete')
        self.assertIsNone(self.s.gps)
        self.assertTrue(all(r['position_source']=='simulated' for r in self.s.samples))

    def test_real_arm_and_control_units(self):
        self.prepare_real()
        with self.assertRaises(ValueError): self.s.start('o')
        self.s.start('o',True); self.s.tick(); cmd=self.s.output()
        self.assertAlmostEqual(cmd['servo'],90,places=3)
        self.assertEqual(cmd['throttle'],20)
        with self.assertRaises(ValueError): self.s.start('o',True)

    def test_gps_invalid_or_stale_stops(self):
        self.prepare_real(); self.s.start('o',True)
        self.s.gps_update(0,0,False,utc()); self.s.tick()
        self.assertEqual(self.s.state,'stopped'); self.assertEqual(self.s.output()['throttle'],0)

    def test_imu_stale_stops(self):
        self.prepare_real(); self.s.start('o',True)
        self.clock.advance(2.1); self.s.pulse('o'); self.s.tick()
        self.assertEqual(self.s.state,'stopped'); self.assertIn('IMU',self.s.reason)

    def test_no_browser_heartbeat_required(self):
        self.s.configure('simulated',[self.p],{}); self.s.set_virtual(**self.p); self.sample(); self.s.start('a')
        self.clock.advance(5.1); self.sample(); self.s.pulse('observer'); self.s.tick()
        self.assertEqual(self.s.state,'dwelling')

    def test_do_stale_stops(self):
        self.s.configure('simulated',[self.p],{}); self.s.set_virtual(**self.p); self.sample(); self.s.start('a')
        self.clock.advance(5.1); self.s.pulse('a'); self.s.tick()
        self.assertEqual(self.s.state,'stopped'); self.assertIn('DO',self.s.reason)

    def test_old_gps_is_not_mapped(self):
        self.prepare_real(); self.s.start('o',True)
        self.clock.advance(2.1); self.sample()
        self.assertEqual(self.s.count,1); self.assertEqual(self.s.mapped,0)
        self.assertIsNone(self.s.samples[-1]['lat'])

    def test_grid_average_and_exports(self):
        self.s.configure('simulated',[self.p],{}); self.s.set_virtual(**self.p); self.sample(); self.s.start('o')
        self.sample(6); self.sample(8); self.s.stop()
        self.assertEqual(self.s.snapshot()['cells'][0]['mean'],7)
        z=zipfile.ZipFile(io.BytesIO(self.rec.bundle(self.s.mission_id)))
        self.assertTrue({'samples.csv','samples.geojson','grid.geojson','route.geojson','map.svg','timeseries.svg','report.html'}<=set(z.namelist()))
        self.assertEqual(len(list(csv.DictReader(io.StringIO(z.read('samples.csv').decode())))),2)
        grid=json.loads(z.read('grid.geojson')); self.assertEqual(grid['features'][0]['properties']['mean_mg_l'],7)
        self.assertIn('실내 시험',z.read('report.html').decode())
        self.assertTrue(json.loads(z.read('mission.json'))['experimental'])

    def test_malformed_inputs_and_active_mutation(self):
        with self.assertRaises(ValueError): self.s.configure('simulated',[{'lat':float('nan'),'lon':1}],{})
        with self.assertRaises(ValueError): self.s.configure('simulated',[self.p],{'throttle':90})
        with self.assertRaises(ValueError): self.rec.folder_for('../escape')
        self.s.configure('simulated',[self.p],{}); self.s.set_virtual(**self.p); self.sample(); self.s.start('o')
        with self.assertRaises(ValueError): self.s.set_virtual(**self.p)
        with self.assertRaises(ValueError): self.s.configure('real',[],{})

    def test_two_fresh_fixes_for_arrival(self):
        self.prepare_real(); self.s.route=[self.p]; self.s.start('o',True)
        self.s.tick(); self.s.tick(); self.assertEqual(self.s.state,'running')
        self.clock.advance(.5); self.s.gps_update(self.p['lat'],self.p['lon'],True,utc()); self.s.tick()
        self.assertEqual(self.s.state,'dwelling'); self.assertEqual(self.s.output()['throttle'],0)

    def test_geodesy_and_corrupt_frames(self):
        east=latlon(10,0,self.p)
        self.assertAlmostEqual(distance(self.p,east),10,places=3)
        self.assertAlmostEqual(bearing(self.p,east),90,places=3)
        for kind,offset in [('s',2),('t',4),('u',3)]:
            b=bytearray(12); b[0]=ord(kind); struct.pack_into('<f',b,offset,8.45); b[11]=sum(b[:11])&255
            f=b'{{'+base64.b64encode(b)+b'}}'; p=FrameParser()
            self.assertEqual(p.feed(f[:6]),[])
            self.assertAlmostEqual(decode_snapshot(p.feed(f[6:])[0])[0],8.45,places=4)
            b[11]^=1; self.assertEqual(p.feed(b'{{'+base64.b64encode(b)+b'}}'),[])

class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp.test_utils import TestServer, TestClient
        self.tmp=tempfile.TemporaryDirectory()
        self.rec=Recorder(self.tmp.name); self.s=Survey(False,recorder=self.rec)
        self.dashboard=Dashboard(self.s,self.rec,Path(__file__).parents[1]/'water_survey/static')
        self.client=TestClient(TestServer(self.dashboard.app)); await self.client.start_server()
        self.origin=str(self.client.make_url('/')).rstrip('/')
        self.ws=await self.client.ws_connect('/ws',headers={'Origin':self.origin})
        hello=await self.ws.receive_json(); self.key=hello['client']

    async def asyncTearDown(self):
        await self.ws.close(); await self.client.close(); self.tmp.cleanup()

    async def post(self,action,data):
        return await self.client.post('/api/'+action,json={'client':self.key,**data},headers={'Origin':self.origin})

    async def test_click_virtual_start_export_and_survive_disconnect(self):
        p={'lat':35.1777,'lon':129.1198}
        self.assertEqual((await self.post('configure',{'mode':'simulated','route':[p],'settings':{}})).status,200)
        self.assertEqual((await self.post('virtual',p)).status,200)
        self.s.add_do(8.5,utc(),utc(),20,'73','WL118D283','V403')
        self.assertEqual((await self.post('start',{})).status,200)
        self.s.add_do(8.6,utc(),utc(),20,'73','WL118D283','V403')
        self.assertIsNone(self.s.output())
        self.assertEqual((await self.post('virtual',p)).status,400)
        await self.ws.close(); await asyncio.sleep(.05)
        self.assertEqual(self.s.state,'running')
        self.s.stop()
        r=await self.client.get('/api/export/'+self.s.mission_id)
        self.assertEqual(r.status,200); self.assertTrue(zipfile.is_zipfile(io.BytesIO(await r.read())))

    async def test_cross_origin_and_missing_session_rejected(self):
        r=await self.client.post('/api/stop',json={'client':self.key},headers={'Origin':'https://evil.invalid'})
        self.assertEqual(r.status,403)
        r=await self.client.post('/api/start',json={'client':'missing'},headers={'Origin':self.origin})
        self.assertEqual(r.status,400)

if __name__=='__main__': unittest.main()
