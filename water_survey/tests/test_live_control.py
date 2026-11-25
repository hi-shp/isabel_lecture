import io
import json
import math
from pathlib import Path
import tempfile
import unittest
import zipfile
from aiohttp.test_utils import TestClient,TestServer
from water_survey.core import Survey,utc,latlon,bearing
from water_survey.recording import Recorder
from water_survey.node import Dashboard
from water_survey.river_navigation import forward_scan


class LiveControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.now=100.;self.s=Survey(True,lambda:self.now,Recorder(self.tmp.name))
        self.p={'lat':35.1777,'lon':129.1198}
        self.s.configure('real',[latlon(0,50,self.p),latlon(0,100,self.p)],{})
        self.fresh();self.s.hardware_ready=True

    def tearDown(self):self.s.stop()

    def fresh(self,front=10):
        self.now+=.1;self.s.gps_update(**self.p,valid=True,stamp=utc(self.now))
        self.s.imu_update(45,utc(self.now));self.s.add_do(8.4,utc(),utc(),1,'73','LOCAL_TEST','test')
        self.s.scan_update(forward_scan([front]*10,-.004,.001,.1,100),str(self.now))

    def update(self,settings=None,waypoint=None):
        self.s.live_update(settings or {},waypoint,self.s.plan_revision)

    def test_throttle_angle_and_radius_apply_without_resetting_data_or_arming(self):
        self.s.start('owner',True,'river_zigzag');self.s.tick();self.fresh()
        mission=self.s.mission_id;count=self.s.count;cells=self.s.cells.copy()
        self.update({'throttle':27,'zigzag_deg':60,'radius_m':5,'dwell_s':3})
        self.assertEqual(self.s.guidance['course_deg'],60)
        self.assertAlmostEqual(self.s.command['servo'],105)
        self.assertEqual(self.s.output()['throttle'],27)
        self.assertEqual((self.s.mission_id,self.s.count,self.s.cells),(mission,count,cells))
        self.assertTrue(self.s.armed);self.assertEqual(self.s.index,0)
        self.s.stop()
        with zipfile.ZipFile(io.BytesIO(self.s.recorder.bundle(mission))) as z:
            meta=json.loads(z.read('mission.json'))
            self.assertEqual(meta['initial_settings']['zigzag_deg'],45)
            self.assertEqual(meta['settings']['zigzag_deg'],60)
            events=[json.loads(line) for line in z.read('events.jsonl').splitlines()]
            self.assertEqual(sum(e['event']=='live_update' for e in events),1)

    def test_distance_edit_rechecks_latest_frame_immediately(self):
        self.fresh(front=3);self.s.start('owner',True,'river_zigzag');self.s.tick()
        self.assertEqual(self.s.guidance['bank_switches'],0)
        self.update({'obstacle_m':4})
        self.assertTrue(self.s.scan['obstacle']);self.assertEqual(self.s.guidance['bank_switches'],1)
        self.assertEqual(self.s.guidance['course_deg'],315)

    def test_zero_angle_continuously_tracks_the_goal_instead_of_holding_a_leg(self):
        self.s.start('owner',True,'river_zigzag');self.s.tick();self.update({'zigzag_deg':0})
        self.assertEqual(self.s.guidance['phase'],'direct');self.assertAlmostEqual(self.s.guidance['course_deg'],0)
        moved=latlon(10,0,self.p);self.now+=.1
        self.s.gps_update(**moved,valid=True,stamp=utc(self.now));self.s.tick()
        self.assertAlmostEqual(self.s.guidance['course_deg'],bearing(moved,self.s.route[0]))
        self.assertEqual(self.s.guidance['bank_switches'],0)

    def test_current_waypoint_drag_immediately_retargets_but_future_drag_does_not(self):
        self.s.start('owner',True,'river_zigzag');self.s.tick()
        original=self.s.guidance['course_deg']
        self.update(waypoint=dict(index=1,**latlon(20,100,self.p)))
        self.assertEqual(self.s.guidance['course_deg'],original)
        new=latlon(50,0,self.p);self.update(waypoint=dict(index=0,**new))
        self.assertAlmostEqual(self.s.guidance['course_deg'],135,places=3)
        self.assertEqual(self.s.index,0);self.assertTrue(self.s.armed)
        self.assertNotEqual(self.s.recorder.meta['initial_route'][0],new)

    def test_drag_while_dwelling_resumes_current_waypoint_and_dwell_duration_is_live(self):
        self.s.route[0]=self.p.copy();self.s.start('owner',True)
        self.fresh();self.s.tick();self.fresh();self.s.tick()
        self.assertEqual(self.s.state,'dwelling')
        self.update(waypoint=dict(index=0,**latlon(20,0,self.p)))
        self.assertEqual(self.s.state,'running');self.assertEqual(self.s.output()['throttle'],20)
        self.s.state='dwelling';self.s.dwell_start=self.now-4;self.s.route[0]=self.p.copy()
        self.update({'dwell_s':3})
        self.assertEqual(self.s.index,1);self.assertEqual(self.s.state,'running')

    def test_invalid_or_stale_edits_leave_settings_and_route_unchanged(self):
        self.s.start('owner',True,'river_zigzag');self.s.tick();old=self.s.settings.copy()
        with self.assertRaises(ValueError):self.update({'throttle':25,'zigzag_deg':90})
        with self.assertRaises(ValueError):self.update({'cell_m':10})
        with self.assertRaises(ValueError):self.s.live_update({'throttle':25},None,self.s.plan_revision-1)
        self.assertEqual(self.s.settings,old);self.assertTrue(self.s.armed)

    def test_gps_speed_uses_sensor_time_and_is_not_throttle_percentage(self):
        self.s.gps_update(**self.p,valid=True,stamp=utc(1000))
        self.s.gps_update(**latlon(0,4,self.p),valid=True,stamp=utc(1002))
        self.assertAlmostEqual(self.s.snapshot()['speed_m_s'],2,places=4)
        self.now+=3.1;self.assertIsNone(self.s.snapshot()['speed_m_s'])


class LiveApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_updates_and_reconnect_keep_authoritative_state_and_reject_late_edits(self):
        with tempfile.TemporaryDirectory() as root:
            survey=Survey(False,recorder=Recorder(root));p={'lat':35.1777,'lon':129.1198}
            survey.configure('simulated',[latlon(0,50,p)],{});survey.set_virtual(**p)
            survey.add_do(8.,utc(),utc(),1,'73','LOCAL_TEST','test');survey.start('owner')
            dash=Dashboard(survey,survey.recorder,Path(__file__).parents[1]/'water_survey/static')
            async with TestClient(TestServer(dash.app)) as client:
                origin=str(client.make_url('/')).rstrip('/');headers={'Origin':origin}
                ws=await client.ws_connect('/ws',headers=headers);key=(await ws.receive_json())['client']
                body=dict(client=key,settings={'throttle':26,'zigzag_deg':60,'obstacle_m':4},
                    control_revision=0,plan_revision=survey.plan_revision,mission_id=survey.mission_id)
                response=await client.post('/api/live-update',json=body,headers=headers)
                self.assertEqual(response.status,200);self.assertEqual((await response.json())['settings']['throttle'],26)
                self.assertEqual((await client.post('/api/live-update',json=body,headers=headers)).status,400)
                body['plan_revision']=survey.plan_revision
                await ws.close()
                ws=await client.ws_connect('/ws',headers=headers);key=(await ws.receive_json())['client'];body['client']=key
                state=await (await client.get('/api/state')).json()
                self.assertEqual(state['settings']['zigzag_deg'],60);self.assertEqual(state['state'],'running')
                await client.post('/api/stop',headers=headers)
                self.assertEqual((await client.post('/api/live-update',json=body,headers=headers)).status,400)
                self.assertEqual(survey.settings['throttle'],26);self.assertEqual(survey.state,'stopped')
                await ws.close()


if __name__=='__main__':unittest.main()
