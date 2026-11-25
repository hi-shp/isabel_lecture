import asyncio
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
import zipfile

from water_survey.core import Survey,utc,latlon
from water_survey.node import Dashboard
from water_survey.recording import Recorder
from water_survey.river_navigation import forward_scan,trimmed_mean,RiverZigzag


class FrontScanTests(unittest.TestCase):
    def test_trim_each_tail_and_inclusive_two_metre_threshold(self):
        obs=forward_scan([.2,.3,2,2,2,2,2,2,30,40],math.radians(-.45),math.radians(.1),.1,100)
        self.assertTrue(obs['valid']);self.assertEqual(obs['front_hits'],10)
        self.assertEqual(obs['front_kept'],6);self.assertEqual(obs['front_mean_m'],2)
        self.assertTrue(obs['obstacle']);self.assertEqual(len(obs['sector_means_m']),180)
        obs=forward_scan([2.01]*10,math.radians(-.45),math.radians(.1),.1,100)
        self.assertFalse(obs['obstacle'])

    def test_front_window_does_not_average_the_whole_river_view(self):
        # One sample per one-degree sector; side banks must not trigger a turn.
        ranges=[.5]*181;ranges[90]=12
        obs=forward_scan(ranges,-math.pi/2,math.pi/180,.1,100)
        self.assertEqual(obs['front_mean_m'],12);self.assertFalse(obs['obstacle'])
        self.assertEqual(obs['sector_means_m'][120],.5)

    def test_no_return_and_invalid_measurements_are_distinct(self):
        clear=forward_scan([math.inf]*10,-.004,.001,.1,100)
        self.assertTrue(clear['valid']);self.assertIsNone(clear['front_mean_m']);self.assertFalse(clear['obstacle'])
        invalid=forward_scan([math.nan,0,-math.inf],-.001,.001,.1,100)
        self.assertFalse(invalid['valid']);self.assertIsNone(invalid['front_mean_m'])
        self.assertEqual(trimmed_mean([1,2,3,4]),(2.5,4))

    def test_scan_mount_offset_selects_the_correct_bow(self):
        obs=forward_scan([1.5]*10,math.radians(29.6),math.radians(.08),.1,100,-30)
        self.assertTrue(obs['obstacle']);self.assertEqual(obs['front_mean_m'],1.5)


class RiverGuideTests(unittest.TestCase):
    def observation(self,sequence,blocked):
        return dict(sequence=sequence,front_mean_m=2 if blocked else 10,obstacle=blocked)

    def test_hold_straight_without_time_or_distance_switches(self):
        guide=RiverZigzag()
        self.assertEqual(guide.guide(0,45,0,self.observation(1,False))['course_deg'],45)
        for sequence in range(2,100):
            value=guide.guide(20,45,0,self.observation(sequence,False))
            self.assertEqual(value['course_deg'],45);self.assertEqual(value['bank_switches'],0)

    def test_alternate_only_after_new_bank_event_and_clear_alignment(self):
        guide=RiverZigzag();guide.guide(0,45,0,self.observation(1,False))
        value=guide.guide(0,45,0,self.observation(2,True))
        self.assertEqual(value['course_deg'],315);self.assertEqual(value['side'],-1)
        for sequence in (2,3,4):
            value=guide.guide(0,315,0,self.observation(sequence,True))
            self.assertEqual(value['bank_switches'],1)
        guide.guide(0,0,0,self.observation(5,False))  # Not aligned: still not armed.
        self.assertEqual(guide.guide(0,315,0,self.observation(6,True))['bank_switches'],1)
        self.assertTrue(guide.guide(0,315,0,self.observation(7,False))['bank_armed'])
        value=guide.guide(0,315,0,self.observation(8,True))
        self.assertEqual(value['course_deg'],45);self.assertEqual(value['bank_switches'],2)


class RiverSurveyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.now=100.;self.recorder=Recorder(self.tmp.name)
        self.s=Survey(True,clock=lambda:self.now,recorder=self.recorder)
        self.origin={'lat':35.1777,'lon':129.1198}
        self.s.configure('real',[latlon(10,0,self.origin),latlon(20,0,self.origin)],{})
        self.s.hardware_ready=True;self.fresh(0,0)

    def tearDown(self):self.s.stop()

    def fresh(self,x,y,scan=True):
        self.now+=.1;p=latlon(x,y,self.origin)
        self.s.gps_update(p['lat'],p['lon'],True,utc())
        self.s.imu_update(90,utc());self.s.add_do(8.4,utc(),utc(),1,'fixture','LOCAL_TEST','fixture')
        if scan:self.s.scan_update(forward_scan([10.]*10,-.004,.001,.1,100),str(self.now))

    def test_centreline_progress_is_continuous_then_neutral_at_end(self):
        self.s.start('owner',True,'river_zigzag');self.s.tick()
        self.assertAlmostEqual(self.s.guidance['course_deg'],135,places=3)
        self.assertEqual(self.s.output()['throttle'],20)
        for _ in range(2):self.fresh(12,5);self.s.tick()
        self.assertEqual(self.s.index,1);self.assertEqual(self.s.state,'running')
        self.assertEqual(self.s.output()['throttle'],20)  # No dwell at a centreline marker.
        for _ in range(2):self.fresh(22,5);self.s.tick()
        self.assertEqual(self.s.state,'complete');self.assertEqual(self.s.output()['throttle'],0)
        bundle=zipfile.ZipFile(io.BytesIO(self.recorder.bundle(self.s.mission_id)))
        meta=json.loads(bundle.read('mission.json'))
        self.assertEqual(meta['navigation'],'river_zigzag');self.assertEqual(meta['river_zigzag']['obstacle_m'],2)
        self.assertIn('lidar.jsonl',bundle.namelist())

    def test_autonomy_requires_fresh_lidar_but_old_waypoint_mode_does_not(self):
        self.s.scan=None
        with self.assertRaisesRegex(ValueError,'라이다'):self.s.start('owner',True,'river_zigzag')
        self.s.start('owner',True);self.s.tick()
        self.assertEqual(self.s.state,'running');self.assertAlmostEqual(self.s.command['servo'],90,places=3)

    def test_lidar_loss_stops_autonomy_and_sends_zero(self):
        self.s.start('owner',True,'river_zigzag');self.now+=1.1;self.s.tick()
        self.assertEqual(self.s.state,'stopped');self.assertIn('라이다',self.s.reason)
        self.assertEqual(self.s.output(),dict(servo=90.,throttle=0.))

    def test_click_mode_never_enables_propulsion_with_new_navigation(self):
        self.s.configure('simulated',[latlon(10,0,self.origin)],{})
        self.s.set_virtual(**self.origin);self.s.start('owner',False,'river_zigzag');self.s.tick()
        self.assertIsNone(self.s.output());self.assertFalse(self.s.armed)

    def test_explicit_stop_also_neutralises_an_idle_real_interface(self):
        self.s.command.update(servo=140,throttle=25)
        self.s.stop(neutral=True)
        self.assertEqual(self.s.output(),dict(servo=90.,throttle=0.))
        self.now+=2.1;self.assertIsNone(self.s.output())


class StopApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_neutral_precedes_failed_record_finalisation(self):
        with tempfile.TemporaryDirectory() as root:
            survey=Survey(True,recorder=Recorder(root));events=[]
            survey.mode='real';survey.state='running';survey.armed=True
            survey.command.update(throttle=25,servo=130)
            def neutral():events.append(('neutral',survey.command.copy()));return True
            def finish(*args):
                events.append(('finish',survey.state,survey.armed))
                raise OSError('fixture disk failure')
            survey.recorder.finish=finish
            dash=Dashboard(survey,survey.recorder,Path(__file__).parents[1]/'water_survey/static',neutral_publish=neutral)
            dash.end_motion()
            self.assertEqual(events[0],('neutral',dict(survey.command)))
            self.assertEqual(events[0][1]['throttle'],0)
            self.assertEqual(events[1],('finish','stopped',False))
            self.assertTrue(dash.last_stop['neutral_published'])
            self.assertIn('disk failure',dash.last_stop['record_error'])
            self.assertEqual(survey.output(),dict(servo=90.,throttle=0.))

    async def test_distinct_start_actions_and_simulated_no_output(self):
        from aiohttp.test_utils import TestClient,TestServer
        with tempfile.TemporaryDirectory() as root:
            survey=Survey(False,recorder=Recorder(root))
            dash=Dashboard(survey,survey.recorder,Path(__file__).parents[1]/'water_survey/static')
            async with TestClient(TestServer(dash.app)) as client:
                origin=str(client.make_url('/')).rstrip('/')
                ws=await client.ws_connect('/ws',headers={'Origin':origin})
                key=(await ws.receive_json())['client']
                p={'lat':35.1777,'lon':129.1198}
                survey.configure('simulated',[latlon(20,0,p)],{});survey.set_virtual(**p)
                survey.add_do(8.4,utc(),utc(),1,'fixture','LOCAL_TEST','fixture');survey.imu_update(90,utc())
                survey.scan_update(forward_scan([10.]*10,-.004,.001,.1,100),'scan1')
                r=await client.post('/api/start-autonomous',json={'client':key},headers={'Origin':origin})
                self.assertEqual(r.status,200);self.assertEqual((await r.json())['navigation'],'river_zigzag')
                self.assertIsNone(survey.output())
                stop=await client.post('/api/stop',headers={'Origin':origin});stopped=await stop.json()
                old=await client.post('/api/start-waypoints',json={'client':key,'control_revision':0},headers={'Origin':origin})
                self.assertEqual(old.status,400);self.assertFalse(survey.armed)
                r=await client.post('/api/start-waypoints',json={'client':key,'control_revision':stopped['control_revision']},headers={'Origin':origin})
                self.assertEqual(r.status,200);self.assertEqual((await r.json())['navigation'],'waypoint')
                self.assertIsNone(survey.output());await ws.close()

    async def test_expired_session_and_busy_bringup_cannot_block_neutral(self):
        from aiohttp.test_utils import TestClient,TestServer
        with tempfile.TemporaryDirectory() as root:
            survey=Survey(True,recorder=Recorder(root));sent=[]
            def neutral():sent.append(survey.command.copy());return True
            dash=Dashboard(survey,survey.recorder,Path(__file__).parents[1]/'water_survey/static',neutral_publish=neutral)
            dash.lifecycle_busy=True;survey.command.update(throttle=25,servo=130)
            async with TestClient(TestServer(dash.app)) as client:
                origin=str(client.make_url('/')).rstrip('/')
                result=await client.post('/api/stop',data='not-json',headers={'Origin':origin})
                self.assertEqual(result.status,200)
                data=await result.json();self.assertEqual(sent[-1]['throttle'],0)
                self.assertEqual(sent[-1]['servo'],90);self.assertFalse(data['armed'])
                self.assertTrue(data['last_stop']['neutral_published'])
                self.assertFalse(data['last_stop']['actuator_acknowledged'])
                rejected=await client.post('/api/stop',headers={'Origin':'https://other.invalid'})
                self.assertEqual(rejected.status,403)


if __name__=='__main__':unittest.main()
