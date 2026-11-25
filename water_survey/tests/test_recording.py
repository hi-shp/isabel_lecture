import array
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from water_survey.core import Survey,utc,latlon
from water_survey.recording import Recorder,message_data
from water_survey.replay import rebuild


class AutomaticRecordingTests(unittest.TestCase):
    def test_storage_unavailable_cannot_leave_a_new_mission_armed(self):
        with tempfile.TemporaryDirectory() as root:
            rec=Recorder(root);survey=Survey(True,recorder=rec)
            p={'lat':35.1777,'lon':129.1198}
            survey.configure('real',[latlon(50,0,p)],{})
            survey.gps_update(p['lat'],p['lon'],True,utc());survey.imu_update(90,utc())
            survey.hardware_ready=True;survey.add_do(8.,utc(),utc(),1,'73','LOCAL_TEST','test')
            with patch.object(rec,'start',side_effect=OSError('disk full')):
                with self.assertRaisesRegex(ValueError,'기록 파일'):survey.start('owner',True)
            self.assertFalse(survey.armed);self.assertEqual(survey.state,'stopped')
            self.assertEqual(survey.output(),dict(servo=90.,throttle=0.))

    def test_ros_fields_and_nonfinite_ranges_survive_json(self):
        class Scan:
            ranges=array.array('f',[1.,math.inf,-math.inf,math.nan])
            intensities=array.array('f',[12.,13.])
            def get_fields_and_field_types(self):return {'ranges':'sequence<float>','intensities':'sequence<float>'}
        restored=json.loads(json.dumps(message_data(Scan()),allow_nan=False))
        self.assertEqual(restored['ranges'],[1.,'+Inf','-Inf','NaN'])
        self.assertEqual(restored['intensities'],[12.,13.])

    def test_full_history_offline_rebuild_without_browser_or_ros(self):
        with tempfile.TemporaryDirectory() as root:
            rec=Recorder(root);survey=Survey(False,recorder=rec)
            p={'lat':35.1777,'lon':129.1198}
            survey.configure('simulated',[latlon(100,0,p)],{})
            survey.set_virtual(**p);survey.add_do(8.,utc(),utc(),1,'73','LOCAL_TEST','test')
            survey.start('owner')
            for i in range(605):survey.add_do(8+i/1000,utc(),utc(),1,'73','LOCAL_TEST','test')
            for i in range(120):survey.imu_update(45,'same timestamp')
            for i in range(20):survey.set_virtual(**latlon(i,0,p));survey.tick()
            raw={'received_utc':utc(),'message':{'ranges':[1.,math.inf,math.nan],'intensities':[2.,3.]}}
            rec.telemetry('lidar_raw',raw)
            with patch('water_survey.recording.os.fsync',wraps=__import__('os').fsync) as fsync:
                rec.sync(force=True);self.assertGreaterEqual(fsync.call_count,5)
            survey.stop()
            rec.telemetry('commands',{'received_utc':utc(),'servo':90.,'throttle':0.})
            folder=rec.folder
            self.assertEqual(len((folder/'imu.jsonl').read_text().splitlines()),120)
            self.assertEqual(len(survey.samples),600)
            # A NEW recorder, with no in-memory survey state, can rebuild everything.
            output=rebuild(folder)
            meta=json.loads((output/'mission.json').read_text(encoding='utf-8'))
            self.assertEqual(meta['exported_samples'],605)
            self.assertFalse(meta['recording']['screen_capture_required'])
            self.assertEqual(meta['telemetry_counts']['navigation'],21)
            self.assertEqual(meta['telemetry_counts']['commands'],1)
            self.assertIn('로컬 프로그램 시험',(output/'report.html').read_text(encoding='utf-8'))
            self.assertIn('observed-track',(output/'map.svg').read_text(encoding='utf-8'))
            track=json.loads((output/'track.geojson').read_text(encoding='utf-8'))
            self.assertEqual(len(track['features']),21)
            with zipfile.ZipFile(output/'survey-export.zip') as z:
                self.assertIn('navigation.jsonl',z.namelist());self.assertIn('commands.jsonl',z.namelist())
                saved=json.loads(z.read('lidar_raw.jsonl'))
                self.assertEqual(saved['message']['ranges'],[1.,'+Inf','NaN'])
            self.assertTrue((folder/'samples.csv').is_file())

    def test_power_loss_partial_tail_does_not_destroy_complete_samples(self):
        with tempfile.TemporaryDirectory() as root:
            rec=Recorder(root);p={'lat':35.1777,'lon':129.1198}
            mission=rec.start('real',[p],{'cell_m':5},p)
            rec.sample(dict(sequence=1,request_utc=utc(),received_utc=utc(),do_mg_l=8.,lat=p['lat'],
                lon=p['lon'],cell_x=0,cell_y=0))
            rec.finish('stopped','test')
            path=rec.folder/'samples.csv'
            with path.open('ab') as f:f.write(b'2,incomplete,\xe3')
            original=path.read_bytes()
            with zipfile.ZipFile(io.BytesIO(Recorder(root).bundle(mission))) as z:
                meta=json.loads(z.read('mission.json'))
                self.assertEqual(meta['exported_samples'],1);self.assertTrue(meta['incomplete_csv_tail_ignored'])
                self.assertEqual(z.read('samples.csv'),original)
            self.assertEqual(path.read_bytes(),original)


if __name__=='__main__':unittest.main()
