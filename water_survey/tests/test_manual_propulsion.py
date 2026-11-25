import tempfile
import unittest
from water_survey.core import Survey,utc,latlon
from water_survey.recording import Recorder

class ManualPropulsionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.now=100.;self.s=Survey(True,lambda:self.now,Recorder(self.tmp.name))
        self.p={'lat':35.1777,'lon':129.1198}
        self.s.configure('simulated',[latlon(20,0,self.p)],{})
        self.s.set_virtual(**self.p);self.s.add_do(8.,utc(),utc(),1,'73','LOCAL_TEST','test')
        self.s.imu_update(0,utc());self.s.hardware_ready=True
    def tearDown(self):self.s.stop()
    def test_manual_gps_can_use_real_steering_and_throttle_after_armed_start(self):
        self.s.start('owner',True);self.s.tick()
        self.assertTrue(self.s.armed);self.assertEqual(self.s.output()['throttle'],20)
        self.assertEqual(self.s.output()['servo'],150)
        self.assertTrue(self.s.recorder.meta['propulsion_enabled'])
        self.assertEqual(self.s.recorder.meta['position_input'],'map_click')
        self.s.stop();self.assertEqual(self.s.output(),dict(servo=90.,throttle=0.))
        self.now+=2.1;self.assertIsNone(self.s.output())
    def test_manual_gps_still_requires_actuator_connection_and_actual_imu(self):
        self.s.hardware_ready=False
        with self.assertRaises(ValueError):self.s.start('owner',True)
        self.s.hardware_ready=True;self.now+=2.1
        with self.assertRaisesRegex(ValueError,'IMU'):self.s.start('owner',True)
        self.assertFalse(self.s.armed)
    def test_unarmed_recording_never_becomes_armed_on_a_manual_position_update(self):
        self.s.start('owner',False);self.s.tick();self.s.set_virtual(**self.p)
        self.assertFalse(self.s.armed);self.assertIsNone(self.s.output())
    def test_lost_imu_stops_real_output_even_when_position_is_manual(self):
        self.s.start('owner',True);self.s.tick();self.now+=2.1;self.s.tick()
        self.assertEqual(self.s.state,'stopped');self.assertEqual(self.s.output()['throttle'],0)

if __name__=='__main__':unittest.main()
