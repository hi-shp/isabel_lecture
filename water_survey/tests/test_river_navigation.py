import asyncio
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
import zipfile

from water_survey.core import Survey,utc,latlon,distance,xy
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

    def test_keep_requested_offset_from_current_goal_without_timed_switches(self):
        guide=RiverZigzag()
        self.assertEqual(guide.guide(0,45,0,self.observation(1,False))['course_deg'],45)
        for sequence in range(2,100):
            value=guide.guide(20,45,0,self.observation(sequence,False))
            self.assertEqual(value['course_deg'],65);self.assertEqual(value['bank_switches'],0)

    def test_start_side_is_closest_to_current_imu_heading(self):
        for heading,expected in [(40,45),(320,315)]:
            guide=RiverZigzag()
            value=guide.guide(0,heading,0,self.observation(1,False))
            self.assertEqual(value['course_deg'],expected)

    def test_offset_stays_exact_while_approaching_and_changing_goal_bearing(self):
        guide=RiverZigzag()
        for sequence,bearing_deg in enumerate([0,10,30,90,150,240,350]):
            value=guide.guide(bearing_deg,45,0,self.observation(sequence,False),60)
            error=(value['course_deg']-bearing_deg+180)%360-180
            self.assertEqual(error,60);self.assertEqual(value['bank_switches'],0)

    def test_only_explicit_operator_requests_change_sides(self):
        guide=RiverZigzag(clock=lambda:0);guide.guide(0,45,0,self.observation(1,False))
        guide.request_turn()
        value=guide.guide(0,45,0,self.observation(2,True))
        self.assertEqual(value['course_deg'],315);self.assertEqual(value['side'],-1)
        for sequence in (2,3,4):
            value=guide.guide(0,315,0,self.observation(sequence,True))
            self.assertEqual(value['bank_switches'],1)
        guide.guide(0,0,0,self.observation(5,False))
        self.assertEqual(guide.guide(0,315,0,self.observation(6,True))['bank_switches'],1)
        self.assertEqual(guide.guide(0,315,0,self.observation(7,False))['bank_switches'],1)
        guide.clock=lambda:3
        guide.request_turn()
        value=guide.guide(0,0,0,self.observation(8,True))
        self.assertEqual(value['course_deg'],45);self.assertEqual(value['bank_switches'],2)

    def test_repeated_operator_requests_alternate_without_automatic_turns(self):
        now=[0.];guide=RiverZigzag(clock=lambda:now[0]);sequence=0
        def obs(blocked,heading):
            nonlocal sequence
            sequence+=1
            return guide.guide(0,heading,0,self.observation(sequence,blocked),turn_interval_s=3)
        for turn in range(1,11):
            incoming=45 if turn%2 else 315
            now[0]=(turn-1)*3
            guide.request_turn()
            v=obs(True,incoming)
            self.assertEqual(v['bank_switches'],turn)
            self.assertEqual(v['course_deg'],315 if turn%2 else 45)
            # No clear frame or heading alignment is required for the next turn.
            now[0]+=.2
            for _ in range(3):
                self.assertEqual(obs(True,0)['bank_switches'],turn)
        now[0]+=1
        sequence+=1
        guide.request_turn()
        v=guide.guide(0,45,0,self.observation(sequence,True),turn_interval_s=.5)
        self.assertEqual(v['bank_switches'],11)


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

    def test_centreline_progress_then_destination_holds_without_finishing_recording(self):
        self.s.start('owner',True,'river_zigzag');self.s.tick()
        self.assertAlmostEqual(self.s.guidance['course_deg'],135,places=3)
        self.assertEqual(self.s.output()['throttle'],20)
        for _ in range(2):self.fresh(12,5);self.s.tick()
        self.assertEqual(self.s.index,1);self.assertEqual(self.s.state,'running')
        self.assertEqual(self.s.output()['throttle'],20)  # No dwell at a centreline marker.
        for _ in range(2):self.fresh(22,5);self.s.tick()
        self.assertEqual(self.s.state,'running');self.assertEqual(self.s.output()['throttle'],20)
        for _ in range(2):self.fresh(20,0);self.s.tick()
        self.assertTrue(self.s.route_complete);self.assertEqual(self.s.output()['throttle'],0)
        self.assertEqual(self.s.state,'running');self.assertIsNotNone(self.recorder.file)
        self.s.stop()
        bundle=zipfile.ZipFile(io.BytesIO(self.recorder.bundle(self.s.mission_id)))
        meta=json.loads(bundle.read('mission.json'))
        self.assertEqual(meta['navigation'],'river_zigzag');self.assertEqual(meta['river_zigzag']['obstacle_m'],2)
        self.assertIn('lidar.jsonl',bundle.namelist())

    def test_keyboard_autonomy_starts_and_turns_without_lidar(self):
        self.s.scan=None
        self.s.start('owner',True,'river_zigzag');self.s.tick()
        self.assertEqual(self.s.state,'running');self.assertEqual(self.s.output()['throttle'],20)
        self.s.manual_turn();self.assertEqual(self.s.guidance['bank_switches'],1)

    def test_lidar_loss_does_not_interrupt_keyboard_navigation(self):
        self.s.start('owner',True,'river_zigzag');self.now+=1.1;self.s.tick()
        self.assertEqual(self.s.state,'running');self.assertIsNone(self.s.pause_reason)
        self.assertEqual(self.s.output()['throttle'],20)
        self.assertTrue(self.s.armed);self.assertIsNotNone(self.recorder.file)
        self.fresh(0,0);self.s.tick()
        self.assertEqual(self.s.output()['throttle'],20);self.assertIsNone(self.s.pause_reason)

    def test_missing_front_frames_do_not_pause_keyboard_navigation(self):
        self.s.start('owner',True,'river_zigzag');self.s.tick();mission=self.s.mission_id
        self.now+=.1
        invalid=forward_scan([math.nan]*10,-.004,.001,.1,100)
        self.s.scan_update(invalid,str(self.now));self.s.tick()
        self.assertEqual(self.s.output()['throttle'],20)
        for _ in range(15):
            self.now+=.1;self.s.imu_update(90,utc())
            self.s.scan_update(invalid,str(self.now));self.s.tick()
        self.assertEqual(self.s.output()['throttle'],20)
        self.assertEqual(self.s.state,'running');self.assertTrue(self.s.armed)
        self.assertIsNone(self.s.pause_reason)
        self.fresh(0,0);self.s.tick()
        self.assertEqual(self.s.output()['throttle'],20);self.assertEqual(self.s.mission_id,mission)
        self.s.stop();self.fresh(0,0);self.s.tick()
        self.assertFalse(self.s.armed);self.assertEqual(self.s.output()['throttle'],0)

    def test_operator_turns_are_immediate_ignore_obstacles_and_keep_the_same_mission(self):
        self.s.scan=None;self.s.start('owner',True,'river_zigzag');self.s.tick()
        mission=self.s.mission_id;initial=self.s.guidance['side'];now=self.now
        for expected in range(1,5):
            self.s.manual_turn()
            self.assertEqual(self.s.guidance['bank_switches'],expected)
            self.assertEqual(self.s.guidance['side'],initial*(-1 if expected%2 else 1))
            self.assertEqual(self.s.output()['throttle'],20)
            self.assertEqual(self.s.mission_id,mission)
        self.assertEqual(self.now,now)  # No three-second wait.
        self.s.scan_update(forward_scan([.2]*10,-.004,.001,.1,100),'blocked')
        self.s.tick();self.assertEqual(self.s.guidance['bank_switches'],4)
        self.s.stop()
        with self.assertRaises(ValueError):self.s.manual_turn()

    def test_held_leg_steering_corrects_both_sides_of_the_imu_heading(self):
        self.s.start('owner',True,'river_zigzag');self.s.tick()
        course=self.s.guidance['course_deg']
        self.s.imu_update(course-20,utc());self.s.tick()
        self.assertAlmostEqual(self.s.command['servo'],110)
        self.s.imu_update(course+20,utc());self.s.tick()
        self.assertAlmostEqual(self.s.command['servo'],70)
        self.assertEqual(self.s.guidance['course_deg'],course)

    def test_fixed_requested_angle_reaches_final_goal_under_ideal_heading_tracking(self):
        self.s.configure('real',[latlon(0,50,self.origin)],{'zigzag_deg':45})
        self.s.start('owner',True,'river_zigzag')
        for _ in range(300):
            self.s.tick()
            if self.s.route_complete:break
            guide=self.s.guidance
            offset=(guide['course_deg']-guide['goal_bearing_deg']+180)%360-180
            self.assertAlmostEqual(abs(offset),45)
            x,y=xy(self.s.position(),self.origin);a=math.radians(guide['course_deg'])
            self.fresh(x+.5*math.sin(a),y+.5*math.cos(a))
            self.s.imu_update(guide['course_deg'],utc())
        self.assertTrue(self.s.route_complete)
        self.assertLessEqual(distance(self.s.position(),self.s.route[-1]),3)
        self.assertEqual(self.s.state,'running');self.assertEqual(self.s.output()['throttle'],0)

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
    async def test_plus_turn_api_deduplicates_and_rejects_requests_after_stop(self):
        from aiohttp.test_utils import TestClient,TestServer
        with tempfile.TemporaryDirectory() as root:
            survey=Survey(False,recorder=Recorder(root));p=dict(lat=35.,lon=129.)
            survey.configure('simulated',[latlon(20,0,p)],{});survey.set_virtual(**p)
            survey.imu_update(90,utc());survey.add_do(8.4,utc(),utc(),1,'fixture','LOCAL_TEST','fixture')
            dash=Dashboard(survey,survey.recorder,Path(__file__).parents[1]/'water_survey/static')
            async with TestClient(TestServer(dash.app)) as client:
                origin=str(client.make_url('/')).rstrip('/');headers={'Origin':origin}
                ws=await client.ws_connect('/ws',headers=headers);key=(await ws.receive_json())['client']
                r=await client.post('/api/start-autonomous',json={'client':key},headers=headers)
                self.assertEqual(r.status,200);started=await r.json();survey.tick()
                body=dict(client=key,mission_id=started['mission_id'],control_revision=0,command_id='one')
                for _ in range(2):
                    r=await client.post('/api/turn',json=body,headers=headers)
                    self.assertEqual(r.status,200);value=await r.json()
                    self.assertEqual(value['guidance']['bank_switches'],1)
                body['command_id']='two'
                r=await client.post('/api/turn',json=body,headers=headers)
                self.assertEqual((await r.json())['guidance']['bank_switches'],2)
                await client.post('/api/stop',headers=headers)
                r=await client.post('/api/turn',json=body,headers=headers)
                self.assertEqual(r.status,400);self.assertFalse(survey.armed)
                self.assertIsNone(survey.output());await ws.close()

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
