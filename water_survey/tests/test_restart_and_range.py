import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest
from aiohttp.test_utils import TestClient,TestServer
from water_survey.core import Survey,latlon,utc,coverage_plan
from water_survey.recording import Recorder
from water_survey.node import Dashboard


class RangeAndCleanupTests(unittest.TestCase):
    def test_long_routes_start_and_drag_without_two_kilometre_limit(self):
        with tempfile.TemporaryDirectory() as root:
            s=Survey(False,recorder=Recorder(root));p={'lat':35.1777,'lon':129.1198}
            s.configure('simulated',[latlon(5000,0,p)],{});s.set_virtual(**p)
            s.add_do(8.,utc(),utc(),1,'73','LOCAL_TEST','test');s.start('test')
            s.live_update({},dict(index=0,**latlon(8000,0,p)),s.plan_revision)
            self.assertEqual(s.state,'running');s.stop()
            ne=latlon(3000,1,p)
            plan,route=coverage_plan(dict(south=p['lat'],west=p['lon'],north=ne['lat'],east=ne['lon']),100)
            self.assertEqual(len(route),30)

    def test_duplicate_cleanup_protects_other_stacks_and_managed_tree(self):
        spec=importlib.util.spec_from_file_location('cleanup',Path(__file__).parents[1]/'scripts/stop_duplicate_bringup.py')
        m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        argv=['python3','ros2','launch','mechaship_bringup','mechaship_bringup.launch.py']
        def proc(uid=1000,parent=1,args=None,cgroup='user.slice'):
            return dict(uid=uid,ppid=parent,argv=argv if args is None else args,cgroup=cgroup)
        processes={10:proc(cgroup='/system.slice/water-survey-base.service'),20:proc(),21:proc(parent=20,args=['sensor']),
                   30:proc(args=['ros2','launch','kaboat2026','all_sensor.launch.py']),40:proc(uid=2000),
                   50:proc(args=['survey_dashboard']),60:proc(args=['mcu_service.py'])}
        self.assertEqual(m.targets(processes,1000),{20,21})


class RestartApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_neutralises_once_and_rejects_duplicate_requests(self):
        class Manager:
            state=dict(available=True,state='active',detail='running')
            calls=[]
            async def refresh(self):pass
            async def command(self,action):self.calls.append(action)
        with tempfile.TemporaryDirectory() as root:
            s=Survey(False,recorder=Recorder(root));mgr=Manager()
            dash=Dashboard(s,s.recorder,Path(__file__).parents[1]/'water_survey/static',bringup=mgr)
            async with TestClient(TestServer(dash.app)) as client:
                origin=str(client.make_url('/')).rstrip('/');headers={'Origin':origin}
                ws=await client.ws_connect('/ws',headers=headers);key=(await ws.receive_json())['client']
                task=asyncio.create_task(client.post('/api/bringup-restart',json={'client':key},headers=headers))
                for _ in range(50):
                    if dash.lifecycle_busy:break
                    await asyncio.sleep(.01)
                self.assertTrue(dash.lifecycle_busy);self.assertIsNotNone(dash.last_stop)
                second=await client.post('/api/bringup-restart',json={'client':key},headers=headers)
                self.assertEqual(second.status,400)
                first=await task;self.assertEqual(first.status,200);self.assertEqual(mgr.calls,['restart'])
                self.assertFalse(s.armed);await ws.close()


if __name__=='__main__':unittest.main()
